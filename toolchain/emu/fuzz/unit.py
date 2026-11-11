"""The fuzz unit (docs/design/bus-fuzzer.md §2-§3): corpus, seed picker, LFSR, mutator, staging,
fingerprint, coverage bitmap and findings log. It is the golden model for the RTL, so every
LFSR draw follows the normative draw sequence of §3, draw for draw.

The unit knows nothing about engines. `next`, `read_byte`, `tok`, `event`, `end` and `abort` are
what `fz.next`, `XFER src=FZ`, a token, an event kind, `fz.end` and a fatal abort do to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SLOTS = 32
SLOT_BYTES = 32
ENERGY_MAX = 15
LFSR_BITS = 32
LFSR_POLY = 0x80200003   # x^32 + x^22 + x^2 + x + 1, maximal length (period 2^32 - 1)
INTERESTING = (0x00, 0x01, 0x7F, 0x80, 0xFF, 0x00, 0xFF, 0x01)
PICK_ROUNDS = 4
DRAW_STEPS = 16   # LFSR steps per draw: 16 fresh bits (one step only shifts the old ones)

# event kinds of isa.md §7, as the model's Event.kind strings; the bit index is the code
EVENT_KINDS = ("OVERRUN", "EARLY", "TIMEOUT", "LEVEL", "EDGE", "CLK_PHASE", "ABORT", "OVERFLOW",
               "UNDERRUN", "BUS", "ILLEGAL")
EVENT_BIT = {k: i for i, k in enumerate(EVENT_KINDS)}

NONE, DELETE, DUP, SPLICE = range(4)
FLIP, INTEREST, ADD, RANDOM = range(4)


def lfsr_step(s: int) -> int:
    """32-bit Galois LFSR, right shift, poly 0x80200003."""
    return (s >> 1) ^ (LFSR_POLY if s & 1 else 0)


def lfsr_draw(s: int) -> int:
    """One draw: DRAW_STEPS steps. The draw's value is the new state's low 16 bits."""
    for _ in range(DRAW_STEPS):
        s = lfsr_step(s)
    return s


def lfsr_seed(seed: int) -> int:
    """The 32-bit start state from the 16-bit LFSR_SEED register: {seed, ~seed}."""
    return seed << 16 | (seed ^ 0xFFFF)


def crc16_byte(crc: int, b: int) -> int:
    """CRC-16/CCITT (poly 0x1021, MSB first), one byte."""
    crc ^= (b & 0xFF) << 8
    for _ in range(8):
        crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
    return crc & 0xFFFF


def bucket(m: int) -> int:
    """Latency bucket: min(15, floor(log2(m + 1))), so cycle jitter doesn't look new."""
    return min(15, max(0, m + 1).bit_length() - 1)


@dataclass(frozen=True)
class FuzzConfig:
    seed: int = 1                                  # LFSR_SEED, nonzero
    length: int = 8                                # FZ_LEN, 1..32
    length_mode: Literal["fixed", "wire"] = "fixed"
    mask: int = (1 << SLOT_BYTES) - 1              # MASK: bit i set = byte i may mutate
    bitmap_bits: int = 8192                        # 1K..8K, a power of two
    feedback: bool = True                          # False: the no-feedback baseline (see FuzzUnit)

    def __post_init__(self):
        if not 1 <= self.seed <= 0xFFFF:
            raise ValueError("LFSR seed must be 1..0xFFFF (0 locks the LFSR)")
        if not 1 <= self.length <= SLOT_BYTES:
            raise ValueError(f"length must be 1..{SLOT_BYTES}")
        if self.length_mode not in ("fixed", "wire"):
            raise ValueError("length_mode must be 'fixed' or 'wire'")
        if self.bitmap_bits not in (1024, 2048, 4096, 8192):
            raise ValueError("bitmap_bits must be 1K, 2K, 4K or 8K")


@dataclass(frozen=True)
class Finding:
    kind: Literal["new", "hang", "hang not saved"]
    slot: int | None
    iteration: int
    data: bytes          # the staged message (model only; the chip keeps it in the slot)
    fingerprint: int | None = None


@dataclass
class Stats:
    iterations: int = 0
    new: int = 0
    hangs: int = 0
    saves: int = 0
    evictions: int = 0
    not_saved: int = 0
    empty_picks: int = 0


