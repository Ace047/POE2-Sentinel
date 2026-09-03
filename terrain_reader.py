"""
Terrain Reader Module - Reads POE2 map terrain data from memory.

Based on POE2Radar's reverse-engineered offsets (https://github.com/Sikaka/POE2Radar).
This is READ-ONLY - no game modification, just data extraction.

The terrain DATA is always fully loaded in memory when you enter a zone.
The game just doesn't RENDER it until you explore. We read that data directly.
"""

import pymem
import pymem.process
import struct
import logging
from typing import Optional, Tuple, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TerrainData:
    """Container for terrain grid data."""
    width: int              # Grid width in cells
    height: int             # Grid height in cells
    walkable_grid: bytes    # Raw walkable grid data (packed nibbles)
    tiles_x: int            # Number of tiles X
    tiles_y: int            # Number of tiles Y
    bytes_per_row: int      # Bytes per row in grid

    def is_walkable(self, grid_x: int, grid_y: int) -> bool:
        """Check if a grid cell is walkable.

        Grid data is packed as nibbles (4 bits per cell).
        Value 0 = wall/blocked, 1+ = walkable.
        """
        if grid_x < 0 or grid_x >= self.width or grid_y < 0 or grid_y >= self.height:
            return False

        # Calculate byte position
        byte_index = grid_y * self.bytes_per_row + (grid_x // 2)
        if byte_index >= len(self.walkable_grid):
            return False

        # Extract nibble (4 bits)
        byte_val = self.walkable_grid[byte_index]
        if grid_x % 2 == 0:
            nibble = byte_val & 0x0F  # Low nibble
        else:
            nibble = (byte_val >> 4) & 0x0F  # High nibble

        return nibble > 0


from enum import IntEnum

class EntityCategory(IntEnum):
    """Categories of entities in the game world."""
    PLAYER = 0
    MONSTER = 1
    NPC = 2
    CHEST = 3
    TRANSITION = 4
    OBJECT = 5
    OTHER = 6


class Rarity(IntEnum):
    """Monster/item rarity levels."""
    NORMAL = 0
    MAGIC = 1
    RARE = 2
    UNIQUE = 3
    NON_MONSTER = -1


@dataclass
class EntityDot:
    """Represents an entity dot on the radar."""
    entity_id: int          # Unique entity ID
    address: int            # Memory address
    grid_x: float           # Grid X position
    grid_y: float           # Grid Y position
    world_x: float          # World X position
    world_y: float          # World Y position
    world_z: float          # World Z position
    category: EntityCategory
    metadata: str           # Entity metadata path
    hp_cur: int = 0         # Current HP
    hp_max: int = 0         # Max HP
    is_poi: bool = False    # Is a point of interest (has MinimapIcon)
    reaction: int = 0       # Hostility: friendly = (reaction & 0x7F) == 1
    rarity: Rarity = Rarity.NON_MONSTER
    is_opened: bool = False # For chests: True if opened/looted

    @property
    def is_alive(self) -> bool:
        """Monsters are alive only with positive HP; non-life entities always shown."""
        return self.hp_max <= 0 or self.hp_cur > 0

    @property
    def has_life(self) -> bool:
        """Whether this entity has HP."""
        return self.hp_max > 0

    @property
    def is_friendly(self) -> bool:
        """Check if entity is friendly (not hostile)."""
        return (self.reaction & 0x7F) == 1


class Poe2Offsets:
    """POE2 memory offsets - EXACT COPY from POE2Radar source code (Poe2Offsets.cs).

    These are confirmed working in POE2Radar v0.5.1.
    Source: C:\\Users\\riven\\Downloads\\POE2 RADAR\\POE2 RADAR CODE\\POE2Radar\\src\\POE2Radar.Core\\Game\\Poe2Offsets.cs

    Chain: GameState -> CurrentStatePtr (StdVector) -> [0] = InGameState
           InGameState -> AreaInstanceData (0x290) -> AreaInstance
           AreaInstance -> TerrainMetadata (0x8A0) -> TerrainStruct
    """

    # Constants
    WORLD_TO_GRID_RATIO = 250.0 / 23.0  # ~10.87, tile→world = 250, tile→grid = 23
    TILE_GRID_CELLS = 23  # Each tile = 23x23 grid cells
    NETWORK_BUBBLE_GRID = 150  # Entities within this radius are loaded

    # GameState root (found via AOB pattern)
    class GameState:
        CURRENT_STATE_PTR = 0x08    # StdVector - first element is active state (InGameState)
        STATES = 0x48               # Inline array of 12 state slots (16 bytes each)
        STATE_SLOT_STRIDE = 0x10    # Each slot is 16 bytes
        STATE_SLOT_COUNT = 12

    # InGameState offsets (from InGameState base)
    class InGameState:
        AREA_INSTANCE_DATA = 0x290  # ✓ -> AreaInstance (validated in POE2Radar)
        UI_ROOT = 0x2F0             # ✓ -> root UiElement
        CAMERA = 0x368              # ✓ -> Camera object
        WORLD_DATA = 0x310          # (GH2-drift) -> WorldData

    # AreaInstance offsets (from AreaInstance base)
    class AreaInstance:
        AREA_INFO_PTR = 0x0A0       # ✓ -> AreaInfo
        LOCAL_PLAYER = 0x5A0        # ✓ -> player Entity (unchanged in 0.5.4)
        # Patch 0.5.4 inserted 0x18 bytes after LocalPlayer, shifting every
        # AreaInstance field above 0x5A0 by +0x18 (verified live via
        # tools/diagnose_overlay.py).
        AWAKE_ENTITIES = 0x6D8      # ✓ StdMap of live entities (was 0x6C0)
        SLEEPING_ENTITIES = 0x6E8   # ✓ StdMap of inactive entities (was 0x6D0)
        TERRAIN_METADATA = 0x8B8    # ✓ -> TerrainStruct base (was 0x8A0)
        CURRENT_AREA_LEVEL = 0x0C4  # ✓ int
        CURRENT_AREA_HASH = 0x11C   # ✓ uint

    # Terrain offsets (from TerrainStruct at AreaInstance+0x8A0)
    # POE2 has 4 grid layers - BytesPerRow moved to 0x130
    class Terrain:
        TOTAL_TILES = 0x18          # ✓ StdTuple2D<long> (tilesX, tilesY)
        TILE_DETAILS_PTR = 0x28     # ✓ StdVector of TileStructure (0x38 bytes each)
        GRID_WALKABLE_DATA = 0xD0   # ✓ StdVector - packed walkable grid bytes
        GRID_LANDSCAPE_DATA = 0xE8  # ✓ StdVector
        GRID_LAYER_3 = 0x100        # ✓ StdVector (extra POE2 layer)
        GRID_LAYER_4 = 0x118        # ✓ StdVector (extra POE2 layer)
        BYTES_PER_ROW = 0x130       # ✓ int (e.g., 621 live) - cellsPerRow = bytes * 2

    # StdVector structure (common container in PoE)
    class StdVector:
        BEGIN = 0x00    # Pointer to first element
        END = 0x08      # Pointer past last element
        # Size in bytes = (END - BEGIN)

    # Camera offsets (from Camera base at InGameState+0x368)
    class Camera:
        WORLD_TO_SCREEN_MATRIX = 0x1A0  # Matrix4x4 (row-major, 64 bytes)
        ZOOM = 0x528                     # float (1.0 = default)

    # Render component offsets (from Render component base)
    class Render:
        WORLD_POSITION = 0x138  # ✓ Vector3 (X, Y, Z floats)

    # Entity offsets
    class Entity:
        ENTITY_DETAILS_PTR = 0x08   # ✓ -> EntityDetails
        COMPONENT_LIST = 0x10       # ✓ StdVector of component pointers
        ID = 0x80                   # (GH2) uint
        IS_VALID = 0x84             # (GH2) byte; valid when bit0 clear

    # EntityDetails offsets
    class EntityDetails:
        NAME = 0x08                     # ✓ StdWString - metadata path
        COMPONENT_LOOKUP_PTR = 0x28     # ✓ -> ComponentLookUp

    # ComponentLookUp: a StdBucket of (NamePtr, Index) at +0x28
    class ComponentLookUp:
        NAME_AND_INDEX_BUCKET = 0x28    # ✓ StdBucket
        ENTRY_STRIDE = 0x10             # ✓ {IntPtr NamePtr; int Index; int pad}

    # std::map node for entity traversal
    class StdMapNode:
        LEFT = 0x00
        PARENT = 0x08
        RIGHT = 0x10
        IS_NIL = 0x19                   # bool
        DATA = 0x20                     # Key/Value start
        KEY_ID = 0x20                   # uint entity id
        VALUE_ENTITY_PTR = 0x28         # IntPtr

    # EntityList constants
    class EntityList:
        STD_MAP_SIZE = 0x10             # Each StdMap is {Head ptr, int Size, pad}
        VISUAL_ID_THRESHOLD = 0x40000000  # Entities below are real, above are visuals

    # Life component offsets - ✓ validated live 2026-09-03
    class Life:
        OWNER = 0x008                   # ComponentHeader.EntityPtr
        HEALTH = 0x160                  # ✓ VitalStruct (was 0x1B0 pre-patch)
        MANA = 0x1B8                    # ✓ VitalStruct (was 0x208 pre-patch)
        ENERGY_SHIELD = 0x1F8           # ✓ VitalStruct (was 0x248 pre-patch)

    # VitalStruct offsets
    class VitalStruct:
        RESERVED_FLAT = 0x04            # int
        RESERVED_FRACTION = 0x08        # int (over 10000)
        REGEN = 0x28
        MAX = 0x2C                      # ✓
        CURRENT = 0x30                  # ✓

    # Positioned component - for hostility/reaction
    class Positioned:
        REACTION = 0x1E0                # ✓ byte: friendly = (b & 0x7F) == 1

    # ObjectMagicProperties - monster/chest rarity
    class ObjectMagicProperties:
        RARITY = 0x144                  # ✓ 0=Normal, 1=Magic, 2=Rare, 3=Unique

    # MinimapIcon component - for POI markers
    class MinimapIcon:
        COMPLETED_STATE = 0x10          # ✓ int: 0=active, non-zero=completed

    # Chest component
    class ChestComponent:
        OPEN_STATE = 0x168              # ✓ byte: 0=closed, non-zero=opened

    # UiElement offsets (for reading map visibility)
    class UiElement:
        SELF = 0x08                 # ✓ self pointer
        CHILDREN = 0x10             # ✓ StdVector of child UiElement pointers
        FLAGS = 0x180               # ✓ uint; IsVisibleLocal = bit 0x0B
        FLAG_VISIBLE_BIT = 0x0B     # ✓ visible bit (set when shown)

    # MapUiElement offsets (for reading map shift/zoom)
    class MapUiElement:
        SHIFT = 0x368               # ✓ StdTuple2D<float> (shiftX, shiftY)
        DEFAULT_SHIFT = 0x370       # ✓ StdTuple2D<float> (0, -20)
        ZOOM = 0x3A8                # ✓ float (0.5 live)


class AobScanner:
    """AOB (Array of Bytes) pattern scanner for finding game structures."""

    # GameStates AOB pattern (works for POE1 and POE2)
    # Pattern: 48 83 EC ?? 48 8B F1 33 ED 48 39 2D ?? ?? ?? ??
    # From UnknownCheats: This finds the GameStates base
    GAME_STATES_PATTERN = [
        0x48, 0x83, 0xEC, None,           # sub rsp, ??
        0x48, 0x8B, 0xF1,                 # mov rsi, rcx
        0x33, 0xED,                       # xor ebp, ebp
        0x48, 0x39, 0x2D, None, None, None, None  # cmp [rip+rel32], rbp
    ]
    GAME_STATES_DISP_OFFSET = 12  # rel32 starts at byte 12
    GAME_STATES_INSTR_LEN = 16    # full pattern length

    # Offset from GameStates to InGameState pointer
    # POE2 uses 0x88 according to UnknownCheats (d2k2)
    # Some versions may use 0x70, 0x80, 0xB0 - try multiple
    INGAME_STATE_OFFSET = 0x88
    INGAME_STATE_OFFSETS_TO_TRY = [0x88, 0x70, 0x80, 0xB0]

    # Legacy pattern (kept for fallback)
    GAME_STATE_PATTERN = [
        0x48, 0x39, 0x2D, None, None, None, None,  # cmp [rip+rel32], rbp
        0x0F, 0x85, 0x16, 0x01, 0x00, 0x00          # jnz +0x116
    ]
    GAME_STATE_DISP_OFFSET = 3   # rel32 starts at byte 3
    GAME_STATE_INSTR_LEN = 7     # cmp instruction is 7 bytes

    # InGameState AOB pattern (may be outdated - using offset approach instead)
    INGAME_STATE_PATTERN = [
        0x8B, 0xC7, 0x48, 0x83, 0xC4, 0x40, 0x5F, 0xC3,
        0x48, 0x8B, 0x05, None, None, None, None,
        0x48, 0x89, 0x07, 0x48
    ]
    INGAME_STATE_DISP_OFFSET = 11  # rel32 at position 11 (after prefix)
    INGAME_STATE_INSTR_LEN = 15    # instruction ends at position 15

    @staticmethod
    def find_pattern(data: bytes, pattern: List[Optional[int]]) -> List[int]:
        """Find all occurrences of pattern in data. None = wildcard."""
        results = []
        pattern_len = len(pattern)

        for i in range(len(data) - pattern_len + 1):
            match = True
            for j, pb in enumerate(pattern):
                if pb is not None and data[i + j] != pb:
                    match = False
                    break
            if match:
                results.append(i)

        return results

    @staticmethod
    def resolve_rip_relative(base_addr: int, match_offset: int,
                            disp_offset: int, instr_len: int,
                            data: bytes) -> Optional[int]:
        """Resolve a RIP-relative address from pattern match."""
        disp_pos = match_offset + disp_offset
        if disp_pos + 4 > len(data):
            return None

        # Read 4-byte signed displacement
        displacement = struct.unpack('<i', data[disp_pos:disp_pos + 4])[0]

        # RIP-relative: target = RIP + displacement
        # RIP at end of instruction = base_addr + match_offset + instr_len
        next_instr_addr = base_addr + match_offset + instr_len
        return next_instr_addr + displacement


class TerrainReader:
    """Reads terrain/map data from POE2 memory (read-only)."""

    PROCESS_NAMES = {
        "steam": "PathOfExileSteam.exe",
        "standalone": "PathOfExile.exe",
    }

    def __init__(self, game_version: str = "steam"):
        self.game_version = game_version
        self.pm: Optional[pymem.Pymem] = None
        self.base_address: int = 0
        self.module_size: int = 0
        self.connected = False

        # Cached addresses (reset on zone change or reconnect)
        self._game_state_slot: Optional[int] = None    # Global slot address
        self._in_game_state_addr: Optional[int] = None  # Heap object address
        self._area_instance_addr: Optional[int] = None
        self._terrain_addr: Optional[int] = None

        # Maps a raw entity pointer to the pointer whose components actually
        # resolve. Patch 0.5.4 wraps entities behind one extra indirection, so
        # the real entity is read_ptr(raw_ptr). Reset on zone change.
        self._real_entity_cache: dict = {}

        # Map UI element tracking (for toggler detection like POE2Radar)
        self._map_elements: List[int] = []  # Discovered MapUiElement addresses
        self._ever_visible: set = set()      # Elements seen visible at least once
        self._ever_hidden: set = set()       # Elements seen hidden at least once
        self._map_cache_key: Optional[int] = None  # AreaInstance address for cache

    def connect(self) -> bool:
        """Connect to the game process."""
        process_name = self.PROCESS_NAMES.get(self.game_version, "PathOfExileSteam.exe")
        try:
            self.pm = pymem.Pymem(process_name)
            self.base_address = self.pm.base_address

            # Get module size for scanning
            for module in self.pm.list_modules():
                if module.name.lower() == process_name.lower():
                    self.module_size = module.SizeOfImage
                    break

            self.connected = True
            self._reset_cache()
            logger.info(f"TerrainReader connected to {process_name} (base: 0x{self.base_address:X}, size: {self.module_size})")
            return True
        except Exception as e:
            logger.debug(f"TerrainReader failed to connect: {e}")
            self.connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from the game."""
        if self.pm:
            try:
                self.pm.close_process()
            except:
                pass
        self.pm = None
        self.connected = False
        self._reset_cache()

    def _reset_cache(self) -> None:
        """Reset cached addresses."""
        self._game_state_slot = None
        self._in_game_state_addr = None
        self._area_instance_addr = None
        self._terrain_addr = None

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

    def _read_long(self, address: int) -> Optional[int]:
        """Read a 64-bit integer."""
        if not self.pm or address == 0:
            return None
        try:
            return self.pm.read_longlong(address)
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

    def _read_float(self, address: int) -> Optional[float]:
        """Read a 32-bit float."""
        if not self.pm or address == 0:
            return None
        try:
            return self.pm.read_float(address)
        except:
            return None

    def _read_std_vector(self, address: int) -> Optional[Tuple[int, int]]:
        """Read StdVector begin/end pointers. Returns (begin_ptr, size_bytes).

        Standard C++ std::vector layout:
        - offset 0x00: pointer to first element
        - offset 0x08: pointer past last element (end)
        - offset 0x10: pointer to end of allocated storage (capacity)
        """
        begin = self._read_ptr(address + Poe2Offsets.StdVector.BEGIN)
        end = self._read_ptr(address + Poe2Offsets.StdVector.END)
        if begin is None or end is None or begin == 0:
            return None
        size = end - begin
        if size <= 0 or size > 100_000_000:  # Sanity check: max 100MB
            return None
        return (begin, size)

    def _read_native_array(self, address: int) -> Optional[Tuple[int, int]]:
        """Read NativeArray/Span-like structure. Returns (data_ptr, size_bytes).

        Some POE2 structures use:
        - offset 0x00: pointer to data
        - offset 0x08: count (as long/int64)
        """
        data_ptr = self._read_ptr(address)
        count = self._read_long(address + 8)

        if data_ptr is None or count is None or data_ptr == 0:
            return None

        # Count should be reasonable
        if count <= 0 or count > 100_000_000:
            return None

        return (data_ptr, count)

    def _scan_for_pattern(self, pattern: List[Optional[int]],
                          disp_offset: int, instr_len: int) -> Optional[int]:
        """Scan game memory for AOB pattern and resolve RIP-relative address."""
        if not self.pm or self.module_size == 0:
            return None

        try:
            # Read executable sections (scan in chunks to handle large modules)
            chunk_size = 0x1000000  # 16MB chunks

            for offset in range(0, self.module_size, chunk_size):
                read_size = min(chunk_size, self.module_size - offset)
                chunk_base = self.base_address + offset

                try:
                    data = self.pm.read_bytes(chunk_base, read_size)
                except:
                    continue

                matches = AobScanner.find_pattern(data, pattern)

                for match_offset in matches:
                    resolved = AobScanner.resolve_rip_relative(
                        chunk_base, match_offset, disp_offset, instr_len, data
                    )
                    if resolved and resolved > 0x10000:  # Valid address
                        logger.debug(f"AOB match at 0x{chunk_base + match_offset:X} -> 0x{resolved:X}")
                        return resolved

            return None
        except Exception as e:
            logger.error(f"AOB scan error: {e}")
            return None

    def find_game_state(self) -> Optional[int]:
        """Find the GameState global slot via AOB pattern scan."""
        if self._game_state_slot:
            return self._game_state_slot

        logger.info("Scanning for GameState AOB pattern...")
        slot = self._scan_for_pattern(
            AobScanner.GAME_STATE_PATTERN,
            AobScanner.GAME_STATE_DISP_OFFSET,
            AobScanner.GAME_STATE_INSTR_LEN
        )

        if slot:
            self._game_state_slot = slot
            logger.info(f"Found GameState slot at 0x{slot:X}")
        else:
            logger.warning("GameState AOB pattern not found")

        return slot

    def find_ingame_state(self) -> Optional[int]:
        """Find the InGameState object address (RESOLVED FRESH EACH CALL).

        Like POE2Radar's TryResolve(), this is called every tick to get fresh values.
        Only the GameState SLOT (AOB scan result) is cached - everything else is read fresh.

        Chain: GameStateSlot -> GameState -> InGameState -> AreaInstance -> LocalPlayer
        """
        # Cache only the GameState slot (expensive AOB scan)
        if not self._game_state_slot:
            logger.info("Scanning for GameStates AOB pattern...")
            self._game_state_slot = self._scan_for_pattern(
                AobScanner.GAME_STATES_PATTERN,
                AobScanner.GAME_STATES_DISP_OFFSET,
                AobScanner.GAME_STATES_INSTR_LEN
            )
            if self._game_state_slot:
                logger.info(f"Found GameState SLOT at 0x{self._game_state_slot:X}")
            else:
                logger.warning("GameState AOB pattern not found")
                return None

        # Dereference the slot to get the actual GameState object (FRESH READ)
        game_state = self._read_ptr(self._game_state_slot)
        if not game_state or game_state < 0x10000:
            return None

        # Build list of InGameState candidates (same as POE2Radar)
        candidates = []

        # Method 1: CurrentStatePtr StdVector (primary method in POE2Radar)
        vec_first = self._read_ptr(game_state + Poe2Offsets.GameState.CURRENT_STATE_PTR)
        if vec_first and vec_first > 0x10000:
            igs_ptr = self._read_ptr(vec_first)
            if igs_ptr and igs_ptr > 0x10000:
                candidates.append(igs_ptr)

        # Method 2: States array (fallback - 12 slots at +0x48)
        for i in range(Poe2Offsets.GameState.STATE_SLOT_COUNT):
            slot_addr = game_state + Poe2Offsets.GameState.STATES + (i * Poe2Offsets.GameState.STATE_SLOT_STRIDE)
            igs_ptr = self._read_ptr(slot_addr)
            if igs_ptr and igs_ptr > 0x10000:
                if igs_ptr not in candidates:
                    candidates.append(igs_ptr)

        # Validate each candidate (same as POE2Radar's validation)
        for igs in candidates:
            # Check: InGameState -> AreaInstance (0x290)
            area_instance = self._read_ptr(igs + Poe2Offsets.InGameState.AREA_INSTANCE_DATA)
            if not area_instance or area_instance < 0x10000:
                continue

            # Check: AreaInstance -> LocalPlayer (0x5A0)
            local_player = self._read_ptr(area_instance + Poe2Offsets.AreaInstance.LOCAL_PLAYER)
            if not local_player or local_player < 0x10000:
                continue

            # Valid chain found
            return igs

        # Not in-game (loading screen, character select, etc.)
        return None

    def get_area_instance(self) -> Optional[int]:
        """Get AreaInstance address from InGameState (ALWAYS FRESH READ).

        Chain: InGameState -> AreaInstanceData (0x290) -> AreaInstance

        Unlike other cached getters, this always reads fresh from memory
        to enable zone change detection (like POE2Radar's TryResolve).
        """
        igs = self.find_ingame_state()
        if not igs:
            return None

        # ALWAYS read fresh - AreaInstance changes on zone change!
        area_ptr = self._read_ptr(igs + Poe2Offsets.InGameState.AREA_INSTANCE_DATA)
        if area_ptr and area_ptr > 0x10000:
            # Check for zone change (like POE2Radar's: if areaInstance != _lastAreaInstance)
            if self._area_instance_addr and area_ptr != self._area_instance_addr:
                logger.info(f"Zone change detected! Old=0x{self._area_instance_addr:X}, New=0x{area_ptr:X}")
                self._on_zone_change()

            self._area_instance_addr = area_ptr
            return area_ptr

        return None

    def _on_zone_change(self) -> None:
        """Called when zone change is detected. Invalidates terrain and map caches."""
        # Clear terrain address (needs to be re-resolved for new area)
        self._terrain_addr = None

        # Clear map element cache (UI changes per area)
        self._map_elements.clear()
        self._ever_visible.clear()
        self._ever_hidden.clear()
        self._map_cache_key = None

        # Real-entity indirection mapping is per-area (pointers are recycled)
        self._real_entity_cache.clear()

        # Entity caches will be invalidated by entity reader itself (checks area)

    def check_zone_change(self) -> bool:
        """Check if player changed zones. Returns True if zone changed.

        DEPRECATED: Zone changes are now detected automatically in get_area_instance().
        This method still works for explicit checking.
        """
        old_area = self._area_instance_addr
        new_area = self.get_area_instance()  # This now detects zone change internally
        return old_area is not None and new_area is not None and old_area != new_area

    def get_terrain_metadata(self) -> Optional[int]:
        """Get TerrainStruct address from AreaInstance.

        POE2Radar chain: AreaInstance -> TerrainMetadata (0x8A0) -> TerrainStruct

        This is NOT through LocalData! POE2Radar goes directly:
        AreaInstance + 0x8A0 = TerrainStruct base
        """
        if self._terrain_addr:
            return self._terrain_addr

        area_instance = self.get_area_instance()
        if not area_instance:
            logger.warning("Could not find AreaInstance")
            return None

        # TerrainStruct is at AreaInstance + 0x8A0 (TERRAIN_METADATA)
        terrain_addr = area_instance + Poe2Offsets.AreaInstance.TERRAIN_METADATA
        self._terrain_addr = terrain_addr
        logger.debug(f"TerrainStruct at 0x{terrain_addr:X}")
        return terrain_addr

    def read_terrain(self) -> Optional[TerrainData]:
        """Read the current area's terrain data.

        EXACT PORT of POE2Radar's Poe2Live.Terrain() method:

        TerrainStruct layout (at AreaInstance + 0x8A0):
        - 0x18: StdTuple2D<long> TotalTiles (tilesX, tilesY)
        - 0xD0: StdVector GridWalkableData (first/last pointers)
        - 0x130: int BytesPerRow

        Grid is packed nibbles: 2 cells per byte, 0 = blocked, non-0 = walkable.
        """
        if not self.connected:
            if not self.connect():
                return None

        terrain_base = self.get_terrain_metadata()
        if not terrain_base:
            logger.warning("Could not find terrain metadata")
            return None

        try:
            # Read GridWalkableData StdVector (first/last pointers at 0xD0)
            first_ptr = self._read_ptr(terrain_base + Poe2Offsets.Terrain.GRID_WALKABLE_DATA)
            last_ptr = self._read_ptr(terrain_base + Poe2Offsets.Terrain.GRID_WALKABLE_DATA + 8)

            if not first_ptr or first_ptr == 0:
                logger.debug("GridWalkableData first pointer is null")
                self.invalidate_cache()  # Invalidate cache on failure
                return None
            if not last_ptr or last_ptr == 0:
                logger.debug("GridWalkableData last pointer is null")
                self.invalidate_cache()
                return None

            # Read BytesPerRow (int at 0x130)
            bytes_per_row = self._read_int(terrain_base + Poe2Offsets.Terrain.BYTES_PER_ROW)
            if not bytes_per_row or bytes_per_row <= 0 or bytes_per_row > 65536:
                logger.warning(f"Invalid BytesPerRow: {bytes_per_row}")
                return None

            # Calculate total bytes and dimensions
            total_bytes = last_ptr - first_ptr
            if total_bytes <= 0 or total_bytes > 64 * 1024 * 1024:  # Max 64MB
                logger.warning(f"Invalid grid size: {total_bytes} bytes (first=0x{first_ptr:X}, last=0x{last_ptr:X})")
                return None

            rows = total_bytes // bytes_per_row
            width = bytes_per_row * 2  # 2 cells per byte (nibbles)

            if rows <= 0 or rows > 65536:
                logger.warning(f"Invalid row count: {rows}")
                return None

            logger.debug(f"Grid dimensions: {width}x{rows} cells, {bytes_per_row} bytes/row, {total_bytes} total bytes")

            # Read the raw grid data
            raw_data = self._read_bytes(first_ptr, total_bytes)
            if not raw_data or len(raw_data) != total_bytes:
                logger.warning(f"Could not read grid data (got {len(raw_data) if raw_data else 0} of {total_bytes} bytes)")
                return None

            # Calculate tile counts from grid dimensions
            tiles_x = width // Poe2Offsets.TILE_GRID_CELLS
            tiles_y = rows // Poe2Offsets.TILE_GRID_CELLS

            logger.info(f"Read terrain: {tiles_x}x{tiles_y} tiles, {width}x{rows} cells, {total_bytes} bytes, {bytes_per_row} bytes/row")

            return TerrainData(
                width=width,
                height=rows,
                walkable_grid=raw_data,
                tiles_x=tiles_x,
                tiles_y=tiles_y,
                bytes_per_row=bytes_per_row
            )

        except Exception as e:
            logger.error(f"Error reading terrain: {e}")
            import traceback
            traceback.print_exc()
            return None

    def get_player_position(self) -> Optional[Tuple[float, float, float]]:
        """Get player's current world position (X, Y, Z)."""
        area = self.get_area_instance()
        if not area:
            return None

        # LocalPlayer at AreaInstance + 0x5A0
        player_ptr = self._read_ptr(area + Poe2Offsets.AreaInstance.LOCAL_PLAYER)
        if not player_ptr:
            return None

        # 0.5.4 wraps the player entity behind one extra indirection.
        player_ptr = self._real_entity(player_ptr) or player_ptr

        # Get the Render component from the player's component list
        render_comp = self._find_component(player_ptr, "Render")
        if not render_comp:
            return None

        # Read world position Vector3 from Render + 0x138
        x = self._read_float(render_comp + Poe2Offsets.Render.WORLD_POSITION)
        y = self._read_float(render_comp + Poe2Offsets.Render.WORLD_POSITION + 4)
        z = self._read_float(render_comp + Poe2Offsets.Render.WORLD_POSITION + 8)

        if x is None or y is None or z is None:
            return None

        return (x, y, z)

    def get_player_grid_position(self) -> Optional[Tuple[float, float]]:
        """Get player's position in grid coordinates."""
        world_pos = self.get_player_position()
        if not world_pos:
            return None

        x, y, _ = world_pos
        grid_x = x / Poe2Offsets.WORLD_TO_GRID_RATIO
        grid_y = y / Poe2Offsets.WORLD_TO_GRID_RATIO
        return (grid_x, grid_y)

    def _find_component(self, entity: int, component_name: str) -> Optional[int]:
        """Find a component by name from an entity's component list.

        Uses the ComponentLookUp structure at EntityDetails+0x28.
        """
        # Get EntityDetails pointer
        entity_details = self._read_ptr(entity + Poe2Offsets.Entity.ENTITY_DETAILS_PTR)
        if not entity_details:
            return None

        # Get ComponentLookUp pointer
        comp_lookup = self._read_ptr(entity_details + 0x28)  # EntityDetails.ComponentLookUpPtr
        if not comp_lookup:
            return None

        # ComponentLookUp has a StdBucket at +0x28 containing {NamePtr, Index} pairs
        # For simplicity, let's just iterate through the component list and check names

        # Get ComponentList StdVector from Entity
        comp_list_begin = self._read_ptr(entity + Poe2Offsets.Entity.COMPONENT_LIST)
        comp_list_end = self._read_ptr(entity + Poe2Offsets.Entity.COMPONENT_LIST + 8)

        if not comp_list_begin or not comp_list_end or comp_list_end <= comp_list_begin:
            return None

        num_components = (comp_list_end - comp_list_begin) // 8  # 8 bytes per pointer
        if num_components > 100:  # Sanity check
            return None

        # The actual component lookup is complex - for now, let's try a simplified approach
        # POE2Radar uses ComponentLookUp to map name -> index, then ComponentList[index]
        # We'll iterate the StdBucket structure

        bucket_begin = self._read_ptr(comp_lookup + 0x28)  # NameAndIndexBucket = StdVector.Begin
        bucket_end = self._read_ptr(comp_lookup + 0x28 + 8)  # StdVector.End

        if not bucket_begin or not bucket_end or bucket_end <= bucket_begin:
            return None

        entry_stride = 0x10  # {IntPtr NamePtr; int Index; int pad}
        num_entries = (bucket_end - bucket_begin) // entry_stride

        if num_entries > 100:  # Sanity check
            return None

        for i in range(num_entries):
            entry_addr = bucket_begin + i * entry_stride
            name_ptr = self._read_ptr(entry_addr)
            index = self._read_int(entry_addr + 8)

            if name_ptr and index is not None and index >= 0 and index < num_components:
                # Read the component name (StdWString or raw C string)
                # Usually it's a pointer to a StdWString or a simple string
                name_bytes = self._read_bytes(name_ptr, 32)
                if name_bytes:
                    # Try to decode as ASCII first (component names are ASCII)
                    try:
                        name = name_bytes.split(b'\x00')[0].decode('ascii')
                        if name == component_name:
                            # Found it! Get the component pointer from ComponentList
                            comp_ptr = self._read_ptr(comp_list_begin + index * 8)
                            return comp_ptr
                    except:
                        pass

        return None

    def get_map_state(self) -> Optional[Tuple[bool, float, float, float]]:
        """Read the map UI state (visibility, shiftX, shiftY, zoom).

        Uses POE2Radar's toggler detection: tracks elements that have been seen
        in BOTH visible AND hidden states. A permanently-visible element (like
        the always-on minimap) won't count as a toggler.

        Returns:
            Tuple of (is_visible, shift_x, shift_y, zoom) or None if can't read
        """
        area = self.get_area_instance()
        igs = self.find_ingame_state()
        if not igs or not area:
            return None

        # Reset cache if area changed
        if area != self._map_cache_key:
            self._map_cache_key = area
            self._map_elements.clear()
            self._ever_visible.clear()
            self._ever_hidden.clear()

        # Discover map elements once per area
        if not self._map_elements:
            ui_root = self._read_ptr(igs + Poe2Offsets.InGameState.UI_ROOT)
            if not ui_root or ui_root < 0x10000:
                return None
            self._discover_map_elements(ui_root)

        if not self._map_elements:
            return None

        # Read current state of each map element
        visible_count = 0
        saw_toggler = False
        toggler_visible = False
        any_ui = None
        toggler_ui = None

        for element in self._map_elements:
            state = self._read_map_element(element)
            if not state:
                continue

            is_visible, shift_x, shift_y, zoom = state

            # Track visibility history
            if is_visible:
                self._ever_visible.add(element)
                visible_count += 1
            else:
                self._ever_hidden.add(element)

            if any_ui is None:
                any_ui = state

            # A genuine toggler has been seen in BOTH states
            if element in self._ever_visible and element in self._ever_hidden:
                saw_toggler = True
                if is_visible:
                    toggler_visible = True
                if is_visible or toggler_ui is None:
                    toggler_ui = state

        if any_ui is None:
            return None

        # If we've found a toggler, use its state
        if saw_toggler:
            if toggler_ui:
                return (toggler_visible, toggler_ui[1], toggler_ui[2], toggler_ui[3])

        # No toggler observed yet - use heuristic: >= 2 visible = open
        # This matches POE2Radar's fallback logic
        return (visible_count >= 2, any_ui[1], any_ui[2], any_ui[3])

    def _discover_map_elements(self, ui_root: int, max_depth: int = 8) -> None:
        """Walk UI tree and cache all MapUiElements found."""
        self._map_elements.clear()
        visited = set()
        queue = [(ui_root, 0)]

        while queue and len(visited) < 2000:
            element, depth = queue.pop(0)
            if element in visited or element < 0x10000:
                continue
            visited.add(element)

            # Check if this is a MapUiElement (DefaultShift.Y == -20)
            default_shift_y = self._read_float(element + Poe2Offsets.MapUiElement.DEFAULT_SHIFT + 4)
            if default_shift_y is not None and abs(default_shift_y - (-20.0)) < 0.5:
                zoom = self._read_float(element + Poe2Offsets.MapUiElement.ZOOM)
                if zoom is not None and 0.1 < zoom < 10.0:
                    self._map_elements.append(element)
                    logger.debug(f"Discovered MapUiElement: 0x{element:X}")

            # Continue traversing children
            if depth < max_depth:
                children_begin = self._read_ptr(element + Poe2Offsets.UiElement.CHILDREN)
                children_end = self._read_ptr(element + Poe2Offsets.UiElement.CHILDREN + 8)
                if children_begin and children_end and children_end > children_begin:
                    num_children = (children_end - children_begin) // 8
                    if num_children <= 200:
                        for i in range(num_children):
                            child = self._read_ptr(children_begin + i * 8)
                            if child and child > 0x10000:
                                queue.append((child, depth + 1))

        logger.debug(f"Discovered {len(self._map_elements)} MapUiElements")

    def _read_map_element(self, element: int) -> Optional[Tuple[bool, float, float, float]]:
        """Read the current state of a MapUiElement."""
        # Validate element is still a MapUiElement
        default_shift_y = self._read_float(element + Poe2Offsets.MapUiElement.DEFAULT_SHIFT + 4)
        if default_shift_y is None or abs(default_shift_y - (-20.0)) > 0.5:
            return None

        zoom = self._read_float(element + Poe2Offsets.MapUiElement.ZOOM)
        if zoom is None or not (0.1 < zoom < 10.0):
            return None

        # Check visibility
        flags = self._read_int(element + Poe2Offsets.UiElement.FLAGS)
        is_visible = (flags is not None) and (flags & (1 << Poe2Offsets.UiElement.FLAG_VISIBLE_BIT)) != 0

        shift_x = self._read_float(element + Poe2Offsets.MapUiElement.SHIFT)
        shift_y = self._read_float(element + Poe2Offsets.MapUiElement.SHIFT + 4)

        if shift_x is None or shift_y is None:
            return None

        return (is_visible, shift_x, shift_y, zoom)

    def _find_visible_map_element(self, ui_root: int, max_depth: int = 8) -> Optional[Tuple[bool, float, float, float]]:
        """Walk UI tree to find a visible map element."""
        visited = set()
        queue = [(ui_root, 0)]
        map_elements = []
        elements_checked = 0

        while queue and len(visited) < 2000:  # Increase limit
            element, depth = queue.pop(0)
            if element in visited or element < 0x10000:
                continue
            visited.add(element)
            elements_checked += 1

            # Check if this is a MapUiElement (DefaultShift.Y == -20)
            default_shift_y = self._read_float(element + Poe2Offsets.MapUiElement.DEFAULT_SHIFT + 4)
            if default_shift_y is not None and abs(default_shift_y - (-20.0)) < 0.5:  # Slightly wider tolerance
                # This is a MapUiElement!
                zoom = self._read_float(element + Poe2Offsets.MapUiElement.ZOOM)
                if zoom is not None and 0.1 < zoom < 10.0:  # Valid zoom range
                    # Check visibility
                    flags = self._read_int(element + Poe2Offsets.UiElement.FLAGS)
                    is_visible = (flags is not None) and (flags & (1 << Poe2Offsets.UiElement.FLAG_VISIBLE_BIT)) != 0

                    shift_x = self._read_float(element + Poe2Offsets.MapUiElement.SHIFT)
                    shift_y = self._read_float(element + Poe2Offsets.MapUiElement.SHIFT + 4)

                    if shift_x is not None and shift_y is not None:
                        map_elements.append((element, is_visible, shift_x, shift_y, zoom, flags))
                        flags_str = f"0x{flags:X}" if flags else "None"
                        logger.debug(f"Found MapUiElement: 0x{element:X} visible={is_visible} zoom={zoom:.2f} flags={flags_str}")

            # Continue traversing children
            if depth < max_depth:
                children_begin = self._read_ptr(element + Poe2Offsets.UiElement.CHILDREN)
                children_end = self._read_ptr(element + Poe2Offsets.UiElement.CHILDREN + 8)
                if children_begin and children_end and children_end > children_begin:
                    num_children = (children_end - children_begin) // 8
                    if num_children <= 200:  # Increase limit
                        for i in range(num_children):
                            child = self._read_ptr(children_begin + i * 8)
                            if child and child > 0x10000:
                                queue.append((child, depth + 1))

        logger.debug(f"UI tree walk: checked {elements_checked} elements, found {len(map_elements)} map elements")

        # Check all found map elements for visibility
        for element, is_visible, shift_x, shift_y, zoom, flags in map_elements:
            if is_visible:
                return (True, shift_x, shift_y, zoom)

        # No visible map element found, return hidden state with first element's params
        if map_elements:
            _, is_visible, shift_x, shift_y, zoom, _ = map_elements[0]
            return (False, shift_x, shift_y, zoom)

        return None

    def is_map_visible(self) -> bool:
        """Check if the in-game map (Tab overlay) is visible."""
        map_state = self.get_map_state()
        if map_state:
            is_visible, shift_x, shift_y, zoom = map_state
            return is_visible
        # If we can't read map state, default to True so overlay shows
        return True

    def invalidate_cache(self) -> None:
        """Call this when zone changes to refresh addresses."""
        self._in_game_state_addr = None
        self._area_instance_addr = None
        self._terrain_addr = None
        self._map_elements.clear()
        self._ever_visible.clear()
        self._ever_hidden.clear()
        self._map_cache_key = None
        # Clear entity caches too
        self._entity_cache_key = None
        self._render_addr_cache = {}
        self._life_addr_cache = {}
        self._pos_addr_cache = {}
        self._omp_addr_cache = {}
        self._category_cache = {}
        self._meta_cache = {}
        self._rarity_cache = {}

    # ========================================================================
    # Entity Reading Methods (based on POE2Radar's Poe2Live.Entities())
    # ========================================================================

    def get_entities(self) -> List['EntityDot']:
        """Read all awake entities in the current area.

        Walks the std::map of entities at AreaInstance + AwakeEntities offset,
        categorizes each entity, and returns their positions on the grid.

        Returns:
            List of EntityDot objects representing entities on the map
        """
        area = self.get_area_instance()
        if not area:
            return []

        # Reset entity caches on area change
        if not hasattr(self, '_entity_cache_key') or self._entity_cache_key != area:
            self._entity_cache_key = area
            self._render_addr_cache = {}
            self._life_addr_cache = {}
            self._pos_addr_cache = {}
            self._omp_addr_cache = {}
            self._category_cache = {}
            self._meta_cache = {}
            self._rarity_cache = {}
            self._entity_id_map = {}  # Track entity IDs for recycled address detection
            self._real_entity_cache.clear()  # Indirection mapping is per-area

        entities = []

        # Read the std::map head pointer and size
        map_addr = area + Poe2Offsets.AreaInstance.AWAKE_ENTITIES
        head = self._read_ptr(map_addr)
        size = self._read_int(map_addr + 8)

        if not head or not size or size <= 0 or size > 100000:
            return entities

        # Get the root node (head->parent is root)
        root = self._read_ptr(head + Poe2Offsets.StdMapNode.PARENT)
        if not root:
            return entities

        # BFS traversal of std::map red-black tree
        visited = set()
        queue = [root]

        while queue and len(visited) < 200000:
            node = queue.pop(0)
            if not node or node == head or node in visited:
                continue
            visited.add(node)

            # Read node data (48 bytes for Left/Right/IsNil/KeyId/ValueEntityPtr)
            node_data = self._read_bytes(node, 48)
            if not node_data or len(node_data) < 48:
                continue

            # Check IsNil flag
            is_nil = node_data[Poe2Offsets.StdMapNode.IS_NIL]
            if is_nil != 0:
                continue  # Sentinel node

            # Extract entity id and pointer
            entity_id = struct.unpack_from('<I', node_data, Poe2Offsets.StdMapNode.KEY_ID)[0]
            entity_ptr = struct.unpack_from('<Q', node_data, Poe2Offsets.StdMapNode.VALUE_ENTITY_PTR)[0]

            # Queue children
            left = struct.unpack_from('<Q', node_data, Poe2Offsets.StdMapNode.LEFT)[0]
            right = struct.unpack_from('<Q', node_data, Poe2Offsets.StdMapNode.RIGHT)[0]
            if left and left != head:
                queue.append(left)
            if right and right != head:
                queue.append(right)

            # Skip visuals/decorations (high entity ids)
            if entity_ptr == 0 or entity_id >= Poe2Offsets.EntityList.VISUAL_ID_THRESHOLD:
                continue

            # 0.5.4 wraps entities behind one extra indirection. Resolve to the
            # real entity so all downstream component/metadata reads work.
            entity_ptr = self._real_entity(entity_ptr) or entity_ptr

            # Recycled address detection: if this address had a different ID before,
            # clear cached data for this address (the old entity is gone)
            if entity_ptr in self._entity_id_map:
                old_id = self._entity_id_map[entity_ptr]
                if old_id != entity_id:
                    # Address was recycled - clear caches for this entity
                    self._render_addr_cache.pop(entity_ptr, None)
                    self._life_addr_cache.pop(entity_ptr, None)
                    self._pos_addr_cache.pop(entity_ptr, None)
                    self._omp_addr_cache.pop(entity_ptr, None)
                    self._category_cache.pop(entity_ptr, None)
                    self._meta_cache.pop(entity_ptr, None)
                    self._rarity_cache.pop(entity_ptr, None)
            self._entity_id_map[entity_ptr] = entity_id

            # Get entity world position (always read fresh for position updates)
            world_pos = self._get_entity_world_pos(entity_ptr)
            if not world_pos:
                continue

            wx, wy, wz = world_pos
            grid_x = wx / Poe2Offsets.WORLD_TO_GRID_RATIO
            grid_y = wy / Poe2Offsets.WORLD_TO_GRID_RATIO

            # Categorize entity
            category = self._categorize_entity(entity_ptr)
            metadata = self._meta_cache.get(entity_ptr, "")

            # Read HP for monsters and players
            hp_cur, hp_max = 0, 0
            if category in (EntityCategory.MONSTER, EntityCategory.PLAYER):
                hp_cur, hp_max = self._read_entity_hp(entity_ptr)

            # Read rarity for monsters and chests
            rarity = Rarity.NON_MONSTER
            if category in (EntityCategory.MONSTER, EntityCategory.CHEST):
                rarity = self._read_entity_rarity(entity_ptr)

            # Read opened state for chests
            is_opened = False
            if category == EntityCategory.CHEST:
                is_opened = self._read_chest_opened(entity_ptr)

            # Read reaction (hostility)
            reaction = self._read_entity_reaction(entity_ptr)

            entities.append(EntityDot(
                entity_id=entity_id,
                address=entity_ptr,
                grid_x=grid_x,
                grid_y=grid_y,
                world_x=wx,
                world_y=wy,
                world_z=wz,
                category=category,
                metadata=metadata,
                hp_cur=hp_cur,
                hp_max=hp_max,
                is_poi=False,  # TODO: read MinimapIcon
                reaction=reaction,
                rarity=rarity,
                is_opened=is_opened
            ))

        return entities

    def _get_entity_world_pos(self, entity_ptr: int) -> Optional[Tuple[float, float, float]]:
        """Get entity world position from Render component."""
        # Check cache first
        if entity_ptr in self._render_addr_cache:
            render = self._render_addr_cache[entity_ptr]
        else:
            render = self._resolve_component(entity_ptr, "Render")
            self._render_addr_cache[entity_ptr] = render

        if not render:
            return None

        # Read Vector3 at Render + WORLD_POSITION
        data = self._read_bytes(render + Poe2Offsets.Render.WORLD_POSITION, 12)
        if not data or len(data) < 12:
            return None

        x, y, z = struct.unpack('<fff', data)
        return (x, y, z)

    def _categorize_entity(self, entity_ptr: int) -> EntityCategory:
        """Categorize entity based on its metadata path."""
        if entity_ptr in self._category_cache:
            return self._category_cache[entity_ptr]

        meta = self._read_entity_metadata(entity_ptr)
        self._meta_cache[entity_ptr] = meta

        # Skip unwanted terrain objects early (boulders, rocks, doodads, etc)
        if self._is_terrain_clutter(meta):
            cat = EntityCategory.OTHER
        # Order matters! NPC check before Monster check
        elif "/NPC/" in meta:
            cat = EntityCategory.NPC
        elif "/Monsters/" in meta and self._is_non_combat(meta):
            cat = EntityCategory.OTHER
        elif "/Monsters/" in meta:
            cat = EntityCategory.MONSTER
        elif "/Characters/" in meta:
            cat = EntityCategory.PLAYER
        elif "/Chests" in meta and self._is_breakable_prop(meta):
            cat = EntityCategory.OTHER
        # Strongboxes are special chests that should always show
        elif "/Chests" in meta and "Strongbox" in meta:
            cat = EntityCategory.CHEST
        # Regular chests - might be filtered later by rarity
        elif "/Chests" in meta:
            cat = EntityCategory.CHEST
        elif "Transition" in meta:
            cat = EntityCategory.TRANSITION
        elif "/Terrain/" in meta or "/MiscellaneousObjects/" in meta:
            cat = EntityCategory.OTHER  # Most terrain/misc objects are clutter
        else:
            cat = EntityCategory.OTHER

        self._category_cache[entity_ptr] = cat
        return cat

    def _is_non_combat(self, meta: str) -> bool:
        """Check if monster metadata is a non-combat entity or hazard.

        Be careful not to filter out real monsters with similar names!
        E.g., FungusZombie is a REAL monster, not a mushroom hazard.
        """
        # These are always non-combat (regardless of context)
        always_skip = [
            "MonsterMods", "/Daemon/", "Invisible",
            "OnDeath", "Spawner",
            # Curses, auras, and ground effects (skills cast by enemies or players)
            "Curse", "Aura", "GroundEffect", "Effect",
            "TempChains", "TemporalChains", "Enfeeble", "Vulnerability",
            "Despair", "Conductivity", "Flammability", "Frostbite",
            "Punishment", "Elemental Weakness", "Projectile Weakness",
            # Other skill effects
            "Consecrated", "Profane", "Desecrate",
            "Pulse", "Nova", "Beam", "Bolt",
        ]
        if any(skip in meta for skip in always_skip):
            return True

        # Skip if it looks like a ground hazard (not a zombie or real monster type)
        # Only filter these if NOT a real monster (check for common monster suffixes)
        real_monster_types = ["Zombie", "Skeleton", "Beast", "Spider", "Golem", "Elemental",
                              "Ghost", "Bandit", "Human", "Cannibal", "Warrior", "Archer"]
        is_likely_real_monster = any(mt in meta for mt in real_monster_types)

        if not is_likely_real_monster:
            ground_hazards = [
                "Explosive", "Trap", "Mine",
                "/Clone/", "Immobile",
            ]
            if any(hazard in meta for hazard in ground_hazards):
                return True

        return False

    def _is_breakable_prop(self, meta: str) -> bool:
        """Check if chest metadata is a breakable prop (urn, vase, etc)."""
        breakables = ["Urn", "Vase", "Pot", "Barrel", "Breakable", "Crate"]
        return any(b in meta for b in breakables)

    def _is_terrain_clutter(self, meta: str) -> bool:
        """Check if entity is terrain clutter (boulders, rocks, decorations)."""
        clutter = [
            "Doodad", "Boulder", "Rock", "Stone", "Debris",
            "Plant", "Tree", "Bush", "Grass", "Flower",
            "Fence", "Pillar", "Column", "Statue",
            "Torch", "Fire", "Light", "Glow",
            "Effect", "Particle", "Ambient",
            "RitualRune",  # Skip ritual ground markers
            "League",      # Skip league mechanic markers
            "Waypoint",    # Skip waypoints (usually just clutter on radar)
        ]
        return any(c in meta for c in clutter)

    def _read_entity_metadata(self, entity_ptr: int) -> str:
        """Read entity's metadata path from EntityDetails."""
        details = self._read_ptr(entity_ptr + Poe2Offsets.Entity.ENTITY_DETAILS_PTR)
        if not details:
            return ""

        return self._read_std_wstring(details + Poe2Offsets.EntityDetails.NAME)

    def _read_std_wstring(self, addr: int) -> str:
        """Read a std::wstring (UTF-16 string) from memory."""
        if not addr:
            return ""

        # std::wstring layout: data ptr or inline buffer, size at +0x10
        length = self._read_int(addr + 0x10)
        if not length or length <= 0 or length > 1024:
            return ""

        # If length <= 7, string is inline at addr; else ptr at addr
        if length <= 7:
            str_ptr = addr
        else:
            str_ptr = self._read_ptr(addr)
            if not str_ptr:
                return ""

        data = self._read_bytes(str_ptr, length * 2)  # UTF-16 = 2 bytes per char
        if not data:
            return ""

        try:
            return data.decode('utf-16-le').rstrip('\x00')
        except:
            return ""

    def _read_entity_hp(self, entity_ptr: int) -> Tuple[int, int]:
        """Read entity HP from Life component."""
        if entity_ptr in self._life_addr_cache:
            life = self._life_addr_cache[entity_ptr]
        else:
            life = self._resolve_component(entity_ptr, "Life")
            self._life_addr_cache[entity_ptr] = life

        if not life:
            return (0, 0)

        # Read VitalStruct at Life + HEALTH
        data = self._read_bytes(life + Poe2Offsets.Life.HEALTH, 52)  # VitalStruct size
        if not data or len(data) < 52:
            return (0, 0)

        max_hp = struct.unpack_from('<i', data, Poe2Offsets.VitalStruct.MAX)[0]
        cur_hp = struct.unpack_from('<i', data, Poe2Offsets.VitalStruct.CURRENT)[0]

        return (cur_hp, max_hp)

    def _read_entity_rarity(self, entity_ptr: int) -> Rarity:
        """Read entity rarity from ObjectMagicProperties component."""
        if entity_ptr in self._rarity_cache:
            return self._rarity_cache[entity_ptr]

        if entity_ptr in self._omp_addr_cache:
            omp = self._omp_addr_cache[entity_ptr]
        else:
            omp = self._resolve_component(entity_ptr, "ObjectMagicProperties")
            self._omp_addr_cache[entity_ptr] = omp

        if not omp:
            self._rarity_cache[entity_ptr] = Rarity.NORMAL
            return Rarity.NORMAL

        rarity_val = self._read_int(omp + Poe2Offsets.ObjectMagicProperties.RARITY)
        if rarity_val is None or rarity_val < 0 or rarity_val > 3:
            rarity_val = 0

        rarity = Rarity(rarity_val)
        self._rarity_cache[entity_ptr] = rarity
        return rarity

    def _read_entity_reaction(self, entity_ptr: int) -> int:
        """Read entity reaction/hostility from Positioned component."""
        if entity_ptr in self._pos_addr_cache:
            pos = self._pos_addr_cache[entity_ptr]
        else:
            pos = self._resolve_component(entity_ptr, "Positioned")
            self._pos_addr_cache[entity_ptr] = pos

        if not pos:
            return 0

        data = self._read_bytes(pos + Poe2Offsets.Positioned.REACTION, 1)
        if not data:
            return 0

        return data[0]

    def _read_chest_opened(self, entity_ptr: int) -> bool:
        """Read whether a chest has been opened from Chest component.

        As per Poe2Offsets.cs: Chest +0x168 is 0 while closed, non-zero once opened.
        """
        # Cache key for chest component
        cache_key = f"chest_{entity_ptr}"
        if cache_key not in self._render_addr_cache:
            chest = self._resolve_component(entity_ptr, "Chest")
            self._render_addr_cache[cache_key] = chest
        else:
            chest = self._render_addr_cache.get(cache_key)

        if not chest:
            return False

        data = self._read_bytes(chest + Poe2Offsets.ChestComponent.OPEN_STATE, 1)
        if not data:
            return False

        return data[0] != 0

    def _entity_resolves(self, entity_ptr: int) -> bool:
        """Return True if entity_ptr exposes a valid, non-empty component table.

        Used to distinguish a real entity from the 0.5.4 wrapper pointer (which
        has no usable EntityDetails/ComponentLookUp/ComponentList).
        """
        if not entity_ptr or entity_ptr < 0x10000:
            return False
        details = self._read_ptr(entity_ptr + Poe2Offsets.Entity.ENTITY_DETAILS_PTR)
        if not details or details < 0x10000:
            return False
        lookup = self._read_ptr(details + Poe2Offsets.EntityDetails.COMPONENT_LOOKUP_PTR)
        if not lookup or lookup < 0x10000:
            return False
        comp_list = self._read_std_vector(entity_ptr + Poe2Offsets.Entity.COMPONENT_LIST)
        if not comp_list:
            return False
        comp_count = comp_list[1] // 8
        return 0 < comp_count <= 256

    def _real_entity(self, entity_ptr: int) -> Optional[int]:
        """Return the pointer whose components resolve, tolerating indirection.

        Pre-0.5.4 the entity pointer is used directly. Patch 0.5.4 wraps it, so
        the real entity is read_ptr(entity_ptr). This tries the pointer directly
        first and falls back to one dereference, caching the decision per raw
        pointer (mirrors the flask bot's Life-component fix).
        """
        if not entity_ptr or entity_ptr < 0x10000:
            return None
        cached = self._real_entity_cache.get(entity_ptr)
        if cached is not None:
            return cached
        real = entity_ptr
        if not self._entity_resolves(entity_ptr):
            deref = self._read_ptr(entity_ptr)
            if deref and deref > 0x10000 and self._entity_resolves(deref):
                real = deref
        self._real_entity_cache[entity_ptr] = real
        return real

    def _resolve_component(self, entity_ptr: int, component_name: str) -> Optional[int]:
        """Resolve a component address by name via EntityDetails -> ComponentLookUp."""
        details = self._read_ptr(entity_ptr + Poe2Offsets.Entity.ENTITY_DETAILS_PTR)
        if not details:
            return None

        lookup = self._read_ptr(details + Poe2Offsets.EntityDetails.COMPONENT_LOOKUP_PTR)
        if not lookup:
            return None

        # Read component list vector
        comp_list = self._read_std_vector(entity_ptr + Poe2Offsets.Entity.COMPONENT_LIST)
        if not comp_list:
            return None
        comp_list_begin, comp_list_size = comp_list
        comp_count = comp_list_size // 8
        if comp_count <= 0 or comp_count > 256:
            return None

        # Read bucket (StdVector-like structure)
        bucket_begin = self._read_ptr(lookup + Poe2Offsets.ComponentLookUp.NAME_AND_INDEX_BUCKET)
        bucket_end = self._read_ptr(lookup + Poe2Offsets.ComponentLookUp.NAME_AND_INDEX_BUCKET + 8)

        if not bucket_begin or not bucket_end or bucket_end <= bucket_begin:
            return None

        num_entries = (bucket_end - bucket_begin) // Poe2Offsets.ComponentLookUp.ENTRY_STRIDE
        if num_entries <= 0 or num_entries > 256:
            return None

        # Search for component by name
        # Note: Component names are UTF-8 strings, not UTF-16!
        for i in range(num_entries):
            entry_addr = bucket_begin + i * Poe2Offsets.ComponentLookUp.ENTRY_STRIDE
            name_ptr = self._read_ptr(entry_addr)
            if not name_ptr:
                continue

            name = self._read_utf8_string(name_ptr, 32)
            if name == component_name:
                index = self._read_int(entry_addr + 8)
                if index is not None and 0 <= index < comp_count:
                    return self._read_ptr(comp_list_begin + index * 8)

        return None

    def _read_utf8_string(self, addr: int, max_length: int = 64) -> str:
        """Read a null-terminated UTF-8 string from memory."""
        if not addr:
            return ""
        data = self._read_bytes(addr, max_length)
        if not data:
            return ""
        try:
            # Find null terminator
            null_idx = data.find(b'\x00')
            if null_idx >= 0:
                data = data[:null_idx]
            return data.decode('utf-8', errors='ignore')
        except:
            return ""


# ============================================================================
# Test function - run this to verify terrain reading works
# ============================================================================
def test_terrain_reader():
    """Test terrain reading functionality."""
    import time

    logging.basicConfig(level=logging.DEBUG)

    print("=" * 60)
    print("POE2 Terrain Reader Test")
    print("=" * 60)
    print("\nMake sure POE2 is running and you're in a zone.\n")

    reader = TerrainReader("steam")

    print("Connecting to game...")
    if not reader.connect():
        print("ERROR: Could not connect to PathOfExileSteam.exe")
        print("Make sure the game is running and you have admin rights.")
        return

    print(f"Connected! Base: 0x{reader.base_address:X}")

    print("\nSearching for InGameState...")
    igs = reader.find_ingame_state()
    if igs:
        print(f"Found InGameState at: 0x{igs:X}")
    else:
        print("Could not find InGameState - trying GameState pattern...")
        gs = reader.find_game_state()
        if gs:
            print(f"Found GameState slot at: 0x{gs:X}")
        else:
            print("Could not find GameState either.")
            print("\nThe AOB patterns might need updating for this game version.")
            return

    print("\nReading terrain data...")
    terrain = reader.read_terrain()

    if terrain:
        print(f"\n✓ SUCCESS! Terrain data read:")
        print(f"  Tiles: {terrain.tiles_x} x {terrain.tiles_y}")
        print(f"  Grid: {terrain.width} x {terrain.height} cells")
        print(f"  Bytes per row: {terrain.bytes_per_row}")
        print(f"  Grid data size: {len(terrain.walkable_grid)} bytes")

        # Count walkable cells (sample)
        walkable = 0
        blocked = 0
        for y in range(0, terrain.height, 10):  # Sample every 10th row
            for x in range(0, terrain.width, 10):
                if terrain.is_walkable(x, y):
                    walkable += 1
                else:
                    blocked += 1

        print(f"  Sample walkability: {walkable} walkable, {blocked} blocked")
    else:
        print("ERROR: Could not read terrain data")

    # Test player position
    print("\nReading player position...")
    player_grid = reader.get_player_grid_position()
    if player_grid:
        gx, gy = player_grid
        print(f"✓ Player at grid ({gx:.1f}, {gy:.1f})")
        if terrain:
            # Check if player position is within terrain bounds
            if 0 <= gx < terrain.width and 0 <= gy < terrain.height:
                walkable = terrain.is_walkable(int(gx), int(gy))
                print(f"  At grid position: {'walkable' if walkable else 'blocked'}")
            else:
                print(f"  WARNING: Player position outside terrain bounds")
    else:
        print("  Could not read player position (component lookup not implemented yet)")

    # Test map visibility
    print("\nChecking map visibility...")
    print("Press Tab in-game to toggle the map, then check output.")
    for i in range(5):
        map_state = reader.get_map_state()
        if map_state:
            is_visible, shift_x, shift_y, zoom = map_state
            print(f"  Map visible: {is_visible}, shift=({shift_x:.1f}, {shift_y:.1f}), zoom={zoom:.2f}")
        else:
            print("  Could not read map state")
        time.sleep(1)

    reader.disconnect()
    print("\nDone!")


if __name__ == "__main__":
    test_terrain_reader()
