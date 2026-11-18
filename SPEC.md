# snap-bak Specification

## Overview

snap-bak is a simple btrfs snapshot → restic backup orchestrator.
It creates snapshots, and performs restic backups to efficiently manage retention.
Frequent snapshots instantly protect against accidents while older (known good) snapshots are used for weekly backups to minimize churn and extend retention.

### Constraints

- [[R-GLOBAL-01]] snap-bak is the exclusive authority for snapshot and backup management.
- [[R-SNAP-02]] Only snapshots matching snap-bak's expected format and directory are managed; all others are ignored.
- [[R-TIME-01]] All datetimes in snap-bak are GMT/UTC, except the `backup_window_start`/`backup_window_end` times, which are interpreted in the server's local timezone.

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

[[R-FLOW-01]] Check global conditions once per run. If invalid, log error and exit.

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

[[R-FLOW-02]] Manipulate job state to determine what to do this run. No separate
data structure — planning is implicit in job attributes.

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

[[R-FLOW-03]] For each job with planned backup. Never uses today's new snapshot.

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

[[R-FLOW-04]] A `failed` status persists the failing snapshot name; the next run
retries that exact snapshot before planning (same source data, same mount, same
repo). This failed-backup retry is separate from the delayed-job machinery: the
two are mutually exclusive statuses, so a delayed job never takes the
failed-retry path and vice versa. If the snapshot is gone (deleted by
retention), the job skips gracefully instead of failing.

> **Execution ordering**: [[R-FLOW-05]] although planning is described as Phase 2, a scheduled
> snapshot is created at the very start of the run (Phase 4). This is safe
> because the 12-hour minimum-age rule guarantees the new snapshot is never
> backed up in the same run (see Snapshot Safety Rule).

### Phase 4: Create Snapshots (btrfs)

[[R-FLOW-06]] Create new btrfs snapshot of `source_subvolume` at
`{snapshot_dir}/{name}` for each job. Only one snapshot per `snapshot_interval`
seconds per job.

A scheduled snapshot is **suppressed** when the power-saving gate says so:

1. [[R-FLOW-07]] **Disk spun down (power saving).** The wrapper's `status <job>` op resolves
   the source's btrfs pool to its physical disks (from kernel state:
   `/proc/self/mountinfo`, `/sys/fs/btrfs/<uuid>/devices`, `/sys/class/block`)
   and reports `any_spun_down`. If **any** rotational disk in the pool is spun
   down (`standby`/`slumber`/`powered_off`/`offline`/`sleep`), the snapshot is
   skipped — creating it would wake that drive. This gate makes no disk I/O at
   all (no btrfs calls, no snapshot-dir listing), so the drive stays asleep.
   Other states (`running`/`active`/`live`/`parked`) and unreadable states are
   never treated as spin-down; unknown pools fail open.
2. [[R-FLOW-08]] **No changes.** When the pool is not spun down, the wrapper compares the
   source's btrfs `Generation` with the latest snapshot's. Equal generations
   mean the source is unchanged since that snapshot, so the new snapshot would
   capture nothing and is skipped.

[[R-FLOW-09]] Both gates **fail open**: an error or indeterminate result falls
back to the historic behavior (create when the interval has elapsed). A fresh
install (no snapshot yet) always creates one. Config per job:
`skip_snapshot_if_idle` (default `true`) disables the spun-down-disk gate only;
the no-change gate is always on. Backups are independent of these gates and
still run on schedule. A dry run evaluates neither gate, so it predicts no
skip from them and reports that the gate was not checked.

### Phase 5: Cleanup Local Snapshots (btrfs)

[[R-FLOW-10]] Retention is a choice of how timeslot boundaries are distributed —
picking a function and its parameters. Two strategies are supported:

```
snapshot_retention = {"fn": "exponential", "config": {"n": 9}}        # boundaries at 2^(i-1) h: 1h, 2h, 4h, 8h, ...
snapshot_retention = {"fn": "datetimes",  "config": {"hourly": 8, "daily": 7, "weekly": 4, "monthly": 3}}
```

[[R-FLOW-11]] Algorithm (shared by both strategies):

- Generate retention boundaries (oldest to newest) from the chosen function.
- For each boundary, keep the **oldest snapshot still newer than that boundary**.
- Delete every snapshot not on the keep-list.
- The snapshot currently being backed up (the protected snapshot) is always kept.
- Only snapshots under `snapshot_dir` matching the naming format are considered; all others are ignored.

---

## Configuration

### Config File Location

[[R-CONFIG-01]] Default: `config/config.json` (repo-relative)

[[R-CONFIG-02]] Override: `-c/--config` flag or `SNAPBAK_CONFIG_FILE` env var

[[R-CONFIG-03]] Production: `SNAPBAK_CONFIG_FILE=/opt/snap-bak/config.json` (set by the systemd service)

### Config Schema

[[R-CONFIG-04]]

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

[[R-CONFIG-05]] `restic_update` (`after|never`, default `never`) controls when
the privileged wrapper's `update-restic` op runs. `after` = update only after a
run in which a backup actually completed (never on skipped/dry-run runs —
updating restic rewrites a binary carrying `cap_dac_read_search`, so it must
never run without reason). `never` = manual only. `before` is deliberately
unsupported: it would update before knowing whether any backup will happen.

[[R-CONFIG-06]] Duration and size fields (`stuck_threshold`, `stuck_wait`,
`size_limit`, `snapshot_interval`, `backup_interval`) accept either a bare
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

| ID | Field | Type | Default | Description |
|----|-------|------|---------|-------------|
| [[R-REPO-01]] | `name` | string | required | Unique repo identifier |
| [[R-REPO-02]] | `path` | string | required | Restic repo path (e.g., `rclone:CONFIG_ENTRY:DIRECTORY`) |
| [[R-REPO-03]] | `password_file` | string | "" | Path to restic password file (per-repo, under `/opt/snap-bak/passwords/`) |
| [[R-REPO-04]] | `size_limit` | int/string | 0 | Max repo size (0 = unlimited). Int bytes or e.g. `"200GB"` |
| [[R-REPO-05]] | `latency_ms` | int | 100 | Estimated latency for ETA calculation |
| [[R-REPO-06]] | `throughput_mbps` | int | 1 | Estimated throughput for ETA calculation (seeding hint) |
| [[R-REPO-07]] | `restic_options` | list | `[]` | Extra restic CLI flags against this repo; global options apply to every command, backup-only options to `backup` alone |

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

| ID | Field | Type | Default | Constraint | Description |
|----|-------|------|---------|------------|-------------|
| [[R-JOB-01]] | `name` | string | required | `[A-Za-z0-9_-]` | Job id; must be filesystem-safe (`<state>/<name>.json`, restic tag). Does NOT set the mount point |
| [[R-JOB-02]] | `snapshot_dir` | string | required | must exist | Path to btrfs snapshot directory |
| [[R-JOB-03]] | `source_subvolume` | string | required | — | Btrfs subvolume to snapshot; mount point = `<mount_base>/<last component>` |
| [[R-JOB-04]] | `repo` | string | required | must reference repo.name | Repo to backup to |
| [[R-JOB-05]] | `priority` | int | `100` | `ge=1` | Job priority (higher = more important) |
| [[R-JOB-06]] | `snapshot_interval` | int/string | `3600` | `ge=900` | Seconds between snapshots, or e.g. `"2h"` |
| [[R-JOB-07]] | `skip_snapshot_if_idle` | bool | `true` | — | Power saving: skip snapshot creation when any disk in the source's btrfs pool is physically spun down (never wake it), and never snapshot an unchanged subvolume. See Power Saving. |
| [[R-JOB-08]] | `backup_retention` | dict | `{}` | — | Restic retention policy (e.g., `{"daily": 6}`) |
| [[R-JOB-09]] | `snapshot_retention` | dict | `{"fn":"exponential","config":{"n":5}}` | — | Local snapshot retention strategy |
| [[R-JOB-10]] | `backup_interval` | int/string | `3600` | `ge=1` | Seconds between backups, or e.g. `"1w"` |
| [[R-JOB-11]] | `backup_window_start` | string | `null` | `HH:MM` format | Backup window start (optional); interpreted in the server's LOCAL timezone |
| [[R-JOB-12]] | `backup_window_end` | string | `null` | `HH:MM` format | Backup window end (optional) |
| [[R-JOB-13]] | `ignore_file` | string | `""` | — | Restic exclude-from file |
| [[R-JOB-14]] | `excludes` | list | `[]` | — | Restic exclude patterns |
| [[R-JOB-15]] | `tag` | string | job name | `[A-Za-z0-9_-]` | Restic tag override |
| [[R-JOB-16]] | `restic_options` | list | `[]` | — | Extra restic CLI flags for this job; global options apply to every command, backup-only options to `backup` alone |

