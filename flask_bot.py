"""
Flask Bot Module - POE2 auto flask bot with Memory and OCR support.
Uses pointer chains to read HP/Mana directly from game memory.
Falls back to OCR-based detection if memory reading fails.
Includes map reveal functionality via AOB pattern scanning.
"""

import keyboard
import time
import threading
import json
import os
import sys
import logging
import struct
import pymem
import pymem.process
import ctypes
from collections import deque
from ctypes import wintypes
from typing import Optional, Callable, Dict, Any, List, Tuple

# OCR imports (optional - graceful fallback if not available)
try:
    import mss
    import pytesseract
    from PIL import Image, ImageOps
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

logger = logging.getLogger(__name__)
_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS if OCR_AVAILABLE else None

# Windows API constants for memory operations
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_ALL_ACCESS = 0x1F0FFF


def _get_base_dir() -> str:
    """Return the directory for config files."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(_get_base_dir(), "config.json")

DEFAULT_CONFIG = {
    "game_version": "steam",
    "detection_mode": "structure",  # "structure" (recommended), "memory", or "ocr"
    "life": {
        "threshold_mode": "percent",
        "threshold_percent": 70.0,
        "threshold_absolute": 500,
        "flask_key": "1",
        "confirmations_required": 2,
        "cooldown": 2.0,
        "pool_type": "hp",  # "hp", "es" (for ES builds), or "combined" (HP+ES)
        "region": {"top": 1089, "left": 151, "width": 119, "height": 27},  # OCR region
    },
    "mana": {
        "threshold_mode": "percent",
        "threshold_percent": 50.0,
        "threshold_absolute": 300,
        "flask_key": "2",
        "confirmations_required": 2,
        "cooldown": 2.0,
        "region": {"top": 1054, "left": 2397, "width": 83, "height": 34},  # OCR region
    },
    "poll_interval": 0.1,
}


def load_config() -> dict:
    """Load configuration from file."""
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    """Save configuration to file."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


