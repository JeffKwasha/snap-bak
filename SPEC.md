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
| `name` | string | required | `[A-Za-z0-9_-]` | Job id; must be filesystem-safe (`<state>/<name>.json`, restic tag). Does NOT set the mount point |
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
global set (`RESTIC_GLOBAL_FLAGS` in `src/snap_bak/restic.py`, derived from
`restic --help`) apply to every restic command; **all other options are
backup-only** and apply to the `backup` subcommand alone. The `--exclude-file`
and `--exclude` flags (from `ignore_file`/`excludes`) apply to `backup` **and**
`rewrite` — the two subcommands that take a file set — and to no other
subcommand (e.g. `forget` rejects them). Flags snap-bak
manages itself are filtered out with a warning if a config author sets them:
`-r/--repo`, `--password-file`, `--json`, `--dry-run`, `--tag`, `--keep-*`,
`--prune`, `--exclude`, `--exclude-file`, `--forget`.

---

## Status File

Per-job state stored at `/opt/snap-bak/state/<job_name>.json` — the last status
plus that job's queued work.

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

## Excludes: Inspection & Application

### Purpose

`ignore_file` / `excludes` decide what a **future** backup skips. Files already
uploaded before a rule was added stay in the repo forever, because restic's
forget/prune retention works on whole snapshots, never on a file subset.
This feature answers "what do my current rules match?" and, on request,
removes exactly those files from **existing** snapshots.

### Invariants

1. **The local filesystem is never modified.** Every `show` mode is a read-only
   walk; `apply` never touches the live subvolume, `snapshot_dir`, or any btrfs
   snapshot. Patterns are only ever applied to restic snapshots. The only local
   write any of them performs is restic's own cache, and restic's repository
   lock file is created and released per invocation — both restic-managed.
2. **Snapshots are never mounted to test patterns.** Matching runs against the
   path *strings* restic stored, so no mount can race an in-flight backup.
3. **Editing `excludes` alone changes nothing retroactively.** Removal requires
   an explicit `apply` (`--now` / `--later`). There is no config key that
   schedules it.
4. **No persistent config key arms removal implicitly.** The default is `off`.
5. **The dry-run and the run are the same plan.** `plan()` decides scope and
   `execute()` performs it; `show excludes --repo`, the `apply` dry-run, and
   every mutating path call that one pair. A selector that would target the live
   filesystem or local snapshots is refused by `apply` rather than ignored.

### Commands

```
snap-bak show excludes [--live | --snaps | --repo | --all]
                       [--job NAME] [--json] [--limit N]
                       [--format text|csv] [-v]
                       [--excludes-file PATH] [-e PATTERN]
snap-bak apply excludes [--now | --later] [--job NAME] [--json] [--limit N]
```

Grouped by verb: `show` is read-only, `apply` is the only mutating command.
Pattern linting runs on every `show`, silently when clean, so there is no
`--lint` flag — it is not optional, and offering it would imply the modes skip it.