[[R-JOB-17]] The planning state fields that appear in the status file —
`delayed`, `delayed_reason`, `protected_snapshot`, `missed_slots` — are part of
the program's interface: they are persisted in the status file and never read
from config.

- [[R-JOB-18]] `restic_options` on both Repo and Job are appended verbatim to
  restic command lines (repo options first, then job options).
- [[R-JOB-19]] Options in restic's global set (`RESTIC_GLOBAL_FLAGS` in
  `src/snap_bak/restic.py`, derived from `restic --help`) apply to every restic
  command; **all other options are backup-only** and apply to the `backup`
  subcommand alone.
- [[R-JOB-20]] The `--exclude-file` and `--exclude` flags (from
  `ignore_file`/`excludes`) apply to `backup` **and** `rewrite` — the two
  subcommands that take a file set — and to no other subcommand (e.g. `forget`
  rejects them).
- [[R-JOB-21]] Flags snap-bak manages itself are filtered out with a warning if a
  config author sets them: `-r/--repo`, `--password-file`, `--json`,
  `--dry-run`, `--tag`, `--keep-*`, `--prune`, `--exclude`, `--exclude-file`,
  `--forget`.

---

## Status File

Per-job state stored at `/opt/snap-bak/state/<job_name>.json` — the last status
plus that job's queued work.

### Schema

[[R-STATUS-01]]

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

[[R-STATUS-02]] `restic_snapshot_id` is present on success; `failure_category`
and `last_error` are present on failure. All fields except `job`, `status`,
`timestamp`, and `message` are optional.

[[R-STATUS-03]] The `delayed` group carries the delayed-job state (see Delayed
Job Handling): `delayed` is true while the job cannot proceed; `delayed_reason`
is `"unreachable"` or `"size_limit"`; `protected_snapshot` names the exact
snapshot to retry (same `YYYY-MM-DD_HH:MMZ` format as the snapshot naming rule);
`missed_slots` counts missed backup slots and feeds the priority system. On
success the group resets to `delayed=false`, `delayed_reason=null`,
`protected_snapshot=null`, `missed_slots=0`.

### Status Values

| ID | Status | Meaning | Exit Code |
|----|--------|---------|-----------|
| [[R-STATUS-04]] | `started` | Backup initiated | — |
| [[R-STATUS-05]] | `success` | Backup completed | 0 |
| [[R-STATUS-06]] | `skipped` | Not time / outside window / snapshot too young | 0 |
| [[R-STATUS-07]] | `delayed` | Repo unreachable or over size_limit | 30-31 (temp) |
| [[R-STATUS-08]] | `failed` | Backup failed and slot expired | 41 (perm) |
| [[R-STATUS-09]] | `dry_run` | Preflight only | 0 |

---

## Error Codes

| ID | Code | Constant | Category | Meaning |
|----|------|----------|----------|---------|
| [[R-ERROR-01]] | 0 | EXIT_SUCCESS | Success | All jobs succeeded or skipped |
| [[R-ERROR-02]] | 1 | EXIT_ERROR | General | Unexpected error |
| [[R-ERROR-03]] | 2 | EXIT_CONFIG_ERROR | Config | Config validation failed |
| [[R-ERROR-04]] | 3 | EXIT_MOUNT_ERROR | Mount | Mount base not found |
| [[R-ERROR-05]] | 4 | EXIT_PERMISSION_ERROR | Permission | Password file readable by a group other than snap-bak, or by other |
| [[R-ERROR-06]] | 30 | EXIT_TEMP_UNREACHABLE | Temporary | Repo unreachable (may self-rectify) |
| [[R-ERROR-07]] | 31 | EXIT_TEMP_SIZE_LIMIT | Temporary | Repo over size_limit (may self-rectify) |
| [[R-ERROR-08]] | 40 | EXIT_PERM_TIMEOUT | Permanent | Backup not completed in window |
| [[R-ERROR-09]] | 41 | EXIT_PERM_FAILED | Permanent | Backup failed and slot expired |
| [[R-ERROR-10]] | 50 | EXIT_SYSTEM | System | Lock acquisition failed (no stale instance to clean up) |
| [[R-ERROR-11]] | 51 | EXIT_STUCK_WAIT | System | Stuck wait exceeded |

### Temporary vs Permanent

- [[R-ERROR-12]] **Temporary (30-39)**: Can self-rectify on next run (e.g., network comes back, space frees up)
- [[R-ERROR-13]] **Permanent (40-49)**: Won't retry until next scheduled backup window
- [[R-ERROR-14]] **System (50-59)**: Infrastructure issues, not job-specific

---

## Delayed Job Handling

### When a Job is Delayed

A job is delayed when:

1. [[R-DELAY-01]] **Repo unreachable**: `restic cat config` fails (network, drive offline)
2. [[R-DELAY-02]] **Repo over size_limit**: After forget/prune, repo size exceeds `size_limit`

### Delay State

[[R-DELAY-03]] Delaying a job: mark it delayed with a reason, protect the source
snapshot, increment `missed_slots`, and record a `delayed` status naming the
snapshot.

```
delay(snapshot, reason):
    mark job delayed (reason)
    protect snapshot
    missed_slots += 1
    write status "delayed", snapshot = snapshot
```

### Delayed Recovery Flow

[[R-DELAY-04]] On next run, during planning phase:

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

- [[R-DELAY-05]] Protected snapshot = snapshot name referenced in `status_file["protected_snapshot"]`
- [[R-DELAY-06]] During exponential cleanup: simply not deleted
- [[R-DELAY-07]] Otherwise treated like any other snapshot (counted toward retention)
- [[R-DELAY-08]] When delayed state clears (success): protection clears automatically
- No special deletion logic needed

### Multi-Job Repo Handling

If multiple jobs share a repo:

1. [[R-DELAY-09]] Check repo reachability once per run (cached)
2. [[R-DELAY-10]] If unreachable: all jobs for that repo are delayed
3. If reachable but over size_limit:
   - [[R-DELAY-11]] Sort jobs by effective priority; the highest priority job runs
   - [[R-DELAY-12]] Remaining jobs for that repo are skipped (not delayed); they accumulate missed_slots for next run

---

## Priority System

### Effective Priority

[[R-PRIORITY-01]]

```
effective_priority = job.priority × (job.missed_slots + 1)
```

- [[R-PRIORITY-02]] Higher effective priority processed first
- [[R-PRIORITY-03]] `missed_slots` accumulates when a job is delayed
- [[R-PRIORITY-04]] Reset to 0 on success
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

- [[R-SNAP-01]] Format: `YYYY-MM-DD_HH:MMZ`
- [[R-SNAP-02]] This is the only accepted format. Snapshots with any other naming convention are ignored.

