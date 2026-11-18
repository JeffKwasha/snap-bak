"""How much of the code and the tests name their requirements.

The taxonomy is many-to-many and adopted incrementally, so there is no pass
mark: the numbers are printed at the end of every run and written into the
commit message, and the next change is expected to beat them.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"\[\[R-[A-Z]+-\d+\]\]")
TEST_DEF = re.compile(r"^\s*def test_")


def ids_in(path):
    return set(ID.findall(path.read_text()))


def counts(root=ROOT):
    total = cited = 0
    for path in sorted((root / "tests").glob("test_*.py")):
        for line in path.read_text().splitlines():
            if TEST_DEF.match(line):
                total += 1
                cited += bool(ID.search(line))
    spec = ids_in(root / "SPEC.md")
    used = set()
    for area in ("src", "tests"):
        for path in (root / area).rglob("*.py"):
            used |= ids_in(path)
    return {
        "tests": total,
        "tests_cited": cited,
        "requirements": len(spec),
        "requirements_cited": len(used & spec),
        "spec_index_agree": spec == ids_in(root / "INDEX.md"),
    }
