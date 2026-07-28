# snap-bak Specification

## Overview

snap-bak is a simple btrfs snapshot → restic backup orchestrator.
It creates snapshots, and performs restic backups to efficiently manage retention with the goal being efficiency.
Snapshots are frequent with short retention, backups are infrequent leverage week old snapshots to avoid churn, and retain for years.
Both schedules are configurable, but backups are never made from live filesystems or the most recent snapshot.

### Design Principles

1. **Planning separates from action**: Validation → Planning → Execution. No replanning after execution begins.
2. **Never backup new snapshots**: Backups always use OLD snapshots (≥1 hour old). New snapshots are for future backups. This prevents persisting bad data.
3. **Protected snapshots**: When a backup is delayed, the source snapshot is protected simply by being referenced in the status file. No special deletion logic needed.
4. **One run = one process**: A systemd timer invocation is one run. All size checks and state are fresh at start, cached during the run.
5. **No complex state machines**: Delayed is a flag, not a separate code path. It affects the planning decision, not the execution flow.

### Protection Strategy

- Snapshots are young (hours to weeks old)
- Offsite backups are always slightly older
- Snapshots scheduled as a source for backup are protected
- Backups can be configured for a time window (1am - 8am)
- Backup size and speed are tracked to attempt to sidestep out-of-space or out-of-timewindow failures. (however a running backup will not be stopped)

---

## Execution Flow

### Phase 1: Validate

Check global conditions once per run. If invalid, log error and exit.

```
- Config/status file(s) exist, valid syntax (pydantic)
- Password files exist with correct permissions (warn if group/other readable)
- Snapshot directories exist (validated by pydantic field_validator)
- Mount base directory exists (snapshots are mounted at a static path for backup)
- Lock acquisition (global lock prevents multiple instances)
```

### Phase 2: Plan & Prepare (implicit)

Manipulate job state to determine what to do this run. No separate data structure — planning is implicit in job attributes.

```
For each job:
  1. Check repo reachability (restic cat config, 30s timeout)
     - If unreachable: mark job delayed (reason="unreachable")
     - If reachable: continue

   2. Check repo available space (restic stats --mode raw-data --json)
      - Fresh per run, once per repo, cached during run
      - Store in repo_available_space
      - If size_limit > 0 and repo size >= size_limit: mark job delayed (reason="size_limit")

   3. Check backup_window and time_for_backup (interval since last success)
      - If not within backup_window: Plan = skip (outside window)
      - Else if time_for_backup:
          - If was_delayed: missed_slots += 1
          - Clear delayed state
          - Select OLD snapshot for backup (normal selection)
          - Plan = backup

      - Else if was_delayed and protected_snapshot exists:
          - Plan = backup (use exact same protected snapshot)

      - Else:
          - Plan = skip (not time)

  4. Plan local snapshot creation (always — new snapshots are for future backups)

  5. Plan local cleanup (always — exponential cleanup, respect protected snapshot)
```

### Phase 3: Execute Backups (restic)

For each job with planned backup. Never uses today's new snapshot.

```
For each job with backup_planned:
  1. Assert snapshot is ≥1 hour old (safety check)
  2. Mount OLD snapshot read-only
  3. Preflight (restic dry-run: check connectivity, estimate size)
  4. Backup to restic (retry on failure until the next backup slot)
  5. Forget + prune (apply retention policy)
   6. On success: subtract actual backup size used from repo_available_space; clear delayed state, reset missed_slots=0
  7. On failure: log error, record status, continue to next job
     - Do NOT replan remaining jobs
     - Do NOT retry this job in this run
```

### Phase 4: Create Snapshots (btrfs)

Create new btrfs snapshots for each job by ISO timestamp `YYYY-MM-DD_HH:MM`

### Phase 5: Cleanup Local Snapshots (btrfs)

Retention
choice of how timeslots are distributed is choosing a function and its parameters
current support:

snapshot_retention = {
    "fn": "exponential",
    "config": {
        "n": 9
    }
}
OR
snapshot_retention = {
    "fn": "datetimes",
    "config": {
        "hourly": 8,
        "daily": 7,
        "weekly": 4,
        "monthly": 3,
    }
}


