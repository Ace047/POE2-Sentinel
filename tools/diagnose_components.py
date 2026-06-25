"""
Component-lookup discovery tool for POE2 Sentinel.

When a patch breaks structure-mode HP/Mana reading at the "resolve Life
component by name" step, the component-lookup chain offsets have shifted:

    player +ENTITY_DETAILS_PTR -> details +COMPONENT_LOOKUP_PTR
          -> lookup +NAME_AND_INDEX_BUCKET -> entries[] (stride ENTRY_STRIDE)

Each entry is {name_ptr (qword), index (int)}. This tool does a breadth-first
walk of the pointer graph from the player entity and, at every reachable
struct, checks whether it is the entries array by dereferencing its slots and
matching them against real PoE component name strings (Life, Positioned,
Render, ...). When found, it reconstructs the exact offset chain back to the
player so the offsets can be applied to flask_bot.py / config.json.

Usage (POE2 running, in-game, terminal as Administrator), from repo root:
    py tools/diagnose_components.py
"""

import os
import sys
import string
import struct
import logging
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

# This script lives in tools/; make the repo root importable for flask_bot.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_bot import load_config, StructureReader  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Real PoE component names used to anchor the search (a player entity exposes
# many of these). "Life" must be present for a positive bucket match.
KNOWN_NAMES = frozenset({
    "Life", "Positioned", "Render", "Actor", "Animated", "Stats", "Buffs",
    "Player", "Targetable", "Pathfinding", "Mods", "ObjectMagicProperties",
    "BaseEvents", "TriggerableBlockage", "Functions", "DiesAfterTime",
})

PTR_MIN = 0x10000
PTR_MAX = 0x7FFFFFFFFFFF
WINDOW = 0xC0          # bytes scanned per struct for child pointers (24 qwords)
MAX_VISITED = 40000    # safety cap on graph nodes
MAX_DEPTH = 5          # natural bucket depth is 3; allow for extra hops
STRIDES = (0x10, 0x18, 0x20)
MAX_ENTRIES = 96       # max entries to read while measuring an array
MIN_NAMELIKE = 4       # min consecutive namelike entries to accept a bucket
SCAN_BYTES = 0x600     # Life component window for vital value-finding


def _plausible(ptr: Optional[int]) -> bool:
    """True if the value looks like a user-space heap pointer."""
    return ptr is not None and PTR_MIN < ptr < PTR_MAX


def _namelike(s: str) -> bool:
    """True if the string looks like a PoE component name token."""
    if not (2 <= len(s) <= 30):
        return False
    if s[0] not in string.ascii_uppercase:
        return False
    return all(c in (string.ascii_letters + string.digits) for c in s)


def _detect_entries(reader: StructureReader, base: int
                    ) -> Optional[Tuple[int, List[Tuple[int, str]]]]:
    """Check if `base` is a component name/index entries array.

    Returns (stride, [(entry_index, name), ...]) for the best matching stride,
    or None. Cheap fast-reject: the first qword of a real entries array points
    to a component name string.
    """
    first = reader._read_ptr(base)
    if not _plausible(first) or not _namelike(reader._read_utf8_string(first, 32)):
        return None

    best: Optional[Tuple[int, List[Tuple[int, str]]]] = None
    for stride in STRIDES:
        names: List[Tuple[int, str]] = []
        for i in range(MAX_ENTRIES):
            name_ptr = reader._read_ptr(base + i * stride)
            if not _plausible(name_ptr):
                break
            s = reader._read_utf8_string(name_ptr, 32)
            if not _namelike(s):
                break
            names.append((i, s))
        has_life = any(n == "Life" for _, n in names)
        known = sum(1 for _, n in names if n in KNOWN_NAMES)
        if len(names) >= MIN_NAMELIKE and has_life and known >= 2:
            if best is None or len(names) > len(best[1]):
                best = (stride, names)
    return best


