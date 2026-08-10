# snap-bak Specification

## Overview

snap-bak is a simple btrfs snapshot → restic backup orchestrator.
It creates snapshots, and performs restic backups to efficiently manage retention.
Frequent snapshots instantly protect against accidents while older (known good) snapshots are used for weekly backups to minimize churn and extend retention.

### Constraints

- snap-bak is the exclusive authority for snapshot and backup management.
- Only snapshots matching snap-bak's expected format and directory are managed; all others are ignored.
- All datetimes in snap-bak are GMT/UTC, except the `backup_window_start`/`backup_window_end` times, which are interpreted in the server's local timezone.

### Design Principles

Validation → Planning → Execution
Backup from OLD snapshots (≥12 hours old). A future goal is to pick snapshots to minimize churn - avoid backing up a snapshot in the middle of active changes.
If a backup is delayed, the source snapshot is protected simply by being referenced in the status file.
A systemd timer invocation is one run. All size checks and state are fresh at start, cached during the run.
Simple process flow, one code path - DRY

### Features

- Backups limited to a daily time window ex: 1am - 8am
- Backup size and speed are tracked to attempt to sidestep out-of-space or running past timewindow. (however a running backup will not be stopped)

---

## Execution Flow

### Phase 1: Validate

Check global conditions once per run. If invalid, log error and exit.

```
- Config/status file(s) exist, valid syntax
- required files and directories exist with correct owner/group/permissions
- directories exist: Snapshot, Mount, status, restic cache
- Global Lock (prevent concurrent exec)
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
  1. Assert snapshot is ≥12 hours old (safety check)
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

Create new btrfs snapshot of `source_subvolume` at `{snapshot_dir}/{name}` for each job
Only one snapshot per `snapshot_interval` seconds per job.

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
    now = datetime.now(timezone.utc)
    hours = [ now - relativedelta(hours=i) for i in range(0, hourly) ]
    days  = [ now - relativedelta(hours=hourly, days=i) for i in range(0, daily) ]
    weeks = [ now - relativedelta(hours=hourly, days=daily, weeks=i) for i in range(0, weekly) ]
    months= [ now - relativedelta(hours=hourly, days=daily, weeks=weekly, months=i) for i in range(0, monthly) ]
    years = [ now - relativedelta(hours=hourly, days=daily, weeks=weekly, months=monthly, years=i) for i in range(0, yearly) ]
    return list(set(hours + days + weeks + months + years))

def build_retention_exponential(n:int = 1) -> list[datetime]:
    now = datetime.now(timezone.utc)
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
assert (datetime.now(timezone.utc) - snapshot.time) > timedelta(hours=12), \
    f"Snapshot {snapshot_name} is too new ({snapshot_time}). " \
    f"Backups must use snapshots >12 hours old."
```

---

## Configuration

### Config File Location

Default: `config/config.json` (repo-relative)
Override: `-c/--config` flag or `SNAPBAK_CONFIG_FILE` env var

### Config Schema

```jsonc
{
  "repos": [Repo],
  "jobs": [Job],
  "mount_base": "/bak",
  "stuck_threshold": "1h",        // int seconds or human-readable duration
  "stuck_wait": 300,              // how long to sleep between checking global lock
  "restic_update": "never"
}
```
`restic_update` (`after|never`, default `never`) controls when the privileged
wrapper's `update-restic` op runs. `after` = update only after a run in which a
backup actually completed (never on skipped/dry-run runs — updating restic
rewrites a binary carrying `cap_dac_read_search`, so it must never run without
reason). `never` = manual only. `before` is deliberately unsupported: it would
update before knowing whether any backup will happen.

### Repo Schema

