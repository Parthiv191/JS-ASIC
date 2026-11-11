"""The fuzz unit model (bus-fuzzer.md §2-§3): LFSR, draw sequence, mutator, bitmap, corpus, findings."""
import pytest

from emu.fuzz.unit import (
    ENERGY_MAX, LFSR_POLY, SLOTS, FuzzConfig, FuzzUnit, bucket, crc16_byte, lfsr_draw, lfsr_seed, lfsr_step,
)


def scripted(unit, draws):
    """Replace the LFSR with a script of draws; the test checks every one is consumed."""
    it = iter(draws)
    unit.draw = lambda: next(it)
    return it


def _maximal(mask, n):
    """The Galois LFSR's step matrix has order exactly 2^n - 1 (GF(2) matrix powers)."""
    def apply(m, v):
        r, i = 0, 0
        while v:
            if v & 1:
                r ^= m[i]
            v, i = v >> 1, i + 1
        return r

    def power(m, e):
        r = [1 << i for i in range(n)]
        while e:
            if e & 1:
                r = [apply(m, b) for b in r]
            m, e = [apply(m, b) for b in m], e >> 1
        return r

    step = [(1 << i >> 1) ^ (mask if i == 0 else 0) for i in range(n)]
    ident, order = [1 << i for i in range(n)], (1 << n) - 1
    return power(step, order) == ident and all(power(step, order // p) != ident
                                               for p in (3, 5, 17, 257, 65537))


def test_lfsr_known_states_and_maximal_period():
    s, seq = 1, []
    for _ in range(3):
        s = lfsr_step(s)
        seq.append(s)
    assert seq == [0x80200003, 0xC0300002, 0x60180001]
    assert _maximal(LFSR_POLY, 32)
    assert not _maximal(0x80000000, 32)


def test_seed_register_loads_seed_and_complement():
    assert lfsr_seed(0x1234) == 0x1234EDCB
    assert FuzzUnit(FuzzConfig(seed=0x1234)).lfsr == 0x1234EDCB


def test_a_draw_is_16_steps():
    s = lfsr_seed(0xACE1)
    t = s
    for _ in range(16):
        t = lfsr_step(t)
    assert lfsr_draw(s) == t
    assert FuzzUnit(FuzzConfig(seed=0xACE1)).draw() == t & 0xFFFF


def test_crc16_ccitt_check_value():
    crc = 0xFFFF
    for b in b"123456789":
        crc = crc16_byte(crc, b)
    assert crc == 0x29B1


@pytest.mark.parametrize("m,b", [(0, 0), (1, 1), (2, 1), (3, 2), (6, 2), (7, 3), (62, 5), (63, 6), (0xFFFF, 15)])
def test_bucket(m, b):
    assert bucket(m) == b


def test_seed_zero_rejected():
    with pytest.raises(ValueError):
        FuzzConfig(seed=0)


def test_draw_sequence_structural_then_point_ops():
    """§3 draw sequence: pick (slot, keep), structural op + pos, count, then type/pos/operand."""
    u = FuzzUnit(FuzzConfig(length=4))
    u.load(0, bytes([1, 2, 3, 4]))
    rest = scripted(u, [0x00, 0x00,          # round 1: slot 0, keep (0 < 15)
                        0x01, 0x80,          # DELETE at pos((0x80 * 4) >> 8) = 2
                        0x00,                # one point op
                        0x03, 0x00, 0x5A])   # random byte 0x5A at pos 0
    assert u.next() == 4
    assert u.staged() == bytes([0x5A, 2, 4, 0])
    assert next(rest, None) is None
    assert u.parent == 0 and u.armed


def test_draw_sequence_round_four_keeps_without_a_keep_draw():
    u = FuzzUnit(FuzzConfig(length=3))
    u.load(5, bytes([9, 9, 9]), energy=1)
    rest = scripted(u, [0x05, 0x0F,  # slot 5, reject (15 >= 1)
                        0x05, 0x0F,  # reject
                        0x02, 0x00,  # slot 2 (empty, energy 0): reject
                        0x05,        # round 4: slot 5, kept with no keep draw
                        0x02, 0x00,  # DUP at 0
                        0x03,        # four point ops:
                        0x00, 0x00, 0x03,    # FLIP bit 3 at 0
                        0x01, 0x60, 0x02,    # INTERESTING[2] = 0x7F at pos 1
                        0x02, 0xC0, 0x05,    # ADD -2 at pos 2
                        0x02, 0xC0, 0x01])   # ADD +2 at pos 2
    u.next()
    assert u.parent == 5
    assert u.staged() == bytes([9 ^ 8, 0x7F, 9])
    assert next(rest, None) is None


def test_splice_takes_the_donor_tail_and_empty_slot_reads_zero():
    u = FuzzUnit(FuzzConfig(length=4))
    u.load(0, bytes([1, 2, 3, 4]))
    u.load(7, bytes([0xA, 0xB]))
    scripted(u, [0, 0, 3, 7, 0x40, 0, 0, 0, 0])  # SPLICE donor 7 at cut 1; FLIP bit 0 at 0
    u.next()
    assert u.staged() == bytes([0, 0xB, 0, 0])
    v = FuzzUnit(FuzzConfig(length=4))
    rest = scripted(v, [3, 0, 3, 0, 3, 0, 3,     # rounds 1-3 reject empty slot 3, round 4 keeps it
                        0, 0, 0, 0, 0])          # no structural op; FLIP bit 0 at 0
    v.next()
    assert v.staged() == bytes([1, 0, 0, 0]) and next(rest, None) is None


def test_same_seed_same_mutation_sequence():
    def seq(seed):
        u = FuzzUnit(FuzzConfig(seed=seed, length=8))
        u.load(0, bytes(range(8)))
        out = []
        for i in range(200):
            u.next()
            out.append(u.staged())
            u.tok(i)
            u.end()
        return out
    assert seq(0x1234) == seq(0x1234)
    assert seq(0x1234) != seq(0x4321)


def test_mutable_mask_respected():
    u = FuzzUnit(FuzzConfig(seed=0xBEEF, length=8, mask=0b0011))
    u.load(0, bytes([0x11] * 8))
    for i in range(1000):
        u.next()
        parent = bytes(u.data[u.parent][:8]) if u.length[u.parent] else bytes(8)
        assert u.staged()[2:] == parent[2:]
        u.tok(i & 0xFF)  # mostly new fingerprints, so the corpus grows
        u.end()
    assert u.stats.saves > 100


def test_fixed_mode_preserves_length():
    u = FuzzUnit(FuzzConfig(seed=7, length=5))
    u.load(0, bytes(5))
    for i in range(500):
        assert u.next() == 5 and len(u.staged()) == 5
        u.tok(i)
        u.end()


def test_wire_mode_length_within_1_32_and_sent_first():
    u = FuzzUnit(FuzzConfig(seed=9, length=4, length_mode="wire"))
    u.load(0, bytes([1, 2, 3]))
    lengths = set()
    for i in range(2000):
        n = u.next()
        assert 1 <= n <= 32
        lengths.add(n)
        sent = [u.read_byte() for _ in range(n + 2)]
        assert sent[0] == n and bytes(sent[1:n + 1]) == u.staged() and sent[n + 1] == 0
        u.tok(i & 0xFF)
        u.tok(i >> 8)
        u.end()
    assert len(lengths) > 3


def test_end_saves_only_new_fingerprints_and_decays_energy_to_1():
    u = FuzzUnit(FuzzConfig(seed=3, length=4))
    u.load(0, bytes(4))
    u.next()
    u.tok(0x42)
    assert u.end() == 1
    saved = [i for i in range(SLOTS) if u.length[i]]
    assert saved == [0, 1] and u.energy[1] == ENERGY_MAX
    assert u.log[-1].kind == "new" and u.log[-1].slot == 1 and u.findings_count == 1
    for _ in range(40):              # the same fingerprint every time: never new again
        u.next()
        p = u.parent
        before = u.energy[p]
        u.tok(0x42)
        assert u.end() == 0
        assert u.energy[p] == (max(1, before - 1) if before else 0)
    assert [i for i in range(SLOTS) if u.length[i]] == [0, 1]
    assert u.energy[0] + u.energy[1] < 2 * ENERGY_MAX


def test_eviction_takes_lowest_energy_unpinned_slot():
    u = FuzzUnit(FuzzConfig(seed=5, length=2))
    for i in range(SLOTS):
        u.load(i, bytes([i, 0]), energy=4, pinned=i == 3)
    u.energy[3] = 1                  # pinned: never evicted
    u.energy[9] = 2
    u.energy[20] = 2
    u.next()
    u.tok(1)
    assert u.end() == 1
    assert u.length[9] == 2 and u.energy[9] == ENERGY_MAX and u.stats.evictions == 1
    assert u.energy[3] == 1 and u.energy[20] == 2


def test_abort_while_armed_logs_and_pins_a_hang():
    u = FuzzUnit(FuzzConfig(seed=5, length=2))
    u.load(0, bytes(2))
    u.next()
    u.abort()
    assert not u.armed and u.log[-1].kind == "hang" and u.pinned[u.log[-1].slot]
    assert u.end() == 0              # disarmed: fz.end does nothing
    for i in range(SLOTS):
        u.pinned[i], u.length[i] = True, 2
    u.next()
    u.abort()
    assert u.log[-1].kind == "hang not saved" and u.log[-1].slot is None
    u.abort()                        # not armed: nothing
    assert u.findings_count == 2


def test_findings_log_keeps_the_two_newest():
    u = FuzzUnit(FuzzConfig(seed=11, length=2))
    u.load(0, bytes(2))
    for i in range(5):
        u.next()
        u.tok(i)
        u.end()
    assert u.findings_count == 5 and len(u.log) == 2
    assert [f.iteration for f in u.log] == [4, 5]


def test_event_mask_enters_the_fingerprint():
    def fp(kinds):
        u = FuzzUnit(FuzzConfig(seed=1, length=2))
        u.next()
        u.tok(7)
        for k in kinds:
            u.event(k)
        u.end()
        return u.fingerprint
    assert fp([]) != fp(["TIMEOUT"]) != fp(["EARLY"])
    assert fp(["EARLY", "EARLY"]) == fp(["EARLY"])  # which kinds, not how often


def test_baseline_never_saves_but_still_counts_coverage():
    u = FuzzUnit(FuzzConfig(seed=13, length=4, feedback=False))
    u.load(0, bytes(4))
    for i in range(300):
        u.next()
        u.tok(i & 0xFF)
        u.end()
    assert [i for i in range(SLOTS) if u.length[i]] == [0]
    assert u.energy[0] == ENERGY_MAX and u.coverage == 256
