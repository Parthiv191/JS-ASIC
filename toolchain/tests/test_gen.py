"""The random programs and the rule cross-check (emu/gen, verification.md layer 4).

A checker that can't fail is worse than none, so besides a clean campaign these check that the
generator isn't vacuous (most recipes compile, none breaks a structural rule) and that planted
model bugs fail it and shrink to a small case."""
from pathlib import Path

import pytest

import emu.model.engine as engine
from emu.compiler import compile_desc
from emu.gen.check import Case, Env, campaign, compile_recipe, run_case, shrink
from emu.gen.randdesc import random_recipe

CORPUS = Path(__file__).resolve().parent / "gen_corpus"


def test_recipes_are_deterministic_per_seed():
    assert random_recipe(12345) == random_recipe(12345)
    assert random_recipe(12345) != random_recipe(12346)


def test_a_case_round_trips_through_json():
    import random
    r = random_recipe(77)
    case = Case(r, Env.draw(random.Random(1), r.ppm))
    assert Case.from_json(case.to_json()) == case


def test_no_generated_program_breaks_a_structural_rule():
    """The generator keeps R1, R2-branch, R3 and S by construction; only the timing rules and
    R2-edge are left to the compiler."""
    bad = [(s, res.rules) for s in range(300) if (res := compile_recipe(random_recipe(s))[1]) is not None
           and res.verdict == "structural"]
    assert bad == []


def test_most_programs_are_accepted():
    accepted = sum(compile_recipe(random_recipe(s))[1] is None for s in range(300))
    assert accepted >= 90  # about 45%: enough to exercise the oracles


def test_accepted_programs_pass_the_oracles():
    rep = campaign(seed=3, iters=150)
    assert rep.accepted >= 40 and rep.structural == []
    assert [(c.recipe.seed, r.detail) for c, r in rep.failures] == []


@pytest.fixture
def flipped_followers(monkeypatch):
    """A planted model bug: a follower stores its XFER value with bit 0 flipped."""
    orig = engine.Engine._record

    def bad(self, ins, mode, value=None):
        if mode == "follow" and ins.op.name == "XFER" and value is not None:
            value ^= 1
        return orig(self, ins, mode, value)
    monkeypatch.setattr(engine.Engine, "_record", bad)


def test_a_planted_bug_fails_the_campaign_and_shrinks(flipped_followers):
    rep = campaign(seed=5, iters=20)
    assert rep.failures, "the planted bug went unseen"
    case, res = rep.failures[0]
    assert "agreement" in res.rules
    small = shrink(case)
    assert len(small.recipe.frames) == 1 and small.recipe.frames[0].prep == ()
    assert run_case(small).rules == res.rules


@pytest.mark.parametrize("path", sorted(CORPUS.glob("*.json")), ids=lambda p: p.stem)
def test_the_corpus_stays_fixed(path):
    """Every case a campaign found, after its fix: the compiler rejects it, or it passes."""
    res = run_case(Case.from_json(path.read_text(encoding="utf-8")))
    assert res.verdict in ("ok", "rejected"), res.detail


def test_the_first_finding_is_rejected_by_r2_edge():
    res = run_case(Case.from_json((CORPUS / "r2_edge_clocked_then_start_bit.json").read_text(encoding="utf-8")))
    assert res.verdict == "rejected" and "R2-edge" in res.rules


def _alu_writes_0(monkeypatch):
    def bad(self, ins):
        self.regs[ins.rd] = 0
        self.pc += 1
        yield
    monkeypatch.setattr(engine.Engine, "_op_alu", bad)


def _pop_drops_a_word(monkeypatch):
    orig = engine.Engine._op_pop

    def bad(self, ins):
        if len(self.tx) > 1:
            self.tx.popleft()
        yield from orig(self, ins)
    monkeypatch.setattr(engine.Engine, "_op_pop", bad)


@pytest.mark.parametrize("plant", [_alu_writes_0, _pop_drops_a_word], ids=["alu writes 0", "pop drops a word"])
def test_the_value_oracle_catches_bugs_every_role_shares(monkeypatch, plant):
    """Every role runs the same wrong semantics, so agreement passes; the recipe's own values don't."""
    plant(monkeypatch)
    rep = campaign(seed=3, iters=60)
    assert any("value" in r.rules for _, r in rep.failures), "the shared bug went unseen"