**`show excludes` with no selector defaults to `--live`** — the mode that is
local and instant, so the common case ("are my patterns sane, and what do they
hit right now?") costs nothing.

`apply excludes` operates on restic only and **assumes `--repo`**. Passing
`--live`, `--snaps` or `--all` to it is an error with an explanation, not a flag
that is quietly discarded: those name sources `apply` never removes files from.

### Trial pattern sets

`--excludes-file PATH` and `-e PATTERN` let `show` evaluate a working rule set
without the root-owned config being edited. They **add to** the job's patterns
rather than replacing them, and the config is never modified — not even when the
command is a dry-run.

Both take **exactly the syntax an `ignore_file` uses**: one restic exclude
pattern per line, `#` comments, `!` negation, `$VAR` expansion. A rule can be
copied between the two unchanged, which is the point: working in a file is the
normal way to edit patterns, and re-typing dozens of `-e` arguments is not.

Effective order, which is the part that constrains what a trial can express:

```
config patterns  →  --excludes-file patterns  →  -e patterns
```

Duplicates are dropped keeping first occurrence (`dict.fromkeys`), so a rule
named both on the command line and in the file is one rule, announced once. Not
a plain `set`: restic applies `!` negation sequentially, so order changes
answers and a set would silently reorder the user's rules.

Two consequences, both worth stating because they are easy to assume otherwise.
Both verified against `restic backup` rather than reasoned about, and they cut
in opposite directions:

- **A trial's `!` does override a config rule naming the same target.**
  `--exclude cache --exclude '!cache'` stores the directory; so does a config
  `cache` plus a trial `!cache`. Negation is sequential, and the trial's rules
  are evaluated after the config's, so they win. What a trial cannot do is
  *reorder* — the effective order is fixed at `config → file → -e`.
- **A trial's `!` cannot reach *inside* a directory an earlier pattern already
  matched.** Those patterns run first and prune the directory, so restic never
  descends and the later `!` has nothing to act on — the same prune semantics as
  any other walk (see *Pattern matching*). Once `node_modules/` matched, both
  `!node_modules/keepme/` and `!node_modules/keepme/vital.js` were ignored and
  nothing under it was stored. The negation that works is one naming the
  **directory itself**, in the same file and after the exclusion (`logs/` then
  `!logs/`, which does restore the directory and its contents). To keep one file
  out of a directory excluded wholesale, exclude that file directly rather than
  its parent.

So a trial set is not strictly "tightening only": it can cancel a rule outright,
and it can never reach beneath a pruned directory.

An unreadable trial file exits 1 naming the path. Falling back silently to the
config's rules would report figures the caller did not ask for.

`apply` refuses `--excludes-file` and `-e` with an explanation: rewriting real
backups against rules the deployment does not use would remove data on the
strength of an experiment. The trial belongs to `show`; `apply` acts only on
what is configured.

Without `-v`, a trial lists only rows attributed to its **own added rules** — the
config's rules are already in force and would bury the answer. Totals always
cover the whole set.

| Selector | Source | Cost |
|----------|--------|------|
| *(default)* | `--live` — live filesystem walk | instant, local |
| `--snaps` | every local btrfs snapshot dir | local, one stat per match |
| `--repo` | files stored in restic snapshots | **network** (`restic snapshots` + `restic ls` per snapshot) |
| `--all` | every selector above | includes the network path |

`--limit N` bounds displayed rows/columns; **totals always cover the full set**
so a truncated table never understates. `--json` emits the same data
structurally, for agents: one schema for every mode, so `show --live` reports
empty `snapshots` / `per_snapshot` rather than omitting the keys.

### Pattern matching

`src/snap_bak/excludes.py` is a faithful port of restic's own exclusion engine
(`internal/filter`, matching restic 0.19.1) so listings agree with what restic
would do.

**Where the rules come from.** restic's documentation (`doc/040_backup.rst`,
"Excluding files") is the authority for **syntax** and covers it well:
`filepath.Match` per segment, `**` as a complete path component, patterns
matched on complete path components, a leading `/` anchoring at the root, **a
trailing `/` being ignored** (so `foo/` ≡ `foo` — it is *not* a
directories-only marker, and this was confirmed against `restic backup`), blank
and `#` lines dropped, `$VAR` expansion with `$$` for a literal dollar, and
`~` deliberately *not* expanded.

It says nothing at all about **negation**. The word does not appear in the
backup documentation, nor does any statement of what happens when a directory
matches. Everything below about `!` and about pruning was therefore established
by experiment against `restic backup` rather than read, and is pinned by
`tests/test_excludes.py` against restic's own golden cases plus the CLI
integration test. Do not go looking for a citation that does not exist.

Ported semantics:

