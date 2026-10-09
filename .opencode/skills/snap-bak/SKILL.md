---
name: snap-bak
description: Operate and verify the snap-bak backup tool (btrfs snapshots + restic) on a host. Use when asked to run, check, troubleshoot, or validate backups; interpret snap-bak logs or exit codes; inspect, test, or apply exclude patterns; or verify that a backup is actually working. Triggers - snap-bak, restic, btrfs snapshot, backup verification, excludes, ignore_file, "are my backups working", "why did my backup fail", disk space not being reclaimed.
version: 1.0.0
tags: [backups, restic, btrfs, sysadmin, linux, operations]
category: devops
---

# Operating snap-bak

snap-bak is a scheduled backup tool. It takes btrfs snapshots and pushes them
into restic repositories, then applies retention.

**The one rule that matters most here:** a `0` exit from a *verification* command
means the check ran and found nothing wrong. Do not report a backup as healthy
because a command succeeded without confirming which of those two happened.

## Start here

Never guess a job name or a config path. Ask the tool:

```bash
snap-bak --version                 # what is installed
snap-bak --list-jobs               # the real job names
snap-bak -c /opt/snap-bak/config.json --list-jobs
```

Config lives at `/opt/snap-bak/config.json` in production; `--list-jobs` prints
the resolved names, which are the only valid `-j/--job` values.

## Verifying a backup is working

Do these in order, cheapest first. All are read-only.

```bash
# 1. Is the config even valid? (exit 2 = bad config, fix that first)
snap-bak -c /opt/snap-bak/config.json --list-jobs

# 2. Are the exclude patterns sane, and what do they match on disk? (local, instant)
snap-bak show excludes

# 3. What would a backup skip, per local snapshot? (local, instant)
snap-bak show excludes --snaps

# 4. What is already stored in restic? (NETWORK - metadata only)
snap-bak show excludes --repo --json
```

Add `--job NAME` to scope to one job, and `--limit N` to bound rows (totals
still cover everything).

Step 4 is the one that answers "is my backup working". If it exits 0 with
`count: 0`, the stored data genuinely holds nothing your excludes would drop.
If it exits 30 or 41, you learned **nothing** — see exit codes below.

For full per-run detail, read the state file and the journal:

```bash
cat /opt/snap-bak/state/<job>.json     # last status + anything queued
journalctl -u snap-bak --since "2 days ago" --no-pager
```

## Exit codes

| Code | Meaning | What to do |
|------|---------|------------|
| 0 | Success. The operation ran and found nothing wrong | Nothing |
| 1 | Ordinary error (bad job name, refused action) | Read the message |
| 2 | **Config invalid** — a key is unknown or a value is wrong | Fix config; nothing ran |
| 3 | Mount failed | Check `mount_base` and btrfs subvolumes |
| 4 | Permission denied | Check ownership; snap-bak uses a sudo wrapper |
| 30 | Temporary: repo unreachable, locked (restic 10/11/12), or out of time | **Wait and retry.** Not a real failure |
| 31 | Temporary: destination out of space | Free space, or lower retention |
| 40 | Permanent: a timeout that exceeded the threshold | Raise the threshold or reduce scope |
| 41 | Permanent: the operation failed (bad password, restic error) | Fix the cause; retrying unchanged will not help |
| 50 | Internal error | Bug — the log has the traceback |
| 51 | A previous run is stuck | Check for an orphaned restic; see `/opt/snap-bak/state` |

**30 vs 41 is the distinction that matters.** 30 means "the repo was busy or
absent, try again later". 41 means "this will keep failing until you change
something". Reporting a 30 as a broken backup sends an operator chasing a
problem that does not exist.

### Reading JSON output

Every `show`/`apply` mode emits the same keys, so you can rely on them being
present:

```json
{
  "repo": [{
    "job": "data",
    "root": "/bak/E",
    "snapshots": ["10-07Z", "10-06Z"],
    "entries": [{"path": "/bak/E/build/cache", "size": 1073741824,
                 "is_dir": true, "per_snapshot": {"10-07Z": 838860800}}],
    "per_snapshot_totals": {"10-07Z": 838860800, "10-06Z": 1288490188},
    "count": 1,
    "total_size": 2127497984,
    "snapshot_ids": ["a1b2c3d4"],
    "command": ["restic", "rewrite", "a1b2c3d4", "--forget"],
    "query_error": null
  }]
}
```

- `total_size` is reclaimable only if you also prune. Rewriting alone frees
  nothing; `prune` is what repacks and reclaims, and on rclone backends that is
  real network I/O lasting minutes to hours.
- **`query_error` is the field to check first on any repo-scoped output.** It is
  always present, explicitly `null` when restic answered. Non-null means the
  repository was never read, so `snapshots: []` and `count: 0` in that same
  object mean *unknown*, not *clean*. `retryable: true` maps to exit 30.

## Excludes: what they do and don't

`ignore_file` and per-job `excludes` decide what a **future** backup skips.
They do not touch data already in restic — restic retention prunes whole
snapshots, never a file subset. Editing `excludes` therefore never changes
history.

To remove already-stored files, use `apply excludes`. This is restic-only and
never modifies the live filesystem or local snapshots.

```bash
snap-bak apply excludes --job data             # dry-run: plan only
snap-bak apply excludes --job data --now       # rewrite now, then prune
snap-bak apply excludes --job data --later     # queue for the next run
```

Pick deliberately:

- **`--now`** runs `restic rewrite` immediately and then `prune`, which repacks
  and re-uploads affected packs. On a remote backend this can take hours and
  will contend for the repository lock. If the nightly run is active you get
  restic 11 and exit 30; the answer is `--later`, not a retry loop. An
  unreadable repository is **not** reported as "skipped: nothing to do" — it
  exits 30, because unknown is not the same as clean.
- **`--later`** queues the work in the job's state file. The next run performs
  it only when it is inside the backup window with at least half the window
  remaining, and removes the entry when done. This is the default choice.
- No `--now`/`--later` = dry-run. Always do this first.

`apply` refuses `--live`, `--snaps` and `--all` with an explanation. Those name
places it never removes files from. Do not work around that.

## Running a backup

```bash
snap-bak -n                      # dry run: preflight, no upload or deletion
snap-bak                          # run all jobs
snap-bak -j data              # one job
```

Run order is backups → queued work → retention → prune. A run that skips
uploads because nothing changed is normal and still exits 0.

## Pitfalls

- **`skip_snapshot_if_idle` is on by default.** A job whose source has not
  changed produces no new snapshot and no upload. That is correct behaviour, not
  a stall. To confirm a job is alive, look for its timestamp in the journal
  rather than expecting a new snapshot.
- **A 0-byte upload is not proof of failure**, for the same reason. Correlate
  with `show excludes` and the state file.
- **Do not hand-edit state files.** `/opt/snap-bak/state/<job>.json` is written
  atomically by the tool; editing it races the next run.
- **A mistyped config key is now a hard error (exit 2), not a silent no-op.**
  This is deliberate. If you see exit 2 naming `Extra inputs are not permitted`,
  the key you added is not real — check for an old field name.
- **`show excludes` with no selector defaults to `--live`** (local, instant).
  Passing `--repo` is what actually queries the network, and it can be slow on
  a large or remote repo.
- Patterns are linted on every `show` and print nothing when clean, so silence
  there means healthy.

## Verification

After changing anything, confirm with a read-only command and read the exit
code rather than trusting the absence of output:

```bash
snap-bak -c /opt/snap-bak/config.json show excludes --repo --job NAME --json
# expect: exit 0, "query_error": null
# a non-null query_error means the repo was never read — fix that first
```

For the full reference — every config key, every subcommand flag, and the
reasoning behind the exit-code ladder — load
`skill_view("snap-bak", "references/reference.md")`.