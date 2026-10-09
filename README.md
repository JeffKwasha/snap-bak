# snap-bak

Btrfs Snapshot → Restic Backup Orchestrator.

## Purpose

A simple, robust, maintainable script that leverages Btrfs snapshots and
restic to efficiently meet 3-2-1 backup coverage with minimal disruption
to daily use.

## Requirements

- **Btrfs** — snapshots and subvolume mount
- **Python ≥ 3.10**
- **restic**
- **pydantic python-dateutil json5**
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
the orchestrator + a root-owned sudo wrapper, configures a static sudoers
rule, and wires up a systemd *service + timer*.

```bash
sudo snap-bak-setup.sh
sudo systemctl enable --now snap-bak.timer
```

What `snap-bak-setup.sh` does:

1. Verifies you are `root`.
2. Creates the `snap-bak` user/group and `/opt/snap-bak`.
3. Installs the orchestrator package to `/opt/snap-bak/snap_bak/` (root:root
   0644) and the wrapper to `/opt/snap-bak/sudo-exec.py` (root:root 0755).
4. Writes an annotated example config to `/opt/snap-bak/config.json`.
5. Creates service-writable `state/`, `cache/`, `passwords/` and
   `.config/rclone/`; root:snap-bak `excludes/`.
 6. Creates the mount base `/bak` and installs a single static sudoers rule —
    `snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/sudo-exec.py` (bare command
    name = any arguments, per sudoers(5)) — valid on both sudo-rs and
    sudo.ws.
7. Writes `snap-bak.service` (oneshot) and `snap-bak.timer` (hourly).

The timer runs hourly; restic uploads happen per-job only when
`backup_interval` has elapsed (so weekly backups = `backup_interval: "1w"`).

The setup script is **idempotent** — re-running it is safe: it skips the user,
group, and existing `config.json` (so your edits are preserved), refreshes the
program + unit files, and re-validates sudoers.

Before enabling, edit `/opt/snap-bak/config.json` to match your environment and
create the per-repo restic password files under `/opt/snap-bak/passwords/` —
owned by you and readable by the service group:
`sudo chown <you>:snap-bak <file> && sudo chmod 640 <file>`. The gate accepts
owner-only (0600) or owner + `snap-bak` group-read (0640); a group other than
`snap-bak`, group write/execute, or any `other` access is rejected (exit 4).

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

Snapshots are young (minutes to hours old). Backups use snapshots ≥12h old, so
offsite copies are always of older, known-good data.
After a successful backup, snapshots older than the backup time are deleted
(data is now offsite). Younger snapshots remain for quick local restore.

## Permissions & Security

snap-bak uses a **root-owned sudo wrapper** privilege model.

### The Strategy
The orchestrator runs as a dedicated, unprivileged system user (`snap-bak`). It
does not require full root access. Privileged operations (btrfs snapshot,
mount/umount) are delegated to a **root-owned wrapper** that is the only thing
sudoers lets the service user run.

### How it Works
1.  **Static sudoers rule**: `snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/sudo-exec.py`
    — a bare command name matches any arguments (see sudoers(5)), valid on
    both sudo-rs and sudo.ws. A trailing `*` is deliberately NOT used because
    sudo-rs mishandles it. Changing `mount_base` or `snapshot_dir` in config
    NEVER requires touching sudoers, because the wrapper derives every path
    from the root-owned config at runtime.
2.  **The wrapper** (`sudo-exec.py`, root-owned 0755) accepts only
    `snapshot|delete|mount|umount <job> [snapshot]`, `status <job>`, and
    `update-restic`,
    validates the job against `/opt/snap-bak/config.json` and the snapshot
    name against `YYYY-MM-DD_HH:MMZ`, and confines all paths under the
    configured `snapshot_dir` / `mount_base` (realpath traversal guard).
3.  **Stable read-only bind mounts** — snapshots are bound at
    `<mount_base>/<source basename>` (e.g. `/mnt/data` →
    `/mnt/bak/data`) read-only via `mount -o bind,ro`. The mount point
    stays stable across backups, which restic's change tracking requires
    (a changing source path would force full-copy backups).
