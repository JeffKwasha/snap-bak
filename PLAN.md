# snap-bak Improvement Plan

Date: 2026-07-25
Status: Phase 4 in progress

> **Historical document.** This plans the earlier Pydantic/delayed-job work and
> is kept for context. The authoritative design for inspecting and applying
> exclude patterns is SPEC.md → "Excludes: Inspection & Application"; the user
> interface is `snap-bak show excludes` / `snap-bak apply excludes`.

### Phase 4: Collapse Excludes Onto One Planner and One Executor

The feature was built as several parallel implementations of the same question.
Phase 3's design stands; this phase removes the duplication that made its
guarantees false.

#### The problem

| Concern | `cli.py` (reachable, correct) | `job.py` `Runner` (unreachable, divergent) |
|---------|------------------------------|------------------------------------------|
| restic scan loop | `_do_repo` :323, `_plan_for` :470 | `apply_excludes` :851 |
| target predicate | `label in found.per_snapshot` | `len(entries) > before or any(...)` |
| `ls --long` | `_restic_ls` :393 (no error handling) | `_repo_listing` :810 |
| snapshots list | `_repo_snapshots` :357 | `_restic_snapshots` :791 |
| preflight | `_preflight` :587 | inline :895, and a third copy :783 |
| rewrite | `_apply_now_one` :534 | `apply_excludes` :919 |

Consequences, all of which are defects rather than style:

