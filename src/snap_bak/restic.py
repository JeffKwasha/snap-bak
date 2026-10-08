#!/usr/bin/env python3
"""
Everything restic-specific: command construction and JSON output parsing.

This module STAYS dependency-free. It MUST NOT be writable by non-root users.
"""

from __future__ import annotations

import json
from typing import Callable, Sequence

# Default graceful-timeout options applied to EVERY restic subcommand.
#
# A stalled backend (rclone) must make *restic* fail cleanly, release its
# lock, and exit — instead of hanging for hours or being killed mid-operation
# (a SIGKILL'd backup/prune can corrupt the repo). restic's --stuck-request-timeout
# cancels a stuck repository request so restic winds down gracefully.
#
# We do NOT inject `rclone.args` by default. When `rclone.args` is unset, restic
# uses its own rclone-backend default (`serve restic --stdio --b2-hard-delete`),
# which is the happiest path. Overriding `rclone.args` REPLACES that default, so a
# value that omits `serve restic --stdio` breaks the backend: the repo path then
# lands in rclone's subcommand slot ("unknown command "<remote>:<path>" for
# "rclone""). rclone-side timeouts (--timeout/--contimeout) are therefore opt-in
# via config restic_options, and must keep the `serve restic --stdio` prefix, e.g.
#   -o rclone.args="serve restic --stdio --timeout 11m --contimeout 11m"
DEFAULT_STUCK_REQUEST_TIMEOUT = "10m"


def _default_global_options() -> list[str]:
    """Global restic options that make stalled backends fail gracefully.

    Only the restic-side --stuck-request-timeout is applied by default. rclone
    timeouts are opt-in (see module docstring).
    """
    return [
        "--stuck-request-timeout",
        DEFAULT_STUCK_REQUEST_TIMEOUT,
    ]


def _with_defaults(global_options: list[str]) -> list[str]:
    """Append the default --stuck-request-timeout unless already overridden.

    Lets config-supplied restic_options win: if the user already set
    --stuck-request-timeout, we don't add the default. We never inject
    `rclone.args` — leaving it unset lets restic use its own backend default.
    """
    out = list(global_options)
    has_stuck = any(
        o.split("=", 1)[0] == "--stuck-request-timeout"
        for o in out
        if o.startswith("-")
    )
    if not has_stuck:
        out.extend(_default_global_options())
    return out


# Global restic options: accepted by every subcommand (`restic --help`).
RESTIC_GLOBAL_FLAGS = frozenset(
    {
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
    }
)

# Flags snap-bak sets itself for bookkeeping; config restic_options may not
# override them (they would collide and break summary parsing/retention).
# `--forget` belongs here because it decides *which snapshots are removed*, not
# just how: a user must not be able to delete originals through a config value.
RESERVED_FLAGS = frozenset(
    {
        "-r",
        "--repo",
        "--password-file",
        "--json",
        "--dry-run",
        "--tag",
        "--keep-last",
        "--keep-hourly",
        "--keep-daily",
        "--keep-weekly",
        "--keep-monthly",
        "--keep-yearly",
        "--prune",
        "--exclude",
        "--exclude-file",
        "--forget",
    }
)


# Subcommands taking a set of paths, and so accepting the exclusion flags:
# `backup` skips excluded paths while reading, `rewrite` drops them from an
# existing snapshot. Every other subcommand (e.g. `forget`) rejects them with
# 'unknown flag'.
FILE_SET_SUBCOMMANDS = frozenset({"backup", "rewrite"})


def build(
    subcommand: str,
    *,
    repo_path: str,
    password_file: str = "",
    options: Sequence[str] = (),
    exclude_file: str | None = None,
    excludes: Sequence[str] = (),
    forget: bool = False,
    extra_args: Sequence[str] = (),
    warn: Callable[[str], None] | None = None,
) -> list[str]:
    """Build a restic command for `subcommand` from config-derived inputs.

    The base command (`-r`, global options, `--password-file`) is safe for every
    subcommand. Backup-only options are added for `backup` alone, and exclusion
    flags for the subcommands in FILE_SET_SUBCOMMANDS alone.

- `options`: config restic_options (repo + job, verbatim). Flags in
    RESTIC_GLOBAL_FLAGS apply to every subcommand; any other flag is backup-only.
    RESERVED_FLAGS are dropped, calling `warn(opt)`.
    - `exclude_file`/`excludes`: `--exclude-file`/`--exclude`, for `backup`
      and `rewrite` only.
    - `forget`: add `--forget` (`rewrite` only), so the originals of rewritten
      snapshots are removed in the same operation. It is snap-bak's decision,
      never taken from restic_options, and rejected on any other subcommand.
    - `extra_args`: positional arguments (snapshot IDs, subfolders), last.
    """
    cmd = ["restic", "-r", repo_path, subcommand]
    global_options: list[str] = []
    backup_options: list[str] = []
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
    # Defensive guard: a user-supplied rclone.args that omits `serve restic
    # --stdio` silently breaks the rclone backend (the repo path lands in
    # rclone's subcommand slot). Warn so the misconfiguration is obvious.
    for opt in global_options:
        if "rclone.args=" in opt and "serve restic --stdio" not in opt:
            if warn is not None:
                warn(
                    f"{opt}: rclone.args must include 'serve restic --stdio' "
                    f"or the rclone backend breaks"
                )
    cmd.extend(_with_defaults(global_options))
    if password_file:
        cmd.extend(["--password-file", password_file])
    if subcommand == "backup":
        cmd.extend(backup_options)
    if subcommand in FILE_SET_SUBCOMMANDS:
        if exclude_file:
            cmd.extend(["--exclude-file", exclude_file])
        for pattern in excludes:
            cmd.extend(["--exclude", pattern])
    # Validated before any output is assembled, so a caller mistake fails loudly
    # instead of yielding a command that silently means something else.
    if forget and subcommand != "rewrite":
        raise ValueError(f"--forget is only valid for rewrite, not {subcommand}")
    if forget:
        cmd.append("--forget")
    # Positional arguments last: snapshot IDs and subfolders.
    cmd.extend(extra_args)
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