```json
{
  "name": "undefined",
  "path": "",
  "password_file": "/home/user/.restic_password",
  "size_limit": "200GB",
  "latency_ms": 100,
  "throughput_mbps": 1,
  "restic_options": ["--compression=better"]
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Unique repo identifier |
| `path` | string | required | Restic repo path (e.g., `rclone:CONFIG_ENTRY:DIRECTORY`) |
| `password_file` | string | "" | Path to restic password file |
| `size_limit` | int/string | 0 | Max repo size (0 = unlimited). Int bytes or e.g. `"200GB"` |
| `latency_ms` | int | 100 | Estimated latency for ETA calculation |
| `throughput_mbps` | int | 1 | Estimated throughput for ETA calculation (seeding hint) |
| `restic_options` | list | `[]` | Extra restic CLI flags for every command against this repo |

### Job Schema

```json
{
  "name": "data",
  "snapshot_dir": "/mnt/data/.snapshots",
  "source_subvolume": "srv/web",
  "repo": "primary",
  "priority": 100,
  "snapshot_interval": "2h",
  "backup_retention": {"daily": 6, "monthly": 4, "yearly": 2},
  "snapshot_retention": {"fn": "exponential", "config": {"n": 5}},
  "backup_interval": "1w",
  "backup_window_start": "22:00",
  "backup_window_end": "06:00",
  "ignore_file": "/opt/snap-bak/excludes/data.ignore",
  "excludes": ["*.tmp", "*.cache", ".DS_Store"],
  "timeout": "24h",
  "tag": "data",
  "restic_options": []
}
```

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `name` | string | required | `[A-Za-z0-9_-]` | Job id; must be filesystem-safe (`<state>/<name>_status.json`, restic tag). Does NOT set the mount point |
| `snapshot_dir` | string | required | must exist | Path to btrfs snapshot directory |
| `source_subvolume` | string | required | — | Btrfs subvolume to snapshot; mount point = `<mount_base>/<last component>` |
| `repo` | string | required | must reference repo.name | Repo to backup to |
| `priority` | int | `100` | `ge=1` | Job priority (higher = more important) |
| `snapshot_interval` | int/string | `3600` | `ge=900` | Seconds between snapshots, or e.g. `"2h"` |
| `backup_retention` | dict | `{}` | — | Restic retention policy (e.g., `{"daily": 6}`) |
| `snapshot_retention` | dict | `{"fn":"exponential","config":{"n":5}}` | — | Local snapshot retention strategy |
| `backup_interval` | int/string | `3600` | `ge=1` | Seconds between backups, or e.g. `"1w"` |
| `backup_window_start` | string | `null` | `HH:MM` format | Backup window start (optional); interpreted in the server's LOCAL timezone |
| `backup_window_end` | string | `null` | `HH:MM` format | Backup window end (optional) |
| `ignore_file` | string | `""` | — | Restic exclude-from file |
| `excludes` | list | `[]` | — | Restic exclude patterns |
| `timeout` | int/string | `86400` | `ge=1` | Subprocess timeout: seconds or e.g. `"24h"` |
| `tag` | string | job name | `[A-Za-z0-9_-]` | Restic tag override |
| `restic_options` | list | `[]` | — | Extra restic CLI flags for this job |

`restic_options` on both Repo and Job are appended verbatim to restic
command lines (repo options first, then job options). Flags snap-bak manages
itself are filtered out with a warning if a config author sets them:
`-r/--repo`, `--password-file`, `--json`, `--dry-run`, `--tag`, `--keep-*`,
`--prune`, `--exclude`, `--exclude-file`.

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
    restic_options: list = Field(default_factory=list)

    @field_validator('size_limit', mode='before')
    @classmethod
    def _parse_size_limit(cls, v):
        return parse_size(v)  # int bytes or e.g. '200GB'
```

### Job

```python
class Job(BaseModel):
    model_config = ConfigDict(extra='allow')  # extra='allow' for test mocking

    # Config fields
    name: str
    snapshot_dir: str
    source_subvolume: str
    repo: str
    priority: int = Field(default=100, ge=1)
    snapshot_interval: int = Field(default=3600, ge=900)
    backup_retention: dict = Field(default_factory=dict)
    snapshot_retention: dict = Field(default_factory=lambda: {"fn": "exponential", "config": {"n": 5}})
    backup_interval: int = Field(default=3600, ge=1)
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list = Field(default_factory=list)
    timeout: int = Field(default=86400, ge=1)
    tag: str | None = None
    restic_options: list = Field(default_factory=list)

    @field_validator('snapshot_interval', 'backup_interval', 'timeout', mode='before')
    @classmethod
    def _parse_interval(cls, v):
        return parse_duration(v)  # int seconds or e.g. '2h', '1w'

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
    restic_update: str = "never"  # 'after' | 'never'
    stuck_wait: int = Field(default=300, ge=1)

    @field_validator('stuck_threshold', mode='before')
    @classmethod
    def _parse_stuck_threshold(cls, v):
        return parse_duration(v)  # int seconds or e.g. '1h'
```

