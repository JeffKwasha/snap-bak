# snap-bak reference

Full detail behind the operational summary in `SKILL.md`. Load this when you need
an exact config key, an exact flag, or the reasoning behind a decision.

## Command surface

### Main run

```
snap-bak [-c CONFIG] [-j JOB] [-n] [--log-file PATH] [-l] [--version] [show|apply] ...
```

| Flag | Meaning |
|------|---------|
| `-c, --config PATH` | Config file. Default `$SNAPBAK_CONFIG_FILE`, else `/opt/snap-bak/config.json` |
| `-j, --job NAME` | Restrict to one job. Default: all jobs |
| `-n, --dry-run` | Preflight and plan, but no upload and no deletion |
| `--log-file PATH` | Also write logs to a file (default: stderr only) |
| `-l, --list-jobs` | Print resolved job names and exit |
| `--version` | Print the installed version (from package metadata) and exit |

With no subcommand, snap-bak performs a full scheduled run.

### `show excludes`

Read-only. Never creates, removes, or modifies a snapshot or a local file.

| Flag | Meaning |
|------|---------|
| `--live` | Match the live filesystem. **Default when no selector is given** |
| `--snaps` | Match local btrfs snapshots (local) |
| `--repo` | Match files already stored in restic (network, metadata only) |
| `--all` | All three, in one pass |
| `--price` | Walk matched directories for exact sizes; the default prunes them unread and prints `?` |
| `--reveal PATTERN` | What-if: treat PATTERN as a rule, price its matches whole and list them (repeatable) |
| `--reveal-file PATH` | A file of `--reveal` patterns, `ignore_file` syntax |
| `--excludes-file PATH` | Add a working pattern file's rules to the job's |
| `-e, --exclude PATTERN` | Add one rule, repeatable |
| `-j, --job NAME` | Scope to one job |
| `--json` | Machine-readable |
| `--format text\|csv\|markdown` | How to render the grid; only `text` is fitted to the terminal |
| `-v, --verbose` | List every excluded path, not just the trial's new rules |
| `--limit N` | Bound displayed rows. Totals still cover every match |

The local modes prune matched directories unread, exactly as restic's walker
does, so a rule's totals are `?` (unknown, not zero) unless `--price` walks
them out; `+?` marks a price that covers only part of what a rule matched. CSV
writes an empty cell for an unknown total rather than a false `0`, and JSON
carries a `rough` key only when there is a caveat. `--repo` is always exact.

`--excludes-file` and `-e` **add to** the job's patterns rather than replacing
them. They take the same syntax an `ignore_file` uses, so a rule moves between
the two unchanged. The config is never modified. An unreadable trial file exits
1 rather than silently falling back to the config's rules.