def _walk_graph(reader: StructureReader, player: int
                ) -> Tuple[Dict[int, Tuple[Optional[int], int]],
                           List[Tuple[int, int, List[Tuple[int, str]]]]]:
    """BFS the pointer graph from `player`, locating entries arrays.

    Returns (came_from, hits) where came_from[addr] = (parent, offset) and
    hits = [(entries_base, stride, names), ...].
    """
    came_from: Dict[int, Tuple[Optional[int], int]] = {player: (None, 0)}
    depth: Dict[int, int] = {player: 0}
    hits: List[Tuple[int, int, List[Tuple[int, str]]]] = []
    seen_bases: set = set()
    queue: Deque[int] = deque([player])

    while queue and len(came_from) < MAX_VISITED:
        node = queue.popleft()

        found = _detect_entries(reader, node)
        if found and node not in seen_bases:
            seen_bases.add(node)
            hits.append((node, found[0], found[1]))

        if depth[node] >= MAX_DEPTH:
            continue
        window = reader._read_bytes(node, WINDOW)
        if not window:
            continue
        for off in range(0, len(window) - 8 + 1, 8):
            child = struct.unpack_from("<Q", window, off)[0]
            if not _plausible(child) or child in came_from:
                continue
            came_from[child] = (node, off)
            depth[child] = depth[node] + 1
            queue.append(child)

    return came_from, hits


def _chain(came_from: Dict[int, Tuple[Optional[int], int]], addr: int
           ) -> List[Tuple[int, int]]:
    """Reconstruct the (parent_addr, offset) path from player down to addr."""
    path: List[Tuple[int, int]] = []
    while addr in came_from and came_from[addr][0] is not None:
        parent, off = came_from[addr]
        path.append((parent, off))
        addr = parent
    path.reverse()
    return path


def _life_entry_index(reader: StructureReader, base: int, stride: int,
                      names: List[Tuple[int, str]]) -> Optional[int]:
    """Read the component index stored alongside the 'Life' name entry."""
    for i, n in names:
        if n == "Life":
            return reader._read_int(base + i * stride + 8)
    return None


def _find_component_list(reader: StructureReader, player: int, life_index: int
                         ) -> Optional[Tuple[int, int, int]]:
    """Locate the entity's component vector and resolve the Life component.

    Returns (component_list_offset, component_count, life_component_addr).
    """
    for off in range(0, 0x100, 8):
        begin = reader._read_ptr(player + off)
        end = reader._read_ptr(player + off + 8)
        if not _plausible(begin) or not _plausible(end):
            continue
        if end <= begin or (end - begin) % 8 != 0:
            continue
        count = (end - begin) // 8
        if not (life_index < count <= 256):
            continue
        comp = reader._read_ptr(begin + life_index * 8)
        if _plausible(comp):
            return off, count, comp
    return None


def _prompt_int(label: str) -> Optional[int]:
    """Prompt for an integer; blank/invalid returns None (skip)."""
    raw = input(f"  {label}: ").strip()
    return int(raw) if raw.isdigit() else None


def _find_pairs(data: bytes, current: int, maximum: int, delta: int) -> List[int]:
    """Find offsets where int32==maximum and int32 at +delta==current."""
    hits: List[int] = []
    for off in range(0, len(data) - 4, 4):
        if struct.unpack_from("<i", data, off)[0] != maximum:
            continue
        cur_off = off + delta
        if 0 <= cur_off <= len(data) - 4 and \
                struct.unpack_from("<i", data, cur_off)[0] == current:
            hits.append(off)
    return hits


def _report_resource(name: str, data: bytes, current: int, maximum: int,
                     o: "StructureReader.Offsets") -> None:
    """Search for a resource's vital struct and print derived base offset."""
    delta = o.VITAL_CURRENT - o.VITAL_MAX
    hits = _find_pairs(data, current, maximum, delta)
    if not hits:
        print(f"  [{name}] no (max={maximum}, current={current}) pair found. "
              f"Make sure {name} is NOT full and the values are exact.")
        return
    print(f"  [{name}] found {len(hits)} candidate(s):")
    for max_off in hits:
        base = max_off - o.VITAL_MAX
        print(f"     vital base offset = 0x{base:X}  "
              f"(MAX at 0x{max_off:X}, CURRENT at 0x{max_off + delta:X})")


def _print_chain(chain: List[Tuple[int, int]], stride: int) -> None:
    """Print the offset chain, labelling the final 3 hops (entity -> bucket).

    The last three hops are always ENTITY_DETAILS_PTR, COMPONENT_LOOKUP_PTR and
    NAME_AND_INDEX_BUCKET (relative to the entity). Any leading hops are extra
    player->entity indirection introduced by the patch.
    """
    tail = {len(chain) - 3: "ENTITY_DETAILS_PTR",
            len(chain) - 2: "COMPONENT_LOOKUP_PTR",
            len(chain) - 1: "NAME_AND_INDEX_BUCKET"}
    print(f"  hops from player: {len(chain)}")
    for i, (parent, off) in enumerate(chain):
        label = tail.get(i, f"player-indirection hop{i}")
        print(f"    {label:<22} = 0x{off:X}   (from 0x{parent:X})")
    print(f"    ENTRY_STRIDE           = 0x{stride:X}")


