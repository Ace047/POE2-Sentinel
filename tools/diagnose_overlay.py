"""Live diagnostic for the entity/terrain overlay (POE2 0.5.4+).

Pins down two suspected 0.5.4 breakages in terrain_reader.py:
  1. Player/entity component resolution may need the extra read_ptr(entity)
     indirection (the same fix already applied to the flask bot's Life lookup).
  2. The TerrainStruct may have moved off AreaInstance + 0x8A0, so the
     GridWalkableData pointer reads as null.

Read-only. Run with POE2 open and standing in a zone:
    py tools/diagnose_overlay.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terrain_reader import TerrainReader, Poe2Offsets  # noqa: E402

O = Poe2Offsets


def list_components(reader: TerrainReader, entity_ptr: int):
    """Return (ok, comp_count, names) for an entity pointer's component table."""
    if not entity_ptr or entity_ptr < 0x10000:
        return False, 0, []
    details = reader._read_ptr(entity_ptr + O.Entity.ENTITY_DETAILS_PTR)
    if not details or details < 0x10000:
        return False, 0, []
    lookup = reader._read_ptr(details + O.EntityDetails.COMPONENT_LOOKUP_PTR)
    if not lookup or lookup < 0x10000:
        return False, 0, []
    comp_list = reader._read_std_vector(entity_ptr + O.Entity.COMPONENT_LIST)
    if not comp_list:
        return False, 0, []
    comp_count = comp_list[1] // 8
    if comp_count <= 0 or comp_count > 256:
        return False, 0, []
    b_begin = reader._read_ptr(lookup + O.ComponentLookUp.NAME_AND_INDEX_BUCKET)
    b_end = reader._read_ptr(lookup + O.ComponentLookUp.NAME_AND_INDEX_BUCKET + 8)
    if not b_begin or not b_end or b_end <= b_begin:
        return False, comp_count, []
    n = (b_end - b_begin) // O.ComponentLookUp.ENTRY_STRIDE
    if n <= 0 or n > 256:
        return False, comp_count, []
    names = []
    for i in range(n):
        name_ptr = reader._read_ptr(b_begin + i * O.ComponentLookUp.ENTRY_STRIDE)
        if name_ptr:
            nm = reader._read_utf8_string(name_ptr, 32)
            if nm:
                names.append(nm)
    return (len(names) > 0), comp_count, names


def probe_entity(reader: TerrainReader, label: str, raw_ptr: int):
    """Test component resolution directly and one deref deeper."""
    print(f"\n[{label}] raw_ptr=0x{raw_ptr:X}")
    direct_ok, dc, dnames = list_components(reader, raw_ptr)
    deref = reader._read_ptr(raw_ptr)
    deref_ok, ic, inames = (False, 0, [])
    if deref:
        deref_ok, ic, inames = list_components(reader, deref)
    print(f"  direct        -> ok={direct_ok} count={dc} names={dnames[:16]}")
    print(f"  read_ptr=0x{deref or 0:X} -> ok={deref_ok} count={ic} names={inames[:16]}")
    if direct_ok:
        print("  => entity is DIRECT (no indirection needed)")
    elif deref_ok:
        print("  => entity needs INDIRECTION (read_ptr(entity))")
    else:
        print("  => UNRESOLVED both ways")
    return direct_ok, deref_ok


def scan_awake_map(reader: TerrainReader, area: int):
    """Search AreaInstance for the awake-entities std::map {head, size}."""
    print("\n=== Awake-entities map search ===")
    import struct as _s
    found = []
    for off in range(0x600, 0x800, 8):
        head = reader._read_ptr(area + off)
        size = reader._read_int(area + off + 8)
        if not head or head < 0x10000 or not size or size <= 0 or size > 100000:
            continue
        root = reader._read_ptr(head + O.StdMapNode.PARENT)
        if not root or root < 0x10000:
            continue
        # Count a few real (non-visual) entity nodes via limited BFS.
        queue, visited, reals = [root], set(), 0
        while queue and len(visited) < 2000:
            node = queue.pop(0)
            if not node or node == head or node in visited:
                continue
            visited.add(node)
            data = reader._read_bytes(node, 48)
            if not data or len(data) < 48 or data[O.StdMapNode.IS_NIL] != 0:
                continue
            eid = _s.unpack_from('<I', data, O.StdMapNode.KEY_ID)[0]
            eptr = _s.unpack_from('<Q', data, O.StdMapNode.VALUE_ENTITY_PTR)[0]
            for ofs in (O.StdMapNode.LEFT, O.StdMapNode.RIGHT):
                child = _s.unpack_from('<Q', data, ofs)[0]
                if child and child != head:
                    queue.append(child)
            if eptr and eid < O.EntityList.VISUAL_ID_THRESHOLD:
                reals += 1
        print(f"  CANDIDATE off=0x{off:X} head=0x{head:X} size={size} real_nodes~{reals}")
        found.append((off, size, reals))
    if not found:
        print("  No awake-map candidate found in 0x600..0x800 range.")
    return found