Duration/size strings are parsed by `parse_duration` / `parse_size`
(units `s m h d w` and `b k/kb/kib m/mb/mib g/gb/gib t/tb/tib p/pb/pib`,
binary multiples). Bare integers pass through unchanged.

---

## Status File

Per-job status stored at `/opt/snap-bak/state/<job_name>_status.json`.

### Schema

```json
{
  "job": "data",
  "status": "success|skipped|delayed|failed|dry_run",
  "timestamp": "2026-07-26T10:30:00",
  "message": "Backup completed",
  "snapshot_name": "2026-07-25_22:00",
  "snapshot_time": "2026-07-25 22:00",
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

- Format: `YYYY-MM-DD_HH:MM`
- This is the only accepted format. Snapshots with any other naming convention are ignored.

### Selection Logic

1. **Normal backup**: Find oldest snapshot within `backup_interval` seconds
2. **Fallback**: If none within interval, use latest snapshot
3. **Delayed retry**: Use exact same snapshot (from `protected_snapshot` field)

### Safety Rule

**Never backup a snapshot newer than 12 hours old.**

```python
assert (datetime.now(timezone.utc) - snapshot_time).total_seconds() >= 12 * 3600
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

- Lock file: snap-bak.lock
- Uses `fcntl.flock(LOCK_EX | LOCK_NB)`
- Lock file contains JSON with PID, job info, timestamps
- Stuck snap-bak detection: if lock age > `stuck_threshold`
  - If locked PID no longer exists: break lock, warn user, continue
  - If locked PID exists: sleep stick_wait seconds and re-check
  - If lock releases (old process exits): continue normally
- if stuck_threshold exceeded -> temporary failure unless (now - lock age) > 12h

The global lock is the only lock. There is deliberately no per-job lock: it
would duplicate the global lock's job (preventing concurrent instances) for no
additional protection, so it was removed for simplicity.

---

### Contents

```
/opt/snap-bak           # owned by root
  snap_bak.py
  snap-bak-io           # owned by root - sudoers grants priviledges for snapshots, etc
  config.json           # owned by root
  rclone                # optional if restic uses rclone
  restic                # owned by root

/opt/snap-bak/state     # owned by snap-bak
  snap-bak.lock         # Global lock file
  throughput.json       # Per-repo transfer-rate history (capped at 10/repo)
  <job_name>_status.json  # Per-job status
```

---

## Throughput History