def test_a_late_host_stalls_pops_and_every_oracle_holds():
    """The host writes one word at a time, late: POPs stall, and T5-stall must keep the others waiting."""
    import random
    from dataclasses import replace
    from emu.gen.check import underrun_free
    rng = random.Random(3)
    checked = 0
    for s in range(200):
        r = random_recipe(s)
        if compile_recipe(r)[1] is not None or not underrun_free(r):
            continue
        if not any(p[0] == "pop" for f in r.frames for p in f.prep):
            continue
        env = Env.draw(rng, r.ppm)
        assert run_case(Case(r, replace(env, late=(rng.randint(0, 1500), rng.randint(50, 300))))).verdict == "ok"
        checked += 1
    assert checked >= 20


def test_a_pop_that_doesnt_wait_fails_only_with_a_late_host(monkeypatch):
    """A planted bug: POP on an empty TX FIFO reads 0 instead of stalling. The early host never lets
    TX run dry, so only the late host shows it."""
    import emu.gen.check as check
    orig = engine.Engine._op_pop

    def bad(self, ins):
        if not self.tx:
            self.tx.append(0)
        yield from orig(self, ins)
    monkeypatch.setattr(engine.Engine, "_op_pop", bad)
    assert any("value" in r.rules for _, r in campaign(seed=5, iters=120, envs=3).failures)
    monkeypatch.setattr(check, "underrun_free", lambda r: False)  # no late host
    assert campaign(seed=5, iters=120, envs=3).failures == []


# ---- the bus family: I2C programs with plain drives and HALTs (emu/gen/randdesc.py)
def test_bus_recipes_are_deterministic_and_round_trip():
    import random
    from emu.gen.randdesc import random_bus_recipe
    assert random_bus_recipe(12345) == random_bus_recipe(12345) != random_bus_recipe(12346)
    r = random_bus_recipe(77)
    case = Case(r, Env.draw(random.Random(1), r.ppm))
    assert Case.from_json(case.to_json()) == case


def test_no_generated_bus_program_breaks_a_structural_rule():
    """R1-bus's structural part (a bus COND by SCL's owner, SDA changed only by its lane), R1-owner,
    R3-raw-drive (RAW in a role block without MON) and S hold by construction."""
    from emu.gen.randdesc import random_bus_recipe
    bad = [(s, res.rules) for s in range(300) if (res := compile_recipe(random_bus_recipe(s))[1]) is not None
           and res.verdict == "structural"]
    assert bad == []


def test_bus_programs_draw_every_drive_and_a_fair_share_compiles():
    """Every kind of plain drive on every bus line, by both parties, and HALTs, are drawn; about one
    in seven compiles, enough to exercise the oracles."""
    from emu.gen.randdesc import random_bus_recipe
    recipes = [random_bus_recipe(s) for s in range(300)]
    drawn = {(f.kind, f.line, f.owner) for r in recipes for f in r.frames}
    for kind in ("cond", "hold", "raw"):
        assert {(kind, 0, "a"), (kind, 0, "b"), (kind, 1, "a")} <= drawn   # SDA by both, SCL by a
    assert {("halt", 0, "a"), ("halt", 0, "b"), ("hold", 0, "shared")} <= drawn
    accepted = [r for r in recipes if compile_recipe(r)[1] is None]
    assert len(accepted) >= 40
    assert any(f.kind == "halt" for r in accepted for f in r.frames)
    assert any(f.kind in ("hold", "raw") and f.line == 0 for r in accepted for f in r.frames)


def test_accepted_bus_programs_pass_the_oracles():
    rep = campaign(seed=3, iters=400, family="i2c")
    assert rep.accepted >= 40 and rep.structural == []
    assert [(c.recipe.seed, r.detail) for c, r in rep.failures] == []