class FuzzUnit:
    """One fuzz unit. With `cfg.feedback = False` (the baseline) the bitmap still records every
    fingerprint, so coverage can be measured, but nothing is ever saved and no energy changes:
    only the host's seed slots are ever mutated."""

    def __init__(self, cfg: FuzzConfig = FuzzConfig()):
        self.cfg = cfg
        self.lfsr = lfsr_seed(cfg.seed)
        self.data = [bytearray(SLOT_BYTES) for _ in range(SLOTS)]
        self.length = [0] * SLOTS
        self.energy = [0] * SLOTS
        self.pinned = [False] * SLOTS
        self.bitmap = bytearray(cfg.bitmap_bits // 8)
        self.log: list[Finding] = []         # the chip's 2-entry log: the 2 newest findings
        self.findings_count = 0
        self.history: list[Finding] = []     # every finding (model only, for experiments)
        self.stats = Stats()
        self.staging = bytearray(SLOT_BYTES)
        self.msg_len = cfg.length
        self.parent = 0
        self.armed = False
        self.fingerprint = 0xFFFF
        self.events = 0
        self.tokens: list[int] = []          # this iteration's tokens (model only, for tests)
        self._rd = 0
        self._header = False
        self.trace: list[str] = []           # the last fz.next's ops, for tests and debugging

    # ---- host setup ---------------------------------------------------------------------
    def load(self, slot: int, data: bytes, energy: int = ENERGY_MAX, pinned: bool = False) -> None:
        if not 0 <= slot < SLOTS or not 1 <= len(data) <= SLOT_BYTES or not 0 <= energy <= ENERGY_MAX:
            raise ValueError("bad slot, length or energy")
        self.data[slot] = bytearray(data) + bytearray(SLOT_BYTES - len(data))
        self.length[slot] = len(data)
        self.energy[slot] = energy
        self.pinned[slot] = pinned

    def clear_bitmap(self) -> None:
        self.bitmap = bytearray(self.cfg.bitmap_bits // 8)

    @property
    def coverage(self) -> int:
        """Bitmap bits set: distinct fingerprints seen (up to bitmap collisions)."""
        return sum(bin(b).count("1") for b in self.bitmap)

    def corpus(self) -> list[tuple[int, bytes, int, bool]]:
        return [(i, bytes(self.data[i][:self.length[i]]), self.energy[i], self.pinned[i])
                for i in range(SLOTS) if self.length[i]]

    # ---- randomness ---------------------------------------------------------------------
    def draw(self) -> int:
        """One draw: 16 LFSR steps (in RTL one fixed XOR network, one cycle); the low 16 bits."""
        self.lfsr = lfsr_draw(self.lfsr)
        return self.lfsr & 0xFFFF

    def _pos(self, d: int) -> int:
        return ((d & 0xFF) * self.msg_len) >> 8

    def _mutable(self, lo: int, hi: int) -> bool:
        """The mask allows every byte in lo..hi-1."""
        span = ((1 << hi) - 1) & ~((1 << lo) - 1)
        return self.cfg.mask & span == span

    # ---- fz.next ------------------------------------------------------------------------
    def next(self) -> int:
        """Pick a slot, mutate it into staging, rewind, reset the fingerprint, arm. Returns the length."""
        cfg = self.cfg
        self.stats.iterations += 1
        self.trace = []
        # 1. seed pick: energy-weighted rejection sampling, at most PICK_ROUNDS rounds
        for rnd in range(PICK_ROUNDS):
            slot = self.draw() & (SLOTS - 1)
            if rnd == PICK_ROUNDS - 1 or (self.draw() & 15) < self.energy[slot]:
                break
        self.parent = slot
        if self.length[slot]:
            self.staging = bytearray(self.data[slot])
            self.msg_len = cfg.length if cfg.length_mode == "fixed" else self.length[slot]
            if cfg.length_mode == "fixed":   # bytes past the slot's length read 0x00
                self.staging[self.length[slot]:] = bytearray(SLOT_BYTES - self.length[slot])
        else:
            self.stats.empty_picks += 1
            self.staging = bytearray(SLOT_BYTES)
            self.msg_len = cfg.length
        self.trace.append(f"pick {slot}")
        # 2. at most one structural op
        op = self.draw() & 3
        if op in (DELETE, DUP):
            self._shift(op, self._pos(self.draw()))
        elif op == SPLICE:
            donor = self.draw() & (SLOTS - 1)
            self._splice(donor, self._pos(self.draw()))
        # 3-4. 1..4 point ops
        for _ in range((self.draw() & 3) + 1):
            kind = self.draw() & 3
            p = self._pos(self.draw())
            d = self.draw()
            if not self.cfg.mask >> p & 1:
                self.trace.append(f"masked {p}")
                continue
            b = self.staging[p]
            if kind == FLIP:
                b ^= 1 << (d & 7)
            elif kind == INTEREST:
                b = INTERESTING[d & 7]
            elif kind == ADD:
                b = (b - ((d & 3) + 1) if d & 4 else b + (d & 3) + 1) & 0xFF
            else:
                b = d & 0xFF
            self.staging[p] = b
            self.trace.append(f"{('flip', 'interesting', 'add', 'random')[kind]} {p}")
        self._rd = 0
        self._header = cfg.length_mode == "wire"
        self.fingerprint = 0xFFFF
        self.events = 0
        self.tokens = []
        self.armed = True
        return self.msg_len

    def _shift(self, op: int, p: int) -> None:
        n = self.msg_len
        if not self._mutable(p, n if self.cfg.length_mode == "fixed" else SLOT_BYTES):
            self.trace.append(f"masked {('', 'delete', 'dup')[op]} {p}")
            return
        s = self.staging
        if op == DELETE:
            if self.cfg.length_mode == "wire":
                if n == 1:
                    return
                s[p:n] = s[p + 1:n] + b"\x00"
                self.msg_len = n - 1
            else:
                s[p:n] = s[p + 1:n] + b"\x00"
        else:
            if self.cfg.length_mode == "wire" and n < SLOT_BYTES:
                s[p + 1:n + 1] = s[p:n]
                self.msg_len = n + 1
            else:
                s[p + 1:n] = s[p:n - 1]
        self.trace.append(f"{('', 'delete', 'dup')[op]} {p}")

    def _splice(self, donor: int, cut: int) -> None:
        if not self._mutable(cut, self.msg_len if self.cfg.length_mode == "fixed" else SLOT_BYTES):
            self.trace.append(f"masked splice {cut}")
            return
        src = self.data[donor]
        dlen = self.length[donor]
        tail = bytes(src[i] if i < dlen else 0 for i in range(SLOT_BYTES))
        if self.cfg.length_mode == "fixed":
            self.staging[cut:self.msg_len] = tail[cut:self.msg_len]
        else:
            n = max(1, cut, dlen)
            self.staging[cut:n] = tail[cut:n]
            self.staging[n:] = bytearray(SLOT_BYTES - n)
            self.msg_len = n
        self.trace.append(f"splice {donor} {cut}")

    # ---- the iteration ------------------------------------------------------------------
    def staged(self) -> bytes:
        return bytes(self.staging[:self.msg_len])

    def read_byte(self) -> int:
        """XFER src=FZ: the length first in wire mode, then the staged bytes, then 0."""
        if self._header:
            self._header = False
            return self.msg_len
        if self._rd < self.msg_len:
            self._rd += 1
            return self.staging[self._rd - 1]
        return 0

    def tok(self, b: int) -> None:
        """Queue a data token (fz.tok, or an auto-token). Ignored while disarmed."""
        if self.armed:
            self.tokens.append(b & 0xFF)
            self.fingerprint = crc16_byte(self.fingerprint, b)

    def event(self, kind: str) -> None:
        """An event kind raised while armed sets its bit in the event mask (isa §7 codes)."""
        if self.armed and kind in EVENT_BIT:
            self.events |= 1 << EVENT_BIT[kind]

    def _pick_save_slot(self) -> int | None:
        """An empty slot (lowest index), else the lowest-energy unpinned slot (lowest index on ties)."""
        for i in range(SLOTS):
            if not self.length[i]:
                return i
        free = [i for i in range(SLOTS) if not self.pinned[i]]
        return min(free, key=lambda i: (self.energy[i], i)) if free else None

    def _save(self, slot: int, energy: int, pinned: bool) -> None:
        if self.length[slot]:
            self.stats.evictions += 1
        self.data[slot] = bytearray(self.staging[:self.msg_len]) + bytearray(SLOT_BYTES - self.msg_len)
        self.length[slot] = self.msg_len
        self.energy[slot] = energy
        self.pinned[slot] = pinned
        self.stats.saves += 1

    def _finding(self, f: Finding) -> None:
        self.log = (self.log + [f])[-2:]
        self.findings_count += 1
        self.history.append(f)

    def end(self) -> int:
        """fz.end: fold the event mask, finish the fingerprint, test-and-set its bitmap bit.
        New: save staging and log a finding. Not new: the parent loses one energy (floor 1).
        Returns 1 if new. A disarmed unit (after a hang) does nothing and returns 0."""
        if not self.armed:
            return 0
        self.armed = False
        fp = crc16_byte(crc16_byte(self.fingerprint, self.events & 0xFF), self.events >> 8)
        self.fingerprint = fp
        idx = fp & (self.cfg.bitmap_bits - 1)
        new = not self.bitmap[idx >> 3] >> (idx & 7) & 1
        self.bitmap[idx >> 3] |= 1 << (idx & 7)
        if new:
            self.stats.new += 1
            slot = None
            if self.cfg.feedback:
                slot = self._pick_save_slot()
                if slot is None:
                    self.stats.not_saved += 1
                else:
                    self._save(slot, ENERGY_MAX, False)
            self._finding(Finding("new", slot, self.stats.iterations, self.staged(), fp))
            return 1
        if self.cfg.feedback and self.energy[self.parent] > 1:
            self.energy[self.parent] -= 1
        return 0

    def abort(self) -> None:
        """A fatal abort while armed: save staging into a slot and pin it (energy 1), log a hang
        ("hang not saved" if every slot is pinned), disarm."""
        if not self.armed:
            return
        self.armed = False
        self.stats.hangs += 1
        slot = self._pick_save_slot() if self.cfg.feedback else None
        if slot is not None:
            self._save(slot, 1, True)
        kind = "hang" if slot is not None or not self.cfg.feedback else "hang not saved"
        self._finding(Finding(kind, slot, self.stats.iterations, self.staged()))


__all__ = ["FuzzConfig", "FuzzUnit", "Finding", "Stats", "lfsr_step", "lfsr_draw", "lfsr_seed", "crc16_byte", "bucket",
           "EVENT_KINDS", "EVENT_BIT", "INTERESTING", "SLOTS", "SLOT_BYTES", "ENERGY_MAX"]
