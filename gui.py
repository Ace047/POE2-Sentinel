"""
POE2 Sentinel - GUI Application
Flask bot, terrain overlay, and map tools for Path of Exile 2.
Uses structure-based memory reading that auto-updates across game patches.
"""

import customtkinter as ctk
import tkinter as tk
import os
import sys
import threading
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))

from flask_bot import FlaskBot, AtlasFogReveal, load_config, save_config
from custom_dialog import show_info, show_error, ask_yes_no, MemoryOffsetsDialog
from toast_notification import ToastNotification
from coordinate_picker import pick_region
from terrain_reader import TerrainReader
from auto_updater import VERSION, check_for_updates, download_update, apply_update
import keyboard

# Try to import overlay (requires PyQt5)
try:
    from map_overlay import MapOverlayManager, OverlayConfig, PYQT5_AVAILABLE
    OVERLAY_AVAILABLE = PYQT5_AVAILABLE
except ImportError:
    OVERLAY_AVAILABLE = False
    MapOverlayManager = None

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Default global hotkeys (rebindable from Settings → Hotkeys, persisted to config).
DEFAULT_HOTKEYS = {
    "toggle_bot": "f8",
    "toggle_overlay": "f11",
    "toggle_atlas_fog": "f10",
}


class SentinelGUI:
    """Modern GUI for POE2 Sentinel."""

    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("POE2 Sentinel")
        self.root.geometry("820x600")
        self.root.minsize(700, 500)

        # Set window icon (title bar and taskbar)
        # In PyInstaller bundle, resources are extracted to sys._MEIPASS
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.path.dirname(__file__)
        icon_path = os.path.join(base_path, "POE2-Sentinel-Icon.ico")
        if os.path.exists(icon_path):
            self.root.iconbitmap(icon_path)
            # Also set for taskbar on Windows
            self.root.iconbitmap(default=icon_path)

        # GitHub-dark palette (mirrors POE2Tools DarkTheme.xaml). Legacy keys
        # (primary/text/success/danger/life/mana) are kept so existing handlers
        # keep working against the new values.
        self.colors = {
            "primary": "#58A6FF",
            "accent": "#58A6FF",
            "bg_dark": "#0D1117",
            "bg_card": "#161B22",
            "bg_sidebar": "#010409",
            "bg_hover": "#21262D",
            "border": "#30363D",
            "text": "#E6EDF3",
            "text_secondary": "#7D8590",
            "text_muted": "#484F58",
            "success": "#3FB950",
            "danger": "#F85149",
            "warning": "#D29922",
            "purple": "#A371F7",
            "life": "#F85149",
            "mana": "#58A6FF",
        }

        self.root.configure(fg_color=self.colors["bg_dark"])

        self.config = load_config()
        # Ensure rebindable hotkeys exist (older configs won't have them).
        hotkeys = self.config.setdefault("hotkeys", {})
        for action, default_key in DEFAULT_HOTKEYS.items():
            hotkeys.setdefault(action, default_key)

        self.bot = FlaskBot(on_update=self.on_bot_update)
        self.atlas_fog = AtlasFogReveal(self.config.get("game_version", "steam"))

        # Terrain overlay (read-only map display)
        self.terrain_reader = TerrainReader(self.config.get("game_version", "steam"))
        self.terrain_overlay = None
        if OVERLAY_AVAILABLE:
            self.terrain_overlay = MapOverlayManager(self.terrain_reader)
            # Overlay process is spawned lazily on first toggle (F11), not at
            # startup: spawning a multiprocessing child on the UI thread froze
            # the window (and crashed under the one-file build) whenever the
            # game wasn't running.

        # Navigation/panel registries (populated by _build_ui)
        self.panels = {}
        self.nav_buttons = {}
        self._bot_toggle_buttons = []
        self._active_panel = None

        # Hotkey rebinding state
        self._registered_hotkeys = []
        self._hotkey_buttons = {}
        self._rebinding = False

        self._build_ui()
        self.setup_hotkeys()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self._update_connection_status()

    # ========================================================================
    # Layout scaffolding (sidebar + content panels)
    # ========================================================================
    def _build_ui(self):
        """Build the two-column sidebar + content layout."""
        self.root.grid_columnconfigure(0, weight=0)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        self._create_sidebar()
        self._create_content_area()
        self._show_panel("dashboard")

    def _create_sidebar(self):
        """Create the 200px navigation sidebar."""
        sidebar = ctk.CTkFrame(self.root, width=200, corner_radius=0,
                               fg_color=self.colors["bg_sidebar"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(1, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)

        # Logo + live connection status
        header = ctk.CTkFrame(sidebar, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(20, 16))
        ctk.CTkLabel(header, text="POE2 SENTINEL", font=("Segoe UI", 16, "bold"),
                     text_color=self.colors["text"]).pack(anchor="w")
        status_row = ctk.CTkFrame(header, fg_color="transparent")
        status_row.pack(anchor="w", pady=(8, 0))
        self.conn_dot = ctk.CTkLabel(status_row, text="●", font=("Segoe UI", 12),
                                     text_color=self.colors["danger"])
        self.conn_dot.pack(side="left", padx=(0, 6))
        self.conn_label = ctk.CTkLabel(status_row, text="Idle", font=("Segoe UI", 11),
                                       text_color=self.colors["text_secondary"])
        self.conn_label.pack(side="left")

        # Navigation buttons (each row: accent indicator + button)
        nav = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav.grid(row=1, column=0, sticky="new", pady=(8, 0))
        nav_items = [
            ("dashboard", "📊  Dashboard"),
            ("flask", "⚗️  Flask Bot"),
            ("map", "🧭  Map Tools"),
            ("overlay", "🎨  Overlay"),
            ("settings", "⚙️  Settings"),
        ]
        for key, label in nav_items:
            row = ctk.CTkFrame(nav, fg_color="transparent")
            row.pack(fill="x", pady=1)
            indicator = ctk.CTkFrame(row, width=3, height=40, corner_radius=0,
                                     fg_color="transparent")
            indicator.pack(side="left", fill="y")
            indicator.pack_propagate(False)
            btn = ctk.CTkButton(
                row, text=label, anchor="w", height=40, corner_radius=0,
                font=("Segoe UI", 13), fg_color="transparent",
                hover_color=self.colors["bg_hover"],
                text_color=self.colors["text_secondary"],
                command=lambda k=key: self._show_panel(k),
            )
            btn.pack(side="left", fill="x", expand=True)
            self.nav_buttons[key] = (btn, indicator)

        # Bottom primary action: global Start/Stop bot
        self.sidebar_action_btn = ctk.CTkButton(
            sidebar, text="▶  Start Bot", height=40, corner_radius=6,
            font=("Segoe UI", 13, "bold"),
            fg_color=self.colors["success"], hover_color="#2EA043",
            text_color="#FFFFFF", command=self.toggle_bot,
        )
        self.sidebar_action_btn.grid(row=2, column=0, sticky="ew", padx=16, pady=20)
        self._bot_toggle_buttons.append(self.sidebar_action_btn)

    def _create_content_area(self):
        """Create the right-hand content area with one panel per nav item."""
        container = ctk.CTkFrame(self.root, fg_color="transparent")
        container.grid(row=0, column=1, sticky="nsew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        builders = [
            ("dashboard", "Dashboard", self._build_dashboard_panel),
            ("flask", "Flask Bot", self._build_flask_bot_panel),
            ("map", "Map Tools", self._build_map_tools_panel),
            ("overlay", "Overlay", self._build_overlay_panel),
            ("settings", "Settings", self._build_settings_panel),
        ]
        for key, title, builder in builders:
            panel = ctk.CTkScrollableFrame(container, fg_color="transparent")
            panel.grid(row=0, column=0, sticky="nsew", padx=24, pady=18)
            panel.grid_remove()
            ctk.CTkLabel(panel, text=title, font=("Segoe UI", 20, "bold"),
                         text_color=self.colors["text"]).pack(anchor="w", pady=(0, 16))
            builder(panel)
            self.panels[key] = panel

    def _show_panel(self, key: str):
        """Raise the requested panel and update nav highlighting."""
        for panel in self.panels.values():
            panel.grid_remove()
        if key in self.panels:
            self.panels[key].grid()
        self._active_panel = key
        for name, (btn, indicator) in self.nav_buttons.items():
            if name == key:
                btn.configure(fg_color=self.colors["bg_hover"], text_color=self.colors["text"])
                indicator.configure(fg_color=self.colors["accent"])
            else:
                btn.configure(fg_color="transparent", text_color=self.colors["text_secondary"])
                indicator.configure(fg_color="transparent")

    def _make_card(self, parent):
        """Create a standard bordered card and pack it into parent."""
        card = ctk.CTkFrame(parent, fg_color=self.colors["bg_card"],
                            border_width=1, border_color=self.colors["border"],
                            corner_radius=8)
        card.pack(fill="x", pady=(0, 12))
        return card

    def _make_section_header(self, parent, text: str):
        """Add an uppercase section header label to a card."""
        ctk.CTkLabel(parent, text=text.upper(), font=("Segoe UI", 11, "bold"),
                     text_color=self.colors["text_secondary"]).pack(
            anchor="w", padx=16, pady=(14, 10))

    def _update_connection_status(self):
        """Refresh the sidebar status dot from the bot running state."""
        try:
            running = self.bot.is_running()
        except Exception:
            running = False
        if running:
            self.conn_dot.configure(text_color=self.colors["success"])
            self.conn_label.configure(text="Running")
        else:
            self.conn_dot.configure(text_color=self.colors["text_muted"])
            self.conn_label.configure(text="Idle")
        self.root.after(2000, self._update_connection_status)

    def _sync_bot_buttons(self, running: bool):
        """Sync every Start/Stop bot button to the current state."""
        for btn in self._bot_toggle_buttons:
            if running:
                btn.configure(text="⏹  Stop Bot", fg_color=self.colors["danger"],
                              hover_color="#DA3633")
            else:
                btn.configure(text="▶  Start Bot", fg_color=self.colors["success"],
                              hover_color="#2EA043")

    # ========================================================================
    # Panels: Dashboard + Settings (Flask/Map/Overlay are below)
    # ========================================================================
    def _build_dashboard_panel(self, tab):
        """Build the Dashboard: live Life/Mana cards + quick actions."""
        stats_row = ctk.CTkFrame(tab, fg_color="transparent")
        stats_row.pack(fill="x", pady=(0, 4))

        # Life card
        life_card = ctk.CTkFrame(stats_row, fg_color=self.colors["bg_card"],
                                 border_width=1, border_color=self.colors["border"],
                                 corner_radius=8)
        life_card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ctk.CTkLabel(life_card, text="LIFE", font=("Segoe UI", 11, "bold"),
                     text_color=self.colors["text_secondary"]).pack(anchor="w", padx=16, pady=(14, 2))
        self.life_label = ctk.CTkLabel(life_card, text="-- / --", font=("Segoe UI", 24, "bold"),
                                       text_color=self.colors["life"])
        self.life_label.pack(anchor="w", padx=16, pady=(0, 14))

        # Mana card
        mana_card = ctk.CTkFrame(stats_row, fg_color=self.colors["bg_card"],
                                 border_width=1, border_color=self.colors["border"],
                                 corner_radius=8)
        mana_card.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ctk.CTkLabel(mana_card, text="MANA", font=("Segoe UI", 11, "bold"),
                     text_color=self.colors["text_secondary"]).pack(anchor="w", padx=16, pady=(14, 2))
        self.mana_label = ctk.CTkLabel(mana_card, text="-- / --", font=("Segoe UI", 24, "bold"),
                                       text_color=self.colors["mana"])
        self.mana_label.pack(anchor="w", padx=16, pady=(0, 14))

        # Quick actions
        qa = self._make_card(tab)
        self._make_section_header(qa, "Quick Actions")
        qa_inner = ctk.CTkFrame(qa, fg_color="transparent")
        qa_inner.pack(fill="x", padx=16, pady=(0, 14))

        dash_bot_btn = ctk.CTkButton(
            qa_inner, text="▶  Start Bot", font=("Segoe UI", 12, "bold"),
            fg_color=self.colors["success"], hover_color="#2EA043",
            text_color="#FFFFFF", width=150, height=34, corner_radius=6,
            command=self.toggle_bot,
        )
        dash_bot_btn.pack(side="left", padx=(0, 8))
        self._bot_toggle_buttons.append(dash_bot_btn)

        ctk.CTkButton(
            qa_inner, text="🗺️  Toggle Overlay", font=("Segoe UI", 12),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=160, height=34, corner_radius=6,
            command=self.toggle_terrain_overlay,
            state="normal" if OVERLAY_AVAILABLE else "disabled",
        ).pack(side="left")

    def _build_settings_panel(self, tab):
        """Build the Settings panel: OCR region pickers + Hotkeys + Help/About."""
        # OCR regions
        ocr = self._make_card(tab)
        self._make_section_header(ocr, "OCR Regions")
        ctk.CTkLabel(ocr, text="Only needed when running in OCR detection mode.",
                     font=("Segoe UI", 11), text_color=self.colors["text_secondary"]).pack(
            anchor="w", padx=16, pady=(0, 8))
        ocr_inner = ctk.CTkFrame(ocr, fg_color="transparent")
        ocr_inner.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(
            ocr_inner, text="📍  Set Life Region", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=150, height=30, corner_radius=6,
            command=self.pick_life_region,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            ocr_inner, text="📍  Set Mana Region", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=150, height=30, corner_radius=6,
            command=self.pick_mana_region,
        ).pack(side="left")

        # Memory Offsets (for advanced users - game patch updates)
        mem_card = self._make_card(tab)
        self._make_section_header(mem_card, "Memory Offsets")
        ctk.CTkLabel(mem_card, text="Update pointer offsets when game patches break memory reading.",
                     font=("Segoe UI", 11), text_color=self.colors["text_secondary"]).pack(
            anchor="w", padx=16, pady=(0, 8))

        mem_inner = ctk.CTkFrame(mem_card, fg_color="transparent")
        mem_inner.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(
            mem_inner, text="🔧  Edit Offsets", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=150, height=30, corner_radius=6,
            command=self._open_memory_offsets_dialog,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            mem_inner, text="↺  Reset to Default", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=150, height=30, corner_radius=6,
            command=self._reset_memory_offsets,
        ).pack(side="left")

        # Hotkeys (rebindable global toggles)
        hk_card = self._make_card(tab)
        self._make_section_header(hk_card, "Hotkeys")
        ctk.CTkLabel(hk_card, text="Click a key to rebind, then press the new key (Esc to clear).",
                     font=("Segoe UI", 11), text_color=self.colors["text_secondary"]).pack(
            anchor="w", padx=16, pady=(0, 8))
        self._hotkey_buttons = {}
        hotkeys = self.config.get("hotkeys", {})
        hotkey_rows = [
            ("toggle_bot", "Start / Stop Bot"),
            ("toggle_overlay", "Toggle Entity Overlay"),
            ("toggle_atlas_fog", "Toggle Atlas Fog"),
        ]
        for action, label in hotkey_rows:
            self._make_hotkey_row(hk_card, label, action, hotkeys.get(action, ""))

        # About
        about = self._make_card(tab)
        self._make_section_header(about, "About")
        ctk.CTkLabel(about, text=f"POE2 Sentinel v{VERSION}", font=("Segoe UI", 14, "bold"),
                     text_color=self.colors["text"]).pack(anchor="w", padx=16, pady=(0, 2))
        ctk.CTkLabel(about, text="Flask bot, terrain overlay, and map tools "
                                 "for Path of Exile 2.",
                     font=("Segoe UI", 11), text_color=self.colors["text_secondary"],
                     wraplength=420, justify="left").pack(anchor="w", padx=16, pady=(0, 10))
        about_btns = ctk.CTkFrame(about, fg_color="transparent")
        about_btns.pack(anchor="w", padx=16, pady=(0, 14))
        ctk.CTkButton(
            about_btns, text="❓  Help", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=100, height=30, corner_radius=6,
            command=self.show_help,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            about_btns, text="🔄  Updates", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=100, height=30, corner_radius=6,
            command=self.check_for_updates,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            about_btns, text="⭐  GitHub", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"], width=100, height=30, corner_radius=6,
            command=self.open_github,
        ).pack(side="left")

    def setup_hotkeys(self):
        """Register global hotkeys from config (rebindable via Settings)."""
        self._registered_hotkeys = []
        hotkeys = self.config.get("hotkeys", {})
        bindings = [
            (hotkeys.get("toggle_bot"), self._toggle_bot_hotkey),
            (hotkeys.get("toggle_atlas_fog"), self.toggle_atlas_fog_hotkey),
            (hotkeys.get("toggle_overlay"), self.toggle_terrain_overlay_hotkey),
        ]
        for key, handler in bindings:
            if not key:
                continue
            try:
                # Store the handler reference (not the key string): keyboard's
                # internal map only remembers the last registration per key, so
                # removing by key can leave a duplicate-key binding dangling
                # (e.g. after swapping two actions' keys). Removing by handle
                # unhooks each individual registration reliably.
                handle = keyboard.add_hotkey(key, handler)
                self._registered_hotkeys.append(handle)
            except Exception as e:
                print(f"Failed to register hotkey '{key}': {e}")

    def _unregister_hotkeys(self):
        """Remove all hotkeys this app has registered."""
        for handle in getattr(self, "_registered_hotkeys", []):
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
        self._registered_hotkeys = []

    def _reload_hotkeys(self):
        """Re-register hotkeys after a rebind."""
        self._unregister_hotkeys()
        self.setup_hotkeys()

    def _toggle_bot_hotkey(self):
        """Handle the start/stop bot hotkey."""
        self.root.after(0, self.toggle_bot)

    def toggle_atlas_fog_hotkey(self):
        """Handle the atlas fog reveal toggle hotkey."""
        self.root.after(0, self.toggle_atlas_fog)

    def toggle_terrain_overlay_hotkey(self):
        """Handle the entity overlay toggle hotkey."""
        self.root.after(0, self.toggle_terrain_overlay)

    # ------------------------------------------------------------------
    # Hotkey rebinding (Settings → Hotkeys)
    # ------------------------------------------------------------------
    @staticmethod
    def _hotkey_label(key: str) -> str:
        """Human-readable label for a hotkey button."""
        return key.upper() if key else "Unset"

    def _make_hotkey_row(self, parent, label: str, action: str, key: str):
        """Add a hotkey rebind row (label on the left, clickable key on the right)."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(row, text=label, font=("Segoe UI", 11),
                     text_color=self.colors["text"]).pack(side="left")
        btn = ctk.CTkButton(
            row, text=self._hotkey_label(key), font=("Segoe UI", 11, "bold"),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["primary"], width=120, height=30, corner_radius=6,
            command=lambda a=action: self._start_rebind(a),
        )
        btn.pack(side="right")
        self._hotkey_buttons[action] = btn

    def _start_rebind(self, action: str):
        """Listen for the next key press and bind it to the given action."""
        if self._rebinding:
            return
        btn = self._hotkey_buttons.get(action)
        if btn is None:
            return
        self._rebinding = True
        btn.configure(text="Press a key…", text_color=self.colors["warning"])

        def capture():
            captured: Optional[str] = None
            try:
                event = keyboard.read_event(suppress=False)
                while event.event_type != keyboard.KEY_DOWN:
                    event = keyboard.read_event(suppress=False)
                captured = event.name
            except Exception as e:
                print(f"Hotkey capture failed: {e}")
            self.root.after(0, lambda: self._finish_rebind(action, captured))

        threading.Thread(target=capture, daemon=True).start()

    def _finish_rebind(self, action: str, key: Optional[str]):
        """Apply a captured key to the action, persist it, and re-register hotkeys."""
        self._rebinding = False
        btn = self._hotkey_buttons.get(action)
        current = self.config.get("hotkeys", {}).get(action, "")

        if key is None:
            if btn:
                btn.configure(text=self._hotkey_label(current),
                              text_color=self.colors["primary"])
            self.show_toast("Rebind cancelled", "info")
            return

        # Esc clears the binding (action becomes unbound).
        if key == "esc":
            key = ""

        self.config.setdefault("hotkeys", {})[action] = key
        save_config(self.config)
        self._reload_hotkeys()
        if btn:
            btn.configure(text=self._hotkey_label(key), text_color=self.colors["primary"])
        self.show_toast(f"Hotkey set: {self._hotkey_label(key)}", "success")

    def show_help(self):
        """Show help dialog."""
        hotkeys = self.config.get("hotkeys", {})
        bot_key = self._hotkey_label(hotkeys.get("toggle_bot", ""))
        fog_key = self._hotkey_label(hotkeys.get("toggle_atlas_fog", ""))
        overlay_key = self._hotkey_label(hotkeys.get("toggle_overlay", ""))
        show_info(
            self.root,
            "POE2 Sentinel",
            "This bot can read HP/Mana/ES via Memory, Structure, or OCR.\n\n"
            "Detection Modes:\n"
            "• Structure: RECOMMENDED - Auto-updates across patches!\n"
            "• Memory: Fast, but needs manual pointer updates\n"
            "• OCR: Fallback using screen capture\n\n"
            "Setup:\n"
            "1. Make sure POE2 is running\n"
            "2. Select detection mode (Structure recommended)\n"
            "3. For OCR: Use Tools → Set Life/Mana Region\n"
            "4. Adjust thresholds as needed\n"
            "5. Click START to begin\n\n"
            "Hotkeys (rebind in Settings → Hotkeys):\n"
            f"• {bot_key}: Start / Stop Bot\n"
            f"• {fog_key}: Toggle Atlas Fog\n"
            f"• {overlay_key}: Toggle Entity Overlay\n\n"
            "💡 Structure mode works for HP and ES builds!\n"
            "   Set pool_type in config for ES builds.",
            colors=self.colors
        )

    def open_github(self):
        """Open the GitHub repository in the default browser."""
        import webbrowser
        webbrowser.open("https://github.com/Ace047/POE2-Sentinel")

    def check_for_updates(self):
        """Check GitHub for updates."""
        self.show_toast("Checking for updates...", "info")

        def _check_thread():
            release = check_for_updates()
            self.root.after(0, lambda: self._handle_update_result(release))

        threading.Thread(target=_check_thread, daemon=True).start()

    def _handle_update_result(self, release):
        """Handle update check result on main thread."""
        if release is None:
            self.show_toast(f"You're on the latest version (v{VERSION})", "success")
            return

        # Show first 5 lines of release notes to avoid overwhelming the user
        notes_lines = release.release_notes.strip().split('\n')[:5]
        notes = '\n'.join(notes_lines)
        if len(release.release_notes.strip().split('\n')) > 5:
            notes += "\n(+ more...)"

        msg = f"Version {release.version} is available!\n\nWhat's new:\n{notes}\n\nDownload and install now?"

        if ask_yes_no(self.root, "Update Available", msg, colors=self.colors):
            self._download_and_apply_update(release)

    def _download_and_apply_update(self, release):
        """Download and apply update."""
        # Create progress window
        progress_win = ctk.CTkToplevel(self.root)
        progress_win.title("Downloading Update")
        progress_win.geometry("400x120")
        progress_win.resizable(False, False)
        progress_win.transient(self.root)
        progress_win.grab_set()

        # Center on parent
        progress_win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 400) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 120) // 2
        progress_win.geometry(f"+{x}+{y}")

        ctk.CTkLabel(progress_win, text=f"Downloading v{release.version}...",
                     font=("Segoe UI", 12)).pack(pady=(20, 10))
        progress_bar = ctk.CTkProgressBar(progress_win, width=350)
        progress_bar.pack(pady=10)
        progress_bar.set(0)
        progress_label = ctk.CTkLabel(progress_win, text="0%", font=("Segoe UI", 10))
        progress_label.pack()

        def _download_thread():
            def on_progress(downloaded, total):
                if total > 0:
                    pct = downloaded / total
                    self.root.after(0, lambda p=pct: progress_bar.set(p))
                    self.root.after(0, lambda p=pct: progress_label.configure(text=f"{int(p*100)}%"))

            temp_path = download_update(release, on_progress)
            self.root.after(0, lambda: self._finish_update(temp_path, progress_win))

        threading.Thread(target=_download_thread, daemon=True).start()

    def _finish_update(self, temp_path, progress_win):
        """Finish update process."""
        progress_win.destroy()

        if not temp_path:
            show_error(self.root, "Update Failed", "Failed to download update. Please try again later.", colors=self.colors)
            return

        # Check if we're running as exe
        if not getattr(sys, 'frozen', False):
            show_info(self.root, "Update Downloaded",
                      f"Update downloaded to:\n{temp_path}\n\n"
                      "Since you're running from source, please manually replace the exe.",
                      colors=self.colors)
            return

        # Apply update
        self.show_toast("Installing update...", "info")
        if apply_update(temp_path):
            show_info(self.root, "Update Ready",
                      "Update is ready to install.\n\n"
                      "The application will now close. Please relaunch it manually\n"
                      "after the updater window closes.",
                      colors=self.colors)
            self.root.quit()
        else:
            show_error(self.root, "Update Failed",
                       f"Failed to apply update.\n\nThe new version was downloaded to:\n{temp_path}",
                       colors=self.colors)

    def pick_life_region(self):
        """Open region picker for life."""
        if self.bot.is_running():
            self.show_toast("Stop the bot first", "error")
            return

        self.root.iconify()  # Minimize
        self.root.after(300, lambda: self._do_pick_region("life"))

    def pick_mana_region(self):
        """Open region picker for mana."""
        if self.bot.is_running():
            self.show_toast("Stop the bot first", "error")
            return

        self.root.iconify()  # Minimize
        self.root.after(300, lambda: self._do_pick_region("mana"))

    def _do_pick_region(self, region_type: str):
        """Actually perform the region pick after window is minimized."""
        region = pick_region(region_type, parent=self.root)
        self.root.deiconify()  # Restore window

        if region:
            # Save to config
            self.config[region_type]["region"] = region
            save_config(self.config)
            self.bot.reload_config()
            self.show_toast(f"{region_type.title()} region saved!", "success")
        else:
            self.show_toast("Region selection cancelled", "info")

    def reset_atlas_fog(self):
        """Reset atlas fog pattern cache (use after game restart)."""
        self.atlas_fog.reset()
        self.atlas_fog_status.configure(text="OFF", text_color=self.colors["danger"])
        self.show_toast("Atlas fog cache reset", "info")

    def _open_memory_offsets_dialog(self):
        """Open dialog to edit memory pointer offsets."""
        if self.bot.is_running():
            self.show_toast("Stop the bot first", "error")
            return

        dialog = MemoryOffsetsDialog(self.root, self.config, self.colors)
        result = dialog.show()

        if result:
            # Save the updated offsets
            self.config["memory_offsets"] = result
            save_config(self.config)
            self.bot.reload_config()
            self.show_toast("Memory offsets updated!", "success")

    def _reset_memory_offsets(self):
        """Reset memory offsets to default values."""
        if self.bot.is_running():
            self.show_toast("Stop the bot first", "error")
            return

        from flask_bot import MemoryReader

        # Convert defaults to hex strings for config
        defaults = MemoryReader.DEFAULT_OFFSETS
        hex_offsets = {
            "current_hp_base": hex(defaults["current_hp_base"]),
            "current_hp_chain": [hex(v) for v in defaults["current_hp_chain"]],
            "max_hp_base": hex(defaults["max_hp_base"]),
            "max_hp_chain": [hex(v) for v in defaults["max_hp_chain"]],
            "current_mp_base": hex(defaults["current_mp_base"]),
            "current_mp_chain": [hex(v) for v in defaults["current_mp_chain"]],
            "max_mp_base": hex(defaults["max_mp_base"]),
            "max_mp_chain": [hex(v) for v in defaults["max_mp_chain"]],
        }

        self.config["memory_offsets"] = hex_offsets
        save_config(self.config)
        self.bot.reload_config()
        self.show_toast("Memory offsets reset to defaults", "success")

    def _build_flask_bot_panel(self, tab):
        """Build the Flask Bot panel content."""
        # Detection mode selector
        mode_frame = ctk.CTkFrame(tab, fg_color=self.colors["bg_card"], border_width=1,
                                  border_color=self.colors["border"], corner_radius=8)
        mode_frame.pack(fill="x", padx=10, pady=(10, 10))

        mode_inner = ctk.CTkFrame(mode_frame, fg_color="transparent")
        mode_inner.pack(fill="x", padx=12, pady=8)

        ctk.CTkLabel(mode_inner, text="Detection Mode:", font=("Segoe UI", 12),
                     text_color=self.colors["text"]).pack(side="left")

        # Map config value to display name
        mode_map = {"structure": "Structure", "memory": "Memory", "ocr": "OCR"}
        current_mode = self.config.get("detection_mode", "structure")
        display_mode = mode_map.get(current_mode, "Structure")

        self.mode_var = ctk.StringVar(value=display_mode)
        self.mode_selector = ctk.CTkSegmentedButton(
            mode_inner, values=["Structure", "Memory", "OCR"], width=220, height=28,
            font=("Segoe UI", 11), variable=self.mode_var,
            command=self.on_mode_change,
            selected_color=self.colors["primary"], selected_hover_color="#5B4ED1"
        )
        self.mode_selector.pack(side="right")

        # Status and Start/Stop
        status_frame = ctk.CTkFrame(tab, fg_color=self.colors["bg_card"], border_width=1,
                                    border_color=self.colors["border"], corner_radius=8)
        status_frame.pack(fill="x", padx=10, pady=(0, 10))

        status_inner = ctk.CTkFrame(status_frame, fg_color="transparent")
        status_inner.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(status_inner, text="Status:", font=("Segoe UI", 13),
                     text_color=self.colors["text"]).pack(side="left")

        self.status_label = ctk.CTkLabel(
            status_inner, text="● Stopped", font=("Segoe UI", 13, "bold"),
            text_color=self.colors["danger"]
        )
        self.status_label.pack(side="left", padx=(8, 0))

        self.toggle_btn = ctk.CTkButton(
            status_inner, text="▶  Start Bot", font=("Segoe UI", 13, "bold"),
            fg_color=self.colors["success"], hover_color="#2EA043",
            text_color="#FFFFFF", width=120, height=32, corner_radius=6,
            command=self.toggle_bot
        )
        self.toggle_btn.pack(side="right")
        self._bot_toggle_buttons.append(self.toggle_btn)

        # Thresholds card
        thresholds_frame = ctk.CTkFrame(tab, fg_color=self.colors["bg_card"], border_width=1,
                                        border_color=self.colors["border"], corner_radius=8)
        thresholds_frame.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(thresholds_frame, text="Thresholds", font=("Segoe UI", 12, "bold"),
                     text_color=self.colors["text"]).pack(anchor="w", padx=12, pady=(10, 5))

        self.life_threshold_var = ctk.StringVar()
        self.mana_threshold_var = ctk.StringVar()
        self._create_threshold_row(thresholds_frame, "Life:", "life", self.colors["life"], self.life_threshold_var)
        ctk.CTkFrame(thresholds_frame, fg_color="transparent", height=3).pack()
        self._create_threshold_row(thresholds_frame, "Mana:", "mana", self.colors["mana"], self.mana_threshold_var)
        ctk.CTkFrame(thresholds_frame, fg_color="transparent", height=10).pack()

        # Subtitle - set based on current detection mode
        current_mode = self.config.get("detection_mode", "structure")
        if current_mode == "memory":
            subtitle_text = "Memory mode • Pointer Chains"
        elif current_mode == "ocr":
            subtitle_text = "OCR mode • Screen Capture"
        else:
            subtitle_text = "Structure mode • Auto-Updates"

        self.subtitle_label = ctk.CTkLabel(
            tab, text=subtitle_text,
            font=("Segoe UI", 10), text_color=self.colors["text_secondary"]
        )
        self.subtitle_label.pack(pady=(5, 0))

    def _build_map_tools_panel(self, tab):
        """Build the Map Tools panel content."""
        # Atlas Fog Reveal card
        atlas_fog_frame = ctk.CTkFrame(tab, fg_color=self.colors["bg_card"], border_width=1,
                                       border_color=self.colors["border"], corner_radius=8)
        atlas_fog_frame.pack(fill="x", padx=10, pady=(10, 10))

        atlas_fog_inner = ctk.CTkFrame(atlas_fog_frame, fg_color="transparent")
        atlas_fog_inner.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(atlas_fog_inner, text="🌐 Atlas Fog", font=("Segoe UI", 12, "bold"),
                     text_color=self.colors["text"]).pack(side="left")

        self.atlas_fog_status = ctk.CTkLabel(atlas_fog_inner, text="OFF", font=("Segoe UI", 11),
                                              text_color=self.colors["danger"])
        self.atlas_fog_status.pack(side="left", padx=(8, 0))

        self.atlas_fog_btn = ctk.CTkButton(
            atlas_fog_inner, text="Toggle (F10)", font=("Segoe UI", 11),
            fg_color=self.colors["primary"], hover_color="#388BFD", text_color="#FFFFFF",
            width=90, height=28, corner_radius=6, command=self.toggle_atlas_fog
        )
        self.atlas_fog_btn.pack(side="right")

        # Atlas Fog description
        ctk.CTkLabel(atlas_fog_frame, text="Removes fog from Atlas map",
                     font=("Segoe UI", 10), text_color="#888888").pack(padx=12, pady=(0, 8))

        # Shader Reveal card (on-disk shader patch: reveal layout, keep fog)
        shader_frame = ctk.CTkFrame(tab, fg_color=self.colors["bg_card"], border_width=1,
                                    border_color=self.colors["border"], corner_radius=8)
        shader_frame.pack(fill="x", padx=10, pady=(0, 10))

        shader_inner = ctk.CTkFrame(shader_frame, fg_color="transparent")
        shader_inner.pack(fill="x", padx=12, pady=(10, 4))

        ctk.CTkLabel(shader_inner, text="🔦 Shader Reveal (keep fog)", font=("Segoe UI", 12, "bold"),
                     text_color=self.colors["text"]).pack(side="left")

        self.shader_reveal_status = ctk.CTkLabel(shader_inner, text="…", font=("Segoe UI", 11),
                                                 text_color=self.colors["text_secondary"])
        self.shader_reveal_status.pack(side="left", padx=(8, 0))

        # Threshold (visibility floor) slider row
        shader_slider_row = ctk.CTkFrame(shader_frame, fg_color="transparent")
        shader_slider_row.pack(fill="x", padx=12, pady=(2, 4))

        ctk.CTkLabel(shader_slider_row, text="Floor:", font=("Segoe UI", 11),
                     text_color=self.colors["text_secondary"]).pack(side="left")

        self.shader_threshold_var = tk.DoubleVar(value=0.18)
        self.shader_threshold_slider = ctk.CTkSlider(
            shader_slider_row, from_=0.05, to=0.40, number_of_steps=35,
            variable=self.shader_threshold_var, width=150, height=16,
            command=self._on_shader_threshold_change
        )
        self.shader_threshold_slider.pack(side="left", padx=(8, 5))

        self.shader_threshold_label = ctk.CTkLabel(shader_slider_row, text="0.18",
                                                   font=("Segoe UI", 10), text_color="#888888", width=35)
        self.shader_threshold_label.pack(side="left")

        # Apply / Remove buttons row
        shader_btn_row = ctk.CTkFrame(shader_frame, fg_color="transparent")
        shader_btn_row.pack(fill="x", padx=12, pady=(2, 6))

        self.shader_apply_btn = ctk.CTkButton(
            shader_btn_row, text="Apply", font=("Segoe UI", 11),
            fg_color=self.colors["primary"], hover_color="#388BFD", text_color="#FFFFFF",
            width=80, height=28, corner_radius=6, command=self.apply_shader_reveal
        )
        self.shader_apply_btn.pack(side="left", padx=(0, 8))

        self.shader_remove_btn = ctk.CTkButton(
            shader_btn_row, text="Remove", font=("Segoe UI", 11),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"],
            width=80, height=28, corner_radius=6, command=self.remove_shader_reveal
        )
        self.shader_remove_btn.pack(side="left")

        # Shader Reveal description
        ctk.CTkLabel(shader_frame, text="On-disk patch · close POE2 first · applies on relaunch",
                     font=("Segoe UI", 10), text_color="#888888").pack(padx=12, pady=(0, 8))

        # Read current shader patch state in the background
        self._refresh_shader_status()

        # Reset buttons section
        reset_frame = ctk.CTkFrame(tab, fg_color=self.colors["bg_card"], border_width=1,
                                   border_color=self.colors["border"], corner_radius=8)
        reset_frame.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(reset_frame, text="Cache Management", font=("Segoe UI", 12, "bold"),
                     text_color=self.colors["text"]).pack(anchor="w", padx=12, pady=(10, 8))

        reset_inner = ctk.CTkFrame(reset_frame, fg_color="transparent")
        reset_inner.pack(fill="x", padx=12, pady=(0, 10))

        ctk.CTkButton(
            reset_inner, text="Reset Atlas Cache", font=("Segoe UI", 10),
            fg_color=self.colors["bg_hover"], hover_color=self.colors["border"],
            text_color=self.colors["text"],
            width=120, height=26, corner_radius=6, command=self.reset_atlas_fog
        ).pack(side="left")

    def _build_overlay_panel(self, tab):
        """Build the Terrain Overlay panel with color customization."""
        # Container for all overlay settings (parent panel already scrolls)
        scroll_frame = ctk.CTkFrame(tab, fg_color="transparent")
        scroll_frame.pack(fill="both", expand=True, padx=0, pady=0)

        # Enable/Disable section
        enable_frame = ctk.CTkFrame(scroll_frame, fg_color=self.colors["bg_card"], border_width=1,
                                    border_color=self.colors["border"], corner_radius=8)
        enable_frame.pack(fill="x", padx=10, pady=(10, 10))

        enable_inner = ctk.CTkFrame(enable_frame, fg_color="transparent")
        enable_inner.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(enable_inner, text="🗺️ Entity Overlay", font=("Segoe UI", 12, "bold"),
                     text_color=self.colors["text"]).pack(side="left")

        self.terrain_status = ctk.CTkLabel(enable_inner, text="OFF", font=("Segoe UI", 11),
                                            text_color=self.colors["danger"])
        self.terrain_status.pack(side="left", padx=(8, 0))

        self.terrain_btn = ctk.CTkButton(
            enable_inner, text="Toggle", font=("Segoe UI", 11),
            fg_color=self.colors["primary"] if OVERLAY_AVAILABLE else self.colors["bg_hover"],
            hover_color="#388BFD" if OVERLAY_AVAILABLE else self.colors["bg_hover"],
            text_color="#FFFFFF",
            width=90, height=28, corner_radius=6, command=self.toggle_terrain_overlay,
            state="normal" if OVERLAY_AVAILABLE else "disabled"
        )
        self.terrain_btn.pack(side="right")

        # Description
        desc_text = "Shows entity icons (monsters, NPCs, chests, portals) on map • Only visible when POE2 is active" if OVERLAY_AVAILABLE else "Requires PyQt5: pip install PyQt5"
        ctk.CTkLabel(enable_frame, text=desc_text,
                     font=("Segoe UI", 10), text_color="#888888").pack(padx=12, pady=(0, 8))

        # ========================================================================
        # Entity Radar Settings
        # ========================================================================
        entities_frame = ctk.CTkFrame(scroll_frame, fg_color=self.colors["bg_card"], border_width=1,
                                      border_color=self.colors["border"], corner_radius=8)
        entities_frame.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(entities_frame, text="Entity Radar", font=("Segoe UI", 12, "bold"),
                     text_color=self.colors["text"]).pack(anchor="w", padx=12, pady=(10, 8))

        # Get current entity settings
        overlay_cfg = self.config.get("overlay", {})
        show_entities = overlay_cfg.get("show_entities", True)
        show_monsters = overlay_cfg.get("show_monsters", True)
        show_npcs = overlay_cfg.get("show_npcs", True)
        show_chests = overlay_cfg.get("show_chests", True)
        show_transitions = overlay_cfg.get("show_transitions", True)

        # Main toggle
        entity_toggle_row = ctk.CTkFrame(entities_frame, fg_color="transparent")
        entity_toggle_row.pack(fill="x", padx=12, pady=3)

        ctk.CTkLabel(entity_toggle_row, text="Show Entities:", font=("Segoe UI", 11),
                     text_color=self.colors["text"], width=100).pack(side="left")

        self.show_entities_var = ctk.BooleanVar(value=show_entities)
        self.show_entities_switch = ctk.CTkSwitch(
            entity_toggle_row, text="", variable=self.show_entities_var,
            width=40, command=lambda: self._on_entity_toggle("show_entities", self.show_entities_var)
        )
        self.show_entities_switch.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(entity_toggle_row, text="Shows monsters, NPCs, chests on map",
                     font=("Segoe UI", 9), text_color="#888888").pack(side="left")

        # Entity type toggles (in a row) - Monsters has no color since rarity handles it
        entity_types_row = ctk.CTkFrame(entities_frame, fg_color="transparent")
        entity_types_row.pack(fill="x", padx=12, pady=5)

        # Monsters toggle (no color button - rarity colors are used instead)
        self.show_monsters_var = ctk.BooleanVar(value=show_monsters)
        ctk.CTkSwitch(
            entity_types_row, text="Monsters", variable=self.show_monsters_var,
            width=40, font=("Segoe UI", 10),
            command=lambda: self._on_entity_toggle("show_monsters", self.show_monsters_var)
        ).pack(side="left", padx=(0, 15))

        # NPCs toggle with color
        npc_hex = overlay_cfg.get("npc_color", "#00FF00")
        self.show_npcs_var = ctk.BooleanVar(value=show_npcs)
        npc_frame = ctk.CTkFrame(entity_types_row, fg_color="transparent")
        npc_frame.pack(side="left", padx=(0, 15))
        ctk.CTkSwitch(
            npc_frame, text="NPCs", variable=self.show_npcs_var,
            width=40, font=("Segoe UI", 10),
            command=lambda: self._on_entity_toggle("show_npcs", self.show_npcs_var)
        ).pack(side="left")
        self.npc_color_btn = ctk.CTkButton(
            npc_frame, text="", width=18, height=16, corner_radius=3,
            fg_color=npc_hex, hover_color=npc_hex,
            command=lambda: self._pick_entity_color("npc", self.npc_color_btn)
        )
        self.npc_color_btn.pack(side="left", padx=(6, 0))

        # Chests toggle with color
        chest_hex = overlay_cfg.get("chest_color", "#FFD700")
        self.show_chests_var = ctk.BooleanVar(value=show_chests)
        chest_frame = ctk.CTkFrame(entity_types_row, fg_color="transparent")
        chest_frame.pack(side="left", padx=(0, 15))
        ctk.CTkSwitch(
            chest_frame, text="Chests", variable=self.show_chests_var,
            width=40, font=("Segoe UI", 10),
            command=lambda: self._on_entity_toggle("show_chests", self.show_chests_var)
        ).pack(side="left")
        self.chest_color_btn = ctk.CTkButton(
            chest_frame, text="", width=18, height=16, corner_radius=3,
            fg_color=chest_hex, hover_color=chest_hex,
            command=lambda: self._pick_entity_color("chest", self.chest_color_btn)
        )
        self.chest_color_btn.pack(side="left", padx=(6, 0))

        # Transitions toggle with color
        transition_hex = overlay_cfg.get("transition_color", "#00FFFF")
        self.show_transitions_var = ctk.BooleanVar(value=show_transitions)
        transition_frame = ctk.CTkFrame(entity_types_row, fg_color="transparent")
        transition_frame.pack(side="left")
        ctk.CTkSwitch(
            transition_frame, text="Portals", variable=self.show_transitions_var,
            width=40, font=("Segoe UI", 10),
            command=lambda: self._on_entity_toggle("show_transitions", self.show_transitions_var)
        ).pack(side="left")
        self.transition_color_btn = ctk.CTkButton(
            transition_frame, text="", width=18, height=16, corner_radius=3,
            fg_color=transition_hex, hover_color=transition_hex,
            command=lambda: self._pick_entity_color("transition", self.transition_color_btn)
        )
        self.transition_color_btn.pack(side="left", padx=(6, 0))

        # Monster rarity toggles with colors (in a row)
        rarity_row = ctk.CTkFrame(entities_frame, fg_color="transparent")
        rarity_row.pack(fill="x", padx=12, pady=5)

        ctk.CTkLabel(rarity_row, text="Rarity:", font=("Segoe UI", 10),
                     text_color="#888888").pack(side="left", padx=(0, 10))

        # Get rarity toggle states
        show_normal = overlay_cfg.get("show_normal_monsters", True)
        show_magic = overlay_cfg.get("show_magic_monsters", True)
        show_rare = overlay_cfg.get("show_rare_monsters", True)
        show_unique = overlay_cfg.get("show_unique_monsters", True)

        # Normal monsters
        normal_hex = overlay_cfg.get("monster_normal_color", "#FF0000")
        normal_frame = ctk.CTkFrame(rarity_row, fg_color="transparent")
        normal_frame.pack(side="left", padx=(0, 12))
        self.show_normal_var = ctk.BooleanVar(value=show_normal)
        ctk.CTkCheckBox(
            normal_frame, text="Normal", variable=self.show_normal_var,
            width=20, font=("Segoe UI", 10), checkbox_width=16, checkbox_height=16,
            command=lambda: self._on_entity_toggle("show_normal_monsters", self.show_normal_var)
        ).pack(side="left")
        self.normal_color_btn = ctk.CTkButton(
            normal_frame, text="", width=16, height=14, corner_radius=2,
            fg_color=normal_hex, hover_color=normal_hex,
            command=lambda: self._pick_entity_color("monster_normal", self.normal_color_btn)
        )
        self.normal_color_btn.pack(side="left", padx=(6, 0))

        # Magic monsters
        magic_hex = overlay_cfg.get("monster_magic_color", "#0000FF")
        magic_frame = ctk.CTkFrame(rarity_row, fg_color="transparent")
        magic_frame.pack(side="left", padx=(0, 12))
        self.show_magic_var = ctk.BooleanVar(value=show_magic)
        ctk.CTkCheckBox(
            magic_frame, text="Magic", variable=self.show_magic_var,
            width=20, font=("Segoe UI", 10), checkbox_width=16, checkbox_height=16,
            command=lambda: self._on_entity_toggle("show_magic_monsters", self.show_magic_var)
        ).pack(side="left")
        self.magic_color_btn = ctk.CTkButton(
            magic_frame, text="", width=16, height=14, corner_radius=2,
            fg_color=magic_hex, hover_color=magic_hex,
            command=lambda: self._pick_entity_color("monster_magic", self.magic_color_btn)
        )
        self.magic_color_btn.pack(side="left", padx=(6, 0))

        # Rare monsters
        rare_hex = overlay_cfg.get("monster_rare_color", "#FFFF00")
        rare_frame = ctk.CTkFrame(rarity_row, fg_color="transparent")
        rare_frame.pack(side="left", padx=(0, 12))
        self.show_rare_var = ctk.BooleanVar(value=show_rare)
        ctk.CTkCheckBox(
            rare_frame, text="Rare", variable=self.show_rare_var,
            width=20, font=("Segoe UI", 10), checkbox_width=16, checkbox_height=16,
            command=lambda: self._on_entity_toggle("show_rare_monsters", self.show_rare_var)
        ).pack(side="left")
        self.rare_color_btn = ctk.CTkButton(
            rare_frame, text="", width=16, height=14, corner_radius=2,
            fg_color=rare_hex, hover_color=rare_hex,
            command=lambda: self._pick_entity_color("monster_rare", self.rare_color_btn)
        )
        self.rare_color_btn.pack(side="left", padx=(6, 0))

        # Unique/Boss monsters
        unique_hex = overlay_cfg.get("monster_unique_color", "#FFA500")
        unique_frame = ctk.CTkFrame(rarity_row, fg_color="transparent")
        unique_frame.pack(side="left")
        self.show_unique_var = ctk.BooleanVar(value=show_unique)
        ctk.CTkCheckBox(
            unique_frame, text="Unique", variable=self.show_unique_var,
            width=20, font=("Segoe UI", 10), checkbox_width=16, checkbox_height=16,
            command=lambda: self._on_entity_toggle("show_unique_monsters", self.show_unique_var)
        ).pack(side="left")
        self.unique_color_btn = ctk.CTkButton(
            unique_frame, text="", width=16, height=14, corner_radius=2,
            fg_color=unique_hex, hover_color=unique_hex,
            command=lambda: self._pick_entity_color("monster_unique", self.unique_color_btn)
        )
        self.unique_color_btn.pack(side="left", padx=(6, 0))

        ctk.CTkFrame(entities_frame, fg_color="transparent", height=8).pack()

    def _on_entity_toggle(self, setting_name: str, var: ctk.BooleanVar):
        """Handle entity visibility toggle."""
        if "overlay" not in self.config:
            self.config["overlay"] = {}
        self.config["overlay"][setting_name] = var.get()
        save_config(self.config)

    def _pick_entity_color(self, color_type: str, btn: ctk.CTkButton):
        """Open color picker for entity colors."""
        from tkinter import colorchooser

        current_color = self.config.get("overlay", {}).get(f"{color_type}_color", "#FFFFFF")
        color = colorchooser.askcolor(color=current_color, title=f"Choose {color_type.replace('_', ' ').title()} color")

        if color[1]:
            hex_color = color[1].upper()
            btn.configure(fg_color=hex_color, hover_color=hex_color)

            if "overlay" not in self.config:
                self.config["overlay"] = {}
            self.config["overlay"][f"{color_type}_color"] = hex_color
            save_config(self.config)

    def _create_threshold_row(self, parent, label: str, config_key: str, color: str, var: ctk.StringVar):
        """Create a threshold setting row."""
        row = ctk.CTkFrame(parent, fg_color="transparent", height=40)
        row.pack(fill="x", padx=15, pady=8)
        row.pack_propagate(False)  # Fix height

        # Label on left
        ctk.CTkLabel(row, text=label, font=("Segoe UI", 12), text_color=color, width=50).pack(side="left")

        # Entry on far right
        entry = ctk.CTkEntry(row, width=60, height=32, font=("Segoe UI", 12),
                             fg_color=self.colors["bg_dark"], border_color=color, textvariable=var)
        entry.pack(side="right", padx=(5, 0))

        cfg = self.config.get(config_key, {})
        is_percent = cfg.get("threshold_mode", "percent") == "percent"
        mode_var = ctk.StringVar(value="%" if is_percent else "Value")

        if is_percent:
            var.set(str(cfg.get("threshold_percent", 50)))
        else:
            var.set(str(cfg.get("threshold_absolute", 500)))

        def save(event=None):
            try:
                if mode_var.get() == "%":
                    value = float(var.get())
                    if not 0 < value <= 100:
                        raise ValueError
                    self.config[config_key]["threshold_percent"] = value
                    self.config[config_key]["threshold_mode"] = "percent"
                else:
                    value = int(var.get())
                    self.config[config_key]["threshold_absolute"] = value
                    self.config[config_key]["threshold_mode"] = "absolute"
                save_config(self.config)
                self.bot.reload_config()
                self.show_toast(f"{config_key.title()} threshold saved", "success")
            except ValueError:
                self.show_toast("Invalid value", "error")

        def on_mode_change(selected: str):
            cfg = self.config.get(config_key, {})
            if selected == "%":
                var.set(str(cfg.get("threshold_percent", 50)))
            else:
                var.set(str(cfg.get("threshold_absolute", 500)))
            save()

        mode_toggle = ctk.CTkSegmentedButton(
            row, values=["Value", "%"], width=110, height=32, font=("Segoe UI", 11),
            variable=mode_var, command=on_mode_change,
            selected_color=color, selected_hover_color=color
        )
        mode_toggle.pack(side="right", padx=(0, 8))

        entry.bind("<Return>", save)
        entry.bind("<FocusOut>", save)

    def on_mode_change(self, selected: str):
        """Handle detection mode change."""
        if self.bot.is_running():
            self.show_toast("Stop the bot first to change mode", "error")
            # Reset to current mode - map internal mode to display name
            mode_map = {"memory": "Memory", "structure": "Structure", "ocr": "OCR"}
            current = mode_map.get(self.bot.detection_mode, "Memory")
            self.mode_var.set(current)
            return

        mode = selected.lower()
        self.bot.set_detection_mode(mode)
        self.config = load_config()  # Reload to stay in sync

        if mode == "memory":
            self.subtitle_label.configure(text="Memory mode • Pointer Chains")
            self.show_toast("Switched to Memory mode", "success")
        elif mode == "structure":
            self.subtitle_label.configure(text="Structure mode • Auto-Updates")
            self.show_toast("Switched to Structure mode (recommended)", "success")
        else:
            self.subtitle_label.configure(text="OCR mode • Screen Capture")
            self.show_toast("Switched to OCR mode (fallback)", "info")

    def toggle_bot(self):
        """Toggle bot on/off."""
        if self.bot.is_running():
            self.bot.stop()
            self._sync_bot_buttons(False)
            self.status_label.configure(text="● Stopped", text_color=self.colors["danger"])
            self.show_toast("Bot stopped", "info")
        else:
            self.bot.start()
            self._sync_bot_buttons(True)
            self.status_label.configure(text="● Running", text_color=self.colors["success"])
            mode = self.bot.detection_mode.upper()
            self.show_toast(f"Bot started ({mode} mode)", "success")

    def toggle_atlas_fog(self):
        """Toggle atlas fog reveal on/off."""
        success, message = self.atlas_fog.toggle()

        if success:
            if self.atlas_fog.is_enabled:
                self.atlas_fog_status.configure(text="ON", text_color=self.colors["success"])
                self.show_toast("🌐 Atlas fog removed!", "success")
            else:
                self.atlas_fog_status.configure(text="OFF", text_color=self.colors["danger"])
                self.show_toast("🌐 Atlas fog restored", "info")
        else:
            self.show_toast(f"Atlas fog failed: {message}", "error")

    def _on_shader_threshold_change(self, value):
        """Update the floor value label as the shader-reveal slider moves."""
        self.shader_threshold_label.configure(text=f"{float(value):.2f}")

    def _set_shader_controls_enabled(self, enabled: bool):
        """Enable/disable the shader-reveal buttons during a bundle write."""
        state = "normal" if enabled else "disabled"
        self.shader_apply_btn.configure(state=state)
        self.shader_remove_btn.configure(state=state)

    def _refresh_shader_status(self):
        """Read the current on-disk shader patch state in the background."""
        import threading

        def worker():
            try:
                import map_shader_patch as sp
                status = sp.get_status()
                self.root.after(0, lambda: self._show_shader_status_result(status, None))
            except Exception as e:
                self.root.after(0, lambda e=e: self._show_shader_status_result(None, e))

        threading.Thread(target=worker, daemon=True).start()

    def _show_shader_status_result(self, status, error):
        """Update the shader status label (runs on the UI thread)."""
        if error is not None:
            # Usually means POE2 is running (bundle locked) or not found.
            self.shader_reveal_status.configure(text="?", text_color=self.colors["text_secondary"])
            return
        if status.get("patched"):
            thr = status.get("threshold")
            self.shader_reveal_status.configure(
                text=f"ON ({thr:.2f})" if isinstance(thr, float) else "ON",
                text_color=self.colors["success"])
            if isinstance(thr, float):
                self.shader_threshold_var.set(thr)
                self.shader_threshold_label.configure(text=f"{thr:.2f}")
        else:
            self.shader_reveal_status.configure(text="OFF", text_color=self.colors["danger"])

    def apply_shader_reveal(self):
        """Apply the on-disk shader visibility floor (POE2 must be closed)."""
        import threading
        threshold = round(float(self.shader_threshold_var.get()), 2)
        self._set_shader_controls_enabled(False)
        self.shader_reveal_status.configure(text="working…",
                                            text_color=self.colors["text_secondary"])

        def worker():
            try:
                import map_shader_patch as sp
                result = sp.apply_patch(threshold=threshold)
                self.root.after(0, lambda: self._shader_action_done(result, None, "apply"))
            except Exception as e:
                self.root.after(0, lambda e=e: self._shader_action_done(None, e, "apply"))

        threading.Thread(target=worker, daemon=True).start()

    def remove_shader_reveal(self):
        """Remove the on-disk shader visibility floor (POE2 must be closed)."""
        import threading
        self._set_shader_controls_enabled(False)
        self.shader_reveal_status.configure(text="working…",
                                            text_color=self.colors["text_secondary"])

        def worker():
            try:
                import map_shader_patch as sp
                result = sp.remove_patch()
                self.root.after(0, lambda: self._shader_action_done(result, None, "remove"))
            except Exception as e:
                self.root.after(0, lambda e=e: self._shader_action_done(None, e, "remove"))

        threading.Thread(target=worker, daemon=True).start()

    def _shader_action_done(self, result, error, action):
        """Handle completion of an apply/remove shader op (runs on UI thread)."""
        self._set_shader_controls_enabled(True)
        if error is not None:
            import map_shader_patch as sp
            if isinstance(error, sp.GameRunningError):
                self.show_toast("Close Path of Exile 2 first, then retry", "error")
            else:
                self.show_toast(f"Shader patch failed: {error}", "error")
            self._refresh_shader_status()
            return
        status = result.get("status")
        if action == "apply":
            thr = result.get("threshold")
            if status == "already_patched":
                self.show_toast(f"🔦 Already applied at {thr:.2f}", "info")
            else:
                self.show_toast(f"🔦 Applied (floor {thr:.2f}) · relaunch POE2", "success")
        else:  # remove
            if status == "not_patched":
                self.show_toast("🔦 Shader was not patched", "info")
            else:
                self.show_toast("🔦 Removed · relaunch POE2", "success")
        self._refresh_shader_status()

    def toggle_terrain_overlay(self):
        """Toggle terrain overlay on/off (read-only map display)."""
        if not OVERLAY_AVAILABLE:
            self.show_toast("Entity overlay requires PyQt5", "error")
            return

        if not self.terrain_overlay:
            self.show_toast("Terrain overlay not initialized", "error")
            return

        try:
            # Update overlay config with current colors, opacity and entity settings before starting
            from map_overlay import OverlayConfig, hex_to_rgba
            overlay_cfg = self.config.get("overlay", {})

            interior_hex = overlay_cfg.get("interior_color", "#506482")
            edge_hex = overlay_cfg.get("edge_color", "#3CDCFF")
            player_hex = overlay_cfg.get("player_color", "#4DF2FF")

            # Get opacity values (0-100%) and convert to alpha (0-255)
            interior_opacity = int(overlay_cfg.get("interior_opacity", 12) * 255 / 100)
            edge_opacity = int(overlay_cfg.get("edge_opacity", 70) * 255 / 100)
            player_opacity = int(overlay_cfg.get("player_opacity", 100) * 255 / 100)

            # Entity colors
            monster_normal_hex = overlay_cfg.get("monster_normal_color", "#FF0000")
            monster_magic_hex = overlay_cfg.get("monster_magic_color", "#0000FF")
            monster_rare_hex = overlay_cfg.get("monster_rare_color", "#FFFF00")
            monster_unique_hex = overlay_cfg.get("monster_unique_color", "#FFA500")
            npc_hex = overlay_cfg.get("npc_color", "#00FF00")
            chest_hex = overlay_cfg.get("chest_color", "#FFD700")
            transition_hex = overlay_cfg.get("transition_color", "#00FFFF")

            config = OverlayConfig(
                interior_color=hex_to_rgba(interior_hex, alpha=interior_opacity),
                edge_color=hex_to_rgba(edge_hex, alpha=edge_opacity),
                player_color=hex_to_rgba(player_hex, alpha=player_opacity),
                # Entity colors
                monster_normal_color=hex_to_rgba(monster_normal_hex, alpha=200),
                monster_magic_color=hex_to_rgba(monster_magic_hex, alpha=220),
                monster_rare_color=hex_to_rgba(monster_rare_hex, alpha=220),
                monster_unique_color=hex_to_rgba(monster_unique_hex, alpha=255),
                npc_color=hex_to_rgba(npc_hex, alpha=200),
                chest_color=hex_to_rgba(chest_hex, alpha=200),
                transition_color=hex_to_rgba(transition_hex, alpha=200),
                # Entity visibility
                show_entities=overlay_cfg.get("show_entities", True),
                show_monsters=overlay_cfg.get("show_monsters", True),
                show_npcs=overlay_cfg.get("show_npcs", True),
                show_chests=overlay_cfg.get("show_chests", True),
                show_transitions=overlay_cfg.get("show_transitions", True),
                # Monster rarity filters
                show_normal_monsters=overlay_cfg.get("show_normal_monsters", True),
                show_magic_monsters=overlay_cfg.get("show_magic_monsters", True),
                show_rare_monsters=overlay_cfg.get("show_rare_monsters", True),
                show_unique_monsters=overlay_cfg.get("show_unique_monsters", True),
                # Entity-icons-only mode: terrain layer disabled (shader reveal handles the map)
                show_terrain=False,
            )
            print("[TOGGLE] Updating config...")
            self.terrain_overlay.update_config(config)

            print("[TOGGLE] Calling toggle()...")
            import time
            t0 = time.time()
            is_running = self.terrain_overlay.toggle()
            t1 = time.time()
            print(f"[TOGGLE] toggle() took {(t1-t0)*1000:.0f}ms, is_running={is_running}")

            if is_running:
                self.terrain_status.configure(text="ON", text_color=self.colors["success"])
                self.show_toast("🗺️ Entity overlay enabled!", "success")
            else:
                self.terrain_status.configure(text="OFF", text_color=self.colors["danger"])
                self.show_toast("🗺️ Entity overlay disabled", "info")
        except Exception as e:
            self.show_toast(f"Overlay error: {str(e)}", "error")

    def on_bot_update(self, value_type: str, current: Optional[int], maximum: Optional[int]):
        """Callback when bot updates values.

        current/maximum are None when detection is lost (game closed, character
        swap, loading) - in that case we show a placeholder instead of stale
        numbers.
        """
        text = "-- / --" if current is None or maximum is None else f"{current} / {maximum}"
        if value_type == "life":
            self.root.after(0, lambda: self.life_label.configure(text=text))
        elif value_type == "mana":
            self.root.after(0, lambda: self.mana_label.configure(text=text))

    def show_toast(self, message: str, notification_type: str = "info"):
        """Show toast notification (replaces any existing toast)."""
        # Dismiss all existing toasts first
        for toast in ToastNotification.active_toasts[:]:
            toast.dismiss()

        toast = ToastNotification(self.root, message, duration=3000,
                                  notification_type=notification_type, position="top-right")
        toast.show()

    def on_closing(self):
        """Handle window close."""
        if self.bot.is_running():
            result = ask_yes_no(self.root, "Quit", "The bot is still running. Stop and quit?", colors=self.colors)
            if not result:
                return
            self.bot.stop()

        # Shutdown terrain overlay (terminates the persistent process)
        if self.terrain_overlay:
            self.terrain_overlay.shutdown()

        # Clean up hotkeys
        self._unregister_hotkeys()
        self.root.destroy()

    def run(self):
        """Run the GUI main loop."""
        # Check for updates on startup (silent, non-blocking)
        if self.config.get("auto_check_updates", True):
            self.root.after(3000, self._silent_update_check)
        self.root.mainloop()

    def _silent_update_check(self):
        """Silently check for updates on startup."""
        def _check_thread():
            release = check_for_updates()
            if release:
                self.root.after(0, lambda: self._notify_update_available(release))

        threading.Thread(target=_check_thread, daemon=True).start()

    def _notify_update_available(self, release):
        """Show toast notification that update is available."""
        self.show_toast(f"Update v{release.version} available! Check Settings → About", "info")


def main():
    """Main entry point."""
    app = SentinelGUI()
    app.run()


if __name__ == "__main__":
    # Required for multiprocessing on Windows (terrain overlay runs in separate process)
    import multiprocessing
    multiprocessing.freeze_support()

    main()
