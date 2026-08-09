# snap-bak

Btrfs Snapshot → Restic Backup Orchestrator.

## Purpose

A simple, robust, maintainable script that leverages Btrfs snapshots and
restic to efficiently meet 3-2-1 backup coverage with minimal disruption
to daily use.

## Requirements

- **Linux with Btrfs** — used for read-only snapshots and subvolume mount
- **Python ≥ 3.10**
- **restic** (in `$PATH`) — the backup engine; must be exec'd by the service user
- **`btrfs-progs` / `btrfs`** — `btrfs subvolume snapshot|delete` via sudo
- **`pydantic >= 2`** and **`python-dateutil >= 2`** — runtime Python deps
- **optional:** `rclone` — for `rclone:REMOTE:DIR` repos
- **optional:** systemd (Linux) — for the timer-driven service

## Installation

### Option A — Manual / development

```bash
# install python package + deps (or: cd repo && uv sync)
pip install -e .
```

Then configure `config/config.json` and run manually (`snap-bak --dry-run`)
or schedule via cron/systemd yourself.

### Option B — Systemd service (recommended for production)

The fully-automated setup script creates an unprivileged system user, installs
`snapbak.py`, configures granular sudo permissions, and wires up a
systemd *service + timer*.

```bash
sudo scripts/snapbak-setup.sh
sudo systemctl enable --now snap-bak.timer
```

What `scripts/snapbak-setup.sh` does:

1. Verifies you are `root`.
2. Creates the `snapbak-svc` user/group and `/opt/snap-bak`.
3. Installs the orchestrator to `/opt/snap-bak/snapbak.py` (root-owned).
4. Writes an annotated example config to `/opt/snap-bak/config.json`.
5. Creates `/opt/snap-bak/passwords` and `/opt/snap-bak/excludes`.
6. Creates `/mnt/snapbak_mounts` and grants `snapbak-svc` minimal sudo rights
   (btrfs snapshot create/delete restricted to `YYYY-MM-DD_HH:MM` names;
   mount/umount pinned to the mount base).
7. Writes `snap-bak.service` (oneshot) and `snap-bak.timer` (hourly).

The timer runs hourly; restic uploads happen per-job only when
`backup_interval` has elapsed (so weekly backups = `backup_interval: 604800`).

The setup script is **idempotent** — re-running it is safe: it skips the user,
group, and existing `config.json` (so your edits are preserved), refreshes the
program + unit files, and re-validates sudoers.

Before enabling, edit `/opt/snap-bak/config.json` to match your environment and
create the per-repo restic password files under `/opt/snap-bak/passwords/`.

## How It Works

### What snap-bak does

Creates Btrfs snapshots on a schedule, mounts them read-only, backs them
up to restic, and cleans up old snapshots using exponential retention.

### What snap-bak does NOT do

- Manage restic repos (init, etc.)

### Snapshot retention (local)

After a successful backup, old Btrfs snapshots are deleted using the
configured strategy. Controlled by `snapshot_retention` (default: exponential
with N=5). Two strategies are available:

- **exponential** — keeps N snapshots spaced at ~1h, 2h, 4h, 8h, 16h for fast
  local recovery
- **datetimes** — keeps snapshots at specific calendar boundaries (e.g.,
  hourly, daily, weekly)

### Backup retention (offsite)

restic manages its own retention via the `backup_retention` config (e.g.,
`{"daily": 6, "monthly": 4, "yearly": 2}`). Controls how long backup history
is kept in the remote repo. Runs `restic forget --prune` after each backup.

### How they overlap

Snapshots are young (minutes to hours old). Backups are always slightly older.
After a successful backup, snapshots older than the backup time are deleted
(data is now offsite). Younger snapshots remain for quick local restore.

## Permissions & Security

To balance security with the need for system-level operations, `snap-bak` employs a **Hybrid Privilege Model**.

