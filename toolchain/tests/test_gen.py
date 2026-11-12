"""The random programs and the rule cross-check (emu/gen, verification.md layer 4).

A checker that can't fail is worse than none, so besides a clean campaign these check that the
generator isn't vacuous (most recipes compile, none breaks a structural rule) and that planted
model bugs fail it and shrink to a small case."""
from pathlib import Path

import pytest

import emu.model.engine as engine
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