- Per-segment Go `filepath.Match` — `*` never crosses `/`, plus `?`, `[a-z]`,
  `[^a]`, `\` escapes. Deliberately **not** Python `fnmatch`.
- A pattern with no leading `/` matches at **any** path offset (`Trash` matches
  `/bak/home/Trash/x`); a leading `/` anchors to the root.
- `**` matches any number of intermediate directories.
- `!` prefix negates, evaluated in order (last match wins). Verified: with
  `--exclude '*' --exclude '!keep.txt'`, restic stores only `keep.txt`.
- Patterns are `filepath.Clean`ed, so `/foo/../bar` ≡ `/bar`.
- `ignore_file` lines are trimmed; blanks and `#` comments dropped; `$VAR` is
  expanded and `$$` yields a literal `$`.

Errors during inspection are reported but never fatal: an unreadable directory,
a file that vanishes mid-walk, or a pattern restic itself would reject (caught
and reported by the lint that runs on every `show`) all degrade to "skip this
path" rather than aborting
the run.

Directory matching prunes the walk, mirroring restic's walker: it does not
descend into an excluded directory, so a `!` pattern cannot re-include something
inside one. Symlinks are never followed: restic stores a link as a link, so
following one would name paths the repo does not contain.

Because pruning is what restic does, the rule order is load-bearing and cannot
be worked around by reordering later patterns: the excluding pattern is
evaluated first and the subtree is never visited, so nothing after it applies
inside — including a `!` naming a path beneath it. Only a `!` naming the
directory itself, ordered after the exclusion, restores it. Note this is not the
same as negation in general, which is sequential and does let a later `!` cancel
an earlier rule on the *same* target. This is why a trial pattern set's reach is
bounded — see *Trial pattern sets*.

### Path basis

Backups run `restic backup <mount_base>/<source basename>`, so stored paths are
`/bak/<name>/...`. **All matching uses that mount-point path.** `--live` and
`--snaps` map live paths onto their mount-point equivalent before matching, so
every mode answers the same question: *what would the next backup skip?*

Consequence: a relative pattern (`*.tmp`, `Trash`) behaves identically
everywhere, while an anchored pattern (`/bak/home/Trash`) is anchored to the
backup root. The lint that runs on every `show` flags anchored patterns that
can never match that root —
the common authoring mistake of writing `/home/user/x` when the root is
`/bak/data`.

### `--snaps` / `--repo` table

Rows are matched files, one column per snapshot, cell = bytes that snapshot
would drop, `-` when absent. The final row totals per snapshot and overall.
A directory that matches appears as one row carrying its whole subtree, and
its contents are not listed separately (restic prunes rather than descends).

```
                                      10-07Z  10-06Z  10-05Z      TOTAL
/bak/E/build/cache                 800 MiB       -  1.2 GiB    2.0 GiB
/bak/E/build/tmp.o                     -   12 MiB       -     12 MiB
BYTES TO EXCLUDE                  800 MiB   12 MiB  1.2 GiB    2.0 GiB
```

Sizes are raw (uncompressed) apparent sizes. Actual reclaimed space differs:
restic compresses, dedupes, and repacks, so the post-prune `stats` delta is
reported separately and is the authoritative figure.

### `apply excludes`

Dry-run by default: prints affected snapshot IDs, projected bytes, and the exact
`restic rewrite` command that `--now` would run. Only `--now` and the scheduled
run mutate.

`--later` **queues** the rewrite as `rewrite-drop-excluded-files` on that job's
state file, after checking there is genuinely something to do: a job whose repo
has no snapshots holding excluded files is refused rather than queued. An empty
repo means the store went empty or retention took everything, which is a
problem to notice, not a rewrite to schedule. Queuing a task that is already
queued is an error, not a silent no-op.

Execution order (each step is separately failure-isolated):

1. **Plan** — `apply.plan()`, the same call `show excludes --repo` and the
   dry-run use, so what the user approves is what runs.
2. **Preflight** — repo reachable (`restic cat config`; rc 10/12 abort before any
   write) and unlocked (rc 11 abort). Nothing has been modified at this point.