### The Strategy
The orchestrator runs as a dedicated, unprivileged system user (`snapbak-svc`). It does not require full root access. Instead, it uses granular `sudo` permissions for specific kernel-level operations and leverages mount-time UID/GID mapping to bypass filesystem permission barriers.

### How it Works
1.  **Granular Escalation**: The `snapbak-svc` user is granted `NOPASSWD` access via `sudoers` only for the specific commands required to manage Btrfs subvolumes and mount/unmount filesystems.
2. **Mount with uid/gid mapping** — Snapshots are mounted read-only using the service user's uid/gid so restic can read all files without ACL complications:
    * **Command**: `mount -o ro,uid=<svc_uid>,gid=<svc_gid> <source> <target>`
    * **Effect**: The kernel presents the snapshot's contents as owned by the service user, allowing `restic` to read all files without modifying the original filesystem's permissions.
3.  **Minimal Surface Area**: The Restic process itself runs as the unprivileged `snapbak-svc` user, ensuring that even if the backup tool is compromised, the attacker's scope is limited to the service user's context.

- **Per-repo password files** — each restic repo has its own password file
- **Rate tracking** — measures backup speed, updates config for visibility
- **Pluggable snapshot retention** — exponential or calendar-based cleanup
- **Failed backup recovery** — automatic retry with same snapshot
- **Backup windows** — restrict to off-peak hours
- **Configurable retry** — per-job restic retry count
- **XDG-compliant paths** — state in `~/.local/state/snapbak/`
- **Crash recovery** — lock file with JSON job info

## Configuration

The config file is JSONC: standard JSON plus `//` and `/* */` comments and
trailing commas (comments are stripped by `snap-bak` before parsing). The
default config written by `snapbak-setup.sh` includes inline comments
describing every field.