### Selection Logic

1. [[R-SNAP-03]] **Normal backup**: Find oldest snapshot within `backup_interval` seconds
2. [[R-SNAP-04]] **Fallback**: If none within interval, use latest snapshot
3. [[R-SNAP-05]] **Delayed retry**: Use exact same snapshot (from `protected_snapshot` field)

### Safety Rule

[[R-SNAP-06]] **Never backup a snapshot newer than 12 hours old.**

A snapshot younger than 12 hours is skipped (never failed), so a snapshot just
created by the scheduler is only uploaded after it has had time to be noticed
and reviewed.

---

## Backup Window

### Format

- [[R-WINDOW-01]] `backup_window_start`: `HH:MM` (e.g., `"22:00"`)
- [[R-WINDOW-02]] `backup_window_end`: `HH:MM` (e.g., `"06:00"`)

### Support

- [[R-WINDOW-03]] Same-day windows (e.g., 09:00 to 17:00)
- [[R-WINDOW-04]] Overnight windows (e.g., 22:00 to 06:00)
- [[R-WINDOW-05]] No window = always allowed

---

## Excludes: Inspection & Application

### Purpose

`ignore_file` / `excludes` decide what a **future** backup skips. Files already
uploaded before a rule was added stay in the repo forever, because restic's
forget/prune retention works on whole snapshots, never on a file subset.
This feature answers "what do my current rules match?" and, on request,
removes exactly those files from **existing** snapshots.

### Invariants

1. [[R-EXCL-01]] **The local filesystem is never modified.** Every `show` mode is a read-only
   walk; `apply` never touches the live subvolume, `snapshot_dir`, or any btrfs
   snapshot. Patterns are only ever applied to restic snapshots. The only local
   write any of them performs is restic's own cache, and restic's repository
   lock file is created and released per invocation — both restic-managed.
2. [[R-EXCL-02]] **Snapshots are never mounted to test patterns.** Matching runs against the
   path *strings* restic stored, so no mount can race an in-flight backup.
3. [[R-EXCL-03]] **Editing `excludes` alone changes nothing retroactively.** Removal requires
   an explicit `apply` (`--now` / `--later`). There is no config key that
   schedules it.
4. [[R-EXCL-04]] **No persistent config key arms removal implicitly.** The default is `off`.
5. [[R-EXCL-05]] **The dry-run and the run are the same plan.** `plan()` decides scope and
   `execute()` performs it; `show excludes --repo`, the `apply` dry-run, and
   every mutating path call that one pair. A selector that would target the live
   filesystem or local snapshots is refused by `apply` rather than ignored.

### Commands

[[R-EXCL-06]]

```
snap-bak show excludes [--live | --snaps | --repo | --all]
                       [--job NAME] [--json] [--limit N]
                       [--format text|csv] [-v] [--price]
                       [--excludes-file PATH] [-e PATTERN]
                       [--reveal PATTERN]... [--reveal-file PATH]
snap-bak apply excludes [--now | --later] [--job NAME] [--json] [--limit N]
```

[[R-EXCL-07]] Grouped by verb: `show` is read-only, `apply` is the only mutating
command. [[R-EXCL-08]] Pattern linting runs on every `show`, silently when clean,
so there is no `--lint` flag — it is not optional, and offering it would imply
the modes skip it.

[[R-EXCL-09]] **`show excludes` with no selector defaults to `--live`.**
[[R-EXCL-10]] The default walk is shallow, because that is exactly what restic's
own walker does: a matched directory is pruned unread, so the report pays one
`readdir` for a directory restic would skip anyway, not a stat per file beneath
it. Its size is therefore unknown, and unknown is printed as `?` (see *Priced,
unpriced, priced in part*). [[R-EXCL-11]] `--price` restores the old exact
behaviour when the numbers are wanted more than the time.

[[R-EXCL-12]] `apply excludes` operates on restic only and **assumes `--repo`**.
Passing `--live`, `--snaps` or `--all` to it is an error with an explanation, not
a flag that is quietly discarded: those name sources `apply` never removes files
from.

### Trial pattern sets

[[R-EXCL-13]] `--excludes-file PATH` and `-e PATTERN` let `show` evaluate a
working rule set without the root-owned config being edited. They **add to** the
job's patterns rather than replacing them, and the config is never modified — not
even when the command is a dry-run.

[[R-EXCL-14]] Both take **exactly the syntax an `ignore_file` uses**: one restic
exclude pattern per line, `#` comments, `!` negation, `$VAR` expansion. A rule
can be copied between the two unchanged, which is the point: working in a file
is the normal way to edit patterns, and re-typing dozens of `-e` arguments is
not.

[[R-EXCL-15]] Effective order, which is the part that constrains what a trial can
express:

```
config patterns  →  --excludes-file patterns  →  -e patterns
```

[[R-EXCL-16]] Duplicates are dropped keeping first occurrence (`dict.fromkeys`),
so a rule named both on the command line and in the file is one rule, announced
once. Not a plain `set`: restic applies `!` negation sequentially, so order
changes answers and a set would silently reorder the user's rules.

Two consequences, which are easy to assume otherwise and cut in opposite
directions:

- [[R-EXCL-17]] **A trial's `!` does override a config rule naming the same target.**
  Negation is sequential and the trial's rules are evaluated after the config's,
  so `-e '!cache'` cancels a configured `cache`. What a trial cannot do is
  *reorder* — the effective order is fixed at `config → file → -e`.
- [[R-EXCL-18]] **A trial's `!` cannot reach *inside* a directory an earlier pattern already
  matched.** Those patterns run first and prune the directory, so restic never
  descends and the later `!` has nothing to act on — the same prune semantics as
  any other walk (see *Pattern matching*). Both `!node_modules/keepme/` and
  `!node_modules/keepme/vital.js` are therefore ignored once `node_modules/`
  matches. The negation that works is one naming the **directory itself**, in the
  same file and after the exclusion (`logs/` then `!logs/`, which does restore
  the directory and its contents). To keep one file out of a directory excluded
  wholesale, exclude that file directly rather than its parent.

So a trial set is not strictly "tightening only": it can cancel a rule outright,
and it can never reach beneath a pruned directory.

[[R-EXCL-19]] An unreadable trial file exits 1 naming the path. Falling back
silently to the config's rules would report figures the caller did not ask for.

[[R-EXCL-20]] `apply` refuses `--excludes-file`, `-e`, `--reveal`, `--reveal-file`
and `--price` with an explanation: rewriting real backups against rules the
deployment does not use would remove data on the strength of an experiment. The
trial and the survey belong to `show`; `apply` acts only on what is configured.

[[R-EXCL-21]] Without `-v`, a trial lists only rows attributed to its **own added
rules** — the config's rules are already in force and would bury the answer.
Totals always cover the whole set.

### Priced, unpriced, priced in part

Restic prunes a directory the moment a pattern matches it: it never `readdir`s
inside, so nothing beneath is matched, counted or stored. `show excludes` used to
crawl every matched subtree anyway to print exact sizes — on a real `/home` that
is over a million entries and minutes of I/O the backup itself never pays.

[[R-EXCL-22]] The default `--live`/`--snaps` walk now prunes the same way, so its
decisions are restic's decisions. A matched directory is one row with unknown
counts, printed as `?`. **Restic parity is the point**: the report must never
suggest `--live` knows something a backup would decide differently.

- [[R-EXCL-23]] `?` — the whole number is unknown: the directory was pruned unread (restic's
  own behaviour), so no size was paid for. One spelling, in every format; empty
  (not `0`) in CSV, which would otherwise claim "scanned, nothing there".
- [[R-EXCL-24]] `+?` — the price covers only part of what the rule matched: entries vanished
  or could not be read mid-walk, or a revealed directory contains children the
  configuration already excludes. Both numbers carry `+?` where they are printed
  separately; the console cell puts it at the end.

