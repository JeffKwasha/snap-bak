---
name: snap-bak-watch
description: Watch snap-bak for capability errors, backup failures and soft performance drift, and decide when an operator should be told. Use when asked to monitor backups, check whether a backup failed, interpret rc=3 or permission denied, assess high latency or low bandwidth, or run a periodic backup watch. Triggers - snap-bak watch, backup monitoring, capability error, permission denied, rc=3, slow backup, low bandwidth, high latency, delayed backup, notify backup.
version: 1.0.0
tags: [backups, restic, btrfs, monitoring, operations]
category: devops
---

# Watching snap-bak

Watch for hard failures and soft drift. Answer two questions. Did something
break that needs a human now. Is a signal drifting enough to be worth a message.

**Noise rule:** notify once per incident, and never on a single expected event.

## What to read

The journal records what ran. State files hold each job's last outcome. Their
layout, the log lines and the exit codes are in the project documents. Read
those sections instead of keeping a copy here.

- README, "Reading the logs": the journal query and how to read it.
- README, "State Files": where state lives and what each field means.
- README, "Error Codes": what 30, 31, 41 and 4 mean.
- SPEC.md, "Logging": R-CLI-35 to R-CLI-38, the `JOB plan` and `JOB end` lines,
  and the meaning of `snapshot=-`.
- SPEC.md, "Privileged Operations & Security": R-PRIV-16, the restic capability
  and mode.

Use `snap-bak --list-jobs` as the service user when a real job name is needed.
Do not guess one.

## Hard failures, notify now

- The restic binary has lost `cap_dac_read_search=ep` or its `root:snap-bak 0750`
  mode. Check the capability and mode described in R-PRIV-16. Recovery is the
  wrapper's `update-restic` operation.
- `Command failed (rc=3)` with a `lack permission to read` summary. The summary
  already groups paths by top-level directory. Report that, not the full
  stderr, which is at debug.
- `JOB end status=failed rc=41`. Permanent. Retrying unchanged will not help.
- The same job delayed on two consecutive runs for the same reason, or
  `missed_slots` at 3 or more. One delay is temporary.
- `RUN end exit=2`, `4`, `50` or `51`: config, password permissions, internal
  error or a stuck instance.
- `retention_failed`, or a queued entry whose `attempts` keeps rising.

## Soft drift, notify on a trend

Use a baseline of the last five successful runs. Require three consecutive
samples. One slow run is not a signal.

- Latency. The `RUN end` line carries `duration`, and the journal names prune
  duration. Notify when the median of the last three durations doubles the
  baseline, or when a prune exceeds half the job's backup window.
- Bandwidth. State holds learned per-repo throughput and config holds
  `throughput_mbps`. Notify when the median of the last three backups is under
  half the seeded rate, or under 1 Mbps, for three backups.
- Stalls. Notify when a job has no `status=success` in 24 hours with a reason
  other than an expected skip, or when `missed_slots` reaches 3.

Expected skips never notify on their own. They are listed in the pitfalls of
the snap-bak skill: outside the backup window, not time, unchanged source, the
12-hour age defer, and dry runs. `snapshot=-` on a skip is not a missing backup.
`?` in a report means a directory was pruned unread, which is normal.

## Notification policy

Hard failure. Notify immediately, once per incident. Deduplicate on job,
status, failure category and return code. Clear the dedup entry on the next
success for that job. Include the job, status, return code, grouped reason,
snapshot or `snapshot=-` with its meaning, and the UTC timestamp.

Soft drift. Notify once per day while the trend holds. Deduplicate on job and
signal. Include the baseline, the current value and the sample count.

Never notify for a successful `RUN end exit=0`, a report result, an all-zero
row or a single slow run.

Deliver the notification through whatever channel the operator already uses.
This skill does not assume an alerting tool and gives no command for one. The
requirement is that the message reaches the operator with the fields above.
