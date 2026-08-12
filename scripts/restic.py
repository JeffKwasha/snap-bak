#!/usr/bin/env python3
"""
Everything restic-specific: command construction and JSON output parsing.

This module is deliberately dependency-free: it imports only the standard
library and MUST NOT import scripts.snap_bak. That keeps it safe for the
root-owned snap-bak-io wrapper to reuse later (if restic execution moves
behind the wrapper per the SECURITY TODO, restic.py must be installed
root:root and un-writable by the snap-bak service user).

Flag policy (see `build`): restic has a small set of GLOBAL flags accepted by
every subcommand; every other flag is specific to one subcommand (e.g. the
backup-only --exclude* family, which `restic forget` rejects with 'unknown
flag'). RESTIC_GLOBAL_FLAGS below is derived from `restic --help`
(restic 0.19.1). When restic adds a new global flag it must be added here;
until then it is treated as backup-only, which degrades gracefully (it still
applies to `backup`).
"""
from __future__ import annotations

import json
from typing import Callable, Sequence

# Global restic options: accepted by every subcommand (`restic --help`).
RESTIC_GLOBAL_FLAGS = frozenset({
    "--cacert",
    "--cache-dir",
    "--cleanup-cache",
    "--compression",
    "--http-user-agent",
    "--insecure-no-password",
    "--insecure-tls",
    "--json",
    "--key-hint",
    "--limit-download",
    "--limit-upload",
    "--no-cache",
    "--no-extra-verify",
    "--no-lock",
    "--option",
    "-o",
    "--pack-size",
    "--password-command",
    "--password-file",
    "-p",
    "--quiet",
    "-q",
    "--repo",
    "-r",
    "--repository-file",
    "--retry-lock",
    "--stuck-request-timeout",
    "--tls-client-cert",
    "--verbose",
    "-v",
})

# Flags snap-bak sets itself for bookkeeping; config restic_options may not
# override them (they would collide and break summary parsing/retention).
RESERVED_FLAGS = frozenset({
    "-r", "--repo", "--password-file", "--json", "--dry-run", "--tag",
    "--keep-last", "--keep-hourly", "--keep-daily", "--keep-weekly",
    "--keep-monthly", "--keep-yearly", "--prune", "--exclude", "--exclude-file",
})


def build(
    subcommand: str,
    *,
    repo_path: str,
    password_file: str = "",
    options: Sequence[str] = (),
    exclude_file: str | None = None,
    excludes: Sequence[str] = (),
    warn: Callable[[str], None] | None = None,
) -> list[str]:
    """Build a restic command for `subcommand` from config-derived inputs.

    The base command (`-r`, global options, `--password-file`) is safe for
    every subcommand. Backup-specific options and exclude flags are appended
    only for the `backup` subcommand: other restic subcommands (e.g. `forget`)
    reject them with 'unknown flag'.

    - `options`: config restic_options (repo + job, verbatim). Flags in
      RESTIC_GLOBAL_FLAGS apply to every subcommand; any other flag is
      treated as backup-only. RESERVED_FLAGS are dropped, calling `warn(opt)`.
    - `exclude_file`/`excludes`: become `--exclude-file`/`--exclude`, backup only.
    """
    cmd = ["restic", "-r", repo_path, subcommand]
    global_options = []
    backup_options = []
    # Paired flags (e.g. `--option rclone.program=foo`) pass the value as a
    # separate token; a token without a leading '-' inherits the bucket of the
    # flag that precedes it (and is dropped with it if that flag was reserved).
    last_flag_reserved = False
    last_flag_global = False
    for opt in options:
        opt = str(opt)
        if not opt.startswith("-"):
            if not last_flag_reserved:
                (global_options if last_flag_global else backup_options).append(opt)
            continue
        flag = opt.split("=", 1)[0]
        if flag in RESERVED_FLAGS:
            if warn is not None:
                warn(opt)
            last_flag_reserved = True
            last_flag_global = False
            continue
        if flag in RESTIC_GLOBAL_FLAGS:
            global_options.append(opt)
            last_flag_reserved = False
            last_flag_global = True
        else:
            backup_options.append(opt)
            last_flag_reserved = False
            last_flag_global = False
    cmd.extend(global_options)
    if password_file:
        cmd.extend(["--password-file", password_file])
    if subcommand == "backup":
        cmd.extend(backup_options)
        if exclude_file:
            cmd.extend(["--exclude-file", exclude_file])
        for pattern in excludes:
            cmd.extend(["--exclude", pattern])
    return cmd


def last_json_line(stdout: str) -> dict | None:
    """Parse the last JSON dict in restic --json output (or None).

    Restic --json streams progress events then a final summary; the last
    valid JSON line carries the summary (total_size, snapshot_id, ...).
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def find_summary(stdout: str, message_type: str = "summary") -> dict | None:
    """Return the last restic JSON line whose message_type matches, else None."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("message_type") == message_type:
            return obj
    return None
