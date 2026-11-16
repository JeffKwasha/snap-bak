# Changelog

All notable changes to snap-bak are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [0.6.0] — 2026-11-16

### Added

- The install writes `/usr/local/bin/snap-bak`, bound to the venv interpreter
  and the production config. A box that ran only the setup script can run the
  documented commands; before this the docs used a command the install never
  created.
- The install is a standard virtualenv distribution. `/opt/snap-bak/venv` is
  created without pip, and the base interpreter's pip installs the project and
  its dependencies into it. PEP 668 never applies and the system Python is not
  modified.
- SPEC states the deployment contract: baseline, only-install, command,
  operator, version and upgrade requirements. README and the agent skill name
  the service user and run tool commands with `sudo -u snap-bak`.
- With `--backup`, the setup saves the install it replaces (the distribution and
  its metadata, the wrapper, the launcher, the build stamp and the unit files)
  under `rollback/`. Without the flag no copy is made. On the first run after
  the copy layout it also saves the old `snap_bak/` and `VERSION`, so the
  migration can be undone.
- The setup records the build timestamp in `/opt/snap-bak/venv/BUILD`, and the
  lock acquire and release lines report it.

### Changed

- `snap-bak --version` reports the installed distribution's metadata. The
  `VERSION` marker and the copied `snap_bak/` layout are removed; the unit, the
  wrapper and the launcher run the venv interpreter.
- The lock lines report `build <ISO>` from the install stamp rather than the
  newest package-file mtime. A stale install is visible even when the version
  was not bumped.
- The first install needs pip on the base interpreter and PyPI access for the
  build backend and the dependencies. ensurepip and `python3-venv` are not
  needed.

## [0.5.2] — 2026-11-15

### Added

- The global-lock acquire and release lines log the running version and the
  newest modification time among the installed package files, as an ISO 8601
  timestamp. That is the last program update, so a replaced or hand-edited
  install can be told apart even when the version was never bumped.

### Fixed

- A copied install reports its real version instead of `unknown`. The setup
  script writes `/opt/snap-bak/VERSION` from `pyproject.toml`, and the package
  reads it when no package metadata exists. The rollback copy includes the file,
  so restoring an older install also restores the version it reports.

## [0.5.1] — 2026-11-15

### Added

- `snap-bak-setup.sh` saves the install it is about to replace under
  `/opt/snap-bak/rollback/<UTC timestamp>-XXXXXX/`, with the package, the
  wrapper, the unit files and a `restore.sh` that puts them back. Config,
  state, cache, passwords and excludes are not copied: an upgrade never
  modifies them, so a rollback never has to restore them. This gives an
  upgrade of an existing install a one-command undo.

## [0.5.0] — 2026-10-25 — first public release

snap-bak orchestrates btrfs snapshots and restic backups: local snapshots for
fast recovery, restic for offsite 3-2-1 coverage, with retention and scheduling
handled in one place.

### Added

- **Scheduled orchestration.** An hourly systemd timer runs each job; snapshots
  and restic backups happen per-job only when their interval has elapsed.
  Backup windows, snapshot/backup retention (daily/monthly/yearly or
  exponential), a power-saving gate that skips snapshots while the machine is
  suspended, and a global lock with stuck-wait recovery.
- **`snap-bak show excludes`** — a per-rule × per-source grid: the live
  filesystem, every local btrfs snapshot, and what restic already stores.
  Text, markdown, CSV and JSON renderings; `-v` lists every excluded file;
  `--limit` bounds the listing.
- **`snap-bak apply excludes`** — one planner and one executor, the same code
  path as the dry-run. Removes files from existing restic snapshots, then
  prunes to reclaim space. `--now` acts immediately; `--later` queues the work
  for the next scheduled run.
- **Trial pattern sets** (`--excludes-file`, `-e`) for evaluating rules against
  the real config without editing it, and **what-if surveys** (`--reveal`,
  `--reveal-file`) that price and list what a candidate rule would newly
  exclude.
- **A root wrapper** (`sudo-exec.py`) with a single static sudoers rule for the
  privileged steps (mount, snapshot, unmount, delete, restic update), plus a
  permission gate for restic password files.
- **An agent skill** (`.opencode/skills/snap-bak/`) describing how to verify a
  deployment and work out an exclude change.

### Fixed

- The root wrapper reports every device in the btrfs pool hosting a job's
  source, not only the devices listed before the mounted one. On a multi-device
  pool the power-saving gate could miss a spun-down disk and start a snapshot
  that woke it.

### Behaviour worth knowing

- `show excludes --live` and `--snaps` **prune matched directories unread**,
  exactly as restic's own walker does. Their totals are therefore unknown and
  print `?`; `+?` marks a price that covers only part of what a rule matched.
  `--price` walks those directories for exact sizes.
- `--reveal` patterns are a survey lens: restic never sees them, and `apply`
  refuses them.
- CSV writes an empty cell for an unknown total rather than a false `0`; JSON
  carries a `rough` key only when there is a caveat.

### Requirements

- Python 3.14 or newer, `btrfs-progs`, and restic installed as a real file at
  `/usr/local/bin/restic` (the wrapper applies `cap_dac_read_search` there).
- Install from a clone with `snap-bak-setup.sh`. It installs both the package
  and the root wrapper, which is essential — every privileged operation goes
  through it, so a plain `pip install` is not a supported deployment.
- Optional: `rclone` for `rclone:REMOTE:DIR` repositories.

### Notes

- Licensed under the Apache License 2.0 (`LICENSE`). The exclusion engine ports
  restic's matcher and Go's `filepath.Match`; their BSD notices are reproduced
  in `NOTICE`.
- This is the first public release; there is no documented upgrade path from
  earlier private builds.
