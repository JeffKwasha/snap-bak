# Restic wrapper design

## Purpose

Route every **mutating** restic invocation through the root-owned wrapper so its
repository, password file, source path, tag and options come from the root-owned
config rather than from the service process. Read-only repository queries stay
in the service process.

## Threat model

The service process is not trusted. A compromised `snap-bak` can point restic at
any path, and restic carries `cap_dac_read_search`, so it can read any file and
upload it to any repository it can reach.

This design removes the service process's ability to choose the arguments of a
mutating invocation. It does **not** remove the service group's direct execute
permission on the capable binary (`root:snap-bak 0750`, `R-PRIV-16`). A
compromised service process can still exec restic itself and bypass the wrapper.

What the design buys:

- Arguments for backup, forget, prune and rewrite are built from root-owned
  config, so a hostile process cannot redirect an honest run.
- Environment and process-group control live in one place.
- The sudoers rule and the binary's capability and mode are unchanged.

What it does not buy:

- It does not close the direct-exec path. Full closure would need root-only
  restic, which the independent review rejected for environment, blast-radius
  and timeout reasons.

## Boundary

Restic keeps `cap_dac_read_search=+ep` and is `root:snap-bak 0750`. The wrapper
builds the argv as root, then drops to the service user before exec. Restic never
runs as root. The sudoers rule stays
`snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/sudo-exec.py`.

The wrapper stays standalone. It imports `snap_bak.config`, `snap_bak.snapshot`
and the dependency-free `snap_bak.restic` builder. It does not import `job` or
`cli`.

## Ops

| Op | Arguments | Restic command |
|----|-----------|----------------|
| `restic-backup` | `<job>` | `backup <mount_point> --tag <tag> --json` |
| `restic-backup-dry-run` | `<job>` | same plus `--dry-run` |
| `restic-forget` | `<job>` | `forget --tag <tag> --keep-* ...` |
| `restic-prune` | `<job>` | `prune` |
| `restic-rewrite` | `<job> <id>...` | `rewrite <id>... --forget` plus excludes |

Each op takes the job name only, except `restic-rewrite`, which takes snapshot
IDs. Extra arguments are refused. The wrapper refuses an unknown op.

`restic-rewrite` validates each ID against the job's tagged snapshots with
`restic snapshots --json --tag <tag>` before running `--forget`, so a caller
cannot delete an untagged snapshot.

## Read-only queries that stay service-side

`cat config`, `stats`, `snapshots` and `ls` still run in the service process
under `R-PRIV-23`. They read repository metadata but not the mounted snapshot,
and their password file is already service-readable. Keeping them out of the
wrapper avoids a sudo round trip for every inspection and keeps
`show excludes` usable.

## Environment

The wrapper sets an explicit environment before exec:

- `HOME=<install>`
- `RCLONE_CONFIG=<install>/.config/rclone/rclone.conf`
- `RESTIC_CACHE_DIR=<install>/cache`
- `PATH=/usr/local/bin:/usr/bin:/bin`

`<install>` is the directory holding `config.json`, normally `/opt/snap-bak`.
This is what makes an rclone repo and the shared cache work when sudo-rs resets
the caller's environment.

## Process group and timeout

Each wrapped restic runs in its own session (`start_new_session=True`). On
timeout the wrapper sends `SIGTERM` to the process group, waits for a graceful
exit, then sends `SIGKILL` only if the group is still alive. The timeout is
`SNAPBAK_RESTIC_TIMEOUT` seconds, unset by default. Killing the wrapper alone is
not enough, because a root-owned grandchild would outlive it and could hold a
repository lock or a mount.

## What does not change

- Password files stay under `/opt/snap-bak/passwords/`, service-readable.
- The restic cache stays service-owned.
- The capability and `0750` mode stay as they are.
- The service-readable password file is why `show excludes --repo`, `apply` and
  its preflight stay service-side.

## Future work

Restic recommends a limited repository user for backup and a privileged one for
prune and forget. snap-bak uses one credential per repo today, so a compromised
service process can prune. Splitting the credentials is a repo-schema change and
is not part of 0.7.0.

Arbitrary code execution in the service process is out of scope for this design.
If it enters the threat model, the direct-exec path becomes real. The service
group can run the capable binary itself, and no wrapper hardening stops it.
Closing that path needs the binary removed from the service group's execute set,
which means root execution or ambient capabilities. Record this as a potential
future item, not a defect in the wrapper.

## Verification

Unit tests import the wrapper and assert each op's derived argv, the refusal of
extra arguments, the explicit environment, the rewrite ID validation and the
`killpg` path. Live lanes watch `ps`, run a stripped environment, force a
timeout and confirm no child survives. The root-only lanes run on a host with
the wrapper installed, because this development host has no passwordless sudo.