class MemoryReader:
    """Reads HP/Mana from POE2 memory using pointer chains."""

    PROCESS_NAMES = {
        "steam": "PathOfExileSteam.exe",
        "standalone": "PathOfExile.exe",
        "epic": "PathOfExile.exe",
    }

    # Default pointer chains (Patch 0.5.1) - can be overridden via config
    DEFAULT_OFFSETS = {
        "current_hp_base": 0x042A01C8,
        "current_hp_chain": [0x0, 0x28, 0x70, 0x78, 0x504],
        "max_hp_base": 0x0443E9E8,
        "max_hp_chain": [0x30, 0x10, 0x0, 0x10, 0x0, 0x20, 0x2E8],
        "current_mp_base": 0x0443E9E8,
        "current_mp_chain": [0x38, 0x8, 0x10, 0x20, 0x504],
        "max_mp_base": 0x0443E9E8,
        "max_mp_chain": [0x38, 0x10, 0x20, 0x28, 0x3C8],
    }

    def __init__(self, game_version: str = "steam", config: Optional[dict] = None):
        self.game_version = game_version
        self.pm: Optional[pymem.Pymem] = None
        self.base_address: int = 0
        self.connected = False

        # Load offsets from config or use defaults
        self._load_offsets(config)

    def _load_offsets(self, config: Optional[dict]) -> None:
        """Load memory offsets from config or use defaults."""
        offsets = {}
        if config:
            offsets = config.get("memory_offsets", {})

        def parse_hex(value):
            """Parse hex string or return int as-is."""
            if isinstance(value, str):
                return int(value, 16)
            return value

        def parse_chain(chain):
            """Parse a chain of hex strings or ints."""
            if not chain:
                return []
            return [parse_hex(v) for v in chain]

        # Load each offset with fallback to defaults
        self.current_hp_base = parse_hex(offsets.get("current_hp_base", self.DEFAULT_OFFSETS["current_hp_base"]))
        self.current_hp_chain = parse_chain(offsets.get("current_hp_chain", self.DEFAULT_OFFSETS["current_hp_chain"]))
        self.max_hp_base = parse_hex(offsets.get("max_hp_base", self.DEFAULT_OFFSETS["max_hp_base"]))
        self.max_hp_chain = parse_chain(offsets.get("max_hp_chain", self.DEFAULT_OFFSETS["max_hp_chain"]))
        self.current_mp_base = parse_hex(offsets.get("current_mp_base", self.DEFAULT_OFFSETS["current_mp_base"]))
        self.current_mp_chain = parse_chain(offsets.get("current_mp_chain", self.DEFAULT_OFFSETS["current_mp_chain"]))
        self.max_mp_base = parse_hex(offsets.get("max_mp_base", self.DEFAULT_OFFSETS["max_mp_base"]))
        self.max_mp_chain = parse_chain(offsets.get("max_mp_chain", self.DEFAULT_OFFSETS["max_mp_chain"]))

        logger.debug(f"Loaded memory offsets - HP base: {hex(self.current_hp_base)}, MP base: {hex(self.current_mp_base)}")

    def reload_offsets(self, config: dict) -> None:
        """Reload memory offsets from updated config."""
        self._load_offsets(config)
        # Force reconnection to apply new offsets
        self.connected = False
        logger.info("Memory offsets reloaded, will reconnect on next read")

    def connect(self) -> bool:
        """Connect to the game process."""
        process_name = self.PROCESS_NAMES.get(self.game_version, "PathOfExileSteam.exe")
        try:
            self.pm = pymem.Pymem(process_name)
            self.base_address = self.pm.base_address
            self.connected = True
            logger.info(f"Connected to {process_name} at {hex(self.base_address)}")
            return True
        except Exception as e:
            logger.debug(f"Failed to connect: {e}")
            self.connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from the game process."""
        if self.pm:
            try:
                self.pm.close_process()
            except:
                pass
        self.pm = None
        self.connected = False

    def _read_pointer_chain(self, base: int, offsets: List[int]) -> Optional[int]:
        """Follow a chain of pointers to reach the final address.

        For each offset: read pointer at current address, then add offset.
        This matches how Cheat Engine pointer chains work.
        """
        if not self.pm:
            return None
        try:
            address = base
            for offset in offsets:
                # Read the pointer (64-bit) at current address
                ptr = self.pm.read_longlong(address)
                if ptr == 0:
                    return None
                # Add the offset to get next address
                address = ptr + offset
            return address
        except:
            return None

    def _read_value(self, base_offset: int, chain: List[int]) -> int:
        """Read an integer value from a pointer chain."""
        try:
            base = self.base_address + base_offset
            addr = self._read_pointer_chain(base, chain)
            if addr:
                return self.pm.read_int(addr)
        except:
            pass
        return 0

    def read_stats(self) -> Optional[Dict[str, int]]:
        """Read current HP and Mana stats."""
        if not self.connected:
            if not self.connect():
                return None

        try:
            hp_current = self._read_value(self.current_hp_base, self.current_hp_chain)
            hp_max = self._read_value(self.max_hp_base, self.max_hp_chain)
            mp_current = self._read_value(self.current_mp_base, self.current_mp_chain)
            mp_max = self._read_value(self.max_mp_base, self.max_mp_chain)

            # Sanity check
            if not (0 < hp_current < 50000 and 0 < hp_max < 50000):
                self.connected = False
                return None

            return {
                "hp_current": hp_current,
                "hp_max": hp_max,
                "mp_current": mp_current,
                "mp_max": mp_max,
            }
        except Exception as e:
            logger.debug(f"Read failed: {e}")
            self.connected = False
            return None


class StructureReader:
    """Reads HP/Mana/ES from POE2 using structure-based memory reading.

    This approach uses AOB pattern scanning to find the GameState, then walks
    the game's internal structures to find the player entity and Life component.

    Benefits over pointer chains:
    - More stable across patches (only struct offsets may change, not full chains)
    - Works for all characters regardless of HP/ES configuration
    - Same approach used by POE2Radar and other tools

    The only offsets that may need updating per patch:
    - Life.HEALTH, Life.MANA, Life.ENERGY_SHIELD (component offsets)
    - VitalStruct.MAX, VitalStruct.CURRENT (struct layout)
    """

    PROCESS_NAMES = {
        "steam": "PathOfExileSteam.exe",
        "standalone": "PathOfExile.exe",
        "epic": "PathOfExile.exe",
    }

    # AOB pattern for finding GameStates
    GAME_STATES_PATTERN = [
        0x48, 0x83, 0xEC, None,           # sub rsp, ??
        0x48, 0x8B, 0xF1,                 # mov rsi, rcx
        0x33, 0xED,                       # xor ebp, ebp
        0x48, 0x39, 0x2D, None, None, None, None  # cmp [rip+rel32], rbp
    ]
    GAME_STATES_DISP_OFFSET = 12
    GAME_STATES_INSTR_LEN = 16

    # Structure offsets - these are the only things that may change per patch
    # Last validated: 2026-09-03
    class Offsets:
        # GameState
        CURRENT_STATE_PTR = 0x08
        STATES = 0x48
        STATE_SLOT_STRIDE = 0x10
        STATE_SLOT_COUNT = 12

        # InGameState
        AREA_INSTANCE_DATA = 0x290

        # AreaInstance
        LOCAL_PLAYER = 0x5A0

        # Entity
        ENTITY_DETAILS_PTR = 0x08
        COMPONENT_LIST = 0x10

        # EntityDetails
        COMPONENT_LOOKUP_PTR = 0x28

        # ComponentLookUp
        NAME_AND_INDEX_BUCKET = 0x28
        ENTRY_STRIDE = 0x10

        # Life component - VitalStruct locations
        # 2026-09-03 patch shifted the whole Life struct by -0x50 (header shrank).
        HEALTH = 0x160          # was 0x1B0
        MANA = 0x1B8            # was 0x208
        ENERGY_SHIELD = 0x1F8   # was 0x248

        # VitalStruct layout (unchanged this patch)
        VITAL_MAX = 0x2C
        VITAL_CURRENT = 0x30

    def __init__(self, game_version: str = "steam", config: Optional[dict] = None):
        self.game_version = game_version
        self.pm: Optional[pymem.Pymem] = None
        self.base_address: int = 0
        self.connected = False

        # Cached addresses
        self._game_state_slot: Optional[int] = None
        self._life_component_cache: Optional[int] = None
        self._cached_player_ptr: Optional[int] = None  # Track player ptr for zone change detection

        # Load custom offsets from config if provided
        self._load_offsets(config)

    def _load_offsets(self, config: Optional[dict]) -> None:
        """Load structure offsets from config (allows patching without code changes)."""
        if not config:
            return

        offsets = config.get("structure_offsets", {})
        if not offsets:
            return

        def parse_hex(value):
            if isinstance(value, str):
                return int(value, 16)
            return value

        # Override offsets if provided in config
        if "life_health" in offsets:
            self.Offsets.HEALTH = parse_hex(offsets["life_health"])
        if "life_mana" in offsets:
            self.Offsets.MANA = parse_hex(offsets["life_mana"])
        if "life_energy_shield" in offsets:
            self.Offsets.ENERGY_SHIELD = parse_hex(offsets["life_energy_shield"])
        if "vital_max" in offsets:
            self.Offsets.VITAL_MAX = parse_hex(offsets["vital_max"])
        if "vital_current" in offsets:
            self.Offsets.VITAL_CURRENT = parse_hex(offsets["vital_current"])
        if "area_instance_data" in offsets:
            self.Offsets.AREA_INSTANCE_DATA = parse_hex(offsets["area_instance_data"])
        if "local_player" in offsets:
            self.Offsets.LOCAL_PLAYER = parse_hex(offsets["local_player"])

        logger.debug(f"Structure offsets loaded - Life.HEALTH: 0x{self.Offsets.HEALTH:X}")

    def connect(self) -> bool:
        """Connect to the game process."""
        process_name = self.PROCESS_NAMES.get(self.game_version, "PathOfExileSteam.exe")
        try:
            self.pm = pymem.Pymem(process_name)
            self.base_address = self.pm.base_address
            self.connected = True
            self._game_state_slot = None  # Reset AOB cache on reconnect
            self._life_component_cache = None
            self._cached_player_ptr = None  # Reset player cache
            logger.info(f"StructureReader connected to {process_name} at 0x{self.base_address:X}")
            return True
        except Exception as e:
            logger.debug(f"StructureReader failed to connect: {e}")
            self.connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from the game process."""
        if self.pm:
            try:
                self.pm.close_process()
            except:
                pass
        self.pm = None
        self.connected = False
        self._game_state_slot = None
        self._life_component_cache = None

    def _is_process_alive(self) -> bool:
        """Return True if the attached game process is still running.

        When the game is closed and reopened, our cached handle points at the
        now-dead process and every read fails. Detecting that lets read_stats
        drop the stale handle and reconnect to the new process instead of
        silently failing until the user restarts the bot.
        """
        if not self.pm:
            return False
        try:
            handle = self.pm.process_handle
            if not handle:
                return False
            STILL_ACTIVE = 259
            exit_code = wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            )
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        except Exception:
            return False

    def _read_ptr(self, address: int) -> Optional[int]:
        """Read a 64-bit pointer."""
        if not self.pm or address == 0:
            return None
        try:
            return self.pm.read_longlong(address)
        except:
            return None

    def _read_int(self, address: int) -> Optional[int]:
        """Read a 32-bit integer."""
        if not self.pm or address == 0:
            return None
        try:
            return self.pm.read_int(address)
        except:
            return None

    def _read_bytes(self, address: int, size: int) -> Optional[bytes]:
        """Read raw bytes from memory."""
        if not self.pm or address == 0 or size <= 0:
            return None
        try:
            return self.pm.read_bytes(address, size)
        except:
            return None

    def _scan_for_pattern(self, pattern: List[Optional[int]], disp_offset: int,
                          instr_len: int) -> Optional[int]:
        """Scan for AOB pattern and resolve RIP-relative address."""
        if not self.pm:
            return None

        try:
            # Get module info
            module = pymem.process.module_from_name(self.pm.process_handle,
                                                     self.PROCESS_NAMES.get(self.game_version, "PathOfExileSteam.exe"))
            if not module:
                return None

            base = module.lpBaseOfDll
            size = module.SizeOfImage

            # Read in chunks to avoid memory issues
            chunk_size = 0x100000  # 1MB chunks
            pattern_len = len(pattern)

            for offset in range(0, size, chunk_size):
                read_size = min(chunk_size + pattern_len, size - offset)
                if read_size <= 0:
                    break

                chunk_base = base + offset
                try:
                    data = self.pm.read_bytes(chunk_base, read_size)
                except:
                    continue

                # Search for pattern
                for i in range(len(data) - pattern_len + 1):
                    match = True
                    for j, pb in enumerate(pattern):
                        if pb is not None and data[i + j] != pb:
                            match = False
                            break

                    if match:
                        # Resolve RIP-relative address
                        disp_pos = i + disp_offset
                        if disp_pos + 4 > len(data):
                            continue

                        displacement = struct.unpack('<i', data[disp_pos:disp_pos + 4])[0]
                        resolved = chunk_base + i + instr_len + displacement

                        if resolved and resolved > 0x10000:
                            logger.debug(f"AOB match at 0x{chunk_base + i:X} -> 0x{resolved:X}")
                            return resolved

            return None
        except Exception as e:
            logger.error(f"AOB scan error: {e}")
            return None

    def _find_game_state_slot(self) -> Optional[int]:
        """Find GameState slot via AOB pattern scan (cached)."""
        if self._game_state_slot:
            return self._game_state_slot

        logger.info("Scanning for GameState AOB pattern...")
        slot = self._scan_for_pattern(
            self.GAME_STATES_PATTERN,
            self.GAME_STATES_DISP_OFFSET,
            self.GAME_STATES_INSTR_LEN
        )

        if slot:
            self._game_state_slot = slot
            logger.info(f"Found GameState slot at 0x{slot:X}")
        else:
            logger.warning("GameState AOB pattern not found")

        return slot

    def _find_local_player(self) -> Optional[int]:
        """Find the local player entity address.

        Chain: GameStateSlot -> GameState -> InGameState -> AreaInstance -> LocalPlayer
        """
        slot = self._find_game_state_slot()
        if not slot:
            return None

        # Dereference slot to get GameState
        game_state = self._read_ptr(slot)
        if not game_state or game_state < 0x10000:
            return None

        # Try to find valid InGameState
        candidates = []

        # Method 1: CurrentStatePtr StdVector
        vec_first = self._read_ptr(game_state + self.Offsets.CURRENT_STATE_PTR)
        if vec_first and vec_first > 0x10000:
            igs_ptr = self._read_ptr(vec_first)
            if igs_ptr and igs_ptr > 0x10000:
                candidates.append(igs_ptr)

        # Method 2: States array fallback
        for i in range(self.Offsets.STATE_SLOT_COUNT):
            slot_addr = game_state + self.Offsets.STATES + (i * self.Offsets.STATE_SLOT_STRIDE)
            igs_ptr = self._read_ptr(slot_addr)
            if igs_ptr and igs_ptr > 0x10000 and igs_ptr not in candidates:
                candidates.append(igs_ptr)

        # Validate candidates and find LocalPlayer
        for igs in candidates:
            area_instance = self._read_ptr(igs + self.Offsets.AREA_INSTANCE_DATA)
            if not area_instance or area_instance < 0x10000:
                continue

            local_player = self._read_ptr(area_instance + self.Offsets.LOCAL_PLAYER)
            if local_player and local_player > 0x10000:
                return local_player

        return None

    def _read_utf8_string(self, addr: int, max_length: int = 32) -> str:
        """Read a null-terminated UTF-8 string."""
        if not addr:
            return ""
        data = self._read_bytes(addr, max_length)
        if not data:
            return ""
        try:
            return data.split(b'\x00')[0].decode('utf-8')
        except:
            return ""

    def _resolve_life_component(self, player_ptr: int) -> Optional[int]:
        """Resolve the Life component, tolerating a player->entity indirection.

        Patch 0.5.4 inserted one extra level of indirection: the entity that
        owns the components is now read_ptr(player_ptr) rather than the player
        pointer itself. We try the pointer directly first (pre-0.5.4 layout)
        and then one dereference deeper, returning whichever yields a valid
        Life component. This keeps the bot working across both layouts without
        hardcoding a patch-specific offset.

        If the name-bucket chain itself shifts (as in the 2026-09-03 patch,
        which also reorganised EntityDetails/ComponentLookup and added another
        player->entity indirection), we fall back to a signature-based search
        that locates the Life component by its VitalStruct layout.
        """
        mod = self._module_bounds()
        for entity_ptr in (player_ptr, self._read_ptr(player_ptr)):
            if not entity_ptr or entity_ptr < 0x10000:
                continue
            life = self._resolve_life_for_entity(entity_ptr)
            if life and self._looks_like_life(life, mod):
                return life

        # Fallback: chain offsets shifted this patch; find Life by signature.
        return self._find_life_by_signature(player_ptr, mod)

    def _resolve_life_for_entity(self, entity_ptr: int) -> Optional[int]:
        """Resolve the Life component address for a specific entity pointer."""
        details = self._read_ptr(entity_ptr + self.Offsets.ENTITY_DETAILS_PTR)
        if not details:
            return None

        lookup = self._read_ptr(details + self.Offsets.COMPONENT_LOOKUP_PTR)
        if not lookup:
            return None

        # Read component list
        comp_list_begin = self._read_ptr(entity_ptr + self.Offsets.COMPONENT_LIST)
        comp_list_end = self._read_ptr(entity_ptr + self.Offsets.COMPONENT_LIST + 8)
        if not comp_list_begin or not comp_list_end or comp_list_end <= comp_list_begin:
            return None

        comp_count = (comp_list_end - comp_list_begin) // 8
        if comp_count <= 0 or comp_count > 256:
            return None

        # Read bucket
        bucket_begin = self._read_ptr(lookup + self.Offsets.NAME_AND_INDEX_BUCKET)
        bucket_end = self._read_ptr(lookup + self.Offsets.NAME_AND_INDEX_BUCKET + 8)
        if not bucket_begin or not bucket_end or bucket_end <= bucket_begin:
            return None

        num_entries = (bucket_end - bucket_begin) // self.Offsets.ENTRY_STRIDE
        if num_entries <= 0 or num_entries > 256:
            return None

        # Search for "Life" component
        for i in range(num_entries):
            entry_addr = bucket_begin + i * self.Offsets.ENTRY_STRIDE
            name_ptr = self._read_ptr(entry_addr)
            if not name_ptr:
                continue

            name = self._read_utf8_string(name_ptr)
            if name == "Life":
                index = self._read_int(entry_addr + 8)
                if index is not None and 0 <= index < comp_count:
                    return self._read_ptr(comp_list_begin + index * 8)

        return None

    def _module_bounds(self) -> Optional[Tuple[int, int]]:
        """Return (base, base+size) of the game module, or None."""
        if not self.pm:
            return None
        try:
            name = self.PROCESS_NAMES.get(self.game_version, "PathOfExileSteam.exe")
            module = pymem.process.module_from_name(self.pm.process_handle, name)
            if not module:
                return None
            return (module.lpBaseOfDll, module.lpBaseOfDll + module.SizeOfImage)
        except Exception:
            return None

    def _looks_like_life(self, comp_ptr: Optional[int],
                         mod: Optional[Tuple[int, int]]) -> bool:
        """Validate a Life component by its VitalStruct signature.

        A real Life component has a module vtable at offset 0 and three
        internally-consistent VitalStructs (HP/Mana/ES) where the max is sane
        and 0 <= current <= max. This lets us verify a resolved pointer without
        trusting the (patch-fragile) name-bucket index mapping.
        """
        if not comp_ptr or comp_ptr < 0x10000:
            return False
        need = self.Offsets.ENERGY_SHIELD + self.Offsets.VITAL_CURRENT + 4
        data = self._read_bytes(comp_ptr, max(need, 0x18))
        return self._block_is_life(data, mod)

    def _block_is_life(self, data: Optional[bytes],
                       mod: Optional[Tuple[int, int]]) -> bool:
        """Byte-level Life-component check (see _looks_like_life)."""
        need = self.Offsets.ENERGY_SHIELD + self.Offsets.VITAL_CURRENT + 4
        if not data or len(data) < need:
            return False
        if mod:
            vtable = struct.unpack_from('<Q', data, 0)[0]
            if not (mod[0] <= vtable < mod[1]):
                return False

        def vital(base: int) -> Tuple[int, int]:
            mx = struct.unpack_from('<i', data, base + self.Offsets.VITAL_MAX)[0]
            cur = struct.unpack_from('<i', data, base + self.Offsets.VITAL_CURRENT)[0]
            return cur, mx

        hp_cur, hp_max = vital(self.Offsets.HEALTH)
        mp_cur, mp_max = vital(self.Offsets.MANA)
        es_cur, es_max = vital(self.Offsets.ENERGY_SHIELD)

        # A living player: HP present with current >= 1, Mana/ES consistent,
        # and at least one secondary resource (mana or ES) present. The last
        # check rejects mostly-zero structs that only coincidentally carry a
        # pointer high-dword (e.g. 0x559 == 1369) in the HP-max slot.
        if not (1 <= hp_max <= 50000 and 1 <= hp_cur <= hp_max):
            return False
        if not (0 <= mp_max <= 50000 and 0 <= mp_cur <= mp_max):
            return False
        if not (0 <= es_max <= 200000 and 0 <= es_cur <= es_max):
            return False
        if mp_max <= 0 and es_max <= 0:
            return False
        return True

    def _find_life_by_signature(self, player_ptr: int,
                                mod: Optional[Tuple[int, int]]) -> Optional[int]:
        """Find the Life component via a bounded, breadth-first pointer walk.

        Patch-resilient fallback: from the player entity we walk the pointer
        graph and return the shallowest reachable component that passes
        _looks_like_life. Because the local-player pointer only reaches the
        player's own object graph, the shallowest valid Life is the player's.
        Reads one block per node (children + vitals) to keep it fast; the
        result is cached by the caller.
        """
        if not self.pm or not player_ptr or player_ptr < 0x10000:
            return None

        WINDOW = 0x400          # bytes scanned per node for child pointers
        MAX_DEPTH = 7
        MAX_VISITED = 60000
        need = self.Offsets.ENERGY_SHIELD + self.Offsets.VITAL_CURRENT + 4
        block_size = max(WINDOW, need)

        seen = {player_ptr}
        queue = deque([(player_ptr, 0)])
        while queue and len(seen) < MAX_VISITED:
            node, depth = queue.popleft()
            block = self._read_bytes(node, block_size)
            if not block:
                continue
            # Is this node itself a Life component?
            if self._block_is_life(block, mod):
                return node
            if depth >= MAX_DEPTH:
                continue
            limit = min(len(block), WINDOW) - 8
            for off in range(0, limit + 1, 8):
                child = struct.unpack_from('<Q', block, off)[0]
                if child in seen or not (0x10000 < child < 0x7FFFFFFFFFFF):
                    continue
                seen.add(child)
                queue.append((child, depth + 1))
        return None

    def _read_vital_struct(self, life_component: int, vital_offset: int) -> Tuple[int, int]:
        """Read current and max values from a VitalStruct."""
        data = self._read_bytes(life_component + vital_offset, 52)
        if not data or len(data) < 52:
            return (0, 0)

        max_val = struct.unpack_from('<i', data, self.Offsets.VITAL_MAX)[0]
        cur_val = struct.unpack_from('<i', data, self.Offsets.VITAL_CURRENT)[0]

        return (cur_val, max_val)

    def read_stats(self) -> Optional[Dict[str, int]]:
        """Read current HP, ES, and Mana stats using structure-based approach."""
        # If the game was closed and reopened, our handle now points at a dead
        # process. Drop it so we reconnect to the new instance instead of
        # reading a stale handle/GameState slot forever.
        if self.connected and not self._is_process_alive():
            logger.info("Game process no longer alive; reconnecting to new instance...")
            self.disconnect()

        if not self.connected:
            if not self.connect():
                return None

        try:
            # Find local player
            player = self._find_local_player()
            if not player:
                logger.debug("Could not find local player")
                return None

            # Zone change detection: if player pointer changed, invalidate Life cache
            if self._cached_player_ptr and self._cached_player_ptr != player:
                logger.debug(f"Zone change detected (player ptr: 0x{self._cached_player_ptr:X} -> 0x{player:X})")
                self._life_component_cache = None
            self._cached_player_ptr = player

            # Resolve Life component (with caching)
            if self._life_component_cache:
                life = self._life_component_cache
                # Validate cache is still valid
                test = self._read_bytes(life, 4)
                if not test:
                    life = self._resolve_life_component(player)
                    self._life_component_cache = life
            else:
                life = self._resolve_life_component(player)
                self._life_component_cache = life

            if not life:
                logger.debug("Could not resolve Life component")
                return None

            # Read HP, ES, and Mana
            hp_cur, hp_max = self._read_vital_struct(life, self.Offsets.HEALTH)
            es_cur, es_max = self._read_vital_struct(life, self.Offsets.ENERGY_SHIELD)
            mp_cur, mp_max = self._read_vital_struct(life, self.Offsets.MANA)

            # Sanity check
            if hp_max <= 0 or hp_max > 50000:
                self._life_component_cache = None  # Invalidate cache
                self._cached_player_ptr = None  # Also reset player cache
                return None

            return {
                "hp_current": hp_cur,
                "hp_max": hp_max,
                "es_current": es_cur,
                "es_max": es_max,
                "mp_current": mp_cur,
                "mp_max": mp_max,
            }
        except Exception as e:
            logger.debug(f"StructureReader read failed: {e}")
            self._life_component_cache = None
            self._cached_player_ptr = None
            return None


