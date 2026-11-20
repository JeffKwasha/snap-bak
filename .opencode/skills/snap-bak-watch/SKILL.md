---
name: snap-bak-watch
description: Watch snap-bak for capability errors, backup failures and soft performance drift, and decide when an operator should be told. Use when asked to monitor backups, check whether a backup failed, interpret rc=3 or permission denied, assess high latency or low bandwidth, or run a periodic backup watch. Triggers - snap-bak watch, backup monitoring, capability error, permission denied, rc=3, slow backup, low bandwidth, high latency, delayed backup, notify backup.
version: 1.0.0
tags: [backups, restic, btrfs, monitoring, operations]
category: devops
---

# Watching snap-bak

snap-bak takes btrfs snapshots and uploads them to restic. This skill is for an
agent that watches it. It answers two questions. Did a hard failure happen that
needs a human now. Is a soft signal drifting enough to be worth a message.

**The rule that prevents noise:** notify once per incident, and never on a
single expected event. A skip inside the backup window, a 0-byte upload because
the source did not change, and one slow run are all normal. A trend or a hard
failure is the signal.

## Start here

Journal reads need root. State files need root. The service user runs the tool
itself, but the watcher only reads.

Use the `snap-bak` syslog identifier. The unit sets `SyslogIdentifier=snap-bak`,
so this excludes the sudo and PAM audit lines.

```bash
sudo journalctl -u snap-bak -t snap-bak --since '2 hours ago' -o cat --no-pager
```

On a host whose unit predates that setting, filter on the log prefix instead.

```bash
sudo journalctl -u snap-bak --since '2 hours ago' -o cat --no-pager | grep '^\['
```

Read state and throughput as data, not by parsing prose.

```bash
sudo jq -r '[.job,.status,.timestamp,.failure_category,.last_error,.missed_slots] | @tsv' \
  /opt/snap-bak/state/*.json
sudo jq -r '.repos | to_entries[] | [.key, (.value[-1] // 0)] | @tsv' \
  /opt/snap-bak/state/throughput.json
jq -r '.repos[] | [.name, .throughput_mbps] | @tsv' /opt/snap-bak/config.json
```

Never guess a job name. Use `sudo -u snap-bak snap-bak --list-jobs`.

## The log lines you will see

A normal run opens and closes with one line each.

```text
[INFO] [-] Starting snap-bak 0.6.1 (build 2026-11-20T18:14:39Z) run=5c2be7ad jobs=3
[INFO] [-] RUN end run=5c2be7ad exit=0 jobs=3 success=0 skipped=3 delayed=0 failed=0 retention_failed=0 duration=12s at=2026-11-20T18:15:11+00:00
```

Each job gets a plan and a verdict. The job name is in the `[job]` prefix and is
not repeated in the line.

```text
[INFO] [Eternity] JOB plan action=backup snapshot=2026-11-20_09:00Z source=/mnt/bak/Eternity
[INFO] [Eternity] JOB end status=success rc=0 reason='Backup completed at ...' snapshot=2026-11-20_09:00Z at=2026-11-20T10:04:12+00:00
```

`snapshot=-` means no snapshot is attached to the outcome. A skip or a delay
before selection has none. A backup, a failed retry and a defer name the
snapshot. Do not treat `snapshot=-` on a skip as a missing backup.

The other plan actions are `retry`, `skip`, `delay` and `defer`. `reason` says
why, for example `outside backup window` or `younger than 43200s`.

Two resolution lines matter for the watcher.

```text
[INFO] [home] RETRY cleared snapshot=2026-11-20_09:00Z; failed status replaced
[INFO] [home] PROTECTED snapshot 2026-11-20_09:00Z released after successful backup
```

`RETRY cleared` means a previously failed snapshot was backed up on a later run.
`PROTECTED released` means a delayed job's held snapshot is no longer pinned,
so retention may now delete it. Both are recovery, not new failures.

## Hard errors, notify now

### Capability errors

The restic binary must carry the read capability. Check it directly.

```bash
getcap /usr/local/bin/restic
stat -c '%U:%G %a' /usr/local/bin/restic
```