[[R-EXCL-11]] `--price` walks matched directories for exact totals — the old
behaviour, and the only way to fill in a `?`. It costs a stat per file beneath
every rule, which is what made the default slow.

[[R-EXCL-25]] `--reveal PATTERN` (repeatable) and `--reveal-file PATH` are the
what-if counterpart: treat PATTERN as an extra rule and show everything it would
newly exclude. A revealed directory is descended, priced whole, and — even
without `-v` — listed file by file. Two precedence rules keep the answer honest:

- [[R-EXCL-26]] **A reveal pattern whose text is already a configured rule *is* that rule,
  revealed all along**: the directory is descended as if it had always been
  reveal.
- [[R-EXCL-27]] **Everything else follows restic's order: the config's patterns come first.**
  A different configured rule that matches first has already removed the entry,
  so the candidate adds nothing there. [[R-EXCL-28]] Inside a revealed directory the direct
  children the config already excludes get their own unpriced rows (they are not
  what the candidate adds) and mark the revealed row `+?`. [[R-EXCL-29]] Deeper rules are not
  re-consulted: restic pruned the directory at the match, so nothing beneath it
  was ever evaluated — pricing the whole subtree under the revealed rule is the
  parity-true answer.

[[R-EXCL-30]] A reveal pattern is never handed to restic or `apply`: run the
survey, then copy the pattern into the job's `ignore_file` to make it real.

| Selector | Source | Cost |
|----------|--------|------|
| *(default)* | `--live` — live filesystem walk, matched directories pruned unread | local; one `readdir` per directory, cheapest |
| `--price` | the same walk, descending into matched directories | local; adds one stat per file beneath every rule |
| `--reveal` / `--reveal-file` | the same walk, descending only into the revealed matches, listed | local; adds the revealed subtrees' stats |
| `--snaps` | every local btrfs snapshot dir | local, one walk per snapshot |
| `--repo` | files stored in restic snapshots | **network** (`restic snapshots` + `restic ls` per snapshot) |
| `--all` | every selector above | includes the network path |

[[R-EXCL-31]] A selector adds a **column**, not a separate report: the same
table is rendered from whichever sources were scanned, and one invocation emits
exactly one document. Two JSON objects on stdout is not a JSON document, and
three CSV headers is not a table.

[[R-EXCL-32]] `--limit N` bounds the per-file rows under `-v`; rule rows and
every total cover the full set.

### Pattern matching

[[R-EXCL-33]] `src/snap_bak/excludes.py` is a faithful port of restic's own
exclusion engine (`internal/filter`, matching restic 0.19.1) so listings agree
with what restic would do. restic's documentation of pattern syntax is the
authority.

[[R-EXCL-34]] CI installs the **newest** restic release rather than pinning
0.19.1: when restic's behaviour changes, the integration tests fail and the port
gets fixed in the same change. A pinned old binary would hide the divergence
until a user hit it against their own restic.

[[R-EXCL-35]] Negation and prune behaviour are **not** documented upstream —
restic describes pattern syntax and says nothing about `!` or about what a
matched directory does to later rules. Those rules below are therefore the
volatile kind: a restic release may change them, and the port is expected to
follow rather than to be argued with. See `docs/verification.md` for what was
established and when.

Ported semantics:

- [[R-EXCL-36]] Per-segment Go `filepath.Match` — `*` never crosses `/`, plus `?`, `[a-z]`,
  `[^a]`, `\` escapes. Deliberately **not** Python `fnmatch`.
- [[R-EXCL-37]] A pattern with no leading `/` matches at **any** path offset (`Trash` matches
  `/bak/home/Trash/x`); a leading `/` anchors to the root.
- [[R-EXCL-38]] A **trailing `/` is ignored**: `foo/` ≡ `foo`. It is not a directories-only
  marker, so `foo/` also matches a *file* named `foo`.
- [[R-EXCL-39]] `**` must be a complete path component (`foo/**/bar`, not `foo**`), and
  matches any number of intermediate directories.
- [[R-EXCL-40]] `!` prefix negates. Negation is sequential and the **last match wins**:
  `--exclude '*' --exclude '!keep.txt'` keeps only `keep.txt`.
- [[R-EXCL-41]] Patterns are `filepath.Clean`ed, so `/foo/../bar` ≡ `/bar`.
- [[R-EXCL-42]] `ignore_file` lines are trimmed; blanks and `#` comments dropped; `$VAR` is
  expanded and `$$` yields a literal `$`. `~` is **not** expanded — use
  `$HOME`.

[[R-EXCL-43]] Errors during inspection are reported but never fatal: an
unreadable directory, a file that vanishes mid-walk, or a pattern restic itself
would reject (caught and reported by the lint that runs on every `show`) all
degrade to "skip this path" rather than aborting the run.

[[R-EXCL-44]] Directory matching prunes the walk, mirroring restic's walker: it
does not descend into an excluded directory, so a `!` pattern cannot re-include
something inside one. [[R-EXCL-45]] Symlinks are never followed: restic stores a
link as a link, so following one would name paths the repo does not contain.

Because pruning is what restic does, the rule order is load-bearing and cannot
be worked around by reordering later patterns: the excluding pattern is
evaluated first and the subtree is never visited, so nothing after it applies
inside — including a `!` naming a path beneath it. Only a `!` naming the
directory itself, ordered after the exclusion, restores it. Note this is not the
same as negation in general, which is sequential and does let a later `!` cancel
an earlier rule on the *same* target. This is why a trial pattern set's reach is
bounded — see *Trial pattern sets*.

### Path basis

[[R-EXCL-46]] Backups run `restic backup <mount_base>/<source basename>`, so
stored paths are `/bak/<name>/...`. [[R-EXCL-47]] **All matching uses that
mount-point path.** `--live` and `--snaps` map live paths onto their mount-point
equivalent before matching, so every mode answers the same question: *what would
the next backup skip?*

Consequence: a relative pattern (`*.tmp`, `Trash`) behaves identically
everywhere, while an anchored pattern (`/bak/home/Trash`) is anchored to the
backup root. The lint that runs on every `show` flags anchored patterns that
can never match that root —
the common authoring mistake of writing `/home/user/x` when the root is
`/bak/data`.

### The report

[[R-EXCL-48]] One row per **exclude rule**, one column pair (`files / bytes`)
per **source**, with a total row. This is the layout the whole feature exists
for: a rule that drops 40k files from the live filesystem and 3 from what restic
stored has either never been deployed or has stopped matching, and the
difference is invisible unless both numbers are on one page.

```
TOTAL files   TOTAL bytes        live   snap:2026-01-22_10:00Z   restic:a1b2c3d4   exclude pattern
───────────────────────────────────────────────────────────────────────────────────────────────
      4223       8.1T    8 / 28.8K   8 / 28.8K     15 / 4.5G   cache
       135       1.0M  123 / 987 B  135 / 1.0M    12 /  77 B   *.tmp
         0         0        0 / 0     0 / 0          0 / 0     *.iso
         3       4.5G        0 / 0     0 / 0          3 / 4.5G  + *.never-deployed
      4361       13.1T 131 / 29.8M  143 / 1.0M    30 / 4.5G   TOTAL
```

[[R-EXCL-49]] **Every configured rule gets a row, including one that matches
nothing.** The zero row is the finding: it is what separates "not deployed yet"
from "stopped matching", and a report that omits it cannot express either. A
rule added by a trial is marked `+` rather than the config's own rules being
hidden — the table exists to be read against them.

### What counts as a file

[[R-EXCL-50]] A file is a **non-directory entry restic stores**: a regular file,
a symlink, or a hardlink. Established against restic 0.19.1 (see
`docs/verification.md`):