def _configure_tesseract() -> None:
    """Point pytesseract at a usable Tesseract engine."""
    if not OCR_AVAILABLE:
        return

    candidates = [
        os.path.join(_get_base_dir(), "tesseract-portable", "tesseract.exe"),
        os.path.join(_get_base_dir(), "tesseract", "tesseract.exe"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]

    for path in candidates:
        if os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            tessdata = os.path.join(os.path.dirname(path), "tessdata")
            if os.path.isdir(tessdata):
                os.environ.setdefault("TESSDATA_PREFIX", tessdata)
            logger.info(f"Using Tesseract at {path}")
            return

    logger.warning("Tesseract not found; OCR mode may not work")


# Configure tesseract on module load
_configure_tesseract()


class OCRReader:
    """Reads HP/Mana from POE2 using OCR (screen capture + Tesseract).

    This is a fallback method when memory reading doesn't work (e.g., after patches).
    Requires screen regions to be configured for life and mana display.
    """

    def __init__(self, config: dict):
        self.config = config
        self.learned_life_max = 0
        self.learned_mana_max = 0

    def read_stats(self) -> Optional[Dict[str, int]]:
        """Read current HP and Mana stats via OCR."""
        if not OCR_AVAILABLE:
            logger.error("OCR not available - install pytesseract, mss, pillow")
            return None

        life_cfg = self.config.get("life", {})
        mana_cfg = self.config.get("mana", {})

        life_region = life_cfg.get("region")
        mana_region = mana_cfg.get("region")

        if not life_region or not mana_region:
            logger.error("OCR regions not configured")
            return None

        # Read life
        life_reading = self._read_resource(life_region)
        if life_reading is None:
            return None

        life_current, life_max = life_reading
        if life_max:
            self.learned_life_max = life_max
        elif self.learned_life_max:
            life_max = self.learned_life_max
        else:
            life_max = life_current  # Fallback

        # Read mana
        mana_reading = self._read_resource(mana_region)
        if mana_reading is None:
            return None

        mana_current, mana_max = mana_reading
        if mana_max:
            self.learned_mana_max = mana_max
        elif self.learned_mana_max:
            mana_max = self.learned_mana_max
        else:
            mana_max = mana_current  # Fallback

        return {
            "hp_current": life_current,
            "hp_max": life_max,
            "mp_current": mana_current,
            "mp_max": mana_max,
        }

    def _read_resource(self, region: dict) -> Optional[Tuple[int, Optional[int]]]:
        """Capture a resource region and OCR it into (current, max)."""
        try:
            with mss.mss() as sct:
                shot = sct.grab(region)

            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").convert("L")
            img = ImageOps.autocontrast(img)
            img = img.resize((img.width * 3, img.height * 3), _RESAMPLE)

            text = pytesseract.image_to_string(
                img, config="--psm 7 -c tessedit_char_whitelist=0123456789/"
            )
        except Exception as e:
            logger.debug(f"OCR capture failed: {e}")
            return None

        return self._parse_reading(text)

    @staticmethod
    def _parse_reading(text: str) -> Optional[Tuple[int, Optional[int]]]:
        """Parse OCR text into (current, max)."""
        cleaned = text.strip().replace(" ", "")
        if not cleaned:
            return None

        if "/" in cleaned:
            parts = cleaned.split("/")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                return None
            current, maximum = int(parts[0]), int(parts[1])
            if maximum <= 0 or not 0 <= current <= maximum:
                return None
            return current, maximum

        if not cleaned.isdigit():
            return None
        return int(cleaned), None

    def disconnect(self) -> None:
        """No-op for OCR reader (no connection to close)."""
        pass


class MapReveal:
    """POE2 Map Reveal using AOB pattern scanning.

    Finds and modifies a single byte in memory to reveal the minimap layout.
    """

    PROCESS_NAMES = {
        "steam": "PathOfExileSteam.exe",
        "standalone": "PathOfExile.exe",
        "epic": "PathOfExile.exe",
    }

    # AOB pattern to find the map reveal toggle location
    # Pattern: 41 80 7F 58 00 74 05
    # The byte at offset +4 (the 00) is what we modify
    SEARCH_PATTERN = bytes([0x41, 0x80, 0x7F, 0x58, 0x00, 0x74, 0x05])
    PATTERN_OFFSET = 4  # Offset from pattern start to the toggle byte

    def __init__(self, game_version: str = "steam"):
        self.game_version = game_version
        self.process_handle: Optional[int] = None
        self.pattern_address: Optional[int] = None
        self.is_enabled = False
        self._lock = threading.Lock()

        # Load kernel32 functions
        self.kernel32 = ctypes.windll.kernel32

    def _open_process(self, pid: int) -> Optional[int]:
        """Open process with read/write access."""
        handle = self.kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
            False,
            pid
        )
        return handle if handle else None

    def _close_handle(self, handle: int) -> None:
        """Close process handle."""
        if handle:
            self.kernel32.CloseHandle(handle)

    def _read_memory(self, handle: int, address: int, size: int) -> Optional[bytes]:
        """Read memory from process."""
        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t()
        success = self.kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(bytes_read)
        )
        return buffer.raw if success else None

    def _write_memory(self, handle: int, address: int, data: bytes) -> bool:
        """Write memory to process."""
        buffer = ctypes.create_string_buffer(data)
        bytes_written = ctypes.c_size_t()
        success = self.kernel32.WriteProcessMemory(
            handle,
            ctypes.c_void_p(address),
            buffer,
            len(data),
            ctypes.byref(bytes_written)
        )
        return bool(success)

    def _find_pattern(self, handle: int, pm: pymem.Pymem) -> Optional[int]:
        """Search for the AOB pattern in process memory."""
        BUFFER_SIZE = 4096

        # Get list of modules using pymem
        try:
            modules = list(pm.list_modules())
        except:
            # Fallback: just search main module
            modules = []

        # If no modules found, try the main module directly
        if not modules:
            try:
                base_address = pm.base_address
                # Estimate a reasonable size for the main module
                module_size = 0x10000000  # 256MB max search
                modules = [(base_address, module_size)]
            except:
                return None
        else:
            # Convert module objects to (base, size) tuples
            modules = [(m.lpBaseOfDll, m.SizeOfImage) for m in modules]

        for base_address, module_size in modules:
            try:
                for offset in range(0, module_size, BUFFER_SIZE):
                    bytes_to_read = min(BUFFER_SIZE, module_size - offset)
                    data = self._read_memory(handle, base_address + offset, bytes_to_read)

                    if data:
                        # Search for pattern in this chunk
                        idx = data.find(self.SEARCH_PATTERN)
                        if idx != -1:
                            found_addr = base_address + offset + idx
                            logger.info(f"Map reveal pattern found at {hex(found_addr)}")
                            return found_addr
            except Exception as e:
                continue

        return None

    def _get_process(self) -> Optional[pymem.Pymem]:
        """Get the game process."""
        process_name = self.PROCESS_NAMES.get(self.game_version, "PathOfExileSteam.exe")
        try:
            return pymem.Pymem(process_name)
        except:
            return None

    def toggle(self) -> Tuple[bool, str]:
        """Toggle map reveal on/off. Returns (success, message)."""
        with self._lock:
            process = self._get_process()
            if not process:
                return False, "Game not running"

            try:
                handle = self._open_process(process.process_id)
                if not handle:
                    return False, "Failed to open process"

                # Find pattern if we haven't already
                if self.pattern_address is None:
                    self.pattern_address = self._find_pattern(handle, process)
                    if self.pattern_address is None:
                        self._close_handle(handle)
                        return False, "Pattern not found (game version may have changed)"

                # Read current value
                toggle_address = self.pattern_address + self.PATTERN_OFFSET
                current = self._read_memory(handle, toggle_address, 1)

                if current is None:
                    self._close_handle(handle)
                    return False, "Failed to read memory"

                # Toggle the value
                current_byte = current[0]
                new_byte = 0x00 if current_byte == 0x01 else 0x01

                success = self._write_memory(handle, toggle_address, bytes([new_byte]))
                self._close_handle(handle)

                if success:
                    self.is_enabled = (new_byte == 0x01)
                    state = "enabled" if self.is_enabled else "disabled"
                    logger.info(f"Map reveal {state}")
                    return True, f"Map reveal {state}"
                else:
                    return False, "Failed to write memory"

            except Exception as e:
                logger.error(f"Map reveal toggle error: {e}")
                return False, str(e)
            finally:
                try:
                    process.close_process()
                except:
                    pass

    def enable(self) -> Tuple[bool, str]:
        """Enable map reveal."""
        if self.is_enabled:
            return True, "Already enabled"
        return self.toggle()

    def disable(self) -> Tuple[bool, str]:
        """Disable map reveal."""
        if not self.is_enabled:
            return True, "Already disabled"
        return self.toggle()

    def get_status(self) -> bool:
        """Get current map reveal status."""
        return self.is_enabled

    def reset(self) -> None:
        """Reset cached pattern address (use after game restart)."""
        self.pattern_address = None
        self.is_enabled = False


