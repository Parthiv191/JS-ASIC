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
