"""
Unit tests for flask_bot StructureReader's self-healing Life-vital detection.

These exercise the pure byte-level layout logic (no live game needed): the
detector must find the correct HEALTH base among known layouts inside a Life
component, so a base flip-flop (0x160 <-> 0x1B0) heals itself across patches.
"""

import struct

import sys
sys.path.insert(0, '..')
from flask_bot import StructureReader


def _reader() -> StructureReader:
    """A StructureReader that is never connected (pure-logic tests only)."""
    return StructureReader("steam", None)


def _place_vital(buf: bytearray, base: int, cur: int, mx: int,
                 o: "StructureReader.Offsets") -> None:
    struct.pack_into('<i', buf, base + o.VITAL_MAX, mx)
    struct.pack_into('<i', buf, base + o.VITAL_CURRENT, cur)


def _build_life(reader: StructureReader, health_base: int,
                hp=(54, 64), mp=(60, 60), es=(0, 0), vtable=0) -> bytes:
    """Build a synthetic Life component with vitals at the given HEALTH base."""
    o = reader.Offsets
    size = reader._life_block_size() + 0x40
    buf = bytearray(size)
    struct.pack_into('<Q', buf, 0, vtable)
    _place_vital(buf, health_base, hp[0], hp[1], o)
    _place_vital(buf, health_base + reader.VITAL_MANA_FROM_HEALTH, mp[0], mp[1], o)
    _place_vital(buf, health_base + reader.VITAL_ES_FROM_HEALTH, es[0], es[1], o)
    return bytes(buf)


class TestVitalTriple:
    def test_reads_current_and_max_at_base(self):
        r = _reader()
        data = _build_life(r, 0x1B0, hp=(54, 64), mp=(60, 60))
        (hp, mp, es) = r._vital_triple(data, 0x1B0)
        assert hp == (54, 64)
        assert mp == (60, 60)

    def test_returns_none_when_block_too_short(self):
        r = _reader()
        assert r._vital_triple(b"\x00" * 8, 0x1B0) is None


class TestTripleIsValid:
    def test_accepts_living_player(self):
        assert StructureReader._triple_is_valid(((54, 64), (60, 60), (0, 0)))

    def test_rejects_zero_hp_max(self):
        assert not StructureReader._triple_is_valid(((0, 0), (60, 60), (0, 0)))

    def test_rejects_no_secondary_resource(self):
        # HP-max in range but no Mana and no ES -> reject (pointer-highdword trap)
        assert not StructureReader._triple_is_valid(((1, 32758), (0, 0), (0, 0)))

    def test_rejects_current_over_max(self):
        assert not StructureReader._triple_is_valid(((99, 64), (60, 60), (0, 0)))


class TestDetectHealthBase:
    def test_detects_primary_layout(self):
        r = _reader()
        data = _build_life(r, 0x1B0)
        assert r._detect_health_base(data, None) == 0x1B0

    def test_detects_alternate_layout(self):
        r = _reader()
        data = _build_life(r, 0x160)
        assert r._detect_health_base(data, None) == 0x160

    def test_rejects_when_vtable_outside_module(self):
        r = _reader()
        data = _build_life(r, 0x1B0, vtable=0x1234)
        # module bounds that exclude the vtable -> not a real component
        assert r._detect_health_base(data, (0x7FF600000000, 0x7FF700000000)) is None

    def test_accepts_when_vtable_inside_module(self):
        r = _reader()
        mod = (0x7FF600000000, 0x7FF700000000)
        data = _build_life(r, 0x1B0, vtable=0x7FF6DD265E88)
        assert r._detect_health_base(data, mod) == 0x1B0


class TestHealthBaseCandidates:
    def test_config_override_is_tried_first(self):
        r = _reader()
        r.Offsets.HEALTH = 0x300  # simulate config override
        try:
            cands = r._health_base_candidates()
            assert cands[0] == 0x300
            assert 0x1B0 in cands and 0x160 in cands
        finally:
            r.Offsets.HEALTH = 0x1B0  # restore class attr for other tests


class TestResolveVitalBaseSelfHeal:
    def test_stale_cache_reheals_to_correct_base(self):
        r = _reader()
        # Live memory is the 0x1B0 layout...
        data = _build_life(r, 0x1B0)
        r._read_bytes = lambda addr, size: data           # type: ignore
        r._module_bounds = lambda: None                    # type: ignore
        # ...but a stale detected base from a previous layout is cached.
        r._vital_health_base = 0x160
        assert r._resolve_vital_base(0xDEAD) == 0x1B0
        assert r._vital_health_base == 0x1B0

    def test_returns_none_for_unknown_layout(self):
        r = _reader()
        # Vitals sit at an offset not in KNOWN_HEALTH_BASES -> no detection.
        data = _build_life(r, 0x120)
        r._read_bytes = lambda addr, size: data           # type: ignore
        r._module_bounds = lambda: None                    # type: ignore
        assert r._resolve_vital_base(0xDEAD) is None