```python
RETENTION_FN = {
    "exponential": build_retention_exponential,
    "datetimes": build_retention_datetimes,
}

def build_retention_datetimes(hourly:int = 0, daily:int = 0, weekly:int = 0, monthly:int = 0, yearly:int = 0) -> list[datetime]:
    # Alternative strategy: create a flat list of retention boundaries for normal (hierarchical) retention.
    # Can be passed as `retentions` to build_snapshot_keeplist instead of using exponential tiers.
    now = datetime.now()
    hours = [ now - relativedelta(hours=i) for i in range(0, hourly) ]
    days  = [ now - relativedelta(hours=hourly, days=i) for i in range(0, daily) ]
    weeks = [ now - relativedelta(hours=hourly, days=daily, weeks=i) for i in range(0, weekly) ]
    months= [ now - relativedelta(hours=hourly, days=daily, weeks=weekly, months=i) for i in range(0, monthly) ]
    years = [ now - relativedelta(hours=hourly, days=daily, weeks=weekly, months=monthly, years=i) for i in range(0, yearly) ]
    return list(set(hours + days + weeks + months + years))

def build_retention_exponential(n:int = 1) -> list[datetime]:
    now = datetime.now()
    return [ now - relativedelta(hours=2**i) for i in range(0, n) ]


def build_snapshot_keeplist(snapshots: list[Snapshot], retentions: list[datetime]) -> set[Path]:
    # For each retention boundary (oldest to newest), keeps the oldest snapshot
    # that is still newer than that boundary.
    keeps = set()
    for boundary in sorted(retentions):
        candidates = [s for s in snapshots if s.time >= boundary]
        if candidates:
            oldest = min(candidates, key=lambda s: s.time)
            keeps.add(oldest.path)
    return keeps

class Snapshot(NamedTuple):
    path: Path
    time: datetime

def cleanup_snapshots(self:Job) -> int:
    if ARGS.dry_run:
        return 0
        
    cfg = self.snapshot_retention
    fn = RETENTION_FN[cfg["fn"]]
    retentions = fn(**cfg.get("config", {}))

    subs = [p for p in Path(self.snapshot_dir).iterdir() if p.is_dir() and re.match(r'^\d{8}_\d{6}$', p.stem)]
    valid = [Snapshot(p, self._parse_snapshot_time(p)) for p in subs if self._parse_snapshot_time(p) is not None]
    keeps = build_snapshot_keeplist(valid, retentions)

    to_delete = [s for s in valid if s.path not in keeps]
    for s in to_delete:
        snapshot_delete(s.path)

    LOG(f"deleted: {len(to_delete)}")
    return len(to_delete)
```

### Safety Constraint: Snapshot Age Assertion

```python
assert (datetime.now() - snapshot.time) > timedelta(hours=1), \
    f"Snapshot {snapshot_name} is too new ({snapshot_time}). " \
    f"Backups must use snapshots >1 hour old."
```

---

## Configuration

### Config File Location

Default: `$HOME/.config/snapbak/config.json`
Override: `-c/--config` flag or `SNAPBAK_CONFIG_FILE` env var

### Config Schema

```json
{
  "repos": [Repo],
  "jobs": [Job],
  "mount_base": "/bak",
  "stuck_wait": 300
}
```
When we detect a previous run is still running, we wait stuck_wait and keep checking.

### Repo Schema