Edit `config/config.json`:

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
      "source_subvolume": "data",
      "repo": "primary",
      "priority": 100,
      "snapshot_interval": 3600,
      "backup_retention": {"daily": 6, "monthly": 4, "yearly": 2},
      "snapshot_retention": {"fn": "exponential", "config": {"n": 5}},
      "backup_interval": 3600,
      "backup_window_start": "22:00",
      "backup_window_end": "06:00",
      "ignore_file": "/etc/snap-bak/data.ignore",
      "excludes": ["*.tmp", "*.cache", ".DS_Store"],
      "timeout": 86400
    }
  ]
}
```

### Top-level settings

| Field | Description | Default |
|-------|-------------|---------|
| `repos` | Array of restic repository definitions (required) | — |
| `mount_base` | Base directory for mounting snapshots | `/bak` |
| `stuck_threshold` | Seconds before a lock is considered stale | `3600` |

### Repo settings

| Field | Description | Default |
|-------|-------------|---------|
| `name` | Unique repo identifier (required) | — |
| `path` | Restic repository path (required) | — |
| `password_file` | Path to one-line text file containing the restic password | `""` |
| `size_limit` | Max backup size in bytes; `0` = unlimited | `0` |
| `latency_ms` | Estimated latency in milliseconds (metadata) | `100` |
| `throughput_mbps` | Estimated throughput in Mbps; updated after backups | `1` |

### Job settings

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `name` | string | Job name (required). Also derives local paths/tags (see below) | — |
| `snapshot_dir` | string | Directory containing Btrfs snapshots (required) | — |
| `source_subvolume` | string | Btrfs subvolume to snapshot (required) | — |
| `repo` | string | Name of repo to use (must match `repos[].name`) | — |
| `priority` | int | Job priority (higher = more important) | `100` |
| `snapshot_interval` | int | Seconds between snapshots (min 900) | `3600` |
| `backup_retention` | dict | Restic forget/keep policy (e.g., `{"daily": 6}`) | `{}` |
| `snapshot_retention` | dict | Local snapshot retention strategy | `{"fn":"exponential","config":{"n":5}}` |
| `backup_interval` | int | Minimum seconds between backups | `3600` |
| `backup_window_start` | string | Start of allowed backup window (HH:MM) | `null` |
| `backup_window_end` | string | End of allowed backup window (HH:MM, exclusive) | `null` |
| `ignore_file` | string | Path to file with exclude patterns | `""` |
| `excludes` | list | List of glob patterns to exclude | `[]` |
| `timeout` | int | Command timeout in seconds | `86400` |

**Note on `ignore_file` and `excludes`:** Both are passed to restic as
`--exclude-file` and `--exclude` flags respectively. They are additive —
both apply. `ignore_file` uses `.gitignore`-style syntax.

**Note on `backup_retention` vs `snapshot_retention`:** `backup_retention`
controls restic's offsite backup retention (how many backup versions to keep
in the remote repo). `snapshot_retention` controls local Btrfs snapshot
cleanup (how many snapshots to keep on disk).

**Note on `name`:** the job name is used to derive several paths and labels, so
it must be filesystem- and tag-safe. Use only `[A-Za-z0-9_-]` (no `/`, no `..`).

- Mount point: `<mount_base>/<name>` — the read-only snapshot is mounted here
  before backup
- restic tag: `--tag <name>` on every backup/forget
- Lock file: `<state_dir>/<name>.lock`
- Status file: `<state_dir>/<name>_status.json`

## Snapshot and Backup Flow

snap-bak is the exclusive authority for both snapshot creation and backup execution.
Users SHALL NOT use any other automated snapshot management tool, and SHALL NOT
run two copies of snap-bak concurrently.

### When Snapshots Are Created

At the start of every run, snap-bak creates a new btrfs read-only snapshot
whenever `snapshot_interval` has elapsed since the last one (or none exists yet):

```
btrfs subvolume snapshot -r <source_subvolume> <snapshot_dir>/<name>
```

The snapshot name uses the format `YYYY-MM-DD_HH:MM` (e.g., `2026-07-26_10:30`).
Snapshots are created at most once per `snapshot_interval` seconds (default: 1 hour,
minimum: 15 minutes). Creation is independent of backup success: fresh installs
create their first snapshot immediately, and a long-running backup never stalls
the snapshot cadence. Non-matching names in `snapshot_dir` are ignored.

### When Backups Are Created

A backup is created when **all** of the following are met:

1. **Time for backup**: Time since last successful backup ≥ `backup_interval`
   (default: 1 hour).
2. **Within backup window** (if configured): Current time is within
   `backup_window_start`–`backup_window_end`. Overnight windows are supported
   (e.g., `"22:00"` to `"06:00"` means 22:00–06:00). If no window is configured,
   backups are always allowed.

When a backup is triggered:
- The **oldest snapshot within the last `backup_interval`** is selected for backup.
- If no snapshot falls within the interval, the latest snapshot is used as a fallback.
- Snapshots younger than the minimum age (1 hour) are never backed up: the run
  records `skipped` and defers to a later cycle, so a snapshot just created by
  the scheduler is only uploaded after it has aged (e.g. on a fresh install).
- The snapshot is mounted read-only, preflight-checked, then backed up to the
  configured repo.
- After a successful backup, the repo's `throughput_mbps` is updated in the
  config file based on observed performance.

### Backup window examples

| `backup_window_start` | `backup_window_end` | Allowed times |
|----------------------|---------------------|---------------|
| `"22:00"` | `"06:00"` | 22:00–06:00 (overnight) |
| `"09:00"` | `"17:00"` | 09:00–17:00 (business hours) |
| `null` | `null` | Always (no restriction) |

## When Snapshots Are Deleted

After a successful backup, snapshots are deleted using the configured strategy
(`snapshot_retention`). Two strategies are available:

### Exponential strategy

```json
{"fn": "exponential", "config": {"n": 5}}
```

Keeps N snapshots with exponentially increasing time gaps. The algorithm
divides time into exponentially increasing tiers:

| Tier | Window | `2^(tier-1)` |
|------|--------|--------------|
| 1 | Last 1 hour | 2^0 = 1h |
| 2 | Last 2 hours | 2^1 = 2h |
| 3 | Last 4 hours | 2^2 = 4h |
| 4 | Last 8 hours | 2^3 = 8h |
| 5 | Last 16 hours | 2^4 = 16h |

For each tier, the **oldest snapshot** within that window is kept. All other
snapshots in that tier are deleted. This produces snapshots spaced approximately
at 1h, 2h, 4h, 8h, 16h ago — true exponential spacing.

### Datetimes strategy

```json
{"fn": "datetimes", "config": {"hourly": 3, "daily": 1, "weekly": 1}}
```

Keeps snapshots at specific calendar boundaries. Each boundary defines how
many snapshots to keep at that granularity:

- `hourly`: keeps N most recent hourly snapshots
- `daily`: keeps N most recent daily snapshots (one per day)
- `weekly`: keeps N most recent weekly snapshots (one per week)
- `monthly`: keeps N most recent monthly snapshots (one per month)
- `yearly`: keeps N most recent yearly snapshots (one per year)

### Common behavior

- The snapshot currently being backed up is **never deleted** during cleanup
  (protected snapshot).
- Non-standard snapshot names are skipped (not deleted).

### Edge cases

- If fewer snapshots exist than the strategy requests, all are kept.
- If `n` is `0` or negative (exponential), cleanup is skipped entirely.

## Failed Backup Recovery

If a backup fails:
1. The status file records `"failed"` with the snapshot name.
2. On the next run, snap-bak detects the failed status.
3. If the snapshot still exists, it retries the backup with the **same snapshot**
   (data hasn't changed).
4. If the snapshot no longer exists, the job is skipped with a warning.

## 3-2-1 Coverage

snap-bak achieves 3-2-1 backup coverage when configured with multiple jobs:

- **3 copies**: RAID1 Btrfs + offsite restic
- **2 media**: local RAID + offsite
- **1 offsite**: restic repository

For full coverage, configure two jobs:
1. **Local**: external hard drive (fast recovery)
2. **Cloud**: offsite via rclone (disaster recovery)

## CLI Usage

```bash
# Run all jobs
snap-bak