class AtlasFogReveal:
    """POE2 Atlas Fog Reveal using AOB pattern scanning.

    Removes the fog/unexplored areas on the Atlas map by NOPing the fog calculation.
    Pattern: f3 0f 59 51 ? f3 0f 58 c1 (mulss xmm2,[rcx+08] / addss xmm0,xmm1)
    """

    PROCESS_NAMES = {
        "steam": "PathOfExileSteam.exe",
        "standalone": "PathOfExile.exe",
        "epic": "PathOfExile.exe",
    }

    # AOB pattern for atlas fog - the ? is a wildcard (usually 08)
    # f3 0f 59 51 XX f3 0f 58 c1 - where XX is the wildcard byte
    # We search for partial pattern and verify the rest
    SEARCH_PATTERN_START = bytes([0xF3, 0x0F, 0x59, 0x51])  # mulss xmm2, [rcx+?]
    SEARCH_PATTERN_END = bytes([0xF3, 0x0F, 0x58, 0xC1])    # addss xmm0, xmm1

    # Original bytes to restore: f3 0f 59 51 08 f3 0f 58 c1
    ORIGINAL_BYTES = bytes([0xF3, 0x0F, 0x59, 0x51, 0x08, 0xF3, 0x0F, 0x58, 0xC1])

    # NOP replacement for first 5 bytes
    NOP_BYTES = bytes([0x90, 0x90, 0x90, 0x90, 0x90])
    BYTES_TO_PATCH = 5

    def __init__(self, game_version: str = "steam"):
        self.game_version = game_version
        self.process_handle: Optional[int] = None
        self.pattern_address: Optional[int] = None
        self.is_enabled = False
        self.original_bytes: Optional[bytes] = None  # Store original for restore
        self._lock = threading.Lock()

        # Load kernel32 functions
        self.kernel32 = ctypes.windll.kernel32

    def _open_process(self, pid: int) -> Optional[int]:
        """Open process with read/write access."""
        handle = self.kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
            False,
            pid
        )
        return handle if handle else None

    def _close_handle(self, handle: int) -> None:
        """Close process handle."""
        if handle:
            self.kernel32.CloseHandle(handle)

    def _read_memory(self, handle: int, address: int, size: int) -> Optional[bytes]:
        """Read memory from process."""
        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t()
        success = self.kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(bytes_read)
        )
        return buffer.raw if success else None

    def _write_memory(self, handle: int, address: int, data: bytes) -> bool:
        """Write memory to process."""
        buffer = ctypes.create_string_buffer(data)
        bytes_written = ctypes.c_size_t()
        success = self.kernel32.WriteProcessMemory(
            handle,
            ctypes.c_void_p(address),
            buffer,
            len(data),
            ctypes.byref(bytes_written)
        )
        return bool(success)

    def _find_pattern(self, handle: int, pm: pymem.Pymem) -> Optional[int]:
        """Search for the Atlas fog AOB pattern in process memory.

        Pattern: f3 0f 59 51 ? f3 0f 58 c1 (with wildcard at position 4)
        """
        BUFFER_SIZE = 8192  # Larger buffer for efficiency

        # Get list of modules using pymem
        try:
            modules = list(pm.list_modules())
        except:
            modules = []

        if not modules:
            try:
                base_address = pm.base_address
                module_size = 0x10000000  # 256MB max search
                modules = [(base_address, module_size)]
            except:
                return None
        else:
            modules = [(m.lpBaseOfDll, m.SizeOfImage) for m in modules]

        for base_address, module_size in modules:
            try:
                for offset in range(0, module_size, BUFFER_SIZE - 16):
                    bytes_to_read = min(BUFFER_SIZE, module_size - offset)
                    data = self._read_memory(handle, base_address + offset, bytes_to_read)

                    if data:
                        # Search for pattern with wildcard
                        # Look for: f3 0f 59 51 [?] f3 0f 58 c1
                        search_pos = 0
                        while True:
                            idx = data.find(self.SEARCH_PATTERN_START, search_pos)
                            if idx == -1 or idx + 9 > len(data):
                                break

                            # Check if the pattern_end follows at offset +5
                            if data[idx + 5:idx + 9] == self.SEARCH_PATTERN_END:
                                found_addr = base_address + offset + idx
                                logger.info(f"Atlas fog pattern found at {hex(found_addr)}")
                                return found_addr

                            search_pos = idx + 1

            except Exception as e:
                continue

        return None

    def _get_process(self) -> Optional[pymem.Pymem]:
        """Get the game process."""
        process_name = self.PROCESS_NAMES.get(self.game_version, "PathOfExileSteam.exe")
        try:
            return pymem.Pymem(process_name)
        except:
            return None

    def toggle(self) -> Tuple[bool, str]:
        """Toggle atlas fog reveal on/off. Returns (success, message)."""
        with self._lock:
            process = self._get_process()
            if not process:
                return False, "Game not running"

            try:
                handle = self._open_process(process.process_id)
                if not handle:
                    return False, "Failed to open process"

                # Find pattern if we haven't already
                if self.pattern_address is None:
                    self.pattern_address = self._find_pattern(handle, process)
                    if self.pattern_address is None:
                        self._close_handle(handle)
                        return False, "Atlas fog pattern not found"

                # Read current bytes
                current = self._read_memory(handle, self.pattern_address, self.BYTES_TO_PATCH)

                if current is None:
                    self._close_handle(handle)
                    return False, "Failed to read memory"

                # Check if currently NOPed (enabled) or original (disabled)
                if current == self.NOP_BYTES:
                    # Currently enabled (NOPed) - restore original
                    restore_bytes = self.original_bytes if self.original_bytes else self.ORIGINAL_BYTES[:5]
                    success = self._write_memory(handle, self.pattern_address, restore_bytes)
                    self._close_handle(handle)

                    if success:
                        self.is_enabled = False
                        logger.info("Atlas fog reveal disabled (restored original)")
                        return True, "Atlas fog reveal disabled"
                    else:
                        return False, "Failed to write memory"
                else:
                    # Currently disabled - save original and NOP
                    self.original_bytes = current  # Save for restore
                    success = self._write_memory(handle, self.pattern_address, self.NOP_BYTES)
                    self._close_handle(handle)

                    if success:
                        self.is_enabled = True
                        logger.info("Atlas fog reveal enabled (NOPed)")
                        return True, "Atlas fog reveal enabled"
                    else:
                        return False, "Failed to write memory"

            except Exception as e:
                logger.error(f"Atlas fog toggle error: {e}")
                return False, str(e)
            finally:
                try:
                    process.close_process()
                except:
                    pass

    def enable(self) -> Tuple[bool, str]:
        """Enable atlas fog reveal."""
        if self.is_enabled:
            return True, "Already enabled"
        return self.toggle()

    def disable(self) -> Tuple[bool, str]:
        """Disable atlas fog reveal."""
        if not self.is_enabled:
            return True, "Already disabled"
        return self.toggle()

    def get_status(self) -> bool:
        """Get current atlas fog reveal status."""
        return self.is_enabled

    def reset(self) -> None:
        """Reset cached pattern address (use after game restart)."""
        self.pattern_address = None
        self.original_bytes = None
        self.is_enabled = False