| ID | Kind | Counted as a file | Bytes |
|----|------|-------------------|-------|
| [[R-EXCL-51]] | regular file | yes | `lstat` size, matching `restic ls --long` |
| [[R-EXCL-52]] | symlink | yes | **0** — the node restic stores, reported at 0 by `restic ls --long` |
| [[R-EXCL-53]] | hardlink | yes, once per path | full size per path, matching restic's own `ls` |
| [[R-EXCL-54]] | directory | **never** | 0 |

Consequences that are load-bearing:

- [[R-EXCL-55]] **A symlink priced by `lstat` would make `--live` and `--repo` disagree** about
  the same file: `lstat` reports the length of the *target path*, which is not a
  payload restic holds.
- [[R-EXCL-56]] **A hardlink is counted once per path**, which is what keeps the two columns
  equal — `restic ls` reports every hardlinked path at full size. The figure
  therefore overstates what `prune` reclaims by exactly the shared blocks. The
  post-prune `stats` delta remains the authoritative reclaimed figure.
- [[R-EXCL-57]] **A directory is never a file**, so a matched empty directory reports 0 and a
  matched populated one reports its subtree. Counting directories would make the
  total depend on layout rather than on data.

[[R-EXCL-58]] `restic ls --long` renders a symlink as `path -> target`, with Go's
capital-`L` mode. The arrow is part of the rendered line, not the name, so it is
split off — otherwise a symlink matches no pattern and silently never appears in
a `--repo` report at all.

### `-v`: the file sheet

[[R-EXCL-59]] A second table, one line per excluded file, paths **relative to the
mount point** — relative, because that is the only form in which a file present
in three sources appears once instead of three times, which is the entire point
of putting the sources side by side. The file-count columns are dropped: every
line is exactly one file.

[[R-EXCL-60]] A matched directory is opened up into its contents, which costs
nothing extra on the `--repo` path (the listing already holds every path) and one
traversal on the walk path (`_subtree_totals` reports each file as it counts it).
Expanded files are attributed to the rule that matched the **directory**, because
restic pruned it and no pattern ever reached them.

### Output formats

| ID | Format | Columns | Units | Fitted |
|----|--------|---------|-------|--------|
| [[R-EXCL-61]] | `text` (default) | fitted to the terminal, oldest dropped first | abbreviated to ≤4 chars | yes |
| [[R-EXCL-62]] | `markdown` | every column | abbreviated | no |
| [[R-EXCL-63]] | `csv` | union of every job's columns | raw integers | no |
| [[R-EXCL-64]] | `--json` | every column | raw integers | no |

[[R-EXCL-65]] Only the console is fitted. Whole source columns are dropped —
never truncated mid-number — and what they held is stated underneath, so a
fitted table never reads as a complete one. [[R-EXCL-66]] A terminal's row per
source is `files / bytes`; the console puts the numeric columns before the rule
or path, which are the columns of unbounded width and may be folded at the right
without disturbing a number.

[[R-EXCL-67]] `--format csv` emits one header for the whole invocation, built as
the union of every job's source columns: two jobs rarely share a snapshot set,
and a header per job is not a table any importer accepts. [[R-EXCL-68]] `--json`
emits **one** document holding the lint results, every job and every source.

### `apply excludes`

[[R-APPLY-01]] Dry-run by default: prints affected snapshot IDs, projected bytes,
and the exact `restic rewrite` command that `--now` would run. Only `--now` and
the scheduled run mutate.

[[R-APPLY-02]] `--later` **queues** the rewrite as `rewrite-drop-excluded-files`
on that job's state file, after checking there is genuinely something to do: a
job whose repo has no snapshots holding excluded files is refused rather than
queued. [[R-APPLY-03]] An empty repo means the store went empty or retention
took everything, which is a problem to notice, not a rewrite to schedule.
[[R-APPLY-04]] Queuing a task that is already queued is an error, not a silent
no-op.

Execution order (each step is separately failure-isolated):

1. [[R-APPLY-05]] **Plan** — `apply.plan()`, the same call `show excludes --repo` and the
   dry-run use, so what the user approves is what runs.
2. [[R-APPLY-06]] **Preflight** — repo reachable (`restic cat config`; rc 10/12 abort before any
   write) and unlocked (rc 11 abort). Nothing has been modified at this point.
3. [[R-APPLY-07]] **Rewrite** — a single `restic rewrite <ids> --exclude-file … --exclude …
   --forget`. Rewritten snapshots keep their original time, host and tag, so
   retention keeps working. restic adds a `rewrite` tag only when `--forget`
   is *not* used, so `--forget` keeps the tag set exactly as it was.
4. [[R-APPLY-08]] **Prune** — one `restic prune` per repo to actually reclaim the space. The
   measured duration is logged: prune is the expensive step, and seeing how long
   it took is how an operator judges whether the gate is set too generously.

[[R-APPLY-09]] **Contention.** A scheduled apply runs inside the GlobalLock the
run already holds, because that lock is what guards local btrfs snapshots as
well. [[R-APPLY-10]] `--now` deliberately takes no lock and adds no
`--retry-lock`: restic serializes on the repository's own lock, so a contended
`--now` fails immediately with rc 11 and the answer is `--later`, which rides the
next scheduled run. restic's own lock is what prevents repo corruption; a second
lock would duplicate it, not strengthen it.

[[R-APPLY-11]] `--forget` is used in the same command deliberately: a standalone
`restic forget <ids>` would **also apply retention policies** to the group and
could delete snapshots beyond the intent. `--forget` removes exactly the
rewritten originals.

[[R-APPLY-12]] All affected snapshot IDs go into **one** `rewrite` invocation
rather than a loop, so the repo is opened once and the work is all-or-nothing per
snapshot: a snapshot restic could not rewrite keeps its original and its files.
[[R-APPLY-13]] Only snapshots that actually hold a match are rewritten — the
test is a per-snapshot membership check against the collected set, not "did this
snapshot introduce a path we had not seen", which with restic's newest-first
listing would rewrite one snapshot and leave every older copy in place.

[[R-APPLY-14]] Failure behaviour: a failed rewrite leaves the originals in place;
a failed prune leaves data merely unreclaimed. Both are safe to retry.
[[R-APPLY-15]] A snapshot with zero matching files is skipped, so repeated runs
converge and never re-upload identical trees.

[[R-APPLY-16]] Outcomes are reported as `apply.Outcome`, deliberately a
different type from `job.Result` (the one `exit_code()` consumes), so an apply
can never move the run's exit code or be mistaken for a backup result.

### Arming a scheduled cleanup

[[R-APPLY-17]] There is exactly one arming mechanism: the queue. `--later` adds
a task to that job's state file; the next eligible run performs it and removes
the entry. There is no config key that schedules this, so editing `excludes`
alone can never rewrite history.

| Mechanism | Scope | Meaning |
|-----------|-------|---------|
| `--later` | one-shot | the next eligible run does this, then forgets it |

[[R-APPLY-18]] `--now` performs it immediately and clears any matching queue
entry.

[[R-APPLY-19]] A task that fails or is delayed **keeps** its entry, so it is
retried at the next eligible window, with `attempts` and the last restic exit
code recorded so a persistent failure escalates in the log rather than retrying
nightly in silence. [[R-APPLY-20]] The attempt count deliberately does not gate
the retry: the window gates already bound how often this runs — at most once per
eligible window — and the operation is idempotent, so re-running is safe.

[[R-APPLY-21]] A task name is declared once, by the handler that implements it
(`@todo.task("rewrite-drop-excluded-files")`), and the registry is built from
those declarations, so a name cannot drift from the function it names.
[[R-APPLY-22]] Names are stored verbatim in the state file and read by a human
wondering what a queued run will do, so they read as the restic operation plus
its goal.