def main() -> int:
    cfg = load_config()
    reader = StructureReader(cfg.get("game_version", "steam"), cfg)
    o = reader.Offsets

    print("=" * 60)
    print(" POE2 Sentinel - Component Lookup Discovery")
    print("=" * 60)

    if not reader.connect():
        print("FAIL: could not attach. Is POE2 running, and is this tool run "
              "as Administrator?")
        return 1
    if not reader._find_game_state_slot():
        print("FAIL: GameState AOB pattern not found (signature changed).")
        return 1
    player = reader._find_local_player()
    if not player:
        print("FAIL: local player not found (AREA_INSTANCE_DATA / LOCAL_PLAYER "
              "changed).")
        return 1
    print(f"OK: local player at 0x{player:X}\n")

    print("Walking the pointer graph for the component name bucket containing "
          "'Life' ...")
    came_from, hits = _walk_graph(reader, player)
    if not hits:
        print("\nNo entries array containing 'Life' was reachable within "
              f"{MAX_DEPTH} hops. The component map type itself likely changed "
              "(e.g. array -> hashmap). Paste this output to me.")
        reader.disconnect()
        return 1

    # Prefer the natural 3-hop chain; otherwise shortest chain with most names.
    hits.sort(key=lambda h: (abs(len(_chain(came_from, h[0])) - 3),
                             -len(h[2])))
    base, stride, names = hits[0]
    chain = _chain(came_from, base)
    life_index = _life_entry_index(reader, base, stride, names)

    print(f"\nFOUND entries array at 0x{base:X}")
    _print_chain(chain, stride)
    sample = ", ".join(n for _, n in names[:16])
    print(f"  'Life' index           = {life_index}")
    print(f"  {len(names)} names: {sample}{' ...' if len(names) > 16 else ''}")
    if len(hits) > 1:
        print(f"  ({len(hits)} candidate arrays found; showing best match)")

    if life_index is None:
        print("\nCould not read the Life index; paste the output above to me.")
        reader.disconnect()
        return 0

    # The entity that owns the lookup is 3 hops above the bucket. If that is not
    # `player`, the patch added a player->entity indirection.
    entity = chain[-3][0] if len(chain) >= 3 else player
    if entity != player:
        lead_off = chain[0][1]
        print(f"\n  NOTE: real entity is at 0x{entity:X} = read_ptr(player+"
              f"0x{lead_off:X}). The bot must dereference the player pointer "
              "once more before resolving components.")

    cl = _find_component_list(reader, entity, life_index)
    if not cl:
        print("\nCould not auto-locate the component vector (COMPONENT_LIST) "
              f"relative to entity 0x{entity:X}. Paste the output above.")
        reader.disconnect()
        return 0
    cl_off, cl_count, life = cl
    print(f"  COMPONENT_LIST         = 0x{cl_off:X}  (count={cl_count})")
    print(f"  Life component at 0x{life:X}")

    hp = reader._read_vital_struct(life, o.HEALTH)
    mp = reader._read_vital_struct(life, o.MANA)
    es = reader._read_vital_struct(life, o.ENERGY_SHIELD)
    print("\nValues with CURRENT vital offsets (current/max):")
    print(f"  HP : {hp[0]}/{hp[1]}   MP : {mp[0]}/{mp[1]}   ES : {es[0]}/{es[1]}")

    if 0 < hp[1] <= 50000:
        print("\nVital offsets still look correct -- only the component-lookup "
              "offsets above need updating.")
        reader.disconnect()
        return 0

    print("\nVital offsets appear stale too. Stand still, then enter the EXACT "
          "in-game numbers (leave blank to skip).")
    print(" Health:")
    hp_cur, hp_max = _prompt_int("current HP"), _prompt_int("max HP")
    print(" Mana:")
    mp_cur, mp_max = _prompt_int("current Mana"), _prompt_int("max Mana")
    data = reader._read_bytes(life, SCAN_BYTES)
    if data:
        print("\n--- Derived vital offsets ---")
        if hp_cur is not None and hp_max:
            _report_resource("HEALTH", data, hp_cur, hp_max, o)
        if mp_cur is not None and mp_max:
            _report_resource("MANA", data, mp_cur, mp_max, o)

    print("\nPaste the full output to me and I'll update flask_bot.py / "
          "config.json.")
    reader.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