1. **The scheduled path was dead.** `apply_excludes: true` and `--later` were
   silent no-ops: `_maybe_apply_excludes` had no callers and `consume_pending`
   was reachable only from tests. SPEC.md:621 ("the next run consumes it
   regardless of the config key") was untrue.
2. **It would have crashed on first use.** `_maybe_apply_excludes` took
   `runner.job` (a `Job`, job.py:530) where it needed a `ResolvedJob`, so
   `half_window_remaining` and `patterns_for_job` both raised `AttributeError`.
3. **The target predicate was wrong.** `apply_excludes` selected only snapshots
   introducing a *newly seen* path. Restic lists newest-first, so with an
   excluded file present in N snapshots it rewrote 1 and kept the rest forever.
   The dry-run said N. "What a user approves is what runs" held for `--now` and
   was false for the scheduled run.
4. **The dry-run was a second implementation**, not the first one with output
   suppressed — which is exactly how (3) survived.
5. **`apply` silently ignored its own selectors**: `--live`/`--snaps`/`--all`
   were accepted and discarded, so `apply excludes --live --now` rewrote restic.

440 tests passed throughout. `Runner.apply_excludes` had zero callers and zero
tests; the integration "apply" tests called raw `restic rewrite`, never snap-bak,
and used a single snapshot — the one shape in which both predicates agree.

#### The design

One planner, one executor. A mode is what you *do* with a `Plan`, not a variant
of the code that produces it.

```python
# src/snap_bak/apply.py
@dataclass(frozen=True)
class Plan:
    job: str; root: str; found: ExclusionSet
    snapshot_ids: tuple[str, ...]     # the only target predicate
    command: tuple[str, ...]          # the exact rewrite argv

@dataclass(frozen=True)
class Outcome:                        # deliberately not job.Result
    status: str                       # applied | noop | failed | dry-run
    detail: str
    rc: int | None = None

def plan(runner) -> Plan              # read-only scan
def execute(runner, plan) -> Outcome  # preflight + rewrite; never raises, never prunes
```

`Outcome` is not a `Result` so that "this phase cannot change the run's exit
code" (SPEC) is structural: apply outcomes are logged, never passed to
`exit_code()`. Previously an `APPLIED` result was appended to the results list
and merely happened to fall through the ladder.

`Plan.command` is built once and both printed by the dry-run and executed, so
the printed command cannot differ from the run command (SPEC.md:581).

`execute` never prunes. Rewrite is per job; prune is per repo. That mirrors
Phase B's existing `forgotten_repos` pattern, removes the duplicate prune for a
shared repo, and lets the scheduled path prune a repo that has no
`backup_retention` at all.

`Runner` gains public `repo_snapshots()` and `repo_listing(id)` — public
because the planner legitimately needs them. `cli.py` constructing
`job.Runner(rj)` and calling `_run`/`_restic_cmd` is what caused defect (2).

#### Verb surface

`apply` is restic-only by construction, so its plan comes solely from
`plan(runner)` — the same function `show excludes --repo` calls. "Forgets from
every restic repo, as shown by the `repo` dry run" is then not a promise kept by
discipline but the only available code path.

| Verb | Flags |
|------|-------|
| `snap-bak show excludes` | `--live` / `--snaps` / `--repo` / `--all`, `--limit`, `--json` — read-only; never creates, removes, or modifies a snapshot or local file |
| `snap-bak apply excludes` | `--now` / `--later`, `--json` — errors on `--live`/`--snaps`/`--all`, assumes `--repo` |

Pattern linting runs on every `show` and is silent when clean, so it is not a
flag. Per-verb parsers, so a selector cannot be silently discarded.

A `backup` verb (canonical spelling for the existing bare invocation, which
keeps working) is a separate follow-up: it needs no flags of its own, so it is
`_main_parser().parse_args(argv[:sub] + argv[sub+1:])`.

#### Orchestration

```
Phase A   backups (unchanged)
Phase A2  apply-excludes, gated → rewritten_repos
Phase B   retention (unchanged)  → forgotten_repos
Phase C   prune(union) once per distinct repo
```

Inside the existing `GlobalLock`, which stays exactly where it is: the
orchestrator is what creates and deletes btrfs snapshots, which is why the lock
is not superfluous. `armed = job.consume_pending()` is read once at run start;
the arming gate is `job.apply_excludes or job.name in armed`, so it never fires
by default (config.py:297-299).

`--now` takes no lock and adds no `--retry-lock`: restic serializes with its own
lock (rc 11), so a contended `--now` fails instantly and the message recommends
`--later`, where execution rides the next scheduled run.

#### What this resolves

Defects (1)-(5) disappear by deletion rather than by patch, because there is
only one of each left. Also fixed: the dry-run writing a real stamp under `-n`,
the unguarded `ls`, the stale pre-rewrite stamp (full re-scan every run), the
`--json` round-trip through `_entries_json` → `_snaps_set` for rendering, and the
duplicate prune per shared repo.

Not defects, left alone: the `decide()` window-before-`_repo_state` reordering
(it makes escalation a count of missed windows rather than of off-window runs),
and `half_window_remaining` returning true before a daytime window starts (unreachable
behind the window gate).

#### Tests, written first

1. Integration, driving snap-bak against real restic with **three** snapshots
   sharing one excluded path: all three rewritten. Fails on defect (3) today.
2. Orchestration: `apply_excludes: true` rewrites and consumes a `--later` arm;
   key off with no arm, it does not. Fails on defect (1) today.
3. CLI: `apply excludes --live` exits non-zero; `--lint` is not a flag.
4. The three raw-restic "apply" integration tests rewritten to call snap-bak.

#### Sequencing

Tests, then code, then full suite, then commit. One coherent change: the
individual fixes only make sense as part of the collapse, since defect (3)'s
fix is "delete the second copy of the predicate".

### Phase 3: Exclude Inspection & Application

Delivered on `feature/excludes-inspect-and-apply` (see Phase 4 for the
follow-up that actually made this true):

| Concern | Decision |
|---------|----------|
| Matcher | Faithful port of restic's `internal/filter` + Go `filepath.Match`, not `fnmatch`; restic's own 152 golden cases ported as tests |
| Path basis | The mount-point path restic stored (`/bak/<subvol>`), for every mode, so testing patterns never needs a mount |
| Commands | `show excludes` (read-only: `--live`/`--snaps`/`--repo`/`--all`) and `apply excludes` (`--now`/`--later`) — grouped by verb, not by target |
| Default | `show excludes` with no selector = lint (always, silent when clean) + `--live`: local and instant |
| Rewrite | One `rewrite <ids> … --forget`, never a separate `forget` (which would apply retention policies too) |
| Prune | Folded into the run's existing prune, so one pass reclaims retention + cleanup |
| Arming | `apply_excludes: true` (persistent) or `--later` (one-shot); default off, so editing `excludes` never rewrites history |
| Gate | In-window + half the window left + repo reachable/unlocked + matches exist + completion stamp current |
| Failures | Logged with failing phase and restic exit code; never change backup results or the run's exit code; no retry counter |
| Re-scan cost | Completion stamp keyed on a pattern digest + newest snapshot time |

## Overview

Two-phase plan:
- **Phase 1**: Pydantic config models + password_file per-repo + mount base /bak + error codes
- **Phase 2**: Delayed job handling (separate plan)

---

## Phase 1: Pydantic Config + password_file per-repo

### Decisions

| Topic | Decision |
|-------|----------|
| Config library | Pydantic v2 models |
| `password_file` | Per-repo, not top-level |
| `snapshots_to_keep` | `Field(ge=1)` — must be positive |
| `snapshot_dir` | `field_validator` — hard error if path doesn't exist |
| `backup_interval` | `Field(ge=1)` |
| `backup_retries` | `Field(ge=1)` — no infinite (-1) |
| `timeout` | Default 86400 (24h), respects backup window |
| `excludes` | List of restic `--exclude` patterns |
| Mount base | `/bak` default, error if missing |
| Extra config keys | Custom `__init__` that warns |
| Error codes | 0=success, 1=general, 2=config, 3=mount, 4=permission |

### config/config.json

```json
{
  "repos": [
    {
      "name": "primary",
      "path": "rclone:CONFIG_ENTRY:DIRECTORY",
      "password_file": "/home/user/.restic_password",
      "size_limit": 1099511627776,
      "latency_ms": 50,
      "throughput_mbps": 10
    }
  ],
  "mount_base": "/bak",
  "stuck_threshold": 3600,
  "jobs": [
    {
      "name": "data",
      "snapshot_dir": "/mnt/data/.snapshots",
      "repo": "primary",
      "retention": {"daily": 6, "monthly": 4, "yearly": 2},
      "snapshots_to_keep": 5,
      "backup_interval": 3600,
      "backup_window_start": "22:00",
      "backup_window_end": "06:00",
      "ignore_file": "/etc/snap-bak/data.ignore",
      "excludes": ["*.tmp", "*.cache", ".DS_Store"],
      "backup_retries": 3,
      "timeout": 86400
    }
  ]
}
```

### Pydantic Models

```python
from pydantic import BaseModel, Field, field_validator, ConfigDict
from pathlib import Path
import warnings

class Repo(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str
    path: str  # NOT Path — restic paths like 'rclone:CONFIG_ENTRY:DIRECTORY'
    password_file: str = ""
    size_limit: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=100, ge=0)
    throughput_mbps: int = Field(default=1, ge=0)

class Job(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str
    repo: str  # references Repo.name
    snapshot_dir: str
    retention: dict = Field(default_factory=dict)
    snapshots_to_keep: int = Field(default=5, ge=1)
    backup_interval: int = Field(default=3600, ge=1)
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list = Field(default_factory=list)
    backup_retries: int = Field(default=3, ge=1)
    timeout: int = Field(default=86400, ge=1)
    # Runtime fields (not from config)
    repos: list[Repo] = Field(default_factory=list, exclude=True)
    mount_base: str = "/bak"
    dry_run: bool = False
    config_path: str | None = None

    @field_validator('snapshot_dir')
    @classmethod
    def snapshot_dir_must_exist(cls, v):
        if not Path(v).exists():
            raise ValueError(f'snapshot_dir does not exist: {v}')
        return v

class SnapBakConfig(BaseModel):
    model_config = ConfigDict(extra='ignore')
    repos: list[Repo]
    jobs: list[Job]
    mount_base: str = "/bak"
    stuck_threshold: int = Field(default=3600, ge=1)

    def __init__(self, **data):
        known_keys = {'repos', 'jobs', 'mount_base', 'stuck_threshold'}
        unknown = set(data.keys()) - known_keys
        if unknown:
            warnings.warn(f"Unknown config keys: {unknown}")
        super().__init__(**data)
```

### Code Changes

#### 1. Replace dataclasses with pydantic models

| Location | Change |
|----------|--------|
| `Repo` class (line 177) | Replace `@dataclass` with `BaseModel`, add `password_file` |
| `Job` class (line 189) | Replace `@dataclass` with `BaseModel`, add validators |
| Add `SnapBakConfig` | New pydantic model for top-level config |

#### 2. Replace config loading

| Location | Change |
|----------|--------|
| `load_config` (line 781) | Replace with `SnapBakConfig.model_validate_json()` |
| `validate_config` (line 789) | Remove — pydantic handles validation |
| `jobs_from_config` (line 822) | Remove — `config.jobs` already returns Job objects |

#### 3. `self.repo` property returns Repo object

Current (line 209-215):
```python
@property
def repo(self) -> str:
    for r in self.repos:
        if r.name == self.repo_name:
            return r.path
```

Revised:
```python
@property
def repo(self) -> Repo:
    for r in self.repos:
        if r.name == self.repo_name:
            return r
```

Update callers:
- `_build_restic_cmd`: use `repo.path` and `repo.password_file`
- `main()` line 888: use `job.repo.path` for listing

#### 4. `_build_restic_cmd` uses Repo object

```python
def _build_restic_cmd(self, subcommand: str, repo: Repo | None = None) -> list[str]:
    if repo is None:
        repo = self.repo
    cmd = ["restic", "-r", repo.path, subcommand]
    if repo.password_file:
        cmd.extend(["--password-file", repo.password_file])
    if self.ignore_file:
        cmd.extend(["--exclude-file", self.ignore_file])
    for pattern in self.excludes:
        cmd.extend(["--exclude", pattern])
    return cmd
```

#### 5. Update method signatures

```python
def _preflight(self, mount_point: Path, repo: Repo | None = None) -> int:
def _backup(self, mount_point: Path, repo: Repo | None = None):
def _forget_and_prune(self, repo: Repo | None = None):
```

#### 6. Mount base /bak + existence check

- Change `DEFAULT_MOUNT_BASE` to `Path("/bak")`
- Add startup check in `main()`:
```python
mount_base = Path(config.mount_base)
if not mount_base.exists():
    logger.error(f"Mount base {mount_base} does not exist")
    sys.exit(3)
```

#### 7. Move permission checks before global lock

Current flow:
```
load_config → validate_config → jobs_from_config → acquire_global_lock → run()
```

Revised:
```
SnapBakConfig.load() → check mount_base → check password files → acquire_global_lock → run()
```

#### 8. Fix `mount_and_lock` to assert

Current (line 627-628):
```python
if snapshot_path is None:
    snapshot_path = self._get_latest_snapshot()
```

Revised:
```python
assert snapshot_path is not None, "mount_and_lock requires explicit snapshot_path"
```

#### 9. Error status codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (job failed, etc.) |
| 2 | Config validation error |
| 3 | Mount base not found |
| 4 | Permission error |

---

## Phase 2: Delayed Job Handling (separate plan)

### Design principles

1. Jobs have fixed repos (no dynamic selection)
2. If repo can't accept backup (size_limit exceeded, drive missing), job is delayed
3. Delayed snapshots are protected from cleanup
4. Delayed jobs retry on next run when conditions improve
5. Run `restic forget --prune` before size check to free space

### Changes (detailed in separate plan)

- Remove `_select_repo`
- Add size_limit check after forget/prune
- Add "delayed" status
- Add delayed snapshot protection
- Add timeout estimation (Phase 2)

---

## Implementation Order

1. Define pydantic models (Repo, Job, SnapBakConfig)
2. Replace config loading/validation
3. Update `self.repo` property + method signatures
4. Move `password_file` to Repo
5. Change mount base to /bak + existence check
6. Move permission checks before lock
7. Fix `mount_and_lock` assert
8. Add error status codes
9. Update config.json
10. Update tests
11. Update README
12. Run tests

## Verification

```bash
PYTHONPATH=. python -m pytest tests/test_snapbak.py -v
```

All tests must pass.