Observed transfer rates are recorded per repo in `throughput.json` (JSON
`{"repos": {"<name>": [mbps, ...]}}`, newest-first), capped at 10 entries per
repo. New rates seed from the median of existing entries (falling back to the
repo's configured `throughput_mbps`). The config file itself is never
rewritten. If the state directory is unwritable, the rate is logged and the
run continues.

---

## CLI

### Usage

```bash
snap-bak [OPTIONS]
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
| `SNAPBAK_SUDO` | Sudo binary used to invoke the wrapper |
| `SNAPBAK_IO_WRAPPER` | Path to the root-owned wrapper |

---

## Privileged Operations

Btrfs snapshot create/delete and mount/umount require root. snap-bak runs as
an unprivileged service user and delegates these to a **root-owned wrapper**
(`snap-bak-io`), reachable via a single static sudoers rule:

```
snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/snap-bak-io
```

A bare command name (no arguments listed) matches any arguments, per
sudoers(5). This is valid on both sudo-rs and sudo.ws; a trailing `*` is
deliberately NOT used because sudo-rs mishandles it. The wrapper:

- Accepts only `snapshot|delete|mount|umount <job> [snapshot]` and
  `update-restic`.
- Validates `<job>` against the root-owned `/opt/snap-bak/config.json`.
- Validates `[snapshot]` against `^\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$`.
- Confines every path under the job's `snapshot_dir` / `mount_base`
  (via `os.path.realpath` traversal guard).
- Executes `btrfs`, `mount`, `umount` with config-derived paths.
- Mounts snapshots read-only at `<mount_base>/<source_subvolume last
  component>` using `mount -o bind,ro` (btrfs has no uid/gid mount options).
  The mount point always mirrors the real filesystem path (e.g.
  `/mnt/data` -> `<mount_base>/data`) and stays stable across
  backups (restic change-tracking keys on the source path); it is never
  configurable. `<name>` is only a display id and does not affect the mount
  point.
- `update-restic` validates that the restic binary is a root-owned regular
  file in a root-owned, non-writable directory (rejecting symlinks and
  group/other-writable paths), runs `restic self-update` (GPG-verified), and
  re-applies `setcap cap_dac_read_search=+ep` — restic reads root-owned
  snapshots via this capability. Exit codes: `0` ok, `2` self-update failed
  (existing binary retained, setcap applied), `3` binary missing/unusable,
  `4` setcap failed.

Because the wrapper derives paths from config at runtime, changing
`mount_base` or `snapshot_dir` never requires editing sudoers.

The `restic_update` policy (`after|never`, default `never`) controls when the
orchestrator invokes `update-restic`. `after` runs it only after a run in
which a backup actually completed (never when every job was skipped, dry-run,
or failed) — updating restic rewrites a `cap_dac_read_search`-carrying binary,
so it must not run for no reason. `never` leaves updates to the administrator.

---

## Security Requirements

Invariants snap-bak must never breach:

1. **Least privilege.** Only `snap-bak-io` performs privileged operations.
   snap-bak is granted none of them; sudo permits only the wrapper.
2. **Root-owned files.** `snap-bak-io`, `config.json`, and
   `/usr/local/bin/restic` are root-owned, not writable by group/other — the
   wrapper is the root boundary, config is its trusted input, and restic
   carries `cap_dac_read_search`. Writable by snap-bak ⇒ trivial root
   escalation.
3. **Trust boundary.** `snap-bak-io` is trusted, snap-bak is not. The wrapper
   validates every argument against root-owned config; privileged ops never
   trust snap-bak-supplied paths/arguments.
4. **Update trust delegated to restic.** `restic self-update` verifies the
   download against the PGP key embedded in the binary and the GPG-signed
   `SHA256SUMS`. snap-bak adds no signature checks and must not bypass
   restic's. — [stable releases](https://restic.readthedocs.io/en/stable/020_installation.html#stable-releases)
5. **restic execution restricted.** The binary (with `cap_dac_read_search=+ep`)
   is executable only by root and snap-bak. — [file capabilities](https://restic.readthedocs.io/en/stable/080_examples.html#using-file-capabilities)

**NOTE:** anyone able to run the restic binary with `cap_dac_read_search=+ep`
can read files bypassing permissions. Only `snap-bak-io` may run it, and it
sets restic's source/destination itself — without trusted control over
restic's arguments, snap-bak could "back up" secret files to Mallory.

**TODO:** snap-bak currently runs restic directly, building source/repo args
from config (snap_bak.py). Requirement 5's "wrapper sets destination itself"
is not yet implemented — restic must be invoked via `snap-bak-io` so its args
come only from root-owned config.

---

## Testing

### Framework

- pytest with tmp_path / monkeypatch fixtures
- run via `uv run python -m pytest tests/`

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
- Restic option filtering / reserved-flag rejection
- snap-bak-io wrapper validation and path confinement
- Failed backup handling
- Config model
- Config loading
- Backup window logic

### Verification

```bash
uv run pytest -q
```

---

## Dependencies

- Python 3.13+
- python-dateutil (relativedelta for retention calculations)
- pydantic >= 2.0 (config models)
- restic (backup tool)
- btrfs-progs (snapshot management)
