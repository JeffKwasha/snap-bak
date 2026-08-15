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
Simplicity is a requirement: backups are critical infrastructure, and complex systems fail unpredictably. The flow must stay a single, simple code path (KISS/DRY).

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
- directories exist: Snapshot, Mount, status (the restic cache dir + its
  `.cache` symlink are provisioned by setup, not checked at runtime)
- password files are readable only by the owner and (optionally) the snap-bak
  service group; never by other users or with group write/execute (hard gate,
  exit 4)
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
      - Track the repo's available space for the rest of the run
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
For each job that planned a backup:
  1. Assert snapshot is ≥12 hours old (safety check)
  2. Mount OLD snapshot read-only
  3. Preflight (restic dry-run: check connectivity, estimate size)
  4. Backup to restic (retry on failure until the next backup slot)
  5. Forget + prune (apply retention policy)
   6. On success: subtract actual backup size used from the repo's available space; clear delayed state, reset missed_slots=0
  7. On failure: log error, record status, continue to next job
     - Do NOT replan remaining jobs
     - Do NOT retry this job in this run
```

A `failed` status persists the failing snapshot name; the next run retries that
exact snapshot before planning (same source data, same mount, same repo). This
failed-backup retry is separate from the delayed-job machinery: the two are
mutually exclusive statuses, so a delayed job never takes the failed-retry path
and vice versa. If the snapshot is gone (deleted by retention), the job skips
gracefully instead of failing.

> **Execution ordering**: although planning is described as Phase 2, a scheduled
> snapshot is created at the very start of the run (Phase 4). This is safe
> because the 12-hour minimum-age rule guarantees the new snapshot is never
> backed up in the same run (see Snapshot Safety Rule).

### Phase 4: Create Snapshots (btrfs)

Create new btrfs snapshot of `source_subvolume` at `{snapshot_dir}/{name}` for each job
Only one snapshot per `snapshot_interval` seconds per job.

A scheduled snapshot is **suppressed** when the power-saving gate says so:

1. **Disk spun down (power saving).** The wrapper's `status <job>` op resolves
   the source's btrfs pool to its physical disks (from kernel state:
   `/proc/self/mountinfo`, `/sys/fs/btrfs/<uuid>/devices`, `/sys/class/block`)
   and reports `any_spun_down`. If **any** rotational disk in the pool is spun
   down (`standby`/`slumber`/`powered_off`/`offline`/`sleep`), the snapshot is
   skipped — creating it would wake that drive. This gate makes no disk I/O at
   all (no btrfs calls, no snapshot-dir listing), so the drive stays asleep.
   Other states (`running`/`active`/`live`/`parked`) and unreadable states are
   never treated as spin-down; unknown pools fail open.
2. **No changes.** When the pool is not spun down, the wrapper compares the
   source's btrfs `Generation` with the latest snapshot's. Equal generations
   mean the source is unchanged since that snapshot, so the new snapshot would
   capture nothing and is skipped.

Both gates **fail open**: an error or indeterminate result falls back to the
historic behavior (create when the interval has elapsed). A fresh install (no
snapshot yet) always creates one. Config per job: `skip_snapshot_if_idle`
(default `true`) disables the spun-down-disk gate only; the no-change gate is
always on. Backups are independent of these gates and still run on schedule.

### Phase 5: Cleanup Local Snapshots (btrfs)

Retention is a choice of how timeslot boundaries are distributed — picking a
function and its parameters. Two strategies are supported:

```
snapshot_retention = {"fn": "exponential", "config": {"n": 9}}        # boundaries at 2^(i-1) h: 1h, 2h, 4h, 8h, ...
snapshot_retention = {"fn": "datetimes",  "config": {"hourly": 8, "daily": 7, "weekly": 4, "monthly": 3}}
```

Algorithm (shared by both strategies):

- Generate retention boundaries (oldest to newest) from the chosen function.
- For each boundary, keep the **oldest snapshot still newer than that boundary**.
- Delete every snapshot not on the keep-list.
- The snapshot currently being backed up (the protected snapshot) is always kept.
- Only snapshots under `snapshot_dir` matching the naming format are considered; all others are ignored.

---

## Configuration

### Config File Location

Default: `config/config.json` (repo-relative)
Override: `-c/--config` flag or `SNAPBAK_CONFIG_FILE` env var
Production: `SNAPBAK_CONFIG_FILE=/opt/snap-bak/config.json` (set by the systemd service)

### Config Schema

```jsonc
{
  "repos": [Repo],
  "jobs": [Job],
  "mount_base": "/bak",
  "stuck_threshold": "1h",        // int seconds or human-readable duration
  "stuck_wait": 300,              // total seconds to keep re-checking a stuck lock
  "restic_update": "never"
}
```

`restic_update` (`after|never`, default `never`) controls when the privileged
wrapper's `update-restic` op runs. `after` = update only after a run in which a
backup actually completed (never on skipped/dry-run runs — updating restic
rewrites a binary carrying `cap_dac_read_search`, so it must never run without
reason). `never` = manual only. `before` is deliberately unsupported: it would
update before knowing whether any backup will happen.

Duration and size fields (`stuck_threshold`, `stuck_wait`, `size_limit`,
`snapshot_interval`, `backup_interval`) accept either a bare
integer (seconds/bytes) or a human-readable string — e.g. `"90m"`, `"2h"`,
`"1w"`, `"200GB"`, `"1.5TiB"`. Units: durations `s m h d w`; sizes
`b k/kb/kib m/mb/mib g/gb/gib t/tb/tib p/pb/pib` (binary multiples).

### Repo Schema

```json
{
  "name": "primary",
  "path": "rclone:CONFIG_ENTRY:DIRECTORY",
  "password_file": "/opt/snap-bak/passwords/primary",
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
| `password_file` | string | "" | Path to restic password file (per-repo, under `/opt/snap-bak/passwords/`) |
| `size_limit` | int/string | 0 | Max repo size (0 = unlimited). Int bytes or e.g. `"200GB"` |
| `latency_ms` | int | 100 | Estimated latency for ETA calculation |
| `throughput_mbps` | int | 1 | Estimated throughput for ETA calculation (seeding hint) |
| `restic_options` | list | `[]` | Extra restic CLI flags against this repo; global options apply to every command, backup-only options to `backup` alone |

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
| `skip_snapshot_if_idle` | bool | `true` | — | Power saving: skip snapshot creation when any disk in the source's btrfs pool is physically spun down (never wake it), and never snapshot an unchanged subvolume. See Power Saving. |
| `backup_retention` | dict | `{}` | — | Restic retention policy (e.g., `{"daily": 6}`) |
| `snapshot_retention` | dict | `{"fn":"exponential","config":{"n":5}}` | — | Local snapshot retention strategy |
| `backup_interval` | int/string | `3600` | `ge=1` | Seconds between backups, or e.g. `"1w"` |
| `backup_window_start` | string | `null` | `HH:MM` format | Backup window start (optional); interpreted in the server's LOCAL timezone |
| `backup_window_end` | string | `null` | `HH:MM` format | Backup window end (optional) |
| `ignore_file` | string | `""` | — | Restic exclude-from file |
| `excludes` | list | `[]` | — | Restic exclude patterns |
| `tag` | string | job name | `[A-Za-z0-9_-]` | Restic tag override |
| `restic_options` | list | `[]` | — | Extra restic CLI flags for this job; global options apply to every command, backup-only options to `backup` alone |

The planning state fields that appear in the status file — `delayed`,
`delayed_reason`, `protected_snapshot`, `missed_slots` — are part of the
program's interface: they are persisted in the status file and never read from
config.

`restic_options` on both Repo and Job are appended verbatim to restic
command lines (repo options first, then job options). Options in restic's
global set (`RESTIC_GLOBAL_FLAGS` in `scripts/restic.py`, derived from
`restic --help`) apply to every restic command; **all other options are
backup-only** and apply to the `backup` subcommand alone. The `--exclude-file`
and `--exclude` flags (from `ignore_file`/`excludes`) are likewise
backup-only — other subcommands (e.g. `forget`) reject them. Flags snap-bak
manages itself are filtered out with a warning if a config author sets them:
`-r/--repo`, `--password-file`, `--json`, `--dry-run`, `--tag`, `--keep-*`,
`--prune`, `--exclude`, `--exclude-file`.

---

## Status File

Per-job status stored at `/opt/snap-bak/state/<job_name>_status.json`.

### Schema

```json
{
  "job": "data",
  "status": "started|success|skipped|delayed|failed|dry_run",
  "timestamp": "2026-07-26_10:30",
  "message": "Backup completed",
  "snapshot_name": "2026-07-25_22:00",
  "snapshot_time": "2026-07-25_22:00",
  "delayed": false,
  "delayed_reason": null,
  "protected_snapshot": null,
  "missed_slots": 0,
  "restic_snapshot_id": null,
  "failure_category": null,
  "last_error": null
}
```

`restic_snapshot_id` is present on success; `failure_category` and `last_error`
are present on failure. All fields except `job`, `status`, `timestamp`, and
`message` are optional.

The `delayed` group carries the delayed-job state (see Delayed Job Handling):
`delayed` is true while the job cannot proceed; `delayed_reason` is
`"unreachable"` or `"size_limit"`; `protected_snapshot` names the exact
snapshot to retry (same `YYYY-MM-DD_HH:MMZ` format as the snapshot naming rule);
`missed_slots` counts missed backup slots and feeds the priority system. On
success the group resets to `delayed=false`, `delayed_reason=null`,
`protected_snapshot=null`, `missed_slots=0`.

### Status Values

| Status | Meaning | Exit Code |
|--------|---------|-----------|
| `started` | Backup initiated | — |
| `success` | Backup completed | 0 |
| `skipped` | Not time / outside window / snapshot too young | 0 |
| `delayed` | Repo unreachable or over size_limit | 30-31 (temp) |
| `failed` | Backup failed and slot expired | 41 (perm) |
| `dry_run` | Preflight only | 0 |

---

## Error Codes

| Code | Constant | Category | Meaning |
|------|----------|----------|---------|
| 0 | EXIT_SUCCESS | Success | All jobs succeeded or skipped |
| 1 | EXIT_ERROR | General | Unexpected error |
| 2 | EXIT_CONFIG_ERROR | Config | Config validation failed |
| 3 | EXIT_MOUNT_ERROR | Mount | Mount base not found |
| 4 | EXIT_PERMISSION_ERROR | Permission | Password file readable by a group other than snap-bak, or by other |
| 30 | EXIT_TEMP_UNREACHABLE | Temporary | Repo unreachable (may self-rectify) |
| 31 | EXIT_TEMP_SIZE_LIMIT | Temporary | Repo over size_limit (may self-rectify) |
| 40 | EXIT_PERM_TIMEOUT | Permanent | Backup not completed in window |
| 41 | EXIT_PERM_FAILED | Permanent | Backup failed and slot expired |
| 50 | EXIT_SYSTEM | System | Lock acquisition failed (no stale instance to clean up) |
| 51 | EXIT_STUCK_WAIT | System | Stuck wait exceeded |

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

Delaying a job: mark it delayed with a reason, protect the source snapshot,
increment `missed_slots`, and record a `delayed` status naming the snapshot.

```
delay(snapshot, reason):
    mark job delayed (reason)
    protect snapshot
    missed_slots += 1
    write status "delayed", snapshot = snapshot
```

### Delayed Recovery Flow

On next run, during planning phase:

```
was_delayed = job was delayed last run

if time for a new backup:
    if was_delayed: missed_slots += 1
    clear delayed state (delayed=false, protected_snapshot=null)
    select an OLD snapshot normally
    plan backup

else if was_delayed and protected_snapshot still exists:
    plan backup of the exact same protected snapshot
    (same source data, same mount, same repo)

else:
    plan skip (no backup this run)
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

- Format: `YYYY-MM-DD_HH:MMZ`
- This is the only accepted format. Snapshots with any other naming convention are ignored.

### Selection Logic

1. **Normal backup**: Find oldest snapshot within `backup_interval` seconds
2. **Fallback**: If none within interval, use latest snapshot
3. **Delayed retry**: Use exact same snapshot (from `protected_snapshot` field)

### Safety Rule

**Never backup a snapshot newer than 12 hours old.**

A snapshot younger than 12 hours is skipped (never failed), so a snapshot just
created by the scheduler is only uploaded after it has had time to be noticed
and reviewed.

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

- Lock file: `<state>/snap-bak.lock`, an advisory `flock` (`LOCK_EX|LOCK_NB`).
  Records the holder PID and timestamps for stuck detection.
- `stuck_threshold` (config, default 3600s): lock age beyond which the holder
  is considered stuck.
- `stuck_wait` (config, default 300s): total time to keep re-checking a stuck
  lock before giving up.
- Stuck handling: if the holder PID no longer exists, break the lock and
  continue. If it still exists, poll (fixed 60s interval) until the lock clears
  or `stuck_wait` elapses; if `stuck_wait` is exceeded, exit `EXIT_STUCK_WAIT` (51).

The global lock is the only lock — there is deliberately **no per-job lock**.
Concurrent snap-bak instances are prevented solely by the global lock; a
per-job lock would add complexity without additional protection.

---

## State & Runtime Directories

```
<install>                # root-owned: program, wrapper, config
  cli.py                 # entry point / orchestration (root:root 0644)
  config.py              # config loading + models (root:root 0644)
  job.py                 # backup engine (root:root 0644)
  restic.py              # restic command building (root:root 0644)
  snapshot.py            # snapshot domain model (root:root 0644)
  sudo-exec.py            # sudo wrapper (root:root 0755)
  config.json            # root-owned, validated by the wrapper

<install>/state          # owned by snap-bak: runtime state
  snap-bak.lock          # Global lock file
  throughput.json        # Per-repo transfer-rate history
  <job_name>_status.json # Per-job status

<install>/excludes       # ignore_file location (root:snap-bak)
<install>/passwords      # per-repo restic password files (owned by the admin, group snap-bak)
<install>/cache          # restic cache (service-owned, real dir)
<install>/.cache         # symlink -> cache; restic's implicit $HOME/.cache default
```

All paths are relative to the install directory (`<install>`, production
`/opt/snap-bak`). The `restic` and `rclone` binaries are optional (rclone only
if a repo uses an `rclone:` path). No `.local` or `.config` directories are
used.

---

## Throughput History

Transfer rate statistics are kept per repo to provide stable backup time
estimates. If the state directory is unwritable, the rate is logged and the run
continues.

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
| `--log-file` | Also write logs to this file (default: stderr only; captured by systemd journal) |

### Environment Variables

| Variable | Description | Production value |
|----------|-------------|------------------|
| `SNAPBAK_CONFIG_FILE` | Config file path | `/opt/snap-bak/config.json` |
| `SNAPBAK_JOB` | Job name to run | — |
| `SNAPBAK_SUDO` | Sudo binary used to invoke the wrapper | `sudo` |
| `SUDO_EXEC_PATH` | Path to the root-owned wrapper | `/opt/snap-bak/sudo-exec.py` |
| `SNAPBAK_STATE_DIR` | State directory override (default: `/opt/snap-bak/state`) | `/opt/snap-bak/state` |
| `RESTIC_CACHE_DIR` | Restic cache base (not set; restic uses `$HOME/.cache` → `/opt/snap-bak/.cache` → `/opt/snap-bak/cache`) | — |

---

## Privileged Operations & Security

Btrfs snapshot create/delete and mount/umount require root. snap-bak runs as
an unprivileged service user and delegates these to a **root-owned wrapper**
(`sudo-exec.py`), reachable via a single static sudoers rule:

```
snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/sudo-exec.py
```

A bare command name (no arguments listed) matches any arguments, per
sudoers(5). This is valid on both sudo-rs and sudo.ws; a trailing `*` is
deliberately NOT used because sudo-rs mishandles it. The wrapper:

- Accepts only `snapshot|delete|mount|umount <job> [snapshot]`, `status <job>`,
  and `update-restic`.
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

Invariants snap-bak must never breach:

1. **Least privilege.** Only `sudo-exec.py` performs privileged operations.
   snap-bak is granted none of them; sudo permits only the wrapper.
2. **Root-owned files.** `sudo-exec.py`, `config.json`, and the restic binary
   are root-owned, not writable by group/other — the wrapper is the root
   boundary, config is its trusted input, and restic carries
   `cap_dac_read_search`. Writable by snap-bak ⇒ trivial root escalation.
3. **Trust boundary.** `sudo-exec.py` is trusted, snap-bak is not. The wrapper
   validates every argument against root-owned config; privileged ops never
   trust snap-bak-supplied paths/arguments.
4. **Update trust delegated to restic.** `restic self-update` verifies the
   download against the PGP key embedded in the binary and the GPG-signed
   `SHA256SUMS`. snap-bak adds no signature checks and must not bypass
   restic's. — [stable releases](https://restic.readthedocs.io/en/stable/020_installation.html#stable-releases)
5. **restic execution restricted.** The binary (with `cap_dac_read_search=+ep`)
   is executable only by root and snap-bak. — [file capabilities](https://restic.readthedocs.io/en/stable/080_examples.html#using-file-capabilities)

**NOTE:** anyone able to run the restic binary with `cap_dac_read_search=+ep`
can read files bypassing permissions. Only `sudo-exec.py` may run it, and it
sets restic's source/destination itself — without trusted control over
restic's arguments, snap-bak could "back up" secret files to Mallory.

**TODO:** snap-bak currently runs restic directly, building source/repo args
from config (job.py). Requirement 5's "wrapper sets destination itself"
is not yet implemented — restic must be invoked via `sudo-exec.py` so its args
come only from root-owned config.

---

## Testing

### Framework

- pytest (tmp_path / monkeypatch fixtures; `SNAPBAK_STATE_DIR` redirected in
  `conftest.py` so no state leaks into the real system)

### Test Categories

- Snapshot naming formats
- Retention algorithm
- Build retention exponential
- Build retention datetimes
- Build snapshot keeplist
- Datetimes cleanup integration
- Pydantic validation
- Snapshot selection
- Repo throughput learning
- Restic option filtering / reserved-flag rejection
- sudo-exec.py wrapper validation and path confinement
- Failed backup handling
- Config model
- Config loading
- Backup window logic

### Verification

```bash
uv run pytest -q
```

---

## Supported Platform & Dependencies

snap-bak supports:

- Python ≥ 3.10
- pydantic >= 2.0 (config models)
- python-dateutil (relativedelta for retention calculations)
- json5 (JSONC config parsing)
- restic (backup tool)
- btrfs-progs (snapshot management)