```json
{
  "name": "undefined",
  "uri": "",
  "password_file": "/home/user/.restic_password",
  "size_limit": 0
  "latency_ms": 300,
  "throughput_mbps": 1
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Unique repo identifier |
| `uri` | string | required | Restic repo path (e.g., `rclone:CONFIG_ENTRY:DIRECTORY`) |
| `password_file` | string | "" | Path to restic password file |
| `size_limit` | int | 0 | Max repo size in bytes (0 = unlimited) |
| `latency_ms` | int | 100 | Estimated latency for ETA calculation |
| `throughput_mbps` | int | 1 | Estimated throughput for ETA calculation |

### Job Schema

```json
{
  "name": "data",
  "snapshot_dir": "/mnt/data/.snapshots",
  "repo": "primary",
  "priority": 100,
  "backup_retention": {"daily": 6, "monthly": 4, "yearly": 2},
  "snapshot_retention": {"fn": "exponential", "config": {"n": 5}},
  "backup_interval": 3600,
  "backup_window_start": "22:00",
  "backup_window_end": "06:00",
  "ignore_file": "/etc/snap-bak/data.ignore",
  "excludes": ["*.tmp", "*.cache", ".DS_Store"],
  "timeout": 7200
}
```

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `name` | string | required | — | Unique job identifier |
| `snapshot_dir` | string | required | must exist | Path to btrfs snapshot directory |
| `repo` | string | required | must reference repo.name | Repo to backup to |
| `priority` | int | `100` | `ge=1` | Job priority (higher = more important) |
| `backup_retention` | dict | `{}` | — | Restic retention policy (e.g., `{"daily": 6}`) |
| `snapshot_retention` | dict | `{"fn":"exponential","config":{"n":5}}` | — | Local snapshot retention strategy |
| `backup_interval` | int | `3600` | `ge=1` | Seconds between backups |
| `backup_window_start` | string | `null` | `HH:MM` format | Backup window start (optional) |
| `backup_window_end` | string | `null` | `HH:MM` format | Backup window end (optional) |
| `ignore_file` | string | `""` | — | Restic exclude-from file |
| `excludes` | list | `[]` | — | Restic exclude patterns |
| `timeout` | int | `86400` | `ge=1` | Subprocess timeout in seconds |

---

## Data Models (Pydantic)

### Repo

```python
class Repo(BaseModel):
    model_config = ConfigDict(extra='ignore')

    name: str
    path: str  # NOT Path — restic paths like 'rclone:CONFIG_ENTRY:DIRECTORY'
    password_file: str = ""
    size_limit: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=100, ge=0)
    throughput_mbps: int = Field(default=1, ge=0)
```

### Job

```python
class Job(BaseModel):
    model_config = ConfigDict(extra='allow')  # extra='allow' for test mocking

    # Config fields
    name: str
    repo: str
    snapshot_dir: str
    priority: int = Field(default=100, ge=1)
    backup_retention: dict = Field(default_factory=dict)
    snapshot_retention: dict = Field(default_factory=lambda: {"fn": "exponential", "config": {"n": 5}})
    backup_interval: int = Field(default=3600, ge=1)
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list = Field(default_factory=list)
    timeout: int = Field(default=86400, ge=1)

    # Runtime fields (not from config)
    repos: list = Field(default_factory=list, exclude=True)
    mount_base: str = "/bak"
    dry_run: bool = False
    config_path: str | None = None

    # Phase 2 runtime fields
    delayed: bool = Field(default=False, exclude=True)
    protected_snapshot: str | None = Field(default=None, exclude=True)
    missed_slots: int = Field(default=0, exclude=True)
    planned_snapshot: Path | None = Field(default=None, exclude=True)
    backup_planned: bool = Field(default=False, exclude=True)
```

### SnapBakConfig

```python
class SnapBakConfig(BaseModel):
    model_config = ConfigDict(extra='ignore')

    repos: list[Repo]
    jobs: list[Job]
    mount_base: str = "/bak"
    stuck_threshold: int = Field(default=3600, ge=1)
    stuck_wait: int = Field(default=600, ge=1)
