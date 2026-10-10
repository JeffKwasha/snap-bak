# Verification log

Dated record of measurements and external-behaviour checks behind decisions in
SPEC.md and the skill. Documentation states *what* the behaviour is; this file
records *how it was established*, and is expected to go stale.

Each entry names what was measured, against what, and the result. Nothing here
is load-bearing at runtime — the test suite is the regression guard.

---

## 2026-10-09 — snap-bak 0a72b90..2d46958, restic 0.19.1, Python 3.14

### Matcher cost

Symptom: `show excludes --live` on a large source reported as taking "a very
long time". Suspected disk or network; it was neither.

| Setup | Before | After |
|-------|-------:|------:|
| 80k files, 40 `**` patterns | 117s | 56s |
| 80k files, 5 simple patterns | 4.63s | 4.66s |

`_match_parts` rebuilt a pattern's `**` expansion per path. Memoising the
expansion is the 2.1x on the `**` case and is neutral on the simple case.
Pattern count multiplies path count, so `**` rules are the cost driver.

Correctness of the cache was checked two ways, after two wrong versions:

- Caching *results* keyed on (pattern, segment count) is unsound: `*.tmp`
  matches the 4-segment `/bak/E/a.tmp` but not `/bak/E/b.txt`. That version
  returned `True` for `b.txt`.
- Keying on (pattern text, part count) collides, because mid-expansion children
  of one pattern share both. `/a/**/b/**/c` stopped matching `/a/x/b/y/c`.
  Correct key is the resolved parts themselves.
- Differential check: 20,862 (pattern-set, path) combinations, warm vs cold
  cache, 0 mismatches. All 242 ported golden cases pass.

### Server-side pruning of the `--repo` listing — rejected

Hypothesis: `restic ls <snap>:<subdir>` avoids descending into excluded
directories, saving IO on the repository.

| Approach | Time |
|----------|-----:|
| One full `restic ls --long`, 80k-file snapshot | 1.8s |
| 200 pruned `ls <snap>:<subdir>` calls | 150s |

84x slower. Each restic invocation costs ~0.75s before doing any work (index +
tree load), so N pruned calls lose to one full listing at any N above two. On a
remote backend the fixed cost is a network round trip. Not implemented.

### External tooling evaluated and rejected

- `restic backup --dry-run --json` reports only what it **kept** (`unchanged` /
  `modified` actions); excluded paths are never emitted. Cannot answer "what
  would these rules exclude".
- `find` translation of restic patterns is not viable: `!` negation has no
  `find` equivalent, and `**` semantics differ. `find` itself is ~4x faster than
  the Python walk, so this is a real loss of headroom, but it cannot be had.
- Multiprocessing: not pursued. Single-threaded walk is 4.7s on 80k files with a
  realistic pattern set, and `os.scandir` releases the GIL.

### Duplicate `apply.plan()` in the queued phase

`warns_on_newest(apply.plan(runner))` planned, then the task handler planned
again: two `restic ls --long` per stored snapshot per job. Now 4 restic calls
per job where there were 6, with `Plan` carrying the newest snapshot id so the
check does not re-list the repository.

### Exclude semantics: undocumented upstream (restic 0.19.1)

restic's backup documentation covers pattern *syntax* in detail — `filepath.Match`
per segment, `**` as a complete path component, leading `/` anchoring, a trailing
`/` being ignored, `$VAR`/`$$`, and `~` not expanded. All of that is a stable
specification and is stated as plain rules in SPEC.md with no need for anything
here.

Negation and prune behaviour, by contrast, are **undocumented**:
`grep -ci "negat" doc/040_backup.rst` is 0, and nothing describes what a matched
directory does to later rules. The behaviour established for restic 0.19.1:

| Behaviour | Observed |
|-----------|----------|
| `!` negates; last match wins | `--exclude '*' --exclude '!keep.txt'` stores only `keep.txt` |
| A `!` cancels an earlier rule on the same target | `--exclude cache --exclude '!cache'` stores the directory |
| A `!` cannot reach inside a pruned directory | with `node_modules/` matched, `!node_modules/keepme/` and `!node_modules/keepme/vital.js` are both ignored |
| A `!` naming the directory itself works | `logs/` then `!logs/` restores the directory and its contents |

**Red flag.** These are the volatile kind: no upstream document pins them, so a
restic release may change any of them, and a change is expected to be made
towards whatever restic's own behaviour becomes. snap-bak's port must follow, and
`tests/test_excludes.py` (golden cases) plus the CLI integration test are the
guard. If one of these ever stops holding, treat it as an upstream change to
port, not as a snap-bak bug.

Contrast with a genuine docs/code divergence, which is the more serious case and
would be recorded here separately: had restic excluded a *directory* under
`foo/` while its docs said the trailing `/` is ignored, that would be an
upstream bug to be reported and to be worked around only until it is fixed.

### Test coverage audit

Three tests written to accompany the trial-pattern-set documentation were
removed as redundant. They re-tested prune semantics that `walk_excluded` never
changed and which the ported golden suite already pins — including the identical
`["**/cache", "!cache/keep"]` shape at `test_excludes.py:219-241`. Confirmed by
running them against the pre-feature code (identical output) and by mutation
testing: prepending trial rules instead of appending them is caught by the tests
that remain and by none of the removed ones.

One of the surviving tests initially asserted "a trial can only tighten, never
loosen", which is **false** — a trial `!` does cancel a same-target config rule.
The docs were corrected to match.