4.  **`cap_dac_read_search` on restic** — snapshots are root-owned, so restic
    (running as the unprivileged `snap-bak` user) reads them via the
    `cap_dac_read_search` capability applied to the restic binary. This is
    managed entirely by the wrapper's `update-restic` op, which validates the
    binary is root-owned and un-writable by others, runs `restic self-update`
    (dropping any stale caps), and re-applies the capability. A config
    `restic_update` policy (`after|never`) controls when updates run — and it
    is deliberately restricted: updating restic rewrites a
    `cap_dac_read_search`-carrying binary, so snap-bak only does it after a
    run in which a backup actually completed, never for skipped/dry-run runs.
5.  **Minimal Surface Area**: The restic process runs as the unprivileged
    `snap-bak` user. `/opt/snap-bak` is root-owned; only `state/`, `cache/`,
    `.config/rclone/` are writable by the service user, so a compromised backup
    tool cannot replace the wrapper (which would be trivial root escalation).
    Restic credentials under `passwords/` are owned by the admin and readable
    only via the `snap-bak` group, never by other users.

- **Per-repo password files** — each restic repo has its own password file
- **Rate tracking** — measured backup speed is kept in state (`throughput.json`)
- **Pluggable snapshot retention** — exponential or calendar-based cleanup
- **Failed backup recovery** — automatic retry with same snapshot
- **Backup windows** — restrict to off-peak hours
- **Configurable retry** — per-job restic retry count
- **Hardcoded paths** — state in `/opt/snap-bak/state/`
- **Crash recovery** — lock file with JSON job info

## Configuration

The config file is JSONC: standard JSON plus `//` and `/* */` comments and
trailing commas. It is parsed with json5 by both `config.py` and the root
wrapper `sudo-exec.py` (a single code path for JSONC handling). The default
config written by `snap-bak-setup.sh` includes inline comments describing
every field.

Duration and size fields (`stuck_threshold`, `size_limit`,
`snapshot_interval`, `backup_interval`) accept either a bare
integer (seconds/bytes) or a human-readable string — e.g. `"90m"`, `"2h"`,
`"1w"`, `"200GB"`, `"1.5TiB"`. Units: durations `s m h d w`; sizes
`b k/kb/kib m/mb/mib g/gb/gib t/tb/tib p/pb/pib` (binary multiples).

Edit `config/config.json`:

```json
{
  "repos": [
    {
      "name": "primary",
      "path": "rclone:CONFIG_ENTRY:DIRECTORY",
      "password_file": "/opt/snap-bak/passwords/primary",
      "size_limit": "200GB",
      "latency_ms": 50,
      "throughput_mbps": 10,
      "restic_options": ["--compression=better"]
    }
  ],
  "mount_base": "/bak",
  "stuck_threshold": "1h",
  "restic_update": "never",
  "jobs": [
    {
      "name": "data",
      "snapshot_dir": "/mnt/data/.snapshots",
      "source_subvolume": "/mnt/data",
      "repo": "primary",
      "priority": 100,
      "snapshot_interval": "2h",
      "backup_retention": {"daily": 6, "monthly": 4, "yearly": 2},
      "snapshot_retention": {"fn": "exponential", "config": {"n": 5}},
      "backup_interval": "1w",
      "backup_window_start": "01:00",
      "backup_window_end": "07:00",
      "ignore_file": "/opt/snap-bak/excludes/data.ignore",
      "excludes": ["*.tmp", "*.cache", ".DS_Store"]
    }
  ]
}
```

### Top-level settings

| Field | Description | Default |
|-------|-------------|---------|
| `repos` | Array of restic repository definitions (required) | — |
| `mount_base` | Base directory for mounting snapshots | `/bak` |
| `stuck_threshold` | Stale-lock threshold: int seconds or e.g. `"1h"` | `3600` |
| `restic_update` | When to update restic: `after` \| `never` | `never` |

### Repo settings

| Field | Description | Default |
|-------|-------------|---------|
| `name` | Unique repo identifier (required) | — |
| `path` | Restic repository path (required) | — |
| `password_file` | Path to one-line text file containing the restic password | `""` |
| `size_limit` | Max backup size: int bytes or e.g. `"200GB"`; `0` = unlimited | `0` |
| `latency_ms` | Estimated latency in milliseconds (metadata) | `100` |
| `throughput_mbps` | Estimated throughput in Mbps (seeding hint) | `1` |
| `restic_options` | Extra restic CLI flags against this repo; global options apply to every command, backup-only options to `backup` alone | `[]` |