Expected output is `cap_dac_read_search=ep` and `root:snap-bak 750`. Notify at
once if either is wrong. A restic process without the capability returns rc=3
and a `lack permission to read` summary on files owned by other users. Recovery
is `sudo /opt/snap-bak/sudo-exec.py update-restic`, which re-applies the
capability. Do not ask the operator for a password in chat.

### Backup errors

These lines mean a human should look now.

```text
[ERROR] [home] Command failed (rc=3): lack permission to read home/claude (3 paths), home/jk (12 paths)
[INFO] [home] JOB end status=failed rc=41 reason='restic failed (rc=3): lack permission to read home/claude (3 paths), home/jk (12 paths)' snapshot=2026-11-20_09:00Z at=...
```

The grouped paths are the finding. Never expand them into a list of every file
in the notification. The full stderr is at debug and is not for the operator.

Also notify immediately on these state or run results.

| Signal | Meaning |
|---|---|
| `JOB end status=failed rc=41` | Permanent failure. Retrying unchanged will not help |
| `JOB end status=delayed rc=30` | Repo unreachable, locked or out of window. Temporary |
| `JOB end status=delayed rc=31` | Destination out of space |
| `RUN end exit=2` | Config invalid. Nothing ran |
| `RUN end exit=4` | Password file permissions |
| `RUN end exit=50` or `51` | Internal error or stuck previous instance |
| `status=retention_failed` | Prune or forget failed. Data is safe but retention slipped |
| `queued` entry with `attempts` rising | A queued rewrite keeps failing |

A single delayed run is temporary. Notify on delay only when the same job is
delayed for the same reason on two consecutive runs, or when `missed_slots` is
3 or more in the state file.

## Soft errors, notify on a trend

A soft signal is not a failure. It is a direction. Use a baseline of the last
five successful runs and require three consecutive samples before notifying.

### High latency

Sources, cheapest first.

- `RUN end duration=<s>` is the whole run.
- `Prune completed for <repo> in <s>s` is the expensive restic step.
- `PREFLIGHT: <n> files, <x> GB` and `BACKUP: ... MB added` bound the upload.

Notify when the median of the last three durations is more than twice the
five-run baseline, or when one prune takes longer than half the job's backup
window. Check the window in the config before claiming this.

### Low bandwidth

Per-repo throughput is learned in `throughput.json`, seeded by `throughput_mbps`
in the config. Notify when the median of the last three backups is below half
the seeded rate, or below 1 Mbps, for three consecutive backups. Include the
seeded value, the current median and the number of samples.

A slow run over rclone to filen is common. One slow run is not a signal. A
sustained drop with unchanged source data is.

### Repeated skips and missed slots

Notify when a job has no `status=success` in the last 24 hours and the reason is
not an expected window or idle skip. Notify when `missed_slots` reaches 3.
Include the reason from `JOB end` and the timestamp of the last success.

Expected skips, which must never trigger a notification on their own.

- `outside backup window`
- `not time for backup`
- `source unchanged` and the resulting 0-byte upload
- `younger than 43200s`, which defers a fresh snapshot
- a dry run

## Notification policy

Hard failure. Notify immediately, once per incident. Dedup on
`job + status + failure_category + return code`. Clear the dedup entry when a
later `JOB end status=success` arrives for that job. Include the job, status,
return code, grouped reason, snapshot or `snapshot=-` with its meaning, and the
UTC timestamp.

Soft signal. Notify once per day while the trend holds. Dedup on
`job + signal`. Include the baseline, the current value and the sample count.
Never notify from a single sample.

Do not notify for a successful `RUN end exit=0`, a `show excludes` result, an
all-zero report row, or a `?` in a report. `?` means the directory was pruned
unread, which is restic's own behaviour.

## Deep dives

For operation, exit-code reasoning, config keys and how to verify a backup is
actually working, load the `snap-bak` skill. Its full reference is
`skill_view("snap-bak", "references/reference.md")`. This skill is deliberately
short so it can run on every tick without carrying the whole operator manual.