```

---

## Status File

Per-job status stored at `~/.local/state/snapbak/<job_name>_status.json`.

### Schema

```json
{
  "job": "data",
  "status": "success|skipped|delayed|failed|dry_run",
  "timestamp": "2026-07-26T10:30:00",
  "message": "Backup completed",
  "snapshot_name": "20260725_220000",
  "snapshot_time": "2026-07-25T22:00:00",
  "delayed": false,
  "delayed_reason": null,
  "protected_snapshot": null,
  "missed_slots": 0
}
```

### Status Values

| Status | Meaning | Exit Code |
|--------|---------|-----------|
| `success` | Backup completed | 0 |
| `skipped` | Not time / outside window | 0 |
| `delayed` | Repo unreachable or over size_limit | 30-31 (temp) |
| `failed` | Backup failed and slot expired | 41 (perm) |
| `dry_run` | Preflight only | 0 |

### Delayed Fields

| Field | Type | Description |
|-------|------|-------------|
| `delayed` | bool | Whether job is currently delayed |
| `delayed_reason` | string | `"unreachable"` or `"size_limit"` |
| `protected_snapshot` | string | Snapshot name to retry (e.g., `"20260725_220000"`) |
| `missed_slots` | int | How many backup slots were missed (used for priority) |

---

## Error Codes

| Code | Constant | Category | Meaning |
|------|----------|----------|---------|
| 0 | EXIT_SUCCESS | Success | All jobs succeeded or skipped |
| 1 | EXIT_ERROR | General | Unexpected error |
| 2 | EXIT_CONFIG_ERROR | Config | Config validation failed |
| 3 | EXIT_MOUNT_ERROR | Mount | Mount base not found |
| 4 | EXIT_PERMISSION_ERROR | Permission | Password file readable by group/other |
| 30 | EXIT_TEMP_UNREACHABLE | Temporary | Repo unreachable (may self-rectify) |
| 31 | EXIT_TEMP_SIZE_LIMIT | Temporary | Repo over size_limit (may self-rectify) |
| 40 | EXIT_PERM_TIMEOUT | Permanent | Backup not completed in window |
| 41 | EXIT_PERM_FAILED | Permanent | Backup failed and slot expired |
| 50 | EXIT_SYSTEM | System | Lock acquisition failed (no stale instance to clean up) |
| 51 | EXIT_SYSTEM | System | Stuck wait exceeded |

### Temporary vs Permanent

- **Temporary (30-39)**: Can self-rectify on next run (e.g., network comes back, space frees up)
- **Permanent (40-49)**: Won't retry until next scheduled backup window
- **System (50-59)**: Infrastructure issues, not job-specific

---

## Delayed Job Handling

### When a Job is Delayed

A job is delayed when:

1. **Repo unreachable**: `restic cat config` fails (network, drive offline)
2. **Repo over size_limit**: After forget/prune, repo size exceeds `size_limit`

### Delay State

```python
def delay(self, snapshot_name: str, reason: str):
    """Mark this job as delayed."""
    self.delayed = True
    self.delayed_reason = reason
    self.protected_snapshot = snapshot_name
    self.missed_slots += 1
    self._write_status("delayed", f"{reason}: {snapshot_name}",
                       snapshot_name=snapshot_name)
```

### Delayed Recovery Flow

On next run, during planning phase:

```
was_delayed = self.delayed

if self._time_for_backup():
    # Time for new backup — delayed state is stale
    if was_delayed:
        self.missed_slots += 1
    self.delayed = False
    self.protected_snapshot = None
    # Select snapshot normally (OLD one)
    self.backup_planned = True

elif was_delayed and self.protected_snapshot:
    # Not time yet — retry the delayed backup
    # Use exact same snapshot (same source data, same mount, same repo)
    self.planned_snapshot = Path(self.snapshot_dir) / self.protected_snapshot
    self.backup_planned = True

else:
    # Not time, not delayed — skip
    self.backup_planned = False