### Run-time gate

[[R-GATE-01]] Queued work runs only when **all** hold, evaluated after backups
complete, cheapest first:

1. [[R-GATE-02]] The job has something queued.
2. [[R-GATE-03]] The job has excludes to apply.
3. [[R-GATE-04]] Inside the job's backup window.
4. [[R-GATE-05]] At least **half** the window length remains.
5. [[R-GATE-06]] Repo reachable and unlocked.
6. [[R-GATE-07]] At least one snapshot actually contains a matching file.

[[R-GATE-08]] `rewrite` is metadata-scale — it rebuilds and re-uploads *tree*
blobs only; file data blobs stay shared with the original snapshot — which is
what makes a cheap gate sufficient. No duration estimate is needed.

[[R-GATE-09]] Phase order is **backups → queued work → retention(forget) →
prune**, and the work phase registers its repos into the prune set alongside
retention's, so a single prune per repo reclaims both. [[R-GATE-10]] A repo
whose job has no `backup_retention` is still pruned if a task made data there
unreferenced.

[[R-GATE-11]] Queued work is the only thing this phase runs, and it is the only
work a user asked for by name, so its outcome joins the run's own results and
reaches the exit-code ladder. A task that fails or is delayed reports non-zero; a
task that found nothing to do reports as an ordinary skip and changes nothing.
The ladder accumulates with `any()` over all results, so a task can never mask a
backup outcome.

[[R-GATE-12]] A repo that is missing, locked or unreadable (restic 10/11/12) is a
*temporary* problem and reports as `delayed` (exit 30) — it may well be fine on
the next window. Any other restic failure reports as `failed` (exit 41), because
it will not fix itself until the config changes.

[[R-GATE-13]] **A check, not a gate.** The backup that just ran used the job's
current excludes, so its snapshot should hold nothing to remove. If the plan
finds matches in the newest snapshot anyway, the run logs a warning: the backup
path is not honouring the excludes. The rewrite still proceeds — the data ends up
correct — but the bug gets named instead of being quietly cleaned up after.

[[R-GATE-14]] A dry run reports and returns without touching the queue at all:
simulating a run must not consume the request the real run is waiting for.

[[R-GATE-15]] Failures are never retried by a counter. A transient repo problem
(unreachable, locked) breaks every restic operation in the run equally, so the
ordinary run logic already handles it and the next eligible window simply tries
again; the operation is idempotent, so re-running is safe. What snap-bak must not
do is retry silently forever — hence the loud log naming the failing phase and
exit code, and the retained queue entry with its attempt count.

### IO & duration

