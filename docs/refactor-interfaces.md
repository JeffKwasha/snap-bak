# snap-bak refactor — public interfaces

Goal: decompose the old monolithic `snap_bak.py` (~2k lines) into three aligned
modules and delete it. The entry point becomes `snap_bak.cli:main`. `restic.py`,
`snapshot.py` and `sudo-exec.py` are unchanged.

The design applies two rules everywhere:

1. **Process at initialization.** Every value the engine uses is computed once
   when the config is resolved (or the runner is constructed), never per-use.
   No property that re-scans a list, re-joins a path, or re-parses a string.
2. **Align data so if-else trees vanish.** One status object read once, one
   `Decision` from one `decide()` call, dict lookups instead of `if rc == X`
   chains.

Behavior (retention policies, intervals, backup windows, delayed-job handling,
missed-slot escalation, effective-priority ordering, size-limit allocation,
dry-run chokepoint, power-saving gate, throughput learning, mount reuse safety,
global-lock contention/stuck-wait, exit-code semantics, restic-update gating,
password gate, sudo trust model) is **preserved exactly**. The reference for
exact semantics is the current `src/snap_bak/job.py` (kept in the tree during
this work). Line numbers below refer to it.

---

## Module: `src/snap_bak/config.py`

"Initialize." Constants, env paths, human-readable quantity parsing, logging
setup, retention builders, and the config models. Imports only `snapshot`
(plus stdlib + pydantic + dateutil + json5). **Must not import `job` or `cli`.**

Dual-import fallback (also used by every module that imports a sibling):

```python
try:
    from snap_bak import snapshot
except ImportError:  # installed standalone under /opt/snap-bak/
    import snapshot
```

### Constants (module-level, public)

- `DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "config.json"`
- `DEFAULT_MOUNT_BASE = Path("/bak")`
- `DEFAULT_STUCK_THRESHOLD = 3600`
- `DEFAULT_STUCK_WAIT = 300`
- `STUCK_POLL_INTERVAL = 60`
- `DEFAULT_BACKUP_INTERVAL = 3600`
- `DEFAULT_SNAPSHOT_INTERVAL = 3600`
- `DEFAULT_SNAPSHOT_INTERVAL_MIN = 900`
- `DEFAULT_SNAPSHOT_AGE_MIN = 12 * 3600`
- `REPO_CHECK_TIMEOUT = 30`
- `REPO_STATS_TIMEOUT = 300`
- `SUDO = os.environ.get("SNAPBAK_SUDO", "/usr/bin/sudo")`
- `SUDO_EXEC = os.environ.get("SUDO_EXEC_PATH", "/opt/snap-bak/sudo-exec.py")`
- `STATE_DIR = Path(os.environ.get("SNAPBAK_STATE_DIR", "/opt/snap-bak/state"))`
  — `STATE_DIR.mkdir(parents=True, exist_ok=True)` at import (this is why the
  test conftest must set `SNAPBAK_STATE_DIR` before importing; see note below).
- `GLOBAL_LOCK_FILE = STATE_DIR / "snap-bak.lock"`

### Exit codes (module-level, public) — moved here from snap_bak.py lines 174–185

```
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_MOUNT_ERROR = 3
EXIT_PERMISSION_ERROR = 4
EXIT_TEMP_UNREACHABLE = 30
EXIT_TEMP_SIZE_LIMIT = 31
EXIT_PERM_TIMEOUT = 40
EXIT_PERM_FAILED = 41
EXIT_SYSTEM = 50
EXIT_STUCK_WAIT = 51
```

### Quantity parsing (lines 98–171)

```python
def load_config_json(text: str) -> dict          # json5.loads(text)
def parse_duration(value) -> int                 # bare int seconds or '90m'/'2h'/'1w'
def parse_size(value) -> int                     # bare int bytes or '200GB'/'1.5TiB'
```

`_parse_quantity` + `_DURATION_UNITS`/`_SIZE_UNITS` may remain private helpers.

### Logging (lines 288–332)

Set up at import: a stderr `StreamHandler` with format
`"[%(levelname)s] [%(job)s] %(message)s"` and a `_ContextFilter` that injects
the current job from a `contextvars.ContextVar`. Expose:

```python
logger                       # module logger (logging.getLogger(__name__) is fine;
                             #  the root basicConfig carries the handler)
def job_log_context(name: str) -> ContextManager[None]
```

