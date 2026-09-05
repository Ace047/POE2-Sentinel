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


# Fake pointers must clear the real 0x10000 heap-plausibility guards.
_MOD = (0x7FF600000000, 0x7FF700000000)
_SLOT, _GS = 0x101000, 0x102000
_DECOY_IGS, _DECOY_AREA, _DECOY_PLAYER = 0x103000, 0x103100, 0x103200
_REAL_IGS, _REAL_AREA, _REAL_PLAYER = 0x104000, 0x104100, 0x104200
_NEW_IGS, _NEW_AREA, _NEW_PLAYER = 0x105000, 0x105100, 0x105200


class TestLooksLikePlayer:
    def test_true_when_player_component_present(self):
        r = _reader()
        r._entity_component_names = lambda ptr, limit=64: ["Life", "Player", "Stats"]  # type: ignore
        assert r._looks_like_player(0x10BEEF) is True

    def test_false_for_empty_component_bucket(self):
        # A decoy/transient entity resolves an empty bucket -> not the player.
        r = _reader()
        r._entity_component_names = lambda ptr, limit=64: []  # type: ignore
        assert r._looks_like_player(0x10BEEF) is False

    def test_false_for_null_pointer(self):
        assert _reader()._looks_like_player(0) is False


# Fake address space so _find_local_player / _player_from_igs run against a
# deterministic in-memory world (no live game). Mirrors the real chain:
# slot -> GameState -> STATES[i] (InGameState) -> AreaInstance -> LocalPlayer.
def _wire(r: StructureReader, mem: dict, entities: set, comps: dict) -> None:
    o = r.Offsets
    mem[_SLOT] = _GS
    mem.setdefault(_GS + o.CURRENT_STATE_PTR, 0)  # skip StdVector method
    r._find_game_state_slot = lambda: _SLOT                       # type: ignore
    r._read_ptr = lambda a: mem.get(a, 0)                          # type: ignore
    r._module_bounds = lambda: _MOD                                # type: ignore
    r._looks_like_entity = lambda ptr, mod: ptr in entities        # type: ignore
    r._entity_component_names = lambda ptr, limit=64: comps.get(ptr, [])  # type: ignore


def _add_igs(r: StructureReader, mem: dict, state_index: int,
             igs: int, area: int, player: int) -> None:
    o = r.Offsets
    mem[_GS + o.STATES + state_index * o.STATE_SLOT_STRIDE] = igs
    mem[igs + o.AREA_INSTANCE_DATA] = area
    mem[area + o.LOCAL_PLAYER] = player


class TestFindLocalPlayerSelection:
    def test_skips_decoy_and_picks_player_component_entity(self):
        r = _reader()
        mem, entities = {}, {_DECOY_PLAYER, _REAL_PLAYER}
        comps = {_DECOY_PLAYER: [], _REAL_PLAYER: ["Life", "Player", "Stats"]}
        _wire(r, mem, entities, comps)
        _add_igs(r, mem, 0, _DECOY_IGS, _DECOY_AREA, _DECOY_PLAYER)  # no comps
        _add_igs(r, mem, 1, _REAL_IGS, _REAL_AREA, _REAL_PLAYER)     # character
        assert r._find_local_player() == _REAL_PLAYER
        assert r._cached_igs == _REAL_IGS

    def test_fast_path_reuses_cached_igs_without_rescan(self):
        r = _reader()
        mem, entities = {}, {_REAL_PLAYER}
        comps = {_REAL_PLAYER: ["Player"]}
        _wire(r, mem, entities, comps)
        _add_igs(r, mem, 1, _REAL_IGS, _REAL_AREA, _REAL_PLAYER)
        r._cached_igs = _REAL_IGS
        # No STATES entries wired for a rescan; only the cached igs resolves.
        assert r._find_local_player() == _REAL_PLAYER
        assert r._cached_igs == _REAL_IGS

    def test_zone_change_invalidates_cache_and_reresolves(self):
        r = _reader()
        mem, entities = {}, {_DECOY_PLAYER, _NEW_PLAYER}
        comps = {_DECOY_PLAYER: [], _NEW_PLAYER: ["Player"]}
        _wire(r, mem, entities, comps)
        # Cached igs now yields a decoy (post zone change)...
        _add_igs(r, mem, 0, _REAL_IGS, _REAL_AREA, _DECOY_PLAYER)
        # ...and the real character has moved to a different state slot.
        _add_igs(r, mem, 1, _NEW_IGS, _NEW_AREA, _NEW_PLAYER)
        r._cached_igs = _REAL_IGS
        assert r._find_local_player() == _NEW_PLAYER
        assert r._cached_igs == _NEW_IGS


class TestPlayerFromIgs:
    def test_require_life_fallback_accepts_valid_life_entity(self):
        r = _reader()
        mem, entities = {}, {_REAL_PLAYER}
        comps = {_REAL_PLAYER: []}  # no Player component (name bucket shifted)
        _wire(r, mem, entities, comps)
        _add_igs(r, mem, 0, _REAL_IGS, _REAL_AREA, _REAL_PLAYER)
        r._resolve_life_component = lambda p: 0x1FE00               # type: ignore
        r._looks_like_life = lambda life, mod: True                 # type: ignore
        # require_player path rejects (no Player component)...
        assert r._player_from_igs(_REAL_IGS, _MOD, require_player=True) is None
        # ...but the defensive require_life path accepts it.
        assert r._player_from_igs(_REAL_IGS, _MOD, require_life=True) == _REAL_PLAYER
