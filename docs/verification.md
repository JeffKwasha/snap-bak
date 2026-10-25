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
---

## 2026-10-10, restic 0.19.1, Python 3.14 — what a "file" is, and what a symlink is

### Why this was measured

The report was changed from a per-path listing to a rule-by-source grid, so that
two sources could be compared on one page. Comparing them only means anything if
the two sides count and price things identically. Before this was checked they
did not, in three separate ways, each of which would have put two different
numbers in two columns of the same row.

### `restic ls --long` against symlinks and hardlinks

Setup: a tree with a regular file, a symlink to a file, a symlink to a
directory, a hardlinked pair, an empty directory and nested subdirectories,
backed up with real restic.

| Node | `restic ls --long` mode | Reported size |
|------|-------------------------|--------------:|
| regular file | `-rw-rw-r--` | full size |
| symlink to a file | `Lrwxrwxrwx` | **0** |
| symlink to a directory | `Lrwxrwxrwx` | **0** |
| hardlink, first path | `-rw-rw-r--` | full size |
| hardlink, second path | `-rw-rw-r--` | **full size, again** |
| directory | `drwx------` | 0 |

`restic ls --json` reports `type` as `file`, `dir` or `symlink` and gives a
hardlinked path no distinguishing field at all — so **restic's own listing
double-counts hardlink bytes**, and any tool that agrees with it must too.

Three consequences, all now pinned in `tests/test_excludes_integration.py`:

- **Symlinks are priced at 0, not at `lstat` size.** `lstat` on a symlink
  returns the length of its *target path* (11 and 3 bytes in the fixture above),
  which is not a payload restic holds. Charging it would make `--live` and
  `--repo` report different bytes for the same file.
- **Symlinks count as files** — one node each, and `restic rewrite` removes each
  of them.
- **Hardlinks count once per path**, matching the table above. The figure
  over-states what `prune` reclaims by exactly the shared block; that is stated
  rather than corrected, because agreeing with restic is the more useful
  property. The post-prune `stats` delta stays authoritative for reclaimed space.

### A symlink was never reported by `--repo` at all

`restic ls --long` renders a symlink as `path -> target`. `parse_ls_long` took
everything after the six header fields as the path, so a symlink's "path" was
`/bak/E/link.tmp -> keep.txt` — which matches **no pattern ever**. Every symlink
in a repository was silently absent from every `--repo` report.

Two details, both of which cost a wrong guess first:

- The arrow is split on the **last** ` -> `, so a file genuinely named `a -> b`
  still parses.
- Go renders the mode as `Lrwxrwxrwx`: **capital `L`**. A first attempt matched
  on lower-case `l`, which Go never emits, and the test caught it by still
  failing.

### Directories were being counted as files

`collect_from_listing` credited *every* entry under a matched directory,
including nested subdirectories and the directory's own row, as one file each.
`os.walk` counts only non-directory names. So `--repo` reported a higher file
count than `--live` for the identical subtree — 5 versus 3 on the fixture above.
Directories are no longer counted on either side, so a matched empty directory
reports 0 files on both.

### Cross-mode agreement, end to end

`test_the_live_walk_and_the_repository_report_the_same_numbers` walks a live
tree and reads back what restic stored for the same tree, then asserts that
`by_rule()`, `total_files`, `total_size` and the set of paths are all equal, and
that the grid built from them shows the same figure in each column. It is the
test that would have caught all three bugs above.

`test_a_matched_directory_reports_its_subtree_file_count` (unit) is deliberately
paired with `collect_from_listing`'s equivalent: the two pricing paths had
drifted apart, so each now has a test asserting the same numbers.

### Test suite wall time

Measured on this host, 16 cores, restic 0.19.1.

| Suite | Time |
|-------|-----:|
| Before | 157s |
| `init_repo` copying a template | 90s |
| `addopts = "-n auto"` | **17s** |

Where the time went: `restic init` costs **2.7s** and produces an **8 KB**
directory, so inits were 30 of the suite's invocations and most of its wall
time. Copying a pristine repo is ~1ms. What remained was **130 `restic`
invocations at ~0.7s each, which is ~92s of the 93.5s total** — the suite was
essentially all subprocess wait.

`restic init --repository-version 1|2` and a shared warm `RESTIC_CACHE_DIR` were
both tried and neither helps: 2.7s and 0.72s respectively either way. The 0.7s
is Go runtime and repo-config startup on this CPU, so the only lever is fewer
invocations or more cores.

**Sharing a backed-up repository per test file was considered and not done.**
It would remove the 36 `backup` calls (~26s), landing at ~70s serial — still
over the one-minute bar, because `snapshots` and `ls` calls made by the code
under test cannot be removed without faking restic. It would also have meant
giving ~30 tests a source tree at a shared path, and restic stores *absolute*
paths: the stored snapshot would only match a job configured against that exact
path. Saving 26s serially is not worth 30 call sites changed and a shared
mutable fixture. Parallelism gets the same 92s down to ~10s and changes no test
code.

Three consecutive parallel runs: 17.3s, 15.2s, 17.5s — no flakiness, 576 passed.