3. **Rewrite** — a single `restic rewrite <ids> --exclude-file … --exclude …
   --forget`. Rewritten snapshots keep their original time, host and tag, so
   retention keeps working. restic adds a `rewrite` tag only when `--forget`
   is *not* used, so `--forget` keeps the tag set exactly as it was.
4. **Prune** — one `restic prune` per repo to actually reclaim the space. The
   measured duration is logged: prune is the expensive step, and seeing how long
   it took is how an operator judges whether the gate is set too generously.

**Contention.** A scheduled apply runs inside the GlobalLock the run already
holds, because that lock is what guards local btrfs snapshots as well. `--now`
deliberately takes no lock and adds no `--retry-lock`: restic serializes on the
repository's own lock, so a contended `--now` fails immediately with rc 11 and the
answer is `--later`, which rides the next scheduled run. restic's own lock is
what prevents repo corruption; a second lock would duplicate it, not strengthen
it.

`--forget` is used in the same command deliberately: a standalone
`restic forget <ids>` would **also apply retention policies** to the group and
could delete snapshots beyond the intent. `--forget` removes exactly the
rewritten originals.

All affected snapshot IDs go into **one** `rewrite` invocation rather than a
loop, so the repo is opened once and the work is all-or-nothing per snapshot:
a snapshot restic could not rewrite keeps its original and its files. Only
snapshots that actually hold a match are rewritten — the test is a per-snapshot
membership check against the collected set, not "did this snapshot introduce a
path we had not seen", which with restic's newest-first listing would rewrite one
snapshot and leave every older copy in place.

Failure behaviour: a failed rewrite leaves the originals in place; a failed
prune leaves data merely unreclaimed. Both are safe to retry. A snapshot with
zero matching files is skipped, so repeated runs converge and never re-upload
identical trees.

Outcomes are reported as `apply.Outcome`, deliberately a different type from
`job.Result` (the one `exit_code()` consumes), so an apply can never move the
run's exit code or be mistaken for a backup result.

### Arming a scheduled cleanup

There is exactly one arming mechanism: the queue. `--later` adds a task to that
job's state file; the next eligible run performs it and removes the entry. There
is no config key that schedules this, so editing `excludes` alone can never
rewrite history.

| Mechanism | Scope | Meaning |
|-----------|-------|---------|
| `--later` | one-shot | the next eligible run does this, then forgets it |

`--now` performs it immediately and clears any matching queue entry.

A task that fails or is delayed **keeps** its entry, so it is retried at the next
eligible window, with `attempts` and the last restic exit code recorded so a
persistent failure escalates in the log rather than retrying nightly in silence.
The attempt count deliberately does not gate the retry: the window gates already
bound how often this runs — at most once per eligible window — and the operation
is idempotent, so re-running is safe.

A task name is declared once, by the handler that implements it
(`@todo.task("rewrite-drop-excluded-files")`), and the registry is built from
those declarations, so a name cannot drift from the function it names. Names are
stored verbatim in the state file and read by a human wondering what a queued
run will do, so they read as the restic operation plus its goal.

### Run-time gate

Queued work runs only when **all** hold, evaluated after backups complete,
cheapest first:

1. The job has something queued.
2. The job has excludes to apply.
3. Inside the job's backup window.
4. At least **half** the window length remains.
5. Repo reachable and unlocked.
6. At least one snapshot actually contains a matching file.

`rewrite` is metadata-scale — it rebuilds and re-uploads *tree* blobs only; file
data blobs stay shared with the original snapshot — which is what makes a cheap
gate sufficient. No duration estimate is needed.

Phase order is **backups → queued work → retention(forget) → prune**, and the
work phase registers its repos into the prune set alongside retention's, so a
single prune per repo reclaims both. A repo whose job has no `backup_retention`
is still pruned if a task made data there unreferenced.

