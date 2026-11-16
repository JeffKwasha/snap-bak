# INDEX

Requirement taxonomy for snap-bak. Every test and every function should cite one
ID from this file as the requirement it serves. The statement here is the
one-line contract; the named document owns the detail.

Conventions:

- IDs are `R-<AREA>-<NN>` and stable. Never renumber; retire an ID rather than
  reuse it.
- A cite goes on the definition line: `def f(...):  # [[R-LOCK-02]]`. A
  multi-line signature puts it on the `def name(` line, so name, ID and line
  number share one grep line.
- One ID per test and one per function. Pick the primary requirement. The
  statement here is enough for a reader to judge whether the test checks the
  requirement or merely the implementation.
- `rg -n '# \[\[R-'` lists every cite with its definition; a per-ID count shows
  what is cited, and the uncited definitions are the remaining work.
- Tests point at the docs. This file does not track which tests exist, and a
  missing test is not detected here.

## Deployment

| ID | Requirement | Detail |
|---|---|---|
| R-DEPLOY-01 | Every command README presents for an installed box, and every command in the agent skill, works after the baseline plus the official install and nothing else; the development option is outside the contract. | SPEC.md, Deployment Contract |
| R-DEPLOY-02 | The baseline is Linux with btrfs and systemd, Python 3.14 or newer with pip, and PyPI access for the first install. | SPEC.md, Deployment Contract |
| R-DEPLOY-03 | restic is `/usr/local/bin/restic` as a real root-owned file; rclone is required only for `rclone:` repositories. | SPEC.md, Deployment Contract |
| R-DEPLOY-04 | The only supported install is a clone plus `snap-bak-setup.sh`, optionally with `PYTHON_BIN`; no operator pip install, operator virtualenv, or PATH edit. | SPEC.md, Deployment Contract |
| R-DEPLOY-05 | The install writes `/usr/local/bin/snap-bak` bound to the venv interpreter and the installed distribution, so the caller's cwd cannot shadow it. | SPEC.md, Deployment Contract |
| R-DEPLOY-06 | Bare commands read `/opt/snap-bak/config.json` the same way the service does. | SPEC.md, Deployment Contract |
| R-DEPLOY-07 | Tool commands run as the service user with `sudo -u snap-bak`; journal and state reads need root; files passed to the tool must be readable by the service user. | SPEC.md, Deployment Contract |
| R-DEPLOY-08 | `snap-bak --version` reports the installed distribution's metadata; there is no separate version file. | SPEC.md, Deployment Contract |
| R-DEPLOY-09 | Re-running the setup is idempotent and keeps `config.json`. | SPEC.md, Deployment Contract |
| R-DEPLOY-10 | With `--backup` the setup saves the replaced install and a `restore.sh`; without it no copy is made. | SPEC.md, Deployment Contract |
| R-DEPLOY-11 | With `--backup`, the first run after the copy layout also saves `snap_bak/` and `VERSION` so the migration can be undone. | SPEC.md, Deployment Contract |

## Global lock

| ID | Requirement | Detail |
|---|---|---|
| R-LOCK-01 | One global advisory flock at `<state>/snap-bak.lock` (`LOCK_EX\|LOCK_NB`) records the holder PID and timestamps for stuck detection. | SPEC.md, Global Lock |
| R-LOCK-02 | Acquire and release log the running version and the install's build timestamp in ISO 8601, falling back to the newest package file for installs without a stamp. | SPEC.md, Global Lock |
| R-LOCK-03 | `stuck_threshold` is the lock age beyond which the holder is considered stuck (default 3600s). | SPEC.md, Global Lock |
| R-LOCK-04 | `stuck_wait` bounds the re-check time before giving up (default 300s, poll every 60s). | SPEC.md, Global Lock |
| R-LOCK-05 | A dead holder is broken and the run continues; a live holder is polled until the lock clears or `stuck_wait` elapses, then exit 51. | SPEC.md, Global Lock |
| R-LOCK-06 | There is no per-job lock; the global lock is the only exclusion. | SPEC.md, Global Lock |

## State and runtime directories

| ID | Requirement | Detail |
|---|---|---|
| R-STATE-01 | The install layout is a root-owned venv (with `BUILD`), wrapper and config; runtime state is owned by `snap-bak`; `excludes/` is root:snap-bak; passwords are admin:snap-bak; cache is service-owned; `.cache` is a symlink to it. | SPEC.md, State & Runtime Directories |
| R-STATE-02 | One `<job>.json` holds the job's last status and its queued work; an empty queue is the only record that nothing is outstanding. | SPEC.md, State & Runtime Directories |
| R-STATE-03 | `write_status` rewrites the document on every transition and carries `queued` forward as the only carried key. | SPEC.md, State & Runtime Directories |
| R-STATE-04 | State for a job removed from the config is deleted after `state_retention_days` (default 90); a configured job and queued work are never reaped. | SPEC.md, State & Runtime Directories |
| R-STATE-05 | With `--backup`, `rollback/` holds the install artifacts and `restore.sh`, never config, state, cache or passwords, and is never reaped. | SPEC.md, State & Runtime Directories |
| R-STATE-06 | All paths are relative to the install directory; rclone is used only for `rclone:` repos; no `.local` or `.config` directories. | SPEC.md, State & Runtime Directories |