### Job settings

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `name` | string | Job id (required); must be filesystem-safe (`[A-Za-z0-9_-]`, no `/` or `..`). Does NOT control the mount point | — |
| `snapshot_dir` | string | Directory containing Btrfs snapshots (required) | — |
| `source_subvolume` | string | Btrfs subvolume to snapshot (required) | — |
| `repo` | string | Name of repo to use (must match `repos[].name`) | — |
| `priority` | int | Job priority (higher = more important) | `100` |
| `snapshot_interval` | int/str | Seconds between snapshots (min 900) or e.g. `"2h"` | `3600` |
| `skip_snapshot_if_idle` | bool | Power saving: skip snapshot creation when any disk in the source's btrfs pool is physically spun down (never wake it), and never snapshot an unchanged subvolume | `true` |
| `backup_retention` | dict | Restic forget/keep policy (e.g., `{"daily": 6}`) | `{}` |
| `snapshot_retention` | dict | Local snapshot retention strategy | `{"fn":"exponential","config":{"n":5}}` |
| `backup_interval` | int/str | Minimum seconds between backups or e.g. `"1w"` | `3600` |
| `backup_window_start` | string | Start of allowed backup window (HH:MM) | `null` |
| `backup_window_end` | string | End of allowed backup window (HH:MM, exclusive) | `null` |
| `ignore_file` | string | Path to file with exclude patterns | `""` |
| `excludes` | list | List of glob patterns to exclude | `[]` |
| `tag` | string | Restic tag override for this job's backups/forget | job name |
| `restic_options` | list | Extra restic CLI flags for this job; global options apply to every command, backup-only options to `backup` alone | `[]` |

**Note on `restic_options`:** Both `repos[].restic_options` and
`jobs[].restic_options` are appended verbatim to restic command lines. Options
in restic's global set (see `src/snap_bak/restic.py` `RESTIC_GLOBAL_FLAGS`) apply
to **every** restic command; **all other options are backup-only** and apply
to restic's own `backup` subcommand alone. Flags snap-bak manages itself are **ignored
with a warning** if configured here: `-r/--repo`, `--password-file`, `--json`,
`--dry-run`, `--tag`, `--keep-*`, `--prune`, `--exclude`, `--exclude-file`,
`--forget`.

**Note on `ignore_file` and `excludes`:** Both are passed to restic as
`--exclude-file` and `--exclude` flags respectively. They are additive —
both apply. `ignore_file` uses restic's exclude syntax: `*` and `?` (within a
path segment), `[a-z]` classes, `**` across directories, `!` negation, `#`
comments, `$VAR` expansion and `$$` for a literal `$`. Exclude flags are passed
to `backup` and `rewrite` only — never to other restic commands (e.g. `forget`),
which reject them.

**Note:** patterns only govern *future* uploads. Files uploaded before you added
a rule stay in the repo, because restic retention works on whole snapshots,
never on a file subset. `snap-bak apply excludes` removes them retroactively (via
`restic rewrite … --forget`, then prune) when you ask for it — see
[Inspecting & Applying Excludes](#inspecting--applying-excludes). Editing
`excludes` never silently rewrites history.

**Note on `tag`:** Defaults to the job name, so `--tag <name>` is applied to
every backup and forget. Set it to change the restic tag; use
`restic snapshots --tag <your-tag>` to filter by it.

**Note on `backup_retention` vs `snapshot_retention`:** `backup_retention`
controls restic's offsite backup retention (how many backup versions to keep
in the remote repo). `snapshot_retention` controls local Btrfs snapshot
cleanup (how many snapshots to keep on disk).

**Note on `name` vs the mount point:** the job `name` must be filesystem-safe
(`[A-Za-z0-9_-]`, no `/` or `..`) because it drives `<state>/<name>.json`
and the restic tag — but it does NOT control the mount point. The mount point
is always `<mount_base>/<last path component of source_subvolume>` (e.g.
`/mnt/data` → `/mnt/bak/data`), mirroring the real filesystem path. It
is not configurable. A stable mount point matters: restic change-tracking keys
on the source path, so a changing mount point would force full-copy backups.

- Mount point: `<mount_base>/<source basename>` — the read-only snapshot is
  bound here before backup (`mount -o bind,ro`)
- restic tag: `--tag <name>` on every backup/forget (overridable via `tag`)
- State file: `<state_dir>/<name>.json`