Queued work is the only thing this phase runs, and it is the only work a user
asked for by name, so its outcome joins the run's own results and reaches the
exit-code ladder. A task that fails or is delayed reports non-zero; a task that
found nothing to do reports as an ordinary skip and changes nothing. The ladder
accumulates with `any()` over all results, so a task can never mask a backup
outcome.

A repo that is missing, locked or unreadable (restic 10/11/12) is a *temporary*
problem and reports as `delayed` (exit 30) — it may well be fine on the next
window. Any other restic failure reports as `failed` (exit 41), because it will
not fix itself until the config changes.

**A check, not a gate.** The backup that just ran used the job's current
excludes, so its snapshot should hold nothing to remove. If the plan finds
matches in the newest snapshot anyway, the run logs a warning: the backup path is
not honouring the excludes. The rewrite still proceeds — the data ends up
correct — but the bug gets named instead of being quietly cleaned up after.

A dry run reports and returns without touching the queue at all: simulating a run
must not consume the request the real run is waiting for.

Failures are never retried by a counter. A transient repo problem (unreachable,
locked) breaks every restic operation in the run equally, so the ordinary run
logic already handles it and the next eligible window simply tries again; the
operation is idempotent, so re-running is safe. What snap-bak must not do is
retry silently forever — hence the loud log naming the failing phase and exit
code, and the retained queue entry with its attempt count.

### IO & duration

| Step | Cost |
|------|------|
| `--live` | local; measured 4.7s over an 80k-file tree |
| `--snaps` | local; one stat per matched file per snapshot |
| `--repo` | network: metadata only, cache-warm; seconds–minutes |
| `rewrite` | metadata/tree-scale: new tree blobs + index entries only, since file data is shared with the original snapshot |
| `prune` | **the cost centre**: repacks and re-uploads every pack holding excluded chunks; takes restic's exclusive lock; minutes to hours on rclone backends |

`rewrite` not being data-scale is what makes the cheap run-time gate sufficient
— no duration estimate is needed to decide whether it fits in the window.

Measured, and worth recording because two plausible optimisations are wrong:

- **`**` patterns are the cost driver, not the tree size.** The same 80k-file
  walk takes 4.7s with a normal pattern set and **56s with 40 `**` patterns**.
  Pattern count multiplies path count, so an ignore file with many `**` rules is
  the thing to watch. Resolving a pattern's `**` expansion into concrete part
  lists is memoised (keyed on the parts, not on the pattern text, since
  mid-expansion children of one pattern share both) because rebuilding it per
  path was the single largest cost in a scan.
- **Pruning the `--repo` listing is far worse than the listing it replaces.**
  `restic ls <snap>:<subdir>` looks like a way to avoid descending into excluded
  directories, and is 84× slower: one full `restic ls --long` over an 80k-file
  snapshot took **1.8s**, while 200 pruned calls took **150s**. Each restic
  invocation costs ~0.75s before doing any work — it loads the index and tree —
  so N pruned calls lose to one full listing at any N above two, and on a remote
  backend that fixed cost is a network round trip rather than a local read.

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
<install>                # root-owned: package, wrapper, config
  snap_bak/              # Python package (imported as `snap_bak`; root:root 0644)
    __init__.py
    cli.py               # entry point / orchestration
    config.py            # config loading + models
    excludes.py          # restic exclusion-engine port + inspection
    job.py               # backup engine
    restic.py            # restic command building
    snapshot.py          # snapshot domain model
  sudo-exec.py            # sudo wrapper (root:root 0755)
  config.json            # root-owned, validated by the wrapper

<install>/state          # owned by snap-bak: runtime state
  snap-bak.lock          # Global lock file
  throughput.json        # Per-repo transfer-rate history
  <job_name>.json        # Per-job state: last status + that job's queued work