def scan_terrain(reader: TerrainReader, area: int):
    """Search AreaInstance for a TerrainStruct that yields a valid grid."""
    print("\n=== Terrain struct search ===")
    found = []
    for off in range(0x600, 0xC00, 8):
        for mode in ("embedded", "pointer"):
            if mode == "embedded":
                tbase = area + off
            else:
                tbase = reader._read_ptr(area + off)
            if not tbase or tbase < 0x10000:
                continue
            first = reader._read_ptr(tbase + O.Terrain.GRID_WALKABLE_DATA)
            last = reader._read_ptr(tbase + O.Terrain.GRID_WALKABLE_DATA + 8)
            if not first or not last or last <= first:
                continue
            size = last - first
            if size < 1024 or size > 64 * 1024 * 1024:
                continue
            bpr = reader._read_int(tbase + O.Terrain.BYTES_PER_ROW)
            if not bpr or bpr <= 0 or bpr > 65536:
                continue
            rows = size // bpr
            if rows <= 0 or rows > 65536:
                continue
            print(f"  CANDIDATE off=0x{off:X} ({mode}) tbase=0x{tbase:X} "
                  f"size={size} bpr={bpr} rows={rows} cells={bpr*2}x{rows}")
            found.append((off, mode))
    if not found:
        print("  No terrain candidate found in 0x600..0xC00 range.")
    return found


def main():
    reader = TerrainReader("steam")
    if not reader.connect():
        print("Could not connect to POE2. Run as Administrator with the game open.")
        return
    print(f"Connected. base=0x{reader.base_address:X}")

    igs = reader.find_ingame_state()
    if not igs:
        print("InGameState not found (are you in a zone?).")
        return
    print(f"InGameState=0x{igs:X}")

    area = reader.get_area_instance()
    if not area:
        print("AreaInstance not found.")
        return
    print(f"AreaInstance=0x{area:X}")

    # 1) Player entity at AreaInstance + 0x5A0
    player = reader._read_ptr(area + O.AreaInstance.LOCAL_PLAYER)
    if player:
        probe_entity(reader, "PLAYER (area+0x5A0)", player)

    # 2) A few awake entities from the std::map
    print("\n=== Awake entity probe (first few real entities) ===")
    map_addr = area + O.AreaInstance.AWAKE_ENTITIES
    head = reader._read_ptr(map_addr)
    size = reader._read_int(map_addr + 8)
    print(f"awake map head=0x{head or 0:X} size={size}")
    probed = 0
    if head and size and 0 < size < 100000:
        root = reader._read_ptr(head + O.StdMapNode.PARENT)
        queue, visited = [root], set()
        while queue and probed < 3 and len(visited) < 5000:
            node = queue.pop(0)
            if not node or node == head or node in visited:
                continue
            visited.add(node)
            data = reader._read_bytes(node, 48)
            if not data or len(data) < 48:
                continue
            if data[O.StdMapNode.IS_NIL] != 0:
                continue
            import struct as _s
            eid = _s.unpack_from('<I', data, O.StdMapNode.KEY_ID)[0]
            eptr = _s.unpack_from('<Q', data, O.StdMapNode.VALUE_ENTITY_PTR)[0]
            left = _s.unpack_from('<Q', data, O.StdMapNode.LEFT)[0]
            right = _s.unpack_from('<Q', data, O.StdMapNode.RIGHT)[0]
            if left and left != head:
                queue.append(left)
            if right and right != head:
                queue.append(right)
            if eptr == 0 or eid >= O.EntityList.VISUAL_ID_THRESHOLD:
                continue
            probe_entity(reader, f"ENTITY id={eid}", eptr)
            probed += 1

    # 3) Awake-entities map location
    scan_awake_map(reader, area)

    # 4) Terrain struct location
    scan_terrain(reader, area)

    reader.disconnect()
    print("\nDone.")


if __name__ == "__main__":
    main()
