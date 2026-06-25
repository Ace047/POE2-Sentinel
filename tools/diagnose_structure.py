"""
Structure-mode diagnostic & offset finder for POE2 Sentinel.

Run this against the LIVE game (as Administrator) after a patch breaks
structure-mode HP/Mana detection. It reuses the real StructureReader code
path to locate the Life component, reports exactly where reading breaks,
then scans the component memory for the HP/Mana values you read off the
in-game UI to derive the correct offsets.

Usage (from repo root):
    py tools/diagnose_structure.py
"""

import os
import sys
import struct
import logging
from typing import Dict, List, Optional, Tuple

# This script lives in tools/; make the repo root importable for flask_bot.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_bot import load_config, StructureReader  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# How many bytes of the Life component to scan when searching for values.
SCAN_BYTES = 0x600


def _prompt_int(label: str) -> Optional[int]:
    """Prompt for an integer; blank/invalid returns None (skip)."""
    raw = input(f"  {label}: ").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _find_pairs(data: bytes, current: int, maximum: int,
                vital_current_minus_max: int) -> List[int]:
    """Find offsets where int32==maximum and int32 at +delta==current.

    Returns a list of offsets (relative to Life component) where the MAX
    field sits. delta is VITAL_CURRENT - VITAL_MAX (normally 4).
    """
    hits: List[int] = []
    for off in range(0, len(data) - 4, 4):
        if struct.unpack_from("<i", data, off)[0] != maximum:
            continue
        cur_off = off + vital_current_minus_max
        if 0 <= cur_off <= len(data) - 4:
            if struct.unpack_from("<i", data, cur_off)[0] == current:
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
              f"(MAX field at 0x{max_off:X}, CURRENT at 0x{max_off + delta:X})")


def main() -> int:
    cfg = load_config()
    reader = StructureReader(cfg.get("game_version", "steam"), cfg)
    o = reader.Offsets

    print("=" * 60)
    print(" POE2 Sentinel - Structure Mode Diagnostic")
    print("=" * 60)

    if not reader.connect():
        print("FAIL: could not attach to the game process. "
              "Is POE2 running and is this tool run as Administrator?")
        return 1
    print(f"OK: attached at base 0x{reader.base_address:X}")

    slot = reader._find_game_state_slot()
    if not slot:
        print("FAIL: GameState AOB pattern not found (the signature itself "
              "changed this patch). Offset finder cannot continue.")
        return 1
    print(f"OK: GameState slot at 0x{slot:X}")

    player = reader._find_local_player()
    if not player:
        print("FAIL: local player not found. AREA_INSTANCE_DATA "
              f"(0x{o.AREA_INSTANCE_DATA:X}) or LOCAL_PLAYER "
              f"(0x{o.LOCAL_PLAYER:X}) likely changed this patch.")
        return 1
    print(f"OK: local player entity at 0x{player:X}")

    life = reader._resolve_life_component(player)
    if not life:
        print("FAIL: Life component not found by name. The component-lookup "
              "struct offsets changed this patch.")
        return 1
    print(f"OK: Life component at 0x{life:X}")

    # Show what the CURRENT (possibly broken) offsets read.
    hp = reader._read_vital_struct(life, o.HEALTH)
    mp = reader._read_vital_struct(life, o.MANA)
    es = reader._read_vital_struct(life, o.ENERGY_SHIELD)
    print("\nValues with CURRENT offsets (current/max):")
    print(f"  HP : {hp[0]}/{hp[1]}   (HEALTH=0x{o.HEALTH:X})")
    print(f"  MP : {mp[0]}/{mp[1]}   (MANA=0x{o.MANA:X})")
    print(f"  ES : {es[0]}/{es[1]}   (ENERGY_SHIELD=0x{o.ENERGY_SHIELD:X})")

    print("\nStand still (e.g. in your hideout) so the values stay stable,")
    print("then enter the EXACT numbers shown on your in-game UI.")
    print("If a resource is full, current==max and you may get a few")
    print("candidates - pick the offset that lines up across HP/Mana.")
    print("Leave a line blank to skip it.\n")
    print(" Health:")
    hp_cur, hp_max = _prompt_int("current HP"), _prompt_int("max HP")
    print(" Mana:")
    mp_cur, mp_max = _prompt_int("current Mana"), _prompt_int("max Mana")
    print(" Energy Shield (optional):")
    es_cur, es_max = _prompt_int("current ES"), _prompt_int("max ES")

    # Snapshot the component AFTER input so memory matches the on-screen UI.
    data = reader._read_bytes(life, SCAN_BYTES)
    if not data:
        print("FAIL: could not read Life component memory for scanning.")
        return 1

    print("\n--- Derived offsets ---")
    if hp_cur is not None and hp_max:
        _report_resource("HEALTH", data, hp_cur, hp_max, o)
    if mp_cur is not None and mp_max:
        _report_resource("MANA", data, mp_cur, mp_max, o)
    if es_cur is not None and es_max:
        _report_resource("ENERGY_SHIELD", data, es_cur, es_max, o)

    print("\nPaste the chosen base offsets back to me and I'll update "
          "config.json (structure_offsets) / flask_bot.py.")
    reader.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