<install>/excludes       # ignore_file location (root:snap-bak)
<install>/passwords      # per-repo restic password files (owned by the admin, group snap-bak)
<install>/cache          # restic cache (service-owned, real dir)
<install>/.cache         # symlink -> cache; restic's implicit $HOME/.cache default
```

One file per job, holding both that job's last status and the work queued
against it. Keeping them together is deliberate: an entry in `queued` *is* the
record that the work was requested, so nothing else has to remember it, and
there is no record of completed work anywhere. An empty queue is the only thing
that means "nothing outstanding".

`write_status` rewrites the whole document on every status transition, so
`queued` is the one key carried forward (`job._CARRIED_FORWARD_KEYS`).

State for a job that has left the config is deleted once it has been gone for
`state_retention_days` (default 90). A job still in the config is never reaped —
its file is rewritten every run, and reaping it would reset the missed-slot
escalation that drives priority. Queued work is never reaped: an entry is a
request from a human.

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
snap-bak [OPTIONS]                    # one run: validate, plan, backup, retain
snap-bak show excludes [OPTIONS]      # read-only: what do the rules match?
snap-bak apply excludes [OPTIONS]     # dry-run; --now applies, --later arms
```

`show` / `apply` are subcommands. A bare `snap-bak` (no subcommand) is the
normal run, so the systemd service (`ExecStart=python3 -m snap_bak.cli`) is
unaffected.

### Options

| Flag | Description |
|------|-------------|
| `-c, --config` | Path to config JSON (default: `config/config.json`) |
| `-j, --job` | Run specific job by name (default: all jobs) |
| `-n, --dry-run` | Simulate backup (preflight only, no upload/delete) |
| `-l, --list-jobs` | List configured jobs and exit |
| `--log-file` | Also write logs to this file (default: stderr only; captured by systemd journal) |

### `show excludes` / `apply excludes` options

`show` (read-only):

| Flag | Description |
|------|-------------|
| *(always run)* | Static pattern diagnostics — silent when clean, so no flag |
| `--live` | Match against the live filesystem (default) |
| `--snaps` | Match against every local btrfs snapshot, as a per-snapshot table |
| `--repo` | Match against files stored in restic snapshots (network) |
| `--all` | Every selector above |
| `--excludes-file PATH` | Add a working pattern file's rules, same syntax as `ignore_file` |
| `-e, --exclude PATTERN` | Add one rule, repeatable |
| `--format text\|csv` | Row listing format; `csv` emits numeric `bytes` for summing |
| `-v, --verbose` | List every excluded path, not only a trial's own rules |
| `--job NAME` | Restrict to one job |
| `--json` | Machine-readable output |
| `--limit N` | Bound displayed rows/columns; totals still cover everything |

With no selector, `show` runs `--live`: the mode that is local and instant, so
the common case costs nothing.

`apply` (mutating; restic only):

| Flag | Description |
|------|-------------|
| `--now` | Execute now instead of printing the plan |
| `--later` | Arm the next normal run to apply |
| `--job NAME` | Restrict to one job |
| `--json` | Machine-readable output |
| `--limit N` | Bound displayed rows/columns; totals still cover everything |

`--live`, `--snaps` and `--all` are **refused** by `apply` with an explanation,
since they name sources apply never removes files from. So are
`--excludes-file` and `-e`, since a trial rule set must never drive a rewrite of
real backups.

### Exit codes

A verb's status reaches the process: `show` and `apply` exit with what they
return, so a failure is visible to whatever monitors them rather than always
looking successful.

Inspection uses the shared ladder in `cli.py`, unchanged: 0 on success (including
"nothing matched"), 2 for a config error, 1 for anything else. `apply --now`
additionally uses 11/12 (restic's own locked/bad-password codes) when the
preflight aborts, so a caller can distinguish "refused before touching anything"
from "failed mid-run".

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

## Supported Platform & Dependencies

snap-bak supports:

- Python ≥ 3.10
- pydantic >= 2.0 (config models)
- python-dateutil (relativedelta for retention calculations)
- json5 (JSONC config parsing)
- restic (backup tool)
- btrfs-progs (snapshot management)
