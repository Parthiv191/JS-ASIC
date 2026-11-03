import inspect

import pytest

from emu.desc import Description
from emu.image import IDLE, Image, LaneCfg, LineCfg, Profile
from emu.isa import INF, Owner


def _uartish():
    d = Description("u", parties=("tx", "rx"))
    line = d.line("line", kind="pp", owner="tx", idle=1)
    bit = d.profile("BIT", nom=16, min=14, max=18)
    d.label("top")
    with d.only("tx"):
        d.pop("r0")
    d.cond("fall", line, owner="tx")
    d.xfer(8, line, owner="tx", prof=bit, src="r0", dst="fifo")
    d.br("top")
    return d


def test_builder_records_steps_and_locs():
    d = Description("x", parties=("a", "b"))
    ln = d.line("l", owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=8)); here = inspect.currentframe().f_lineno
    step = d.steps[-1]
    assert step.kind == "hold" and step.owner == "a"
    assert step.loc.endswith(f"test_desc.py:{here}")
    assert step.index == 0


def test_only_marks_block_membership():
    d = _uartish()
    kinds = [(s.kind, s.block) for s in d.steps]
    assert kinds == [("pop", frozenset({"tx"})), ("cond", None), ("xfer", None), ("br", None)]
    assert d.steps[0].labels == ["top"]


def test_nested_only_raises():
    d = Description("x", parties=("a", "b"))
    with pytest.raises(ValueError):
        with d.only("a"):
            with d.only("mon"):
                pass


def test_unknown_party_raises():
    d = Description("x", parties=("a", "b"))
    with pytest.raises(ValueError):
        d.line("l", owner="c")
    ln = d.line("l", owner="a")
    with pytest.raises(ValueError):
        d.cond("fall", ln, owner="zz")
    with pytest.raises(ValueError):
        with d.only("q"):
            pass


def test_duplicate_label_raises():
    d = Description("x", parties=("a", "b"))
    d.label("x")
    d.nop()
    with pytest.raises(ValueError):
        d.label("x")


def test_bad_register_raises():
    d = Description("x", parties=("a", "b"))
    with pytest.raises(ValueError):
        d.pop("r4")


def test_profile_defaults_min_max_to_nom():
    d = Description("x", parties=("a", "b"))
    p = d.profile("P", nom=10)
    assert d.profiles[p.index - 1] == Profile("P", 10, 10, 10)
    assert p.index == 1  # entry 0 is IDLE


def test_image_json_roundtrip():
    img = Image(
        name="t", parties=("a", "b"), words=[1, 0xFFFFFFFF],
        profiles=[IDLE, Profile("BIT", 9, 10, 11)],
        lines=[LineCfg("l", "pp", Owner.P0, 1)], lanes=[LaneCfg(0, None, 0, 0), LaneCfg(1, 2, 1, 1)],
        abort_pc=3, tolerance_ppm=20000, debug={0: "x.py:1 nop"},
    )
    assert Image.from_json(img.to_json()) == img
    assert IDLE.bounded is False and Profile("p", 1, 2, 3).bounded and IDLE.max == INF


def test_with_profiles_scales_nom_only():
    img = Image("t", ("a", "b"), [], [IDLE, Profile("BIT", 18, 20, 22)], [], [], 0, 20000, {})
    out = img.with_profiles({"BIT": 1.1})
    assert out.profiles[1] == Profile("BIT", 18, 22, 22)
    assert img.profiles[1].nom == 20  # original untouched


def test_rejects_profile_ref_index_zero():
    """Index 0 is the reserved IDLE profile; a ProfRef that names it is not a user profile."""
    from emu.desc import ProfRef
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a")
    d.profile("P", nom=8)
    with pytest.raises(ValueError, match="unknown profile"):
        d.hold(ln, 0, owner="a", prof=ProfRef("P", 0))


def test_rejects_unknown_lane_ref():
    from emu.desc import LaneRef
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a")
    p = d.profile("P", nom=8)
    with pytest.raises(ValueError, match="unknown lane"):
        d.xfer(8, LaneRef("nope", 5), owner="a", prof=p)