| Step | Cost |
|------|------|
| `--live` | local walk; matching cost scales with entry count x pattern count (matched directories are pruned unread, so nothing beneath them is scanned, matched or stat'd) |
| `--price` | the same walk plus one stat per file beneath every matched directory — the old always-exact cost |
| `--reveal` | the same walk plus the revealed subtrees' stats |
| `--snaps` | local; one walk per snapshot |
| `--repo` | network: metadata only, cache-warm; seconds–minutes |
| `rewrite` | metadata/tree-scale: new tree blobs + index entries only, since file data is shared with the original snapshot |
| `prune` | **the cost centre**: repacks and re-uploads every pack holding excluded chunks; takes restic's exclusive lock; minutes to hours on rclone backends |

`rewrite` not being data-scale is what makes the cheap run-time gate sufficient
— no duration estimate is needed to decide whether it fits in the window.

Two consequences that are load-bearing for anyone optimising this:

- **`**` patterns are the cost driver, not the tree size.** Pattern count
  multiplies path count, so an ignore file with many `**` rules is the thing to
  watch. A pattern's `**` expansion is resolved once and memoised, keyed on the
  resolved parts rather than the pattern text (mid-expansion children of one
  pattern share both, so keying on the text returns one child's shapes for
  another). Segment matching is memoised too (`segment_match`), keyed on its
  full input — pattern text and segment — because both recur constantly
  (`share`, `lib`, extensions: ~97% hits on a real `/home`); it is pure, so the
  cache cannot change an answer, and on the config's own patterns it matched
  2.4x faster than uncached.
- **`--repo` issues one `restic ls --long` per stored snapshot and must not be
  "optimised" into `restic ls <snap>:<subdir>` calls.** Each restic invocation
  costs ~0.75s before doing any work — it loads the index and tree — so N pruned
  calls lose to one full listing at any N above two, and on a remote backend that
  fixed cost is a network round trip.

Neither `restic backup --dry-run` nor `find` can substitute: `--dry-run` reports
only what it **kept** (never an excluded path), and `find` has no equivalent for
`!` negation.

A long scan reports progress to stderr about once a second, and only when
stderr is a terminal or `-v` was passed — a piped or journalled run wants
results, not commentary, and commentary would corrupt `--json` / `--format csv`.

---

## Global Lock

### Purpose

Prevents multiple snap-bak instances from running simultaneously.

### Implementation

- [[R-LOCK-01]] Lock file: `<state>/snap-bak.lock`, an advisory `flock`
  (`LOCK_EX|LOCK_NB`). Records the holder PID and timestamps for stuck detection.
- [[R-LOCK-02]] Both transitions log the running version and the build timestamp
  the install recorded (`<install>/venv/BUILD`), as ISO 8601. It exposes a
  replaced or hand-edited install that shipped without a version bump; installs
  that predate the stamp fall back to the newest package file.
- [[R-LOCK-03]] `stuck_threshold` (config, default 3600s): lock age beyond which
  the holder is considered stuck.
- [[R-LOCK-04]] `stuck_wait` (config, default 300s): total time to keep
  re-checking a stuck lock before giving up.
- [[R-LOCK-05]] Stuck handling: if the holder PID no longer exists, break the
  lock and continue. If it still exists, poll (fixed 60s interval) until the lock
  clears or `stuck_wait` elapses; if `stuck_wait` is exceeded, exit
  `EXIT_STUCK_WAIT` (51).

[[R-LOCK-06]] The global lock is the only lock — there is deliberately **no
per-job lock**. Concurrent snap-bak instances are prevented solely by the global
lock; a per-job lock would add complexity without additional protection.

---

## State & Runtime Directories

[[R-STATE-01]]

```
<install>                # root-owned: venv, wrapper, config
  venv/                  # created without pip; the base pip populates it
    bin/python           # what the unit, the wrapper and the launcher run
    BUILD                # UTC timestamp written at install; the lock line reports it
    REF                  # the commit this install was made from
    lib/pythonX.Y/site-packages/
      snap_bak/          # the installed package (root:root)
      snap_bak-<ver>.dist-info/   # METADATA, entry points, RECORD
  sudo-exec.py            # sudo wrapper (root:snap-bak 0750)
  config.json            # root-owned, validated by the wrapper
  updates.log            # append-only: UTC time, version, commit per install
  .config/rclone/        # rclone's config dir under the service user's HOME
    rclone.conf          # operator-supplied credentials for `rclone:` repos

<install>/state          # owned by snap-bak: runtime state
  snap-bak.lock          # Global lock file
  throughput.json        # Per-repo transfer-rate history
  <job_name>.json        # Per-job state: last status + that job's queued work

<install>/excludes       # ignore_file location (root:snap-bak)
<install>/passwords      # per-repo restic password files (owned by the admin, group snap-bak)
<install>/cache          # restic cache (service-owned, real dir)
<install>/.cache         # symlink -> cache; restic's implicit $HOME/.cache default
```

[[R-STATE-02]] One file per job, holding both that job's last status and the
work queued against it. Keeping them together is deliberate: an entry in
`queued` *is* the record that the work was requested, so nothing else has to
remember it, and there is no record of completed work anywhere. An empty queue
is the only thing that means "nothing outstanding".

[[R-STATE-03]] `write_status` rewrites the whole document on every status
transition, so `queued` is the one key carried forward
(`job._CARRIED_FORWARD_KEYS`).

[[R-STATE-04]] State for a job that has left the config is deleted once it has
been gone for `state_retention_days` (default 90). A job still in the config is
never reaped — its file is rewritten every run, and reaping it would reset the
missed-slot escalation that drives priority. Queued work is never reaped: an
entry is a request from a human.

[[R-STATE-06]] All paths are relative to the install directory (`<install>`,
production
`/opt/snap-bak`). The `restic` and `rclone` binaries are optional (rclone only
if a repo uses an `rclone:` path). `.local` is never used.

[[R-STATE-07]] The service user's home is `<install>`, so rclone reads
`<install>/.config/rclone/rclone.conf`. The setup creates `.config/rclone` as
`snap-bak:snap-bak 0750` and instructs the operator to place `rclone.conf` there
at mode 600; snap-bak never writes it.

---

## Throughput History

[[R-THROUGHPUT-01]] Transfer rate statistics are kept per repo to provide stable
backup time estimates. [[R-THROUGHPUT-02]] If the state directory is unwritable,
the rate is logged and the run continues.

---

## CLI

### Usage

[[R-CLI-01]]

```bash
snap-bak [OPTIONS]                    # one run: validate, plan, backup, retain
snap-bak show excludes [OPTIONS]      # read-only: what do the rules match?
snap-bak apply excludes [OPTIONS]     # dry-run; --now applies, --later arms
```

[[R-CLI-02]] `show` / `apply` are subcommands. A bare `snap-bak` (no subcommand)
is the normal run, so the systemd service
(`ExecStart=<install>/venv/bin/python -P -m snap_bak.cli`) is unaffected. The
installed command is `/usr/local/bin/snap-bak`; tool commands run as the service
user, `sudo -u snap-bak snap-bak ...` (see *Deployment Contract*).

### Options

| ID | Flag | Description |
|----|------|-------------|
| [[R-CLI-03]] | `-c, --config` | Path to config JSON (default: `config/config.json`) |
| [[R-CLI-04]] | `-j, --job` | Run specific job by name (default: all jobs) |
| [[R-CLI-05]] | `-n, --dry-run` | Simulate backup (preflight only, no upload/delete) |
| [[R-CLI-06]] | `-l, --list-jobs` | List configured jobs and exit |
| [[R-CLI-07]] | `--log-file` | Also write logs to this file (default: stderr only; captured by systemd journal) |

### `show excludes` / `apply excludes` options

`show` (read-only):

| ID | Flag | Description |
|----|------|-------------|
| [[R-CLI-08]] | *(always run)* | Static pattern diagnostics — silent when clean, so no flag |
| [[R-CLI-09]] | `--live` | Add the live filesystem as a column (default) |
| [[R-CLI-10]] | `--snaps` | Add a column per local btrfs snapshot |
| [[R-CLI-11]] | `--repo` | Add a column per snapshot stored in restic (network) |
| [[R-CLI-12]] | `--all` | Every selector above, in one table |
| [[R-CLI-13]] | `--price` | Walk matched directories for exact totals; the default prunes them unread and prints `?` |
| [[R-CLI-14]] | `--reveal PATTERN` | What-if: treat PATTERN as a rule, price its matches whole and list them (repeatable) |
| [[R-CLI-15]] | `--reveal-file PATH` | A file of `--reveal` patterns, `ignore_file` syntax |
| [[R-CLI-16]] | `--excludes-file PATH` | Add a working pattern file's rules, same syntax as `ignore_file` |
| [[R-CLI-17]] | `-e, --exclude PATTERN` | Add one rule, repeatable |
| [[R-CLI-18]] | `--format text\|csv\|markdown` | How to render the grid; only `text` is fitted to the terminal |
| [[R-CLI-19]] | `-v, --verbose` | Add the per-file sheet under the rule table |
| [[R-CLI-20]] | `--job NAME` | Restrict to one job |
| [[R-CLI-21]] | `--json` | Machine-readable output; one document for the whole invocation |
| [[R-CLI-22]] | `--limit N` | Bound the per-file rows under `-v`; rule rows and totals cover everything |

With no selector, `show` runs `--live`: a local, shallow walk that prunes matched
directories unread exactly as restic does — local, but not "instant", because the
walk itself still costs the source's directory tree.

`apply` (mutating; restic only):

| ID | Flag | Description |
|----|------|-------------|
| [[R-CLI-23]] | `--now` | Execute now instead of printing the plan |
| [[R-CLI-24]] | `--later` | Arm the next normal run to apply |
| [[R-CLI-25]] | `--job NAME` | Restrict to one job |
| [[R-CLI-26]] | `--json` | Machine-readable output |
| [[R-CLI-27]] | `--limit N` | Bound displayed rows/columns; totals still cover everything |

`--live`, `--snaps` and `--all` are **refused** by `apply` with an explanation,
since they name sources apply never removes files from. So are
`--excludes-file`, `-e`, `--reveal`, `--reveal-file` and `--price`, since a trial
or survey rule set must never drive a rewrite of real backups.

### Exit codes

[[R-CLI-28]] A verb's status reaches the process: `show` and `apply` exit with
what they return, so a failure is visible to whatever monitors them rather than
always looking successful.

[[R-CLI-29]] Inspection uses the shared ladder in `cli.py`, unchanged: 0 on
success (including "nothing matched"), 2 for a config error, 1 for anything else.
[[R-CLI-30]] `apply --now` additionally uses 11/12 (restic's own
locked/bad-password codes) when the preflight aborts, so a caller can distinguish
"refused before touching anything" from "failed mid-run".

### Environment Variables

| ID | Variable | Description | Production value |
|----|----------|-------------|------------------|
| [[R-ENV-01]] | `SNAPBAK_CONFIG_FILE` | Config file path | `/opt/snap-bak/config.json` |
| [[R-ENV-02]] | `SNAPBAK_JOB` | Job name to run | — |
| [[R-ENV-03]] | `SNAPBAK_SUDO` | Sudo binary used to invoke the wrapper | `sudo` |
| [[R-ENV-04]] | `SUDO_EXEC_PATH` | Path to the root-owned wrapper | `/opt/snap-bak/sudo-exec.py` |
| [[R-ENV-05]] | `SNAPBAK_STATE_DIR` | State directory override (default: `/opt/snap-bak/state`) | `/opt/snap-bak/state` |
| [[R-ENV-06]] | `RESTIC_CACHE_DIR` | Restic cache base (not set; restic uses `$HOME/.cache` → `/opt/snap-bak/.cache` → `/opt/snap-bak/cache`) | — |

---

## Privileged Operations & Security

[[R-PRIV-01]] Btrfs snapshot create/delete and mount/umount require root.
snap-bak runs as an unprivileged service user and delegates these to a
**root-owned wrapper** (`sudo-exec.py`, `root:snap-bak 0750`), reachable via a single static sudoers
rule:

```
snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/sudo-exec.py
```

[[R-PRIV-02]] A bare command name (no arguments listed) matches any arguments,
per sudoers(5). This is valid on both sudo-rs and sudo.ws; a trailing `*` is
deliberately NOT used because sudo-rs mishandles it. The wrapper:

- [[R-PRIV-03]] Accepts only `snapshot|delete|mount|umount <job> [snapshot]`, `status <job>`,
  and `update-restic`.
- [[R-PRIV-04]] Validates `<job>` against the root-owned `/opt/snap-bak/config.json`.
- [[R-PRIV-05]] Validates `[snapshot]` against `^\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$`.
- [[R-PRIV-06]] Confines every path under the job's `snapshot_dir` / `mount_base`
  (via `os.path.realpath` traversal guard).
- [[R-PRIV-07]] Executes `btrfs`, `mount`, `umount` with config-derived paths.
- [[R-PRIV-08]] Mounts snapshots read-only at `<mount_base>/<source_subvolume last
  component>` using `mount -o bind,ro` (btrfs has no uid/gid mount options).
  The mount point always mirrors the real filesystem path (e.g.
  `/mnt/data` -> `<mount_base>/data`) and stays stable across
  backups (restic change-tracking keys on the source path); it is never
  configurable. `<name>` is only a display id and does not affect the mount
  point.
- [[R-PRIV-09]] `update-restic` validates that the restic binary is a root-owned regular
  file in a root-owned, non-writable directory (rejecting symlinks and
  group/other-writable paths), runs `restic self-update` (GPG-verified), then
  restricts the binary to `root:snap-bak 0750` and applies
  `cap_dac_read_search=+ep` — restic reads root-owned snapshots via this
  capability, and the mode is what keeps it to root and the service group.
  [[R-PRIV-10]] Exit codes: `0` ok, `2` self-update failed (existing binary
  retained, restricted and capable), `3` binary missing/unusable, `4`
  restriction or setcap failed.

[[R-PRIV-11]] Because the wrapper derives paths from config at runtime, changing
`mount_base` or `snapshot_dir` never requires editing sudoers.

Invariants snap-bak must never breach:

1. [[R-PRIV-12]] **Least privilege.** Only `sudo-exec.py` performs privileged operations.
   snap-bak is granted none of them; sudo permits only the wrapper.
2. [[R-PRIV-13]] **Root-owned files.** `sudo-exec.py`, `config.json`, and the restic binary
   are root-owned, not writable by group/other — the wrapper is the root
   boundary, config is its trusted input, and restic carries
   `cap_dac_read_search`. Writable by snap-bak ⇒ trivial root escalation.
3. [[R-PRIV-14]] **Trust boundary.** `sudo-exec.py` is trusted, snap-bak is not. The wrapper
   validates every argument against root-owned config; the btrfs and mount
   operations it performs never take a path or name from snap-bak unvalidated.
4. [[R-PRIV-15]] **Update trust delegated to restic.** `restic self-update` verifies the
   download against the PGP key embedded in the binary and the GPG-signed
   `SHA256SUMS`. snap-bak adds no signature checks and must not bypass
   restic's. — [stable releases](https://restic.readthedocs.io/en/stable/020_installation.html#stable-releases)
5. [[R-PRIV-16]] **restic execution restricted.** The binary carries
   `cap_dac_read_search=+ep` and is `root:snap-bak 0750`, so only root and the
   service group can run it. That capability reads any file, so execution is the
   boundary: the wrapper restricts the mode *before* it hands out the capability,
   and a failure at either step leaves no capable binary others can run.
   — [file capabilities](https://restic.readthedocs.io/en/stable/080_examples.html#using-file-capabilities)

[[R-PRIV-17]] restic does not run as root. It runs as the service user, and the
capability is what lets it read the root-owned snapshot mount, whose files keep
their original owners and modes. snap-bak invokes restic itself, deriving the
source and repository from the root-owned config, so the paths it backs up come
from a trusted file even though the process is unprivileged; the wrapper is not
in that path.

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
- Exclude pattern matching (port of restic's `internal/filter` golden cases)
- Exclude pattern-file parsing (comments, `$VAR`, `$$`)
- Exclude lint diagnostics (unanchored/anchored/never-matching patterns)
- `restic ls --long` parsing (filenames with spaces, UTF-8)
- Per-snapshot excludes table rendering + `--limit` truncation
- Queued-task dispatch: arming, duplicate refusal, success clearing the entry,
  failure retaining it with an attempt count, unknown task names
- `apply excludes` gate logic (injected clock)
- `apply excludes` end-to-end against a throwaway local restic repo
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

## Deployment Contract

[[R-DEPLOY-01]] These are requirements on the install, not a description of one
host. Every command README presents for operating an installed box, and every
command in the agent skill, must work after a fresh box meets the baseline and
runs the official install, with nothing else done. A command that needs more is
a bug in the docs or the install. The explicitly labelled development option in
README is outside this contract.

- [[R-DEPLOY-02]] **Baseline.** Linux with btrfs and systemd, Python 3.14 or newer
  with uv or pip, git, PyPI access for the first install, and restic at
  `/usr/local/bin/restic` as a real root-owned file. rclone only for `rclone:`
  repositories. See *Supported Platform & Dependencies*.
- [[R-DEPLOY-03]] **Only install.** A clone plus `snap-bak-setup.py`, optionally
  with `PYTHON_BIN`. No pip install by the operator, no operator-managed
  virtualenv, no PATH edit.
- [[R-DEPLOY-04]] **Command.** The install writes `/usr/local/bin/snap-bak`,
  bound to the venv interpreter and `/opt/snap-bak/config.json`. It runs the
  installed distribution whatever the caller's cwd contains, so a stray
  `snap_bak/` cannot shadow it, and bare commands read the production config the
  same way the service does.
- [[R-DEPLOY-05]] **Operator.** The operator is an administrator with sudo. Tool
  commands run as the service user, `sudo -u snap-bak snap-bak ...`, because
  state, cache, the restic config and the sudo wrapper all belong to that
  identity. Journal and state reads need root. Files passed to the tool (`-c`,
  `--excludes-file`) must be readable by the service user.
- [[R-DEPLOY-06]] **Version.** The package is installed as a distribution in
  `/opt/snap-bak/venv`, and `snap-bak --version` reports that distribution's
  metadata. There is no separate version file.
- [[R-DEPLOY-07]] **Upgrade.** Re-running the script is idempotent and keeps
  `config.json`. It installs a committed tree only: a checkout with uncommitted
  changes is refused, because what runs in production must be a commit git can
  name and install again. `--ref <sha|tag>` installs that commit instead of
  `HEAD`, through the current installer, so the layout, launcher, units and
  wrapper stay the ones this script defines. No copy of the previous install is
  kept; undo is installing an older ref.
- [[R-DEPLOY-08]] **Record.** The install writes the commit it installed to
  `<install>/venv/REF` and appends one line per code change to
  `<install>/updates.log`: UTC timestamp, installed version, commit. Together
  they say what the box runs and what it ran before, without trusting the
  checkout to still be there.
- [[R-DEPLOY-10]] **Report.** A file is written only when its content or mode
  differs from the installed one, and the venv is replaced only when its
  contents differ. A run reports what it found — files added, updated and left
  alone, and whether the venv moved — and `-v` names each file. When the
  installed commit and dependency inputs already match, nothing is built and no
  elevation is requested.
- [[R-DEPLOY-09]] **Elevation.** The installer runs as the administrator, not as
  root. Everything it builds — the venv, the package, the wrapper, the launcher,
  the units, the sudoers rule and the example config — is staged under a temp
  directory owned by that user, and one sudo elevation writes the staged files
  into the root-owned paths. The package manager — uv when installed,
  otherwise pip — runs as that user, never as root, and an existing user, group
  or directory is left alone.

---

## Supported Platform & Dependencies

snap-bak supports:

- Python ≥ 3.14 with uv or pip (the setup populates the venv with whichever
  is available; ensurepip is not needed)
- pydantic >= 2.0 (config models)
- python-dateutil (relativedelta for retention calculations)
- json5 (JSONC config parsing)
- rich (terminal rendering of the reports)
- restic (backup tool), installed at /usr/local/bin/restic
- btrfs-progs (snapshot management)
- git, for the clone and for `--ref` installs
- PyPI access for the first install, for the build backend and the dependencies

The four Python libraries are installed into `/opt/snap-bak/venv` by the setup
script. They do not need to be importable by the base interpreter.