```

### Protected Snapshot Lifecycle

- Protected snapshot = snapshot name referenced in `status_file["protected_snapshot"]`
- During exponential cleanup: simply not deleted
- Otherwise treated like any other snapshot (counted toward retention)
- When delayed state clears (success): protection clears automatically
- No special deletion logic needed

### Multi-Job Repo Handling

If multiple jobs share a repo:

1. Check repo reachability once per run (cached)
2. If unreachable: all jobs for that repo are delayed
3. If reachable but over size_limit:
   - Sort jobs by effective priority
   - Highest priority job runs
   - Remaining jobs for that repo are skipped (not delayed)
   - They accumulate missed_slots for next run

---

## Priority System

### Effective Priority

```
effective_priority = job.priority × (job.missed_slots + 1)
```

- Higher effective priority processed first
- `missed_slots` accumulates when a job is delayed
- Reset to 0 on success
- Default priority: 100

### Example

| Job | Priority | Missed Slots | Effective Priority |
|-----|----------|--------------|-------------------|
| A | 100 | 0 | 100 |
| B | 100 | 3 | 400 |
| C | 200 | 0 | 200 |

B runs first (400), then C (200), then A (100).

---

## Snapshot Naming

### Format

Two formats supported:

- ISO 8601: `2026-07-26T10:30:00`
- Compact: `20260726_103000`

### Selection Logic

1. **Normal backup**: Find oldest snapshot within `backup_interval` seconds
2. **Fallback**: If none within interval, use latest snapshot
3. **Delayed retry**: Use exact same snapshot (from `protected_snapshot` field)

### Safety Rule

**Never backup a snapshot newer than 1 hour old.**

```python
assert (datetime.now() - snapshot_time).total_seconds() >= 3600
```

---

## Cleanup

### Algorithm

Keeps N snapshots spaced exponentially:

- Tier 1: oldest snapshot within last 1 hour
- Tier 2: oldest snapshot within last 2 hours
- Tier 3: oldest snapshot within last 4 hours
- Tier N: oldest snapshot within last 2^(N-1) hours

---

## Backup Window

### Format

- `backup_window_start`: `HH:MM` (e.g., `"22:00"`)
- `backup_window_end`: `HH:MM` (e.g., `"06:00"`)

### Support

- Same-day windows (e.g., 09:00 to 17:00)
- Overnight windows (e.g., 22:00 to 06:00)
- No window = always allowed

---

## Global Lock

### Purpose

Prevents multiple snap-bak instances from running simultaneously.

### Implementation

- Lock file: `~/.local/state/snapbak/snapbak.lock`
- Uses `fcntl.flock(LOCK_EX | LOCK_NB)`
- Lock file contains JSON with PID, job info, timestamps
- Stuck detection: if lock age > `stuck_threshold` (default 3600s)
  - If locked PID no longer exists: break lock, warn user, continue
  - If locked PID exists: sleep 1 minute and re-check, up to `stuck_wait` seconds
  - If lock releases (old process exits): continue normally
  - If `stuck_wait` exceeded: log STUCK WAIT EXCEEDED error and exit

### Per-Job Lock

Each job also has a per-job lock file for `mount_and_lock`:
- Lock file: `~/.local/state/snapbak/<job_name>.lock`
- Prevents concurrent execution of the same job

---

## State Directory

Default: `~/.local/state/snapbak/`

Override: `XDG_STATE_HOME` env var

### Contents

```
~/.local/state/snapbak/
  snapbak.log          # Log file
  snapbak.lock         # Global lock file
  <job_name>.lock      # Per-job lock file
  <job_name>_status.json  # Per-job status
```

---

## CLI

### Usage

```bash
snapbak.py [OPTIONS]
```

### Options

| Flag | Description |
|------|-------------|
| `-c, --config` | Path to config JSON (default: `config/config.json`) |
| `-j, --job` | Run specific job by name (default: all jobs) |
| `-n, --dry-run` | Simulate backup (preflight only, no upload/delete) |
| `-l, --list-jobs` | List configured jobs and exit |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `SNAPBAK_CONFIG_FILE` | Config file path |
| `SNAPBAK_JOB` | Job name to run |
| `XDG_STATE_HOME` | State directory base |

---

## Testing

### Framework

- pytest with pyfakefs (filesystem mocking)
- 67 tests, all passing

### Test Categories

- Snapshot naming formats
- Retention algorithm
- Build retention exponential
- Build retention datetimes
- Build snapshot keeplist
- Datetimes cleanup integration
- Pydantic validation
- Repo model properties
- Job model properties
- Snapshot selection
- Repo throughput learning
- Failed backup handling
- Config model
- Config loading
- Backup window logic

### Verification

```bash
PYTHONPATH=. python -m pytest tests/test_snapbak.py -v
```

---

## Dependencies

- Python 3.13+
- python-dateutil (relativedelta for retention calculations)
- pydantic >= 2.0 (config models)
- restic (backup tool)
- btrfs-progs (snapshot management)
