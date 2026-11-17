"""A HOLD's followers check the line at its level (isa §4.2), so a HOLD 1 on an OD line needs
every other party released (R2-edge), and every owner of a SHARED HOLD drives when it starts, so
each starts by ISSUE (T5-overrun). Both found by the review of R1-bus on I2C (#75): each compiled
with every rule on, and on the model the followers logged LEVEL, or the late owner OVERRUN."""
import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF, Role
from emu.model.engine import VIOLATIONS
from emu.model.system import EngineSpec, System

from test_edge_rule import runs_clean


def diags(d):
    try:
        compile_desc(d)
    except CompileError as e:
        return [(x.rule, x.msg) for x in e.diags]
    return []


def events(d):
    img = compile_desc(d, check_levels=False, check_timing=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(50_000)
    return [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS]


def hold_1(other, owner="a"):
    """b pulls the shared OD line s low (`other` = "low"), or leaves it (`other` = "released"),
    then `owner` HOLDs s at 1, then b releases it."""
    d = Description("h", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    x = d.line("x", owner="a", idle=1)
    y = d.line("y", owner="b", idle=1)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.nop()
    if other == "low":
        d.cond("fall", s, owner="b", prof=w)
    else:
        d.cond("fall", y, owner="b", prof=w)
    d.cond("fall", x, owner="a", prof=w)          # (a HOLD may not start late)
    d.hold(s, 1, owner=owner, prof=d.profile("H1", nom=40))
    d.cond("rise", s if other == "low" else y, owner="b", prof=w)
    d.halt()
    return d


def test_a_hold_1_on_an_od_line_another_party_holds_low_is_rejected():
    assert any(r == "R2-edge" and "HOLD 1 on s" in m for r, m in diags(hold_1("low")))
    assert "LEVEL" in events(hold_1("low"))


def test_a_hold_1_on_a_released_od_line_is_legal():
    runs_clean(hold_1("released"))


def shared_hold_after(cond_owner):
    """`cond_owner`'s COND FALL on its own line, then a SHARED HOLD 1 on the OD line s: the party
    that followed the COND finishes it EDGE_LAT after the edge, so it starts the HOLD late."""
    d = Description("sh", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    x = d.line("x", owner=cond_owner, idle=1)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.nop()
    d.cond("fall", x, owner=cond_owner, prof=w)
    d.hold(s, 1, owner="shared", prof=d.profile("H1", nom=40))
    d.halt()
    return d


@pytest.mark.parametrize("cond_owner,late", [("a", "P1"), ("b", "P0")])
def test_every_owner_of_a_shared_hold_starts_by_issue(cond_owner, late):
    assert any(r == "T5-overrun" and m.startswith(f"pc 2: {late}") for r, m in diags(shared_hold_after(cond_owner)))
    assert "OVERRUN" in events(shared_hold_after(cond_owner))


def test_a_shared_hold_both_start_on_time_is_legal():
    """At pc 0, and after a timed step both own (a SHARED HOLD 0), both start a SHARED HOLD by
    ISSUE."""
    d = Description("sh2", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    d.hold(s, 0, owner="shared", prof=d.profile("H0", nom=40))
    d.hold(s, 1, owner="shared", prof=d.profile("H1", nom=40))
    d.halt()
    runs_clean(d)


def test_a_shared_hold_owner_counts_its_latest_start():
    """After a SHARED HOLD of nom 1, each owner finishes it between 1 before and 1 after its end,
    so it may start the next one at 3 (its earliest is 0): late, and on the model both OVERRUN."""
    d = Description("sh3", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    d.hold(s, 0, owner="shared", prof=d.profile("H0", nom=1))
    d.hold(s, 1, owner="shared", prof=d.profile("H1", nom=40))
    d.halt()
    assert ("T5-overrun", "pc 1: P0, an owner of this SHARED HOLD, starts at 3, more than ISSUE after "
                          "the step start") in diags(d)
    assert events(d).count("OVERRUN") == 2


# ---- the review of this PR
def test_a_hold_0_while_another_party_holds_the_line_low_is_legal():
    """Only a HOLD 1 needs the others released: a HOLD 0 reads 0 whatever they drive."""
    d = Description("h0", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    x = d.line("x", owner="a", idle=1)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.nop()
    d.cond("fall", s, owner="b", prof=w)
    d.cond("fall", x, owner="a", prof=w)
    d.hold(s, 0, owner="a", prof=d.profile("H0", nom=40))
    d.cond("rise", x, owner="a", prof=w)
    d.halt()
    runs_clean(d)


def mon_after(k, ppm, nom2):
    """A SHARED HOLD of 40, k NOPs, a SHARED HOLD of nom2. MON follows the first: it finishes it at
    -40 + 20 + EDGE_LAT = -17, so it starts the second at -17 + ISSUE + 2k."""
    d = Description("mon", parties=("a", "b"), tolerance_ppm=ppm)
    s = d.line("s", "od", owner="shared", idle=1)
    d.hold(s, 0, owner="shared", prof=d.profile("H0", nom=40))
    for _ in range(k):
        d.nop()
    d.hold(s, 1, owner="shared", prof=d.profile("H1", nom=nom2))
    d.halt()
    return d


@pytest.mark.parametrize("k,ppm,nom2,bad", [(13, 0, 16, False), (14, 0, 16, True),
                                            (12, 20000, 18, False), (13, 20000, 18, True)])
def test_mon_starts_a_shared_hold_by_its_sample_less_drift(k, ppm, nom2, bad):
    """Its first sample is at nom2 // 2 + EDGE_LAT: 11, or 12. MON starts at 11 (k 13): just in time
    at 0 ppm. At 2%, the drift over the anchor distance, the first HOLD's 40 plus 18, is 2: MON
    at 9 (k 12) is in time, at 11 is not (with the drift over 40 - 18 it would be 1, and pass)."""
    assert any(r == "T5-watch" and m.startswith(f"pc {k + 1}: MON") for r, m in diags(mon_after(k, ppm, nom2))) == bad


def test_mon_follows_a_shared_hold_and_starts_by_its_sample():
    """After a SHARED HOLD and 19 NOPs, both owners start the next SHARED HOLD on time (they
    finished the first when they drove it), but MON, a follower, starts at 23, after its sample at
    16 // 2 + EDGE_LAT = 11 (T5-watch); on the model MON overruns."""
    d = Description("mon", parties=("a", "b"), tolerance_ppm=0)
    s = d.line("s", "od", owner="shared", idle=1)
    d.hold(s, 0, owner="shared", prof=d.profile("H0", nom=40))
    for _ in range(19):
        d.nop()
    d.hold(s, 1, owner="shared", prof=d.profile("H1", nom=16))
    d.halt()
    assert ("T5-watch", "pc 20: MON starts at 23, after its first sample at 11") in diags(d)
    assert "OVERRUN" in events(d)


def raw_in_slot(hold_level, raw):
    """a HOLDs s, then at once (in its own block) RAWs s: an owner finishes a HOLD when it starts,
    so the RAW runs in the HOLD's slot, before the followers sample it (found by the review)."""
    d = Description("rs", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    x = d.line("x", owner="a", idle=1)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.nop()
    d.cond("fall", x, owner="a", prof=w)
    d.hold(s, hold_level, owner="a", prof=d.profile("H", nom=200))
    with d.only("a"):
        if raw == "REL":
            d.raw_rel(s)
        else:
            d.raw_set(s, raw)
    d.cond("rise", x, owner="a", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("hold_level,raw,bad", [(1, 0, True), (0, "REL", True), (0, 1, True),
                                                (1, 1, False), (1, "REL", False), (0, 0, False)])
def test_a_raw_in_a_holds_slot_may_not_change_its_level(hold_level, raw, bad):
    what = "RAW REL" if raw == "REL" else f"RAW SET {raw}"
    flagged = any(r == "R2-edge" and f"{what} on s by P0 comes in the slot of its HOLD {hold_level}" in m
                  for r, m in diags(raw_in_slot(hold_level, raw)))
    assert flagged == bad
    assert ("LEVEL" in events(raw_in_slot(hold_level, raw))) == bad


def test_an_untimed_loop_after_a_hold_ends_the_slot_walk():
    """The walk over a HOLD's slot follows untimed instructions until a timed step: around a loop
    of them it must still end (the program is rejected for its untimed loop, isa §6)."""
    import threading
    d = Description("ul", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    d.nop()
    d.hold(s, 1, owner="a", prof=d.profile("H", nom=40))
    d.label("L")
    d.nop()
    d.br("L")
    done = []
    th = threading.Thread(target=lambda: done.append(diags(d)), daemon=True)
    th.start()
    th.join(30)
    assert done, "the slot walk didn't end"


def halt_after_hold(level: int, kind: str = "od"):
    """a's COND FALL on l, its HOLD `level` on l, then its HALT."""
    d = Description("h", parties=("a", "b"))
    ln = d.line("l", kind, owner="a", idle=1)
    d.cond("fall", ln, owner="a", prof=d.profile("W", nom=20, min=0, max=INF))
    d.hold(ln, level, owner="a", prof=d.profile("Ho", nom=20, min=20, max=20))
    d.halt()
    return d


def test_a_halt_in_a_hold_0_s_slot_releases_the_od_line_before_the_followers_sample_it():
    """R2-edge: a HOLD's owner finishes it when it starts, so its HALT right after comes in the
    HOLD's slot, and a HALT releases OD lines (isa §4.1): the followers sample 1 and log LEVEL
    (found by the random bus programs)."""
    with pytest.raises(CompileError, match=r"\[R2-edge\] .*HALT by P0 comes in the slot of its HOLD 0"):
        compile_desc(halt_after_hold(0))
    img = compile_desc(halt_after_hold(0), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(10_000)
    assert ("LEVEL", 1) in [(e.kind, e.pc) for e in t.engines[1].events]
    compile_desc(halt_after_hold(1))            # released already: no change
    compile_desc(halt_after_hold(0, "pp"))      # a PP line keeps its level at HALT