**Note on `restic_update`:** updating restic is a privileged operation handled
by the wrapper's `update-restic` op, which validates the binary is root-owned
and un-writable by others, runs `restic self-update`, then re-applies
`cap_dac_read_search` (restic self-update replaces the binary and drops
capabilities). Updating restic rewrites a binary that can read any file, so
snap-bak only does it (`after`) after a run in which a backup actually
completed — never for skipped, dry-run, or failed runs. `never` leaves updates
to you. Run it by hand anytime with
`sudo /opt/snap-bak/sudo-exec.py update-restic`.

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

The snapshot name uses the format `YYYY-MM-DD_HH:MMZ` (e.g., `2026-07-26_10:30Z`), where the trailing `Z` marks UTC.
Snapshots are created at most once per `snapshot_interval` seconds (default: 1 hour,
minimum: 15 minutes). Creation is independent of backup success: fresh installs
create their first snapshot immediately, and a long-running backup never stalls
the snapshot cadence. Non-matching names in `snapshot_dir` are ignored.

### Power saving (snapshot suppression)

By default snap-bak avoids waking disks and avoids pointless snapshots:

- **Never wake a spun-down drive.** If *any* physical disk in the btrfs pool
  hosting the source subvolume is spun down, the snapshot is skipped for that
  run (log: `Skipping '<snapshot>': pool has a spun-down disk (power saving; not waking the drive)`). The check reads
  only kernel state (`/sys/fs/btrfs/<uuid>/devices`, `/sys/class/block/<disk>/
  device/state`, `queue/rotational`) and never touches the disk, so an hourly
  timer can't keep the drive spinning. "Parked" (dm/md layer) or unknown states
  are *not* treated as spin-down — the gate fails open. Opt out per job with
  `skip_snapshot_if_idle: false`.
- **Never snapshot an unchanged subvolume.** After the disk-state check, the
  source's btrfs `Generation` (last-change generation) is compared with the
  latest snapshot's; if they are equal there are no changes to capture and the
  snapshot is skipped (log: `Skipping '<snapshot>': source unchanged since '<last_snapshot>'`). This is the
  same mechanism btrbk's `onchange` snapshots use.

Both gates fail open: if the pool state or generation cannot be determined, the
historic behavior (create when due) is preserved — a broken probe never silently
stops snapshots. **Backups are unaffected** and still run in their configured
window (the least-busy time), waking the drive for the scheduled upload as
intended.

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
- Snapshots younger than the minimum age (12 hours) are never backed up: the run
  records `skipped` and defers to a later cycle, so a snapshot just created by
  the scheduler is only uploaded after it has aged (e.g. on a fresh install).
- The snapshot is mounted read-only, preflight-checked, then backed up to the
  configured repo.
- After a successful backup, the observed transfer rate is recorded in the
  per-repo history in `throughput.json` (state dir), which seeds future rate
  estimates. The config file itself is never rewritten.

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

## Inspecting & Applying Excludes

Your `ignore_file` / `excludes` patterns decide what a **future** backup skips.
Anything uploaded *before* you added a rule stays in the repo forever — restic's
retention prunes whole snapshots, never a subset of files. These commands show
what the current rules actually match, and (on request) remove exactly those
files from existing backups.

```bash
# Lint the patterns and list live-filesystem matches — instant, local,
# no network, and the default when you give no selector.
snap-bak show excludes

# Explicit selectors
snap-bak show excludes --snaps      # per-local-snapshot table (local)
snap-bak show excludes --repo       # what's already in restic (network)
snap-bak show excludes --all        # everything, in one pass

# One job, machine-readable, bounded output
snap-bak show excludes --repo --job data --json --limit 50
```

`show` is always read-only — it never creates, removes, or modifies a snapshot or
a local file. Patterns are linted on every invocation and print **nothing** when
they are clean, so there is no `--lint` flag to remember.

The `--snaps` / `--repo` tables put one column per snapshot, showing how many
bytes each snapshot would drop, with a total row. `-` means "not in that
snapshot":

```
                                      10-07Z  10-06Z  10-05Z      TOTAL
/bak/E/build/cache                 800 MiB       -  1.2 GiB    2.0 GiB
/bak/E/build/tmp.o                     -   12 MiB       -     12 MiB
BYTES TO EXCLUDE                  800 MiB   12 MiB  1.2 GiB    2.0 GiB
```

To actually remove them, `apply` prints a plan first and only acts on `--now`.
`apply` works on restic only, and prints the same plan `show excludes --repo`
does — it is the same code, so the dry-run you approve is what runs:

```bash
# Dry-run: which snapshots, which commands, how many bytes
snap-bak apply excludes --job data

# Do it now (rewrite + forget, then prune to reclaim space)
snap-bak apply excludes --job data --now

# Or defer to the next scheduled run instead
snap-bak apply excludes --job data --later
```

`apply` refuses `--live`, `--snaps` and `--all` with an explanation: those name
places it never removes files from. If a scheduled run already holds the
repository, `--now` fails immediately rather than waiting — use `--later` and let
the next scheduled run do it.

`--later` queues the rewrite in that job's state file
(`/opt/snap-bak/state/<job>.json`, under `queued`), and the next run that has
enough window time left, after all backup work is done, carries it out and then
forgets it. There is no config switch for this: rewriting history is always
something you ask for by name.

The queue entry survives a failure — so a transient problem is retried at the
next eligible window — with an attempt count that escalates the log rather than
retrying silently for ever. Queueing the same thing twice is an error, not a
silent no-op, and `--later` refuses outright when there is nothing to remove.

### Things worth knowing

- **Your local files are never touched.** Only restic snapshots are rewritten.
  Live files and btrfs snapshots are read at most.
- **Nothing is ever mounted to test patterns.** Matching runs against the path
  strings restic stored, so it can't interfere with an in-flight backup.
- **Patterns match the backup path**, i.e. `/bak/<source basename>/…` (that's
  what restic saw at backup time). Relative patterns like `*.tmp` or `Trash`
  behave as you'd expect; an anchored `/home/user/x` will never match when the
  backup root is `/bak/data`; the lint that runs on every `show` flags that.
- **A pattern that matches a directory takes its whole subtree with it**, as one
  row totalling its bytes. That mirrors restic, which skips an excluded
  directory rather than descending — so a `!` pattern cannot re-include
  something inside an excluded directory.
- **Rewriting is metadata-scale**, but **`prune` is not**: it repacks and
  re-uploads every affected pack, which is real network I/O on rclone backends
  and can take minutes to hours. That's why a queued rewrite waits for available
  window time. Space actually reclaimed is reported afterwards from
  `restic stats`, and will differ from the raw-byte estimate.
- Runs are idempotent: snapshots with no matching files are skipped, so
  repeating a command converges instead of re-uploading.
- Queued and immediate rewrites never touch your local filesystem.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SNAPBAK_CONFIG_FILE` | Path to config JSON file | `config/config.json` |
| `SNAPBAK_JOB` | Run a specific job by name | (all jobs) |
| `SNAPBAK_STATE_DIR` | State directory override (default: `/opt/snap-bak/state`) | `/opt/snap-bak/state` |
| `RESTIC_CACHE_DIR` | Restic cache directory (not set; restic uses `$HOME/.cache` → `/opt/snap-bak/.cache` → `/opt/snap-bak/cache`) | — |
| `SNAPBAK_SUDO` | Sudo binary used to invoke the wrapper | `sudo` |
| `SUDO_EXEC_PATH` | Path to the root-owned wrapper | `/opt/snap-bak/sudo-exec.py` |

## State Files

All state files live under `/opt/snap-bak/state/`:

```
/opt/snap-bak/state/
├── snap-bak.lock             # Global lock + current job info (JSON)
├── throughput.json           # Per-repo transfer-rate history (capped)
├── {job_name}.json            # Per-job state: last status + queued work
└── {job_name}.json.tmp        # Atomic write temp file
```

One file per job, holding both its last status and the work queued against it.
There is no separate record of work already done: an empty `queued` list is the
only statement needed that nothing is outstanding.

State for a job removed from the config is deleted once it has been gone for
`state_retention_days` (default 90). A job still in the config is never touched,
and queued work is never discarded for being old.

(Logs go to stderr — captured by the systemd journal. A file log is opt-in via `--log-file`.)

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
uv run pytest -q
```

Tests cover retention functions (exponential, datetimes, keeplist), snapshot
parsing, config validation, repo selection, failed backup retry, backup window
logic, throughput calculation, config loading, restic option filtering, the
sudo-exec.py wrapper's validation/confinement, and the exclude engine (a port
of restic's own pattern-matching golden cases, pattern-file parsing, lint
diagnostics, table rendering, and an end-to-end apply against a throwaway local
restic repo in a temp directory). No remote restic or actual Btrfs operations
are required.