Effective order is `config → --excludes-file → -e`, with duplicates dropped
keeping first occurrence (`dict.fromkeys`, not a `set`: restic applies `!`
negation sequentially, so `['**','!keep']` and `['!keep','**']` give opposite
answers and a set would silently reorder the user's rules).

Two consequences:

- A trial's `!` **does** cancel a config rule naming the same target
  (`cache` then `!cache` stores the directory). Negation is sequential and the
  trial runs last. A trial cannot *reorder*, though; the order above is fixed.
- A trial's `!` cannot reach **inside** a directory an earlier pattern already
  matched: once `node_modules/` matches, both `!node_modules/keepme/` and
  `!node_modules/keepme/vital.js` are ignored. The
  negation that works names the directory itself, after the exclusion
  (`logs/` then `!logs/`, which does restore the directory and its contents).
  To keep one file out of a wholesale-excluded directory, exclude that file
  directly rather than its parent.

Without `-v`, a trial lists only rows attributed to its own added rules.
Totals are unaffected.

Cost scales with file count x pattern count, and `**` rules inflate the
pattern side. `--repo` is one `restic ls --long` per stored snapshot.

**Do not prune the repo listing by listing subdirectories instead of the whole
tree.** Each restic invocation costs ~0.75s before doing any work; N pruned
`ls <snap>:<subdir>` calls lose to one full listing at any N above two. On rclone
that fixed cost is a network round trip.

Neither `restic backup --dry-run` nor `find` is a substitute: `--dry-run` reports
only what it **kept**, never an excluded path, and `find` has no equivalent for
`!` negation.

### `apply excludes`

Restic-only. Refuses `--live`, `--snaps`, `--all`, `--excludes-file` and
`-e`, each with an explanation — a trial rule set must never be able to drive a
rewrite of real backups.

| Flag | Meaning |
|------|---------|
| *(none)* | Dry-run: print the plan. Mutates nothing |
| `--now` | Execute the rewrite immediately, then prune |
| `--later` | Queue it for the next eligible scheduled run |
| `-j, --job NAME` | Scope to one job |
| `--json` | Machine-readable |

`--now` and `--later` are mutually exclusive.

### Output

One row per **exclude rule**, one `files / bytes` column pair per **source**,
plus a total row. Every configured rule gets a row, including one that matches
nothing — that zero row is what distinguishes "not deployed yet" from "stopped
matching". `+` marks a rule added by `--excludes-file` / `-e`.

```
TOTAL files   TOTAL bytes        live   snap:2026-01-22_10:00Z   restic:a1b2c3d4   exclude pattern
───────────────────────────────────────────────────────────────────────────────────────────
      4223       8.1T    8 / 28.8K   8 / 28.8K     15 / 4.5G   cache
       135       1.0M  123 / 987 B  135 / 1.0M    12 /  77 B   *.tmp
         0         0        0 / 0     0 / 0          0 / 0     *.iso
      4361       13.1T 131 / 29.8M  143 / 1.0M    30 / 4.5G   TOTAL
```

Source labels: `live`, `snap:<btrfs snapshot name>`, `restic:<short id>`.

**What counts as a file** — a non-directory entry restic stores:

| Kind | Files | Bytes |
|------|-------|-------|
| regular file | 1 | `lstat` size |
| symlink | 1 | **0** — restic reports the node at 0 |
| hardlink | 1, once per path | full size per path, matching restic's `ls` |
| directory | **0** | 0 |

Hardlinked data is therefore counted once per path, over-stating what `prune`
reclaims by exactly the shared blocks. The post-prune `stats` delta is the
authoritative reclaimed figure.

`-v` adds a second table, one line per excluded file, paths **relative to the
mount point**, with the file-count columns dropped (each line is one file). A
matched directory is opened up into its contents.

| Format | Columns | Bytes | Fitted to terminal |
|--------|---------|-------|--------------------|
| `text` (default) | fitted, oldest dropped first | ≤4 chars (`8.7M`) | yes |
| `markdown` | every column | abbreviated | no |
| `csv` | union across jobs, one header | raw integers | no |
| `--json` | every column | raw integers | no |

Only the console is fitted. Dropped columns are reported underneath with what
they held, so a fitted table never reads as a complete one.

`--format csv` columns: `job, root, scope, row, rule_or_path`, then
`<source>_files` / `<source>_bytes` per source, then `total_files`,
`total_bytes`. `scope` is `rule`, `file` or `note`; `row` is `detail` or `total`.
The prose diff header is suppressed under CSV, since it would corrupt the first
record.

`--json` emits **one** document for the whole invocation:

```json
{"lint": {"issues": []},
 "jobs": [{"job": "data", "root": "/bak/E",
           "sources": [{"kind": "repo", "label": "a1b2c3d4", "files": 12, "bytes": 77}],
           "rules": [{"rule": "*.tmp", "added_by_trial": false,
                      "sources": {"restic:a1b2c3d4": {"files": 12, "bytes": 77}},
                      "total_files": 12, "total_bytes": 77}],
           "files": [], "files_omitted": 0,
           "totals": {"rules": 4, "files": 4361, "bytes": 13000000000},
           "notes": []}]}
```

`notes` is where an unreadable repository is reported. **It is the field to
check first on any repo-scoped output.** A non-empty `notes` means the repository
was never read, so zero columns and `count: 0` mean *unknown*, not *clean*. The
exit code says so too: 30 rather than 0.

Scan progress goes to stderr about once a second, only when stderr is a terminal
or `-v` was passed, and never while emitting a machine-readable format.

## Exit codes

Codes are stable and are the intended interface for monitoring and for agents.
Two groups:

- **Permanent** — will not resolve without a change: 2, 3, 4, 40, 41, 50, 51
- **Temporary** — retry later; the environment may fix itself: 30, 31

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | `EXIT_SUCCESS` | Ran, and found nothing wrong |
| 1 | `EXIT_ERROR` | Ordinary error: unknown job name, refused action |
| 2 | `EXIT_CONFIG_ERROR` | Config failed validation. Nothing ran |
| 3 | `EXIT_MOUNT_ERROR` | Mount or subvolume mount failed |
| 4 | `EXIT_PERMISSION_ERROR` | Permission denied (sudo wrapper refused) |
| 30 | `EXIT_TEMP_UNREACHABLE` | Repo unreachable or locked; ran out of window time |
| 31 | `EXIT_TEMP_SIZE_LIMIT` | Destination out of space |
| 40 | `EXIT_PERM_TIMEOUT` | Timeout beyond the stuck threshold |
| 41 | `EXIT_PERM_FAILED` | Permanent failure: bad password, restic error |
| 50 | `EXIT_SYSTEM` | Internal error; the log has the traceback |
| 51 | `EXIT_STUCK_WAIT` | A previous run appears stuck |

restic's own codes map into snap-bak's:

| restic | Meaning | snap-bak |
|--------|---------|----------|
| 10 | Repository does not exist | 30 (temporary) |
| 11 | Repository locked by another restic | 30 (temporary) |
| 12 | Wrong or missing password | 30 (temporary) |
| other | Genuine restic failure | 41 (permanent) |

### `query_error`

The one field to check first on repo-scoped JSON. Present on every `--repo` and
`apply` object, explicitly `null` when restic answered:

```json
"query_error": {
  "path": "/mnt/rclone:backup",
  "returncode": 11,
  "retryable": true,
  "detail": "Fatal: unable to create lock in ..."
}
```

Non-null means **the repository was never read**. The `snapshots: []`,
`count: 0` and `total_size: 0` in that same object are then *unknown*, not
*clean*. `retryable: true` corresponds to exit 30.

`returncode` is `null` for a timeout or unparseable output, where there is no
restic code to report.

## Config

Top level (`/opt/snap-bak/config.json`, JSON5 — comments permitted):

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `repos` | list | required | At least one |
| `jobs` | list | required | At least one |
| `mount_base` | string | required | Parent of every job's mount point |
| `stuck_threshold` | int | `3600` | Seconds before a run counts as stuck |
| `stuck_wait` | int | — | How long to wait on a stuck instance |
| `restic_update` | string | — | `auto` / `never` / an interval |
| `state_retention_days` | int | `90` | Days to keep state for a job that left the config |

Repo fields:

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `name` | string | required | Referenced by `jobs[].repo` |
| `path` | string | required | Local path or `rclone:` / `s3:` / `b2:` URL |
| `password_file` | string | required | Path to the restic password |
| `size_limit` | int | none | Refuse to exceed this many bytes |
| `latency_ms` | int | none | Measured RTT, for scheduling |
| `throughput_mbps` | int | none | Measured throughput, refined over time |
| `restic_options` | list | `[]` | Extra restic flags |

Job fields:

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `name` | string | required | `[A-Za-z0-9_-]`. Sets the state file and restic tag |
| `snapshot_dir` | string | required | Must exist |
| `source_subvolume` | string | required | Mount point = `<mount_base>/<last component>` |
| `repo` | string | required | A `repos[].name` |
| `priority` | int | `100` | Higher runs first |
| `snapshot_interval` | int/string | `3600` | `>= 900`, or `"2h"` |
| `backup_interval` | int/string | `3600` | Or `"1w"` |
| `backup_window_start` | string | `null` | `HH:MM`, server **local** time |
| `backup_window_end` | string | `null` | `HH:MM`, server local time |
| `skip_snapshot_if_idle` | bool | `true` | Never wakes a spun-down disk |
| `backup_retention` | dict | `{}` | e.g. `{"daily": 6, "monthly": 4}` |
| `snapshot_retention` | dict | `{"fn":"exponential","config":{"n":5}}` | Local snapshots |
| `ignore_file` | string | `""` | Restic exclude-from file |
| `excludes` | list | `[]` | Restic exclude patterns |
| `tag` | string | job name | Restic tag override |
| `restic_options` | list | `[]` | Extra restic flags for this job |

**Every model forbids unknown keys.** A mistyped key is exit 2 with
`Extra inputs are not permitted`, never a setting that silently does nothing.

### `restic_options` scoping

Global restic flags (from `RESTIC_GLOBAL_FLAGS` in `src/snap_bak/restic.py`,
derived from `restic --help`) apply to every restic command. All other options
are backup-only and apply to `backup` alone. `--exclude-file` and `--exclude`
apply to `backup` **and** `rewrite` — the two commands that take a file set — and
to nothing else, since e.g. `forget` rejects them.

snap-bak-managed flags are filtered out with a warning if configured: `-r`,
`--password-file`, `--json`, `--dry-run`, `--tag`, `--keep-*`, `--prune`,
`--exclude`, `--exclude-file`, `--forget`.

## Pattern syntax

Go `filepath.Match` per segment, plus `**`. `**` must be a *complete* path
component (`foo/**/bar`, not `foo**` or `foo/**bar`). Patterns match on complete
path components: `foo` matches `/dir1/foo/dir2/file` but not `/dir/foobar`. A
leading `/` anchors at the root. A **trailing `/` is ignored** — `foo/` ≡ `foo`,
so it is not a directories-only marker. Blank and `#` lines are ignored, `$VAR`
is expanded with `$$` for a literal dollar, and `~` is not expanded (use
`$HOME`).

`!` negates. Negation is sequential and the **last match wins**, so
`--exclude '*' --exclude '!keep.txt'` keeps only `keep.txt`. A matched directory
is pruned, so no later `!` can reach inside it; only a `!` naming the directory
itself, ordered after the exclusion, restores it.

Negation and prune behaviour are **undocumented upstream** — restic describes
syntax only. Treat them as volatile: a restic release may change them, and
snap-bak follows. Do not present them as restic's specification.

## State

One file per job: `/opt/snap-bak/state/<job>.json`, holding that job's last
status **and** its queued work.

```json
{
  "job": "data",
  "status": "success",
  "last_run": "2026-10-09T02:14:33Z",
  "last_error": null,
  "missed_slots": 0,
  "queued": []
}
```

The planning fields `delayed`, `delayed_reason`, `protected_snapshot` and
`missed_slots` are part of the tool's interface: persisted here, never read from
config.

`write_status` rewrites the whole document on every transition and carries
`queued` forward — the only key it preserves. An empty queue is the sole record
that nothing is outstanding; there is no record of completed work anywhere.

Other files in that directory: `snap-bak.lock` (global lock) and
`throughput.json` (per-repo transfer history, capped).

State for a job that has left the config is deleted after `state_retention_days`
(90). A job still in the config is never reaped — its file is rewritten every
run, and reaping it would reset the missed-slot escalation that drives priority.
Queued work is never reaped for age.

## Queued work

`apply excludes --later` adds an entry to `queued`:

```json
{
  "task": "rewrite-drop-excluded-files",
  "armed_at": "2026-10-09T12:00:00Z",
  "attempts": 0,
  "last_rc": null,
  "last_error": null
}
```

Rules:

- Succeeding, or finding nothing to do, **removes** the entry.
- Failing or being delayed **keeps** it, with `attempts` and the last restic
  exit code, and retries at the next eligible window. The count escalates the
  log; it never gates the retry, because the window gates already bound how
  often this runs.
- Queueing the same task twice is an error, not a silent no-op.
- `--later` refuses when there is nothing to remove.
- `--now` clears a matching entry only after success.
- A dry run never touches the queue.

The task name is declared once, by the handler that implements it
(`@todo.task(...)` in `src/snap_bak/apply.py`), so a name cannot drift from the
function it names.

### Run-time gate

Queued work runs only when all hold, evaluated after backups complete, cheapest
first:

1. The job has something queued
2. The job has excludes to apply
3. Inside the backup window
4. At least **half** the window length remains
5. Repo reachable and unlocked
6. At least one snapshot actually contains a matching file

Phase order is backups → queued work → retention(forget) → prune. Queued work
registers its repos into the prune set, so one prune per repo reclaims both.

`rewrite` is metadata-scale — it rebuilds tree blobs only, file data stays shared
— which is why a cheap gate suffices. `prune` is not: it repacks and re-uploads
affected packs, which on rclone backends is minutes to hours of real network
I/O.

### A check, not a gate

The backup that just ran used the job's current excludes, so its snapshot should
hold nothing to remove. Matches in the newest snapshot mean the backup path is
not honouring the excludes. The rewrite still proceeds, and the run logs a
warning naming the bug.

## Why `--forget` is bundled into the rewrite

A standalone `restic forget <ids>` would **also apply retention policies** to
that group and could delete snapshots beyond the intent. `--forget` removes
exactly the named snapshots. Restic adds a `rewrite` tag only when `--forget` is
*not* used, so `--forget` also keeps the tag set exactly as it was, which is what
retention matches on.

## Troubleshooting

| Symptom | Cause | Check |
|---------|-------|-------|
| Exit 2, `Extra inputs are not permitted` | Unknown config key | The named key; check for a renamed field |
| Exit 2, `snapshot_dir does not exist` | Job points at a missing path | `snapshot_dir` in the config |
| Exit 30 every run | Repo locked, missing, or bad password | `query_error.detail`; is another restic running? |
| Exit 41 | Wrong password, or restic rejected something | The journal for that run |
| Exit 51 | Previous run stuck | `journalctl -u snap-bak`; an orphaned restic? |
| No new snapshots, no uploads | `skip_snapshot_if_idle` and nothing changed | Expected. Confirm the job is alive via the journal |
| Space not reclaimed after a rewrite | Rewrite makes data unreferenced; `prune` reclaims it | Wait for the run's prune phase, or prune manually |
| Backup ran but restic reports nothing new | Everything excluded | `snap-bak show excludes --live` |

### Lock contention

`--now` deliberately takes no lock and adds no `--retry-lock`: restic serializes
on the repository's own lock, so a contended `--now` fails immediately with rc 11
and the answer is `--later`. A second lock would duplicate restic's, not
strengthen it. Queue writes do take the global lock, because the scheduled run
reads that same file.

## Design notes

- One planner and one executor. `apply.plan()` decides scope; `apply.execute()`
  acts on a plan. `show excludes --repo`, the `apply` dry-run and the scheduled
  run all use the same call, so the plan a user approves cannot drift from the
  run that follows.
- Restoring excluded files from an existing backup is `restic rewrite`, and
  snapshots keep their original time, host and tag, so retention keeps working.
- Config is never mutated by the tool, including when queuing work. Queues live
  in state files.