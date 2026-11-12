"""Rule coverage (verification.md §2), checked rather than kept by hand.

Every rule id isa.md names (§5, §6) and every event kind (§7) must be expected by some test: its
quoted name appears in a test file. Every rule id the compiler or the analysis raises must be named
in isa.md, and every one isa.md names must be raised, so the spec and the code can't drift apart.
A rule with no test or no code is listed in GAPS with its reason; a gap that closes must leave the
list. verification.md §2's table must match what this file finds:
`cd toolchain && PYTHONPATH=. python tests/test_rule_coverage.py` prints it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISA = ROOT / "docs/design/isa.md"
PLAN = ROOT / "docs/design/verification.md"
TESTS = Path(__file__).resolve().parent
CODE = [ROOT / "toolchain/emu/compiler.py", ROOT / "toolchain/emu/analysis.py"]

GAPS = {  # id: why it has no test, or no code
    "R3-fz": "FZ ops reach the compiler with plan step 6 (Dec 6)",
}


def isa_rules() -> set[str]:
    return set(re.findall(r"`([RST]\d?-[a-z][a-z-]*)`", ISA.read_text(encoding="utf-8")))


def event_kinds() -> list[str]:
    """isa §7's event-kind row: OVERRUN | EARLY | ... | ILLEGAL."""
    text = ISA.read_text(encoding="utf-8")
    sec = text[text.index("**Event kinds.**"):]
    row = next(ln for ln in sec.splitlines() if ln.startswith("| OVERRUN"))
    return [c.strip().split()[0] for c in row.strip("|").split("|")]


def code_rules() -> set[str]:
    ids: set[str] = set()
    for p in CODE:
        ids |= set(re.findall(r'(?:err|diags\.add)\(\s*"([RST]\d?-[a-z][a-z-]*)"', p.read_text(encoding="utf-8")))
    return ids


def _tests_naming(name: str) -> list[str]:
    hits = []
    for p in sorted(TESTS.glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        s = p.read_text(encoding="utf-8")
        if f'"{name}' in s or f"'{name}" in s:
            hits.append(p.name)
    return hits


def table() -> str:
    rows = ["| Rule or kind | Tests |", "|---|---|"]
    for name in sorted(isa_rules()) + event_kinds():
        hits = _tests_naming(name)
        rows.append(f"| `{name}` | {', '.join(f'`{h}`' for h in hits) if hits else 'gap: ' + GAPS.get(name, '?')} |")
    return "\n".join(rows)


def test_every_rule_and_kind_has_a_test():
    missing = {n for n in isa_rules() | set(event_kinds()) if not _tests_naming(n)}
    assert missing - set(GAPS) == set(), f"no test names these: {sorted(missing - set(GAPS))}"


def test_every_rule_the_code_raises_is_in_the_spec_and_back():
    isa, code = isa_rules(), code_rules()
    assert code - isa == set(), f"raised by the code but not named in isa.md: {sorted(code - isa)}"
    assert isa - code - set(GAPS) == set(), f"named in isa.md but never raised: {sorted(isa - code - set(GAPS))}"


def test_gaps_are_still_gaps():
    closed = {g for g in GAPS if _tests_naming(g) and g in code_rules()}
    assert not closed, f"these gaps are closed: take them out of GAPS: {sorted(closed)}"


def test_the_plan_lists_the_same_table():
    assert table() in PLAN.read_text(encoding="utf-8"), (
        "verification.md §2 is stale: replace its table with the output of "
        "`PYTHONPATH=. python tests/test_rule_coverage.py`")


if __name__ == "__main__":
    print(table())