# Run a specific job
snap-bak --job data

# Dry run (preflight only, no upload or deletion)
snap-bak --dry-run

# List configured jobs
snap-bak --list-jobs

# Use a custom config file
snap-bak --config /path/to/config.json
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SNAPBAK_CONFIG_FILE` | Path to config JSON file | `config/config.json` |
| `SNAPBAK_JOB` | Run a specific job by name | (all jobs) |
| `XDG_STATE_HOME` | Base directory for state files | `~/.local/state` |

## State Files

All state files live under `$XDG_STATE_HOME/snapbak/` (defaults to
`~/.local/state/snapbak/`):

```
$XDG_STATE_HOME/snapbak/
├── snapbak.lock              # Global lock + current job info (JSON)
├── snapbak.log               # Log file
├── {job_name}.lock           # Per-job lock
├── {job_name}_status.json    # Per-job status (success/failed/skipped)
└── {job_name}_status.json.tmp # Atomic write temp file
```

### Lock file crash recovery

The global lock file stores JSON with:
- `pid`: Process ID
- `started_at`: When snap-bak started
- `current_job`: Name of the job currently running
- `snapshot_name`: Name of the snapshot being backed up
- `backup_started_at`: When the current backup started

On startup, if a stale lock is detected, the job info is logged to help
diagnose what was running when the crash occurred.

## Error Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (job failed, etc.) |
| 2 | Config validation error |
| 3 | Mount base not found |
| 4 | Permission error |

## Testing

```bash
PYTHONPATH=. python -m pytest tests/test_snapbak.py -v
```

Tests cover retention functions (exponential, datetimes, keeplist), snapshot
parsing, config validation, repo selection, failed backup retry, backup window
logic, throughput calculation, and config loading. No remote restic or actual
Btrfs operations are required.