def test_without_the_level_walk_bus_programs_fail_and_some_only_the_bus_oracle(monkeypatch):
    """A planted compiler bug: no R2-edge or R1-bus walk. Accepted programs then hang, misread and
    make STARTs and STOPs no COND made; some of those stray edges fail no oracle but `bus` (the
    followers' detectors may not even confirm one), and a shrunk one stays small."""
    import emu.gen.check as check
    real = check.compile_desc
    monkeypatch.setattr(check, "compile_desc",
                        lambda d, **kw: real(d, **{**kw, "check_levels": False}) if kw.get("check_levels", True)
                        else real(d, **kw))
    rep = campaign(seed=1, iters=150, family="i2c")
    rules = [res.rules for _, res in rep.failures]
    assert len(rules) >= 10 and ("bus",) in rules
    case = next(c for c, res in rep.failures if res.rules == ("bus",))
    small = shrink(case)
    assert run_case(small).rules == ("bus",) and len(small.recipe.frames) <= len(case.recipe.frames)


def test_a_planted_follower_bug_fails_the_bus_campaign(flipped_followers):
    rep = campaign(seed=5, iters=60, family="i2c")
    assert any("agreement" in r.rules for _, r in rep.failures)


# ---- the review of the bus family
def nacked():
    """The target NACKs; the controller's lead block STOPs and branches to abort_pc, and the
    target and MON, in the block's successor with ABORT, take the STOP as a bus exit."""
    from emu.desc import Description
    from emu.isa import INF
    d = Description("nx", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.abort_to("done")
    d.nop()
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="zero", abort=True)
    d.xfer(1, lane, owner="tgt", prof=half, src="ones", dst="r4", abort=True)   # a NACK
    with d.lead("ctl"):
        d.br("ok", cond="z", reg="r4")
        d.xfer(1, lane, owner="ctl", prof=half, src="zero", abort=True)
        d.cond("stop", owner="ctl", prof=w)
        d.br("done")
    d.label("ok")
    d.xfer(8, lane, owner="tgt", prof=half, src="ones", abort=True)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero", abort=True)
    d.cond("stop", owner="ctl", prof=w)
    d.label("done")
    d.halt()
    return d


def bus_run(d):
    from emu.isa import Role
    from emu.model.system import EngineSpec, System
    img = compile_desc(d)
    return img, System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)


def test_the_bus_oracle_lets_a_bus_exit_take_its_flag():
    """Their detectors saw a STOP that no COND STOP of theirs took, which is right."""
    from emu.gen.check import bus_mismatches
    img, t = bus_run(nacked())
    assert [any(e.kind == "BUS" for e in et.events) for et in t.engines] == [False, True, True]
    assert bus_mismatches(img, t) == []


def test_a_detector_that_reports_twice_fails_the_bus_oracle(monkeypatch):
    """A planted model bug: each confirmed START or STOP is logged twice. The flags work as before,
    so every role completes and agrees; only the detector comparison sees it, also in an engine
    that took a bus exit (found by the re-review: such an engine wasn't compared at all)."""
    from emu.gen.check import bus_mismatches
    orig = engine.Engine._confirm_candidate

    def bad(self):
        ok = orig(self)
        self.bus_events.append(self.bus_events[-1])
        return ok
    monkeypatch.setattr(engine.Engine, "_confirm_candidate", bad)
    img, t = bus_run(nacked())
    assert {m.split(":")[0] for m in bus_mismatches(img, t)} == {"P1", "MON"}
    rep = campaign(seed=3, iters=150, family="i2c")
    assert rep.failures and all(r.rules == ("bus",) for _, r in rep.failures)


def test_a_halt_that_does_nothing_fails_the_bus_campaign(monkeypatch):
    """A planted model bug: HALT anywhere but the last word is a NOP. Every role still agrees and
    halts at the end, so only the recipe's own halts see it (found by the review)."""
    orig = engine.Engine._op_halt

    def bad(self, ins):
        if self.pc != len(self.instrs) - 1:
            self.pc += 1
            yield
        else:
            yield from orig(self, ins)
    monkeypatch.setattr(engine.Engine, "_op_halt", bad)
    rep = campaign(seed=3, iters=300, family="i2c")
    assert any("completion" in r.rules and "halt in its block" in r.detail for _, r in rep.failures)