Also expose `_STREAM_FORMAT` / `_FILE_FORMAT` (used by cli's `--log-file` handler).

### Retention (lines 200–269)

```python
Snapshot = snapshot.Snapshot          # re-export
def build_retention_exponential(n: int = 1) -> list[datetime]
def build_retention_datetimes(hourly=0, daily=0, weekly=0, monthly=0, yearly=0) -> list[datetime]
def build_snapshot_keeplist(snapshots: list[Snapshot], retentions: list[datetime]) -> set[Path]
RETENTION_FN: dict[str, Callable[..., list[datetime]]]
```

### `Repo` (lines 620–636)

pydantic model, `extra="ignore"`:

```python
class Repo(BaseModel):
    name: str
    path: str
    password_file: str = ""
    size_limit: int = 0            # ge=0, parse_size before-validation
    latency_ms: int = 100          # ge=0
    throughput_mbps: int = 1       # ge=0
    restic_options: list[str] = []
```

### `Window`

```python
class Window:
    def __init__(self, start_min: int, end_min: int): ...
    def contains(self, now: datetime) -> bool: ...      # local time, overnight-aware
    @classmethod
    def parse(cls, start: str | None, end: str | None) -> "Window | None":
        # None when either is missing; None (+ logger warning "Ignoring invalid
        # backup window format") when unparseable — preserves the fail-open
        # behavior of _is_within_backup_window (lines 1063–1092): no window =>
        # backups always allowed.
```

`contains` semantics must match `_is_within_backup_window` exactly: `start<end`
same-day `start<=now<end`; `start>=end` overnight `now>=start or now<end`.

### `Job` (lines 642–739)

pydantic model, `extra="allow"`. Config fields (validated) plus resolved fields
(populated once by `Config.resolve`, default `None`):

```python
class Job(BaseModel):
    # --- config fields ---
    name: str                                   # [A-Za-z0-9_-]+ (validator)
    snapshot_dir: Path                          # must exist (validator)
    source_subvolume: Path
    repo: str                                   # repo NAME (bound in Config.resolve)
    priority: int = 100                         # ge=1
    snapshot_interval: int = DEFAULT_SNAPSHOT_INTERVAL      # ge=MIN, parse_duration
    backup_retention: dict = {}
    snapshot_retention: dict = {"fn": "exponential", "config": {"n": 5}}
    backup_interval: int = DEFAULT_BACKUP_INTERVAL          # ge=1, parse_duration
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list[str] = []
    tag: str | None = None                      # [A-Za-z0-9_-]+ (validator, None ok)
    restic_options: list[str] = []
    skip_snapshot_if_idle: bool = True
    # --- resolved (set by Config.resolve) ---
    repo_obj: Repo | None = None
    mount_point: Path | None = None
    status_file: Path | None = None
    window: Window | None = None
    config_path: str | None = None
```

Resolved-field rules (set in `Config.resolve`, computed once):

- `repo_obj` — the `Repo` whose `name == job.repo` (validator already guarantees
  it exists).
- `mount_point = Path(config.mount_base) / job.source_subvolume.name`
  (matches `mount_point` property, lines 726–736).
- `status_file = STATE_DIR / f"{job.name}_status.json"` (line 740).
- `window = Window.parse(backup_window_start, backup_window_end)`.
- `tag` — mutate to `tag or name` (resolves `tag_arg` property, line 1338).

### `Config` (lines 1823–1894)

```python
class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    repos: list[Repo]
    jobs: list[Job]
    mount_base: str = "/bak"
    stuck_threshold: int = DEFAULT_STUCK_THRESHOLD     # ge=1, parse_duration
    stuck_wait: int = DEFAULT_STUCK_WAIT               # ge=1, parse_duration
    restic_update: str = "never"                       # in {"after","never"}

    def resolve(self, config_path: str | None = None) -> list[Job]: ...
```

- `__init__` warns `UserWarning` on unknown top-level keys (lines 1835–1847).
- `model_validator(mode="after")`: at least one repo; every `job.repo` exists in
  `repos` (lines 1863–1874).
- `resolve(config_path)` binds `repo_obj`/`mount_point`/`status_file`/`window`/
  `config_path`/resolved `tag` on each job and returns `self.jobs` (replaces the
  old `resolve_jobs`, lines 1876–1882).

### `Config.load` / `get` / `__getitem__` (lines 1885–1894)

```python
@classmethod
def load(cls, path: Path) -> Config          # missing/invalid -> sys.exit(EXIT_CONFIG_ERROR)
def get(self, name: str) -> ResolvedJob | None
def __getitem__(self, name: str) -> ResolvedJob   # KeyError when absent
```

---

## Module: `src/snap_bak/job.py`

The engine: errors, /proc helpers, global lock, status persistence, and the
`Runner` that executes one job. Imports `config` (constants + models) and
`snapshot` and `restic`. **Must not import `cli`.**

### Failure categories + exceptions (lines 187–197, 401–453)

```python
FAIL_CREDENTIALS = "credentials"
FAIL_PERMISSION = "permission"
FAIL_MOUNT = "mount"
FAIL_REMOTE = "remote_unavailable"
FAIL_LOCKED = "locked"
FAIL_SNAPSHOT = "snapshot"
FAIL_INTERNAL = "internal"

class BackupError(Exception):
    category = FAIL_INTERNAL
class CredentialsError(BackupError): category = FAIL_CREDENTIALS
class RepoLockedError(BackupError): category = FAIL_LOCKED
class RemoteUnavailableError(BackupError): category = FAIL_REMOTE
class MountError(BackupError): category = FAIL_MOUNT
class StuckInstanceError(Exception): ...

def categorize_restic_error(e: subprocess.CalledProcessError) -> BackupError:
    # {11: RepoLockedError, 12: CredentialsError}.get(rc, RemoteUnavailableError)
    # message format matches line 447.
```

### /proc helpers (lines 335–398)

```python
def restic_running() -> bool                       # True if any restic (not snap-bak) alive
def mounted_source(mount_point: Path) -> Path | None
```

### Global lock (lines 455–614) as a context manager

```python
class GlobalLock:
    def __init__(self, stuck_threshold: int = DEFAULT_STUCK_THRESHOLD,
                 stuck_wait: int = DEFAULT_STUCK_WAIT): ...
    def __enter__(self): ...        # acquire + write lock info; contention per
                                    # _resolve_lock_contention (fresh->EXIT_SYSTEM,
                                    # stuck+alive->poll then EXIT_STUCK_WAIT,
                                    # stale->break). Returns self.
    def __exit__(self, *exc): ...   # flock LOCK_UN + close + unlink

def touch_global_lock(job_name: str | None = None, snapshot_name: str | None = None): ...
```

Exact contention semantics from lines 512–567 are preserved. Lock-file writes
are in-place (never tmp+rename — would orphan the flock).

### Status persistence (lines 794–821, 960–1018)

One object read once, written once. The delayed group is a nested value, not
loose job fields.

```python
@dataclass
class DelayState:
    delayed: bool = False
    reason: str | None = None
    protected_snapshot: str | None = None
    missed_slots: int = 0
    def reset(self) -> None

def read_status(path: Path) -> dict | None          # json.loads; None on missing/bad
def delay_from_status(data: dict | None) -> DelayState   # restores delayed group

def write_status(path: Path, *, job: str, status: str, message: str,
                 delay: DelayState, snapshot_name: str | None = None,
                 snapshot_time: datetime | None = None,
                 restic_snapshot_id: str | None = None,
                 failure_category: str | None = None,
                 last_error: str | None = None) -> None
```

- File shape must match `_write_status` (lines 980–1004): keys `job`, `status`,
  `timestamp` (`snapshot.SNAPSHOT_FMT`, UTC), `message`, plus the delay group
  keys (`delayed`, `delayed_reason`, `protected_snapshot`, `missed_slots`) and
  optional `snapshot_name`/`snapshot_time`/`restic_snapshot_id`/
  `failure_category`/`last_error`. Atomic tmp+rename; `STATE_DIR.mkdir` first.
- `read_status` must parse the timestamp back to aware UTC for `last_success_time`
  (reuse the logic from `_get_last_backup_time`, lines 794–812).

### Results

```python
class ResultStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DELAYED = "delayed"
    DRY_RUN = "dry_run"
    RETENTION_OK = "retention_ok"
    RETENTION_FAILED = "retention_failed"

@dataclass
class Result:
    name: str
    status: ResultStatus
    reason: str | None = None      # delayed reason / skip reason / error text
    timeout: bool = False
```

`Result.status` compares equal to the bare string (`ResultStatus` subclasses
`str`), so `r.status == "failed"` works.

### `Runner`

One instance per resolved `Job`, holding per-run mutable state. Constructed once
and used for both the backup phase (`run`) and the retention phase
(`retention`/`prune_once`).

```python
class Runner:
    def __init__(self, job: Job, *, dry_run: bool = False,
                 repo_state: dict | None = None): ...
    def run(self) -> Result: ...
    def retention(self) -> Result: ...
    def prune_once(self) -> None: ...

    # effectively public helpers the CLI / tests touch:
    def effective_priority(self) -> int        # job.priority * (delay.missed_slots + 1)
```

Constructor semantics (this is where "read once" happens):
- store `job`, `dry_run`.
- `self.repo_state = repo_state or {}` (shared across runners in one run — the
  per-repo reachability/size cache, lines 1124–1136, 2044–2046).
- read `job.status_file` once into `self.last_status` (dict|None) and restore
  `self.delay` (a `DelayState`). Replaces `_load_delayed_state` +
  `_get_last_status` (lines 1188–1200, 814–821, 1734–1735).

`run()` pipeline (preserve exact behavior of `_run_backup_phase`, lines 1720–1817,
plus its helpers):

1. seed repo throughput from history (`_seed_repo_throughput`, lines 1523–1537).
2. `_check_stale_tmp` (lines 1006–1018).
3. write status "started" (`write_status` with current `delay`).
4. `_cleanup_snapshots(protected_snapshot=delay.protected_snapshot)` (lines 1461–1500).
5. if `_should_create_snapshot()`: `create_snapshot()` (lines 823–865, 921–947, 867–885).
6. if previous status == "failed" and snapshot still exists: retry that snapshot's
   backup (lines 1753–1771), else continue.
7. `decide()` (see below) → if skip/delay: write status + return Result.
8. select snapshot (`planned` or candidate), enforce 12h minimum age
   (`_snapshot_too_young`, lines 886–892), re-check window, then `_backup_snapshot`
   (lines 1684–1704) → Result.

Private helpers must preserve behavior (rename freely, keep semantics):

- `_run(cmd, check=True, timeout=None)` — the dry-run chokepoint + restic cache
  warning surfacing (lines 1259–1312).
- `_restic_cmd(subcommand, repo=None)` → `restic.build(...)` (lines 1314–1336).
- `_preflight` / `_backup` (lines 1345–1403).
- `_forget_only` (lines 1405–1422); `prune_once` (lines 1424–1432);
  `retention` (lines 1434–1459) — reports `retention_failed`, never raises.
- snapshot helpers collapse to the `snapshot` module directly:
  `snapshot.Snapshot.latest/scan/oldest_within` + `snapshot.parse_name` +
  `snapshot.snapshot_name` — no wrapper methods.
- `_wrapper_status` / `_should_create_snapshot` (lines 903–947) — power gate.
- `mount` context manager (lines 1567–1626) — reuse-mount + restic safety.
- throughput helpers (lines 1504–1563) — single shared read primitive.
- `_check_password_file_permissions` logic (lines 1022–1052) exposed as a
  module-level function the CLI calls:

```python
def check_password_file(repo: Repo) -> None   # sys.exit(EXIT_PERMISSION_ERROR) on unsafe
```

### `decide()` (replaces `_plan`, lines 1202–1255)

One function returning one decision; no scattered field mutation.

```python
@dataclass
class Decision:
    action: str            # "backup" | "skip" | "delayed"
    reason: str | None
    snapshot: Path | None  # planned snapshot for the delayed-retry case
```

Evaluation order and effects match `_plan` exactly (reachability → size-limit
allocation via `repo_state["allocated"]` → window → time-for-backup/delay-retry),
including missed_slots bookkeeping and `_delay` (lines 1173–1186).

---

## Module: `src/snap_bak/cli.py`

Entry point. Imports `config` and `job`. Defines `main`, argparse, and the serial
phase orchestration.

```python
def main() -> None          # wraps _main_impl; unexpected Exception ->
                            # logger.critical + sys.exit(EXIT_ERROR) (lines 1900–1908)
def _main_impl() -> None    # argparse + orchestration; sys.exit(...) with exit_code
def exit_code(results: list[Result]) -> int
def _restic_update_due(results: list[Result], restic_update: str) -> bool
def _run_restic_update() -> None
```

Behavior to preserve (lines 1968–2115):

- argparse: `-c/--config` (default `SNAPBAK_CONFIG_FILE` or `DEFAULT_CONFIG`),
  `-j/--job` (default `SNAPBAK_JOB`), `-n/--dry-run`, `--log-file`, `-l/--list-jobs`.
- `--log-file` adds a `FileHandler` with `_FILE_FORMAT` to the root logger.
- `load_config`; `--list-jobs` prints jobs and exits 0.
- `--job` filters; unknown job → error + `EXIT_ERROR`.
- mount_base must exist → else `EXIT_MOUNT_ERROR` (lines 2027–2031).
- Phase 1: `check_password_file(job.repo_obj)` for every job (lines 2033–2035).
- Acquire `GlobalLock(config.stuck_threshold, config.stuck_wait)` (context mgr),
  wrapping everything below in a `finally` release (lines 2037–2115).
- Resolve jobs once via `config.resolve(args.config)`; build one `Runner` per
  job with a **shared** `repo_state` dict; order by `effective_priority`
  descending; set `dry_run` (lines 2040–2051).
- Phase A — backups serially: for each runner `results.append(runner.run())`, then
  `touch_global_lock()` (lines 2057–2061).
- Phase B — retention serially per job with `backup_retention`: `runner.retention()`;
  collect `forgotten_repos` when status is `retention_ok`; then ONE `prune_once`
  per distinct repo path (lines 2063–2088); prune failure → `retention_failed`
  result appended (lines 2076–2088).
- `sys.exit(exit_code(results))` where `exit_code` implements the priority ladder
  from lines 2090–2103 as an ordered data-driven check:
  1. any `failed` → `EXIT_PERM_FAILED`
  2. any `timeout` → `EXIT_PERM_TIMEOUT`
  3. any `delayed` with `reason == "unreachable"` → `EXIT_TEMP_UNREACHABLE`
  4. any `delayed` → `EXIT_TEMP_SIZE_LIMIT`
  5. else `EXIT_SUCCESS`
- Log the "retention failed but backup succeeded" message when any
  `retention_failed` (lines 2104–2109).
- `_restic_update_due` / `_run_restic_update` (lines 1911–1956): update only when
  a backup succeeded this run; `_run_restic_update` uses `config.SUDO`/`SUDO_EXEC`,
  its own `subprocess.run` with 600s timeout, rc 0/2/other handling.

`exit_code` signature consumes `list[Result]`; internally use a table:

```python
def exit_code(results):
    def has(status, reason=None): ...
    for code in (EXIT_PERM_FAILED, EXIT_PERM_TIMEOUT, EXIT_TEMP_UNREACHABLE, EXIT_TEMP_SIZE_LIMIT):
        ...  # ordered predicate table
    return EXIT_SUCCESS
```

---

## Cross-cutting conventions

1. **Dual-import** in every module importing a sibling (see config.py header).
2. **Canonical owners.** `config` owns `STATE_DIR`/`GLOBAL_LOCK_FILE`/`SUDO`/
   `SUDO_EXEC`/exit codes; `job` owns lock/errors/proc/status; `cli` composes.
   Consumers reference `config.STATE_DIR`, `config.SUDO`, etc. via attribute
   access (not `from config import STATE_DIR`) so tests patch the owner once.
3. **No per-use recomputation.** No `repo_obj` scan, no `mount_point`/`status_file`
   path re-join, no `tag or name`, no window re-parse after `Config.resolve`.
4. **Loggers.** `config.py` configures the root logger once at import; `job`/`cli`
   use `logging.getLogger(__name__)`.
5. **Style.** Follow the existing code style (rationale comments welcome where
   non-obvious; no obvious-code comments). Must pass `ruff check` and
   `ruff format --check`. Python 3.10+.
6. **Line budget (non-blank, `awk 'NF'`):** `config.py` ≲ 300, `cli.py` ≲ 180,
   `job.py` < 500.

## Test conftest note

`tests/conftest.py` currently sets `SNAPBAK_STATE_DIR` before importing
`snap_bak`. It will be updated to import `snap_bak.config` instead (and
`snap_bak.job`/`snap_bak.cli`); the env-var-before-import ordering stays, since
`config.py` performs `STATE_DIR.mkdir` at import time.