class FlaskBot:
    """POE2 Auto Flask Bot with Memory and OCR support."""

    def __init__(self, on_update: Optional[Callable[[str, Optional[int], Optional[int]], None]] = None):
        self.config = load_config()
        self.running = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.on_update = on_update
        self.detection_mode = self.config.get("detection_mode", "memory")
        self._init_reader()

        # Track last use times for cooldowns
        self.last_life_use = 0.0
        self.last_mana_use = 0.0
        self.life_low_streak = 0
        self.mana_low_streak = 0

        # Tracks whether live values are currently shown, so we only clear the
        # UI once when detection is lost (game closed, char swap, loading).
        self._values_shown = False

    def _init_reader(self) -> None:
        """Initialize the appropriate reader based on detection mode."""
        # Sync detection mode from config
        self.detection_mode = self.config.get("detection_mode", "memory")

        if self.detection_mode == "ocr":
            self.reader = OCRReader(self.config)
            logger.info("Using OCR detection mode")
        elif self.detection_mode == "structure":
            self.reader = StructureReader(self.config.get("game_version", "steam"), self.config)
            logger.info("Using Structure-based detection mode (auto-updates across patches)")
        else:
            self.reader = MemoryReader(self.config.get("game_version", "steam"), self.config)
            logger.info("Using Memory detection mode (pointer chains)")

    def reload_config(self) -> None:
        """Reload configuration from file."""
        self.config = load_config()
        # Reload memory offsets if using memory reader
        if hasattr(self, 'reader') and isinstance(self.reader, MemoryReader):
            self.reader.reload_offsets(self.config)

    def set_detection_mode(self, mode: str) -> None:
        """Change detection mode (memory/ocr/structure)."""
        if mode not in ("memory", "ocr", "structure"):
            raise ValueError("Mode must be 'memory', 'ocr', or 'structure'")
        self.detection_mode = mode
        self.config["detection_mode"] = mode
        save_config(self.config)
        self._init_reader()

    def _get_threshold(self, resource: str, current_max: int) -> float:
        """Calculate the effective threshold for a resource."""
        cfg = self.config.get(resource, {})
        mode = cfg.get("threshold_mode", "percent")

        if mode == "percent":
            percent = cfg.get("threshold_percent", 50.0)
            return (percent / 100.0) * current_max
        else:
            return float(cfg.get("threshold_absolute", 500))

    def _is_poe_active(self) -> bool:
        """Check if POE2 is the active window."""
        try:
            import win32gui
            import pygetwindow as gw
            hwnd = win32gui.GetForegroundWindow()
            if hwnd == 0:
                return False
            window_title = gw.Window(hwnd).title
            return "Path of Exile" in window_title
        except:
            return False

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        poll_interval = self.config.get("poll_interval", 0.1)

        while self.running:
            stats = self.reader.read_stats()

            if stats is None:
                # Detection lost (game closed, character swap, loading screen).
                # Clear the displayed values once so we don't show stale numbers.
                if self._values_shown and self.on_update:
                    self.on_update("life", None, None)
                    self.on_update("mana", None, None)
                    self.on_update("es", None, None)
                self._values_shown = False
                time.sleep(1.0)  # Wait longer if not connected
                continue

            hp_current = stats["hp_current"]
            hp_max = stats["hp_max"]
            mp_current = stats["mp_current"]
            mp_max = stats["mp_max"]

            # Handle ES for structure reader (ES builds use ES as effective HP)
            es_current = stats.get("es_current", 0)
            es_max = stats.get("es_max", 0)

            # Determine effective life pool based on config
            life_cfg = self.config.get("life", {})
            life_pool = life_cfg.get("pool_type", "hp")  # "hp", "es", or "combined"

            if life_pool == "es" and es_max > 0:
                # Pure ES build - use ES as life pool
                effective_hp_current = es_current
                effective_hp_max = es_max
            elif life_pool == "combined" and es_max > 0:
                # Hybrid build - combine HP + ES
                effective_hp_current = hp_current + es_current
                effective_hp_max = hp_max + es_max
            else:
                # Default: HP only
                effective_hp_current = hp_current
                effective_hp_max = hp_max

            # Update UI (always, even when tabbed out)
            if self.on_update:
                self.on_update("life", effective_hp_current, effective_hp_max)
                self.on_update("mana", mp_current, mp_max)
                # Also report ES separately if available
                if es_max > 0:
                    self.on_update("es", es_current, es_max)
            self._values_shown = True

            # Only press keys if POE2 is the active window!
            if not self._is_poe_active():
                # Reset streaks when tabbed out to avoid instant flask on tab back
                self.life_low_streak = 0
                self.mana_low_streak = 0
                time.sleep(0.5)
                continue

            now = time.time()

            # Check Life (uses effective HP which may include ES based on pool_type)
            life_threshold = self._get_threshold("life", effective_hp_max)
            life_cooldown = life_cfg.get("cooldown", 2.0)
            life_confirms = life_cfg.get("confirmations_required", 2)

            if effective_hp_current <= life_threshold:
                self.life_low_streak += 1
            else:
                self.life_low_streak = 0

            if self.life_low_streak >= life_confirms and now - self.last_life_use >= life_cooldown:
                flask_key = life_cfg.get("flask_key", "1")
                keyboard.press_and_release(flask_key)
                logger.info(f"Life flask used at {effective_hp_current}/{effective_hp_max}")
                self.last_life_use = now
                self.life_low_streak = 0

            # Check Mana
            mana_cfg = self.config.get("mana", {})
            mana_threshold = self._get_threshold("mana", mp_max)
            mana_cooldown = mana_cfg.get("cooldown", 2.0)
            mana_confirms = mana_cfg.get("confirmations_required", 2)

            if mp_current <= mana_threshold:
                self.mana_low_streak += 1
            else:
                self.mana_low_streak = 0

            if self.mana_low_streak >= mana_confirms and now - self.last_mana_use >= mana_cooldown:
                flask_key = mana_cfg.get("flask_key", "2")
                keyboard.press_and_release(flask_key)
                logger.info(f"Mana flask used at {mp_current}/{mp_max}")
                self.last_mana_use = now
                self.mana_low_streak = 0

            time.sleep(poll_interval)

    def start(self) -> None:
        """Start the flask bot."""
        if self.running:
            return

        self.reload_config()
        self._init_reader()  # Use the correct reader based on detection_mode
        self.running = True
        self.life_low_streak = 0
        self.mana_low_streak = 0
        self._values_shown = False

        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def stop(self) -> None:
        """Stop the flask bot."""
        self.running = False
        self.reader.disconnect()
        self.monitor_thread = None

    def is_running(self) -> bool:
        """Check if the bot is running."""
        return self.running
