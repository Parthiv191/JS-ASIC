"""Learning against the no-feedback baseline (bus-fuzzer.md §6 stage 1, the Dec 15 gate).

The smoke tests run on every push. The full experiment (5 seeds x 4 devices x 30K iterations,
a few minutes) runs with FUZZ_LONG=1; its table is in bus-fuzzer.md "Stage 1 results".
"""
import os

import pytest

from emu.fuzz.campaign import ITERS, SEEDS, experiment, run
from emu.fuzz.devices import Target, hang, unlock


@pytest.fixture(scope="module")
def t_unlock():
    return Target(unlock())


@pytest.fixture(scope="module")
def t_hang():
    return Target(hang())


def test_campaign_deterministic_per_seed(t_unlock):
    a = run(t_unlock.dev, 1500, 0xACE1, target=t_unlock, every=500)
    b = run(t_unlock.dev, 1500, 0xACE1, target=t_unlock, every=500)
    assert a == b


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_learning_beats_baseline_on_coverage(t_unlock, seed):
    learn = run(t_unlock.dev, 6000, seed, True, t_unlock)
    base = run(t_unlock.dev, 6000, seed, False, t_unlock)
    assert learn.coverage > 2 * base.coverage
    assert learn.corpus > 1 and base.corpus == 1


def test_learning_finds_the_hang_and_baseline_does_not(t_hang):
    learn = run(t_hang.dev, 6000, 0x5EED, True, t_hang)
    base = run(t_hang.dev, 6000, 0x5EED, False, t_hang)
    assert learn.found is not None and learn.first_hang == learn.found
    assert base.found is None and base.hangs == 0


@pytest.mark.skipif(not os.environ.get("FUZZ_LONG"), reason="the full experiment: set FUZZ_LONG=1")
def test_full_experiment():
    reports = experiment(iters=ITERS)
    by = {}
    for r in reports:
        by.setdefault((r.device, r.feedback), []).append(r)
    for dev in ("unlock", "leak", "hang"):
        learn, base = by[(dev, True)], by[(dev, False)]
        assert sum(r.found is not None for r in learn) >= 4, dev
        assert sum(r.found is not None for r in base) <= 1, dev
        assert min(r.coverage for r in learn) > max(r.coverage for r in base), dev
    # the control: no partial feedback, so learning has nothing to climb
    assert not any(r.found for r in by[("magic", True)] + by[("magic", False)])
