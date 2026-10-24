#!/usr/bin/env python3
"""Backup engine: errors, /proc helpers, global lock, status, and the Runner.

Imports config, excludes, restic and snapshot; must not import cli.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TextIO

from snap_bak import config, excludes, restic, snapshot

logger = logging.getLogger(__name__)

# Failure categories: cheap watchdog triage, assigned explicitly at the call
# site (which operation failed), never inferred from log text.
FAIL_CREDENTIALS = "credentials"
FAIL_PERMISSION = "permission"
FAIL_MOUNT = "mount"
FAIL_REMOTE = "remote_unavailable"
FAIL_LOCKED = "locked"
FAIL_SNAPSHOT = "snapshot"
FAIL_INTERNAL = "internal"


class BackupError(Exception):
    category = FAIL_INTERNAL


class CredentialsError(BackupError):
    category = FAIL_CREDENTIALS


class RemoteUnavailableError(BackupError):
    category = FAIL_REMOTE


class RepoLockedError(BackupError):
    category = FAIL_LOCKED


class MountError(BackupError):
    category = FAIL_MOUNT


class StuckInstanceError(Exception):
    """A previous snap-bak instance appears stuck."""


class RepoQueryError(Exception):
    """restic could not be asked a read-only question about a repository.

    Raised instead of returning an empty answer, because an empty answer is
    indistinguishable from "there is nothing here" — and a tool that reports
    "nothing found" for a repository it never reached is worse than one that
    reports failure. `returncode` is None for timeouts and unparseable output;
    when it is set, it is restic's own code, so callers can tell a missing repo
    (10) from a locked one (11) from a bad password (12) without parsing text.
    """

    def __init__(self, path, returncode: int | None, detail: str):
        self.path = path
        self.returncode = returncode
        self.detail = detail
        super().__init__(f"{path}: {detail}")

    @property
    def retryable(self) -> bool:
        """True when the repo may well answer on the next run.

        restic 10/11/12 are environmental — missing, locked, or unreadable — so
        the next eligible window is a reasonable thing to wait for. Anything
        else will not fix itself without a config change.
        """
        return self.returncode in (10, 11, 12)


def _stderr_excerpt(e: subprocess.CalledProcessError, limit: int = 200) -> str:
    """Restic's own error text, on one line.

    CalledProcessError.__str__ is just the argv, which is already in the debug
    log, so without this the only readable reason for a failure would be the
    exit code.
    """
    text = (e.stderr or b"").decode(errors="replace").strip() if isinstance(
        e.stderr, bytes
    ) else (e.stderr or "")
    text = " ".join(text.split())
    return text[:limit] or f"restic exited {e.returncode}"


def categorize_restic_error(e: subprocess.CalledProcessError) -> BackupError:
    """Map a restic exit code to a categorized BackupError."""
    msg = f"restic failed (rc={e.returncode}): {e.stderr or ''}".strip()
    cls = {11: RepoLockedError, 12: CredentialsError}.get(
        e.returncode, RemoteUnavailableError
    )
    return cls(msg)


# --- /proc helpers ---


def _proc_dir() -> Path:
    return Path("/proc")


def _mountinfo_text() -> str:
    return (Path("/proc") / "self" / "mountinfo").read_text()


def restic_running() -> bool:
    """True if any restic process is alive; never unmount under an in-flight backup."""
    proc = _proc_dir()
    if not proc.is_dir():
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        if (
            "restic" in cmdline
            and "snap_bak" not in cmdline
            and "snap-bak" not in cmdline
        ):
            return True
    return False


def mounted_source(mount_point: Path) -> Path | None:
    """Bind-mount source currently at `mount_point`, or None."""
    try:
        data = _mountinfo_text()
    except OSError:
        return None
    for line in data.splitlines():
        parts = line.split()
        if len(parts) < 7 or "-" not in parts:
            continue
        sep = parts.index("-")
        if sep < 5 or parts[4] != str(mount_point) or sep + 2 >= len(parts):
            continue
        return Path(parts[sep + 2])
    return None


# --- Global lock ---


def _write_lock_info(job_name=None, snapshot_name=None):
    """Write lock info in place (never tmp+rename — that would orphan the flock)."""
    info = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "current_job": job_name,
        "snapshot_name": snapshot_name,
        "backup_started_at": datetime.now(timezone.utc).isoformat()
        if job_name
        else None,
    }
    try:
        # ExitStack, not a bare `with open(...)`: the file may not exist yet and
        # the two modes (`r+`, then `w` as the fallback) cannot both be tried in
        # one `with` header. The handle is still closed on every path.
        with contextlib.ExitStack() as stack:
            try:
                fh = stack.enter_context(open(config.GLOBAL_LOCK_FILE, "r+"))
            except FileNotFoundError:
                fh = stack.enter_context(open(config.GLOBAL_LOCK_FILE, "w"))
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(info, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass  # best-effort heartbeat; lock integrity unaffected


def _read_lock_info():
    try:
        return json.loads(config.GLOBAL_LOCK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False


def _resolve_lock_contention(stuck_threshold, stuck_wait) -> bool:
    """Resolve lock contention per SPEC; returns True to retry acquiring."""
    try:
        age = time.time() - config.GLOBAL_LOCK_FILE.stat().st_mtime
    except OSError:
        age = 0
    info = _read_lock_info()
    pid = info.get("pid") if info else None
    job = info.get("current_job") if info else None

    if age <= stuck_threshold:
        detail = (
            f" (PID {pid}, job: {job}, lock age: {age:.0f}s)"
            if info
            else f" (lock age: {age:.0f}s)"
        )
        logger.info(f"Another snap-bak instance is running{detail}. Exiting.")
        sys.exit(config.EXIT_SYSTEM)

    if _pid_alive(pid):
        logger.error(
            f"Previous snap-bak instance appears stuck (PID {pid}, job: {job}, lock age: "
            f"{age:.0f}s > {stuck_threshold}s). Re-checking every {config.STUCK_POLL_INTERVAL}s "
            f"for up to {stuck_wait}s..."
        )
        deadline = time.time() + stuck_wait
        while time.time() < deadline:
            time.sleep(config.STUCK_POLL_INTERVAL)
            with open(config.GLOBAL_LOCK_FILE, "a+") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                logger.info("Stuck lock released; proceeding.")
                return True
        logger.error(
            f"STUCK WAIT EXCEEDED: lock not released within {stuck_wait}s. Giving up."
        )
        sys.exit(config.EXIT_STUCK_WAIT)

    logger.warning(
        f"Breaking stale lock (age: {age:.0f}s > {stuck_threshold}s, pid={pid or 'unknown'})."
    )
    try:
        config.GLOBAL_LOCK_FILE.unlink()
    except OSError:
        pass
    return True


class GlobalLock:
    """Exclusive, non-blocking flock with stuck-instance resolution."""

    def __init__(
        self,
        stuck_threshold=config.DEFAULT_STUCK_THRESHOLD,
        stuck_wait=config.DEFAULT_STUCK_WAIT,
    ):
        self._stuck_threshold = stuck_threshold
        self._stuck_wait = stuck_wait
        self._fh: TextIO | None = None

    def __enter__(self):
        while True:
            fh = open(config.GLOBAL_LOCK_FILE, "a+")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                if _resolve_lock_contention(self._stuck_threshold, self._stuck_wait):
                    continue
            else:
                break
        self._fh: TextIO | None = fh
        _write_lock_info()
        logger.info("Global lock acquired.")
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
        config.GLOBAL_LOCK_FILE.unlink(missing_ok=True)
        logger.info("Global lock released.")


def touch_global_lock(job_name=None, snapshot_name=None):
    if job_name or snapshot_name:
        _write_lock_info(job_name, snapshot_name)
    else:
        try:
            config.GLOBAL_LOCK_FILE.touch()
        except OSError:
            pass


# --- Status persistence ---


@dataclass
class DelayState:
    """The delayed group, persisted in the status file across runs."""

    delayed: bool = False
    reason: str | None = None
    protected_snapshot: str | None = None
    missed_slots: int = 0

    def reset(self) -> None:
        self.delayed = False
        self.reason = None
        self.protected_snapshot = None
        self.missed_slots = 0

    def to_dict(self) -> dict:
        return {
            "delayed": self.delayed,
            "delayed_reason": self.reason,
            "protected_snapshot": self.protected_snapshot,
            "missed_slots": self.missed_slots,
        }


def read_status(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def delay_from_status(data: dict | None) -> DelayState:
    delay = DelayState()
    if not data:
        return delay
    try:
        delay.missed_slots = int(data.get("missed_slots") or 0)
    except (TypeError, ValueError):
        delay.missed_slots = 0
    if data.get("status") == "delayed":
        delay.delayed = True
        delay.reason = data.get("delayed_reason")
        delay.protected_snapshot = data.get("protected_snapshot")
    return delay


# `write_status` rewrites the job's whole state file on every status transition,
# so the queued list has to survive those writes. It is the only key carried
# forward: the rest of the document reports the run that just happened, while
# `queued` records work that has not happened yet. Declared here as a literal
# rather than copying every unrecognised key forward, so a typo in a key name is
# dropped rather than preserved forever.
_CARRIED_FORWARD_KEYS = ("queued",)


def write_status(
    path: Path,
    *,
    job: str,
    status: str,
    message: str,
    delay: DelayState,
    **fields,
) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "job": job,
        "status": status,
        "timestamp": datetime.now(timezone.utc).strftime(snapshot.SNAPSHOT_FMT),
        "message": message,
        **delay.to_dict(),
    }
    for key, value in fields.items():
        if value:
            data[key] = (
                value.strftime(snapshot.SNAPSHOT_FMT)
                if key == "snapshot_time"
                else value
            )
    existing = read_status(path) or {}
    for key in _CARRIED_FORWARD_KEYS:
        if key not in data and existing.get(key) is not None:
            data[key] = existing[key]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    logger.info(f"STATUS: {job} - {status} - {message}")


class ResultStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DELAYED = "delayed"
    DRY_RUN = "dry_run"
    RETENTION_OK = "retention_ok"
    RETENTION_FAILED = "retention_failed"
    # There is deliberately no status for an apply-excludes result. Those are
    # `job.Result`, a different type, so an apply can never be mistaken for a
    # backup result nor reach exit_code().

    def __str__(self) -> str:
        # Without this, f-string interpolation renders "ResultStatus.SKIPPED"
        # rather than "skipped", which is what reaches the user's terminal and
        # the journal.
        return self.value


@dataclass
class Result:
    name: str
    status: ResultStatus
    reason: str | None = None
    timeout: bool = False


@dataclass
class Decision:
    action: str  # "backup" | "skip" | "delayed"
    reason: str | None
    snapshot: Path | None


def check_password_file(repo: config.Repo) -> None:
    """Exit EXIT_PERMISSION_ERROR if the restic password file leaks access."""
    if not repo.password_file:
        return
    try:
        st = os.stat(repo.password_file)
        mode = st.st_mode
        if (
            mode & 0o007
            or (mode & 0o070) not in (0o000, 0o040)
            or (mode & 0o040 and st.st_gid != os.getgid())
        ):
            logger.error(
                f"Password file {repo.password_file} has unsafe permissions "
                f"(mode {oct(mode & 0o777)}). Fix with: chown <admin>:snap-bak "
                f"{repo.password_file} && chmod 640 {repo.password_file}"
            )
            sys.exit(config.EXIT_PERMISSION_ERROR)
    except FileNotFoundError:
        logger.warning(f"Password file {repo.password_file} does not exist")
    except OSError as e:
        logger.warning(f"Cannot stat password file {repo.password_file}: {e}")


_CACHE_KEYWORDS = ["unable", "cannot", "could not", "can't", "error", "warning", "failed", "denied", "permission", "locate", "open", "missing"]


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) // 2


def half_window_remaining(rj) -> bool:
    """True when at least half the backup window is still ahead.

    The cheap test for "ample time remains", and all a rewrite needs: rewriting
    is metadata-scale. The expensive prune rides along with whatever the run
    already does after a successful backup.
    """
    window = rj.window
    if window is None:
        return True  # no window configured: never time-boxed
    # Naive on purpose: windows are configured as local wall-clock times
    # ("02:00"-"06:00"), and comparing them to an aware "now" would silently
    # shift the window by the machine's UTC offset.
    now = datetime.now()  # noqa: DTZ005
    minutes = now.hour * 60 + now.minute
    length = (window.end_min - window.start_min) % (24 * 60) or 24 * 60
    if window.start_min < window.end_min:
        elapsed = minutes - window.start_min
    else:  # overnight window
        elapsed = (
            minutes - window.start_min
            if minutes >= window.start_min
            else minutes + 24 * 60 - window.start_min
        )
    return length - elapsed >= length / 2


class Runner:
    """Executes one resolved Job: backups, retention, and (opt-in) applies."""

    def __init__(
        self,
        job: config.ResolvedJob,
        *,
        dry_run: bool = False,
        repo_state: dict | None = None,
    ):
        self.job = job.job
        self.repo = job.repo
        self.mount_point = job.mount_point
        self.state_file = job.state_file
        self.tag = job.tag
        self.window = job.window
        self.dry_run = dry_run
        self.repo_state = repo_state or {}
        self.last_status = read_status(self.state_file)
        self.delay = delay_from_status(self.last_status)
        self._last_restic_snapshot_id = None

    def effective_priority(self) -> int:
        return self.job.priority * (self.delay.missed_slots + 1)

    def run(self) -> Result:
        with config.job_log_context(self.job.name):
            try:
                return self._run_backup_phase()
            finally:
                logger.info(f"--- Finished Job: {self.job.name} ---")

    # --- command execution ---

    def run_command(self, cmd, check=True, timeout=None):
        """Single chokepoint for external commands; dry-run logs instead of running."""
        if self.dry_run:
            logger.info(f"DRY-RUN: would execute: {' '.join(cmd)}")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        logger.info(f"CMD: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=check, timeout=timeout
            )
            if result.stdout:
                logger.debug(f"STDOUT: {result.stdout}")
            if result.stderr:
                logger.debug(f"STDERR: {result.stderr}")
                # restic sometimes only warns on cache problems yet still succeeds;
                # surface those so a misconfigured cache dir is visible.
                if cmd and cmd[0] == "restic":
                    for line in result.stderr.splitlines():
                        low = line.lower()
                        if "cache" in low and any(k in low for k in _CACHE_KEYWORDS):
                            logger.warning(f"restic cache: {line.strip()}")
            return result
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Command failed (rc={e.returncode}): {e.cmd} | stderr: {e.stderr}"
            )
            raise

    def _restic_cmd(self, subcommand, repo=None):
        repo = repo or self.repo
        return restic.build(
            subcommand,
            repo_path=repo.path,
            password_file=repo.password_file,
            options=list(repo.restic_options) + list(self.job.restic_options),
            exclude_file=self.job.ignore_file or None,
            excludes=self.job.excludes,
            warn=lambda opt: logger.warning(
                f"restic_options contains reserved flag '{opt}' which snap-bak manages; ignoring it."
            ),
        )

    def _write_status(self, status, message, **fields):
        if not self.dry_run:
            write_status(
                self.state_file,
                job=self.job.name,
                status=status,
                message=message,
                delay=self.delay,
                **fields,
            )

    def _check_stale_tmp(self):
        tmp = self.state_file.with_suffix(".json.tmp")
        if not tmp.exists():
            return
        try:
            age = time.time() - tmp.stat().st_mtime
            logger.warning(
                f"Stale temp status file found for {self.job.name} (age: {age:.0f}s). "
                f"Previous run may have been interrupted."
            )
        except OSError:
            pass

    # --- snapshot creation / retention ---

    def create_snapshot(self):
        snap_name = snapshot.snapshot_name(datetime.now(timezone.utc))
        snap_path = Path(self.job.snapshot_dir) / snap_name
        for path, what in (
            (snap_path.parent, "Snapshot directory"),
            (Path(self.job.source_subvolume), "Source subvolume"),
        ):
            if not path.exists():
                if self.dry_run:
                    logger.warning(f"DRY-RUN: {what} does not exist: {path}")
                else:
                    logger.error(f"{what} does not exist: {path}")
                    sys.exit(config.EXIT_CONFIG_ERROR)
        cmd = [config.SUDO, config.SUDO_EXEC, "snapshot", self.job.name, snap_name]
        logger.info(f"Creating snapshot: {' '.join(cmd)}")
        try:
            self.run_command(cmd)
        except subprocess.CalledProcessError as e:
            msg = f"Snapshot creation failed: {e.stderr}"
            logger.error(msg)
            self._write_status(
                "failed", msg, failure_category=FAIL_SNAPSHOT, last_error=msg
            )
            sys.exit(config.EXIT_ERROR)
        logger.info(f"Snapshot created: {snap_path}")
        return snap_path

    def _snapshot_too_young(self, snapshot_path: Path) -> bool:
        snap_time = snapshot.parse_name(snapshot_path.name)
        return (
            snap_time is None
            or (datetime.now(timezone.utc) - snap_time).total_seconds()
            < config.DEFAULT_SNAPSHOT_AGE_MIN
        )

    def _wrapper_status(self) -> dict:
        """Query the root wrapper for pool/generation status; {} fails open."""
        cmd = [config.SUDO, config.SUDO_EXEC, "status", self.job.name]
        try:
            result = self.run_command(cmd, timeout=config.REPO_CHECK_TIMEOUT)
            data = json.loads(result.stdout)
            return data if isinstance(data, dict) else {}
        # The wrapper is optional; any failure disables one gate, never the run.
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Disabling power-saving gate: wrapper status unavailable ({e}); "
                f"proceeding without power-saving snapshot gate"
            )
            return {}

    def _should_create_snapshot(self) -> bool:
        """Power-saving gate: skip when a disk is spun down or the source is unchanged."""
        status = self._wrapper_status()
        candidate = snapshot.snapshot_name(datetime.now(timezone.utc))
        if self.job.skip_snapshot_if_idle and status.get("any_spun_down") is True:
            logger.info(
                f"Skipping '{candidate}': pool has a spun-down disk (power saving; not waking the drive)"
            )
            return False

        latest = snapshot.Snapshot.latest(self.job.snapshot_dir)
        due = (
            latest is None
            or latest.time is None
            or (datetime.now(timezone.utc) - latest.time).total_seconds()
            >= self.job.snapshot_interval
        )
        if self.dry_run or not due:
            return False

        last = status.get("last_snapshot_generation")
        if last is not None and status.get("changed") is False:
            since = latest.name if latest else f"generation {last}"
            logger.info(f"Skipping '{candidate}': source unchanged since '{since}'")
            return False
        return True

    def _cleanup_snapshots(self, protected_snapshot=None):
        if not Path(self.job.snapshot_dir).exists():
            return
        cfg = self.job.snapshot_retention
        if not cfg or cfg.get("fn") not in config.RETENTION_FN:
            return
        retentions = config.RETENTION_FN[cfg["fn"]](**cfg.get("config", {}))
        valid = snapshot.Snapshot.scan(self.job.snapshot_dir)
        if not valid:
            return
        keep = config.build_snapshot_keeplist(valid, retentions)
        if protected_snapshot:
            keep.add(Path(self.job.snapshot_dir) / protected_snapshot)
        to_delete = [s for s in valid if s.path not in keep]
        if self.dry_run:
            for s in to_delete:
                logger.info(f"DRY-RUN: would delete local snapshot: {s.path.name}")
            logger.info(
                f"DRY-RUN: {self.job.name} snapshot retention would delete "
                f"{len(to_delete)} of {len(valid)} snapshots (keep {len(keep)})"
            )
            return
        for s in to_delete:
            logger.info(f"Deleting old snapshot: {s.path.name}")
            self.run_command(
                [config.SUDO, config.SUDO_EXEC, "delete", self.job.name, s.path.name]
            )

    # --- restic retention (forget / prune) ---

    def _forget_only(self, repo=None):
        if not self.job.backup_retention:
            logger.error(
                "Skipping restic retention: backup_retention is empty — "
                "remote snapshots are kept forever."
            )
            return
        cmd = self._restic_cmd("forget", repo)
        cmd.extend(["--tag", self.tag])
        for key, val in self.job.backup_retention.items():
            cmd.extend([f"--keep-{key}", str(val)])
        self.run_command(cmd)

    def retention(self) -> Result | None:
        """Forget policy; reports (never raises) retention_failed."""
        if not self.job.backup_retention:
            return None
        try:
            self._forget_only(self.repo)
        # Retention is reported, not fatal: the backup that ran is intact.
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            logger.error(f"Retention failed: {msg}")
            self._write_status(
                "retention_failed",
                msg,
                restic_snapshot_id=self._last_restic_snapshot_id,
                failure_category=getattr(e, "category", FAIL_INTERNAL),
                last_error=msg,
            )
            return Result(self.job.name, ResultStatus.RETENTION_FAILED, reason=msg)
        return Result(self.job.name, ResultStatus.RETENTION_OK)

    def prune(self) -> Result:
        """A single `restic prune` (exclusive lock); reports, never raises.

        Prune repacks and re-uploads every pack holding unreferenced data, making
        it by far the most expensive restic operation snap-bak runs. The
        duration is logged because it is the number an operator needs to judge
        whether the work fit comfortably in the window.
        """
        started = time.monotonic()
        try:
            self.run_command(self._restic_cmd("prune"))
        except Exception as e:
            msg = str(e)
            logger.exception(f"Prune failed for {self.repo.path}: {msg}")
            self._write_status(
                "retention_failed",
                msg,
                failure_category=getattr(e, "category", FAIL_INTERNAL),
                last_error=msg,
            )
            return Result(self.job.name, ResultStatus.RETENTION_FAILED, reason=msg)
        elapsed = int(time.monotonic() - started)
        logger.info(f"Prune completed for {self.repo.path} in {elapsed}s")
        return Result(self.job.name, ResultStatus.RETENTION_OK)

    # --- read-only repo inspection (the API apply.py is built on) ---
    #
    # Public because apply.py is a collaborator: planning and executing an
    # apply-excludes legitimately needs to list snapshots and read one stored
    # snapshot's metadata. Both degrade to empty on a dead repo rather than
    # raising, so an inspection reports "nothing found" instead of crashing.

    def repo_snapshots(self) -> list[dict]:
        """This job's snapshots in its repo, as restic's JSON objects.

        Tagged, so a repo shared between jobs never mixes them.

        Raises `RepoQueryError` when restic could not answer, and returns `[]`
        only when it genuinely answered "none". The distinction is the whole
        point: a caller asked "is anything to clean up?" and both answers look
        identical in a list, so folding them together lets an unreachable or
        locked repo report "nothing found" with a success exit code. A `show`
        that cannot reach the repository must say so, not look like a clean bill
        of health.
        """
        cmd = self._restic_cmd("snapshots") + ["--json", "--tag", self.tag]
        try:
            out = self.run_command(cmd, timeout=config.REPO_CHECK_TIMEOUT)
        except subprocess.CalledProcessError as e:
            raise RepoQueryError(
                self.repo.path, e.returncode, _stderr_excerpt(e)
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RepoQueryError(self.repo.path, None, f"timed out: {e}") from e
        try:
            data = json.loads(out.stdout or "[]")
        except ValueError as e:
            raise RepoQueryError(self.repo.path, None, f"unparseable output: {e}") from e
        if not isinstance(data, list):
            raise RepoQueryError(self.repo.path, None, "expected a JSON list")
        return data

    def repo_listing(self, snapshot_id: str):
        """(path, size, is_dir) triples for one stored snapshot (metadata only)."""
        try:
            out = self.run_command(
                self._restic_cmd("ls") + ["--long", snapshot_id],
                timeout=config.REPO_CHECK_TIMEOUT,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Cannot list snapshot {snapshot_id[:8]}: {e}")
            return []
        return list(excludes.parse_ls_long(out.stdout or ""))

    def repo_ping(self) -> int | None:
        """`restic cat config`: None when the repo is readable, else its rc.

        Reports only what restic said; what each code means is the caller's
        business. A timeout propagates, so a caller can tell "unreachable" from
        "locked" without this method inventing an exit code.
        """
        try:
            self.run_command(
                self._restic_cmd("cat") + ["config"],
                timeout=config.REPO_CHECK_TIMEOUT,
            )
        except subprocess.CalledProcessError as e:
            return e.returncode
        return None

    # --- throughput learning ---

    def _load_throughput_history(self) -> dict:
        f = config.STATE_DIR / "throughput.json"
        try:
            if f.exists():
                return config.load_config_json(f.read_text()).get("repos", {})
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read throughput history: {e}")
        return {}

    def _seed_repo_throughput(self, repo) -> int:
        history = self._load_throughput_history().get(repo.name, [])
        return max(_median(history), 1) if history else repo.throughput_mbps

    def _record_throughput(self, repo_name, throughput_mbps):
        history = self._load_throughput_history()
        values = history.get(repo_name, [])
        values.append(throughput_mbps)
        history[repo_name] = values[-10:]
        f = config.STATE_DIR / "throughput.json"
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"repos": history}, indent=2))
            tmp.replace(f)
        except OSError as e:
            logger.warning(f"Could not write throughput history: {e}")

    def _update_repo_throughput(self, repo, total_size, backup_time):
        if self.dry_run or backup_time <= 0 or total_size <= 0:
            return
        throughput_mbps = round((total_size * 8) / (backup_time * 1_000_000))
        if throughput_mbps <= 0:
            return
        repo.throughput_mbps = throughput_mbps
        logger.info(f"Updated {repo.name} throughput to {throughput_mbps} Mbps")
        self._record_throughput(repo.name, throughput_mbps)

    # --- repo reachability / size (Phase 2, cached per run) ---

    def _check_repo_reachable(self, repo) -> bool:
        if self.dry_run:
            return True
        try:
            self.run_command(
                self._restic_cmd("cat", repo) + ["config"],
                timeout=config.REPO_CHECK_TIMEOUT,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Repo {repo.name} unreachable: {e}")
            return False

    def _measure_repo_size(self, repo) -> int:
        if self.dry_run:
            return 0
        try:
            result = self.run_command(
                self._restic_cmd("stats", repo) + ["--mode", "raw-data", "--json"],
                timeout=config.REPO_STATS_TIMEOUT,
            )
            summary = restic.last_json_line(result.stdout)
            return int(summary.get("total_size", 0)) if summary else 0
        # A size probe is advisory; failing it reports 0 rather than aborting.
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not measure repo {repo.name} size ({e}); assuming 0")
            return 0

    def _repo_state(self, repo) -> dict:
        state = self.repo_state.setdefault(
            repo.name, {"reachable": None, "size": None, "allocated": False}
        )
        if state["reachable"] is None:
            state["reachable"] = self._check_repo_reachable(repo)
            state["size"] = (
                self._measure_repo_size(repo) if state["reachable"] else None
            )
        return state

    def _bump_repo_size(self, repo, used_bytes):
        state = self.repo_state.get(repo.name)
        if state and state.get("size") is not None:
            state["size"] = state["size"] + used_bytes

    # --- planning (Phase 2) ---

    def _candidate_snapshot(self) -> Path | None:
        found = snapshot.Snapshot.oldest_within(
            self.job.snapshot_dir, timedelta(seconds=self.job.backup_interval)
        )
        if found is None:
            found = snapshot.Snapshot.latest(self.job.snapshot_dir)
        return found.path if found else None

    def _last_success_time(self):
        data = self.last_status
        if not data or data.get("status") != "success":
            return None
        try:
            ts = data["timestamp"].removesuffix("Z")
            if "T" in ts:
                last = datetime.fromisoformat(ts)
                return last if last.tzinfo else last.replace(tzinfo=timezone.utc)
            return datetime.strptime(ts, "%Y-%m-%d_%H:%M").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            return None

    def _time_for_backup(self) -> bool:
        last = self._last_success_time()
        return (
            last is None
            or (datetime.now(timezone.utc) - last).total_seconds()
            >= self.job.backup_interval
        )

    def within_backup_window(self) -> bool:
        # Naive local time, matching Window.parse's wall-clock semantics; an
        # aware "now" would shift the window by the UTC offset (same reason as
        # half_window_remaining above).
        return self.window is None or self.window.contains(datetime.now())  # noqa: DTZ005

    def _delay(self, reason):
        if not self.delay.delayed:
            cand = self._candidate_snapshot()
            self.delay.protected_snapshot = cand.name if cand else None
        self.delay.delayed = True
        self.delay.reason = reason
        self.delay.missed_slots += 1
        self._write_status(
            "delayed",
            f"{reason}: {self.delay.protected_snapshot or 'no snapshot'}",
            snapshot_name=self.delay.protected_snapshot,
        )

    def decide(self) -> Decision:
        repo = self.repo
        was_delayed = self.delay.delayed

        # Cheap local-time gate first: when no backup can happen anyway, skip
        # before the costly remote repo checks (restic cat config / restic stats).
        if not self.within_backup_window():
            return Decision("skip", "outside backup window", None)

        state = self._repo_state(repo)

        if state.get("reachable") is False:
            return Decision("delayed", "unreachable", None)

        if repo.size_limit > 0 and (state.get("size") or 0) >= repo.size_limit:
            if state.get("allocated"):
                self.delay.missed_slots += 1
                return Decision(
                    "skip", "repo over size_limit (higher-priority job running)", None
                )
            state["allocated"] = True
            return Decision("delayed", "size_limit", None)

        if self._time_for_backup():
            if was_delayed:
                self.delay.missed_slots += 1
            self.delay.delayed = False
            self.delay.reason = None
            self.delay.protected_snapshot = None
            return Decision("backup", None, None)

        if was_delayed and self.delay.protected_snapshot:
            return Decision(
                "backup",
                None,
                Path(self.job.snapshot_dir) / self.delay.protected_snapshot,
            )

        return Decision("skip", "not time for backup", None)

    # --- backup steps ---

    def _preflight(self, mount_point, repo=None) -> int:
        logger.debug("Running preflight")
        cmd = self._restic_cmd("backup", repo)
        cmd.extend([str(mount_point), "--tag", self.tag, "--json", "--dry-run"])
        try:
            result = self.run_command(cmd)
            summary = restic.last_json_line(result.stdout)
            if summary is None:
                logger.warning("Could not parse preflight output")
                return 0
            total_size = summary.get("total_size", 0)
            logger.info(
                f"PREFLIGHT: {summary.get('total_files', 0)} files, {total_size / 1e9:.2f} GB"
            )
            return total_size
        except subprocess.CalledProcessError as e:
            raise categorize_restic_error(e) from e
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse preflight output: {e}")
            return 0

    def _backup(self, mount_point, repo=None) -> str | None:
        cmd = self._restic_cmd("backup", repo)
        cmd.extend([str(mount_point), "--tag", self.tag, "--json"])
        try:
            result = self.run_command(cmd)
        except subprocess.CalledProcessError as e:
            raise categorize_restic_error(e) from e
        if result.stderr and result.stderr.strip():
            for line in result.stderr.strip().splitlines():
                logger.warning(f"restic: {line}")
        summary = restic.find_summary(result.stdout)
        return summary.get("snapshot_id") if summary else None

    @contextmanager
    def mount(self, snapshot_path: Path):
        """Mount read-only, reusing a prior mount of the same snapshot; safe across restarts."""
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not self.dry_run:
            self.state_file.touch()
            logger.debug("Status file touched")

        mount_point = self.mount_point
        mounted = mounted_source(mount_point)
        reuse = False
        try:
            mount_point.mkdir(parents=True, exist_ok=True)
            if mounted is not None:
                if mounted == snapshot_path:
                    reuse = True
                    logger.info(
                        f"{mount_point} already mounted at {snapshot_path.name}; "
                        f"reusing (a prior run's restic may still be reading it)."
                    )
                else:
                    if restic_running():
                        raise MountError(
                            f"{mount_point} is mounted at a different snapshot "
                            f"({mounted.name}) and a restic process is running; refusing to "
                            f"unmount (would disrupt an in-flight backup). Deferring this job."
                        )
                    logger.warning(
                        f"{mount_point} mounted at {mounted.name}; unmounting to mount "
                        f"{snapshot_path.name}."
                    )
                    self.run_command([config.SUDO, config.SUDO_EXEC, "umount", self.job.name])
            if not reuse:
                try:
                    self.run_command(
                        [
                            config.SUDO,
                            config.SUDO_EXEC,
                            "mount",
                            self.job.name,
                            snapshot_path.name,
                        ]
                    )
                except subprocess.CalledProcessError as e:
                    raise MountError(
                        f"Failed to mount {snapshot_path}: {e.stderr}"
                    ) from e
            yield mount_point
        finally:
            if not reuse and mount_point.exists() and os.path.ismount(mount_point):
                if restic_running():
                    logger.warning(
                        f"restic still running; leaving {mount_point} mounted to avoid disrupting it."
                    )
                else:
                    try:
                        self.run_command(
                            [config.SUDO, config.SUDO_EXEC, "umount", self.job.name]
                        )
                    except subprocess.CalledProcessError:
                        logger.warning(f"Failed to unmount {mount_point}")

    def _run_backup(
        self, mount_point, snapshot_path, snapshot_name, retry=False
    ) -> Result:
        total_size = self._preflight(mount_point)
        backup_start = time.time()
        repo = self.repo
        self._last_restic_snapshot_id = self._backup(mount_point, repo)
        backup_time = time.time() - backup_start

        if self.dry_run:
            logger.info(
                f"DRY-RUN: backup simulated for {self.job.name} (no changes made)"
            )
            return Result(self.job.name, ResultStatus.DRY_RUN)

        self._update_repo_throughput(repo, total_size, backup_time)
        self.delay.reset()
        self._bump_repo_size(repo, total_size)

        message = (
            f"Backup completed at {datetime.now(timezone.utc).isoformat()}"
            if not retry
            else "Backup completed (retry of failed snapshot)"
        )
        self._write_status(
            "success",
            message,
            snapshot_name=snapshot_name,
            snapshot_time=snapshot.parse_name(snapshot_path.name),
            restic_snapshot_id=self._last_restic_snapshot_id,
        )
        self._cleanup_snapshots(protected_snapshot=snapshot_name)
        return Result(self.job.name, ResultStatus.SUCCESS)

    def _backup_snapshot(self, snapshot_path: Path, retry=False) -> Result:
        snapshot_name = snapshot_path.name
        try:
            touch_global_lock(self.job.name, snapshot_name)
            with self.mount(snapshot_path) as mount_point:
                return self._run_backup(
                    mount_point, snapshot_path, snapshot_name, retry=retry
                )
        except Exception as e:
            msg = str(e)
            logger.exception(f"Backup failed: {msg}")
            self._write_status(
                "failed",
                msg,
                snapshot_name=snapshot_name,
                failure_category=getattr(e, "category", FAIL_INTERNAL),
                last_error=msg,
            )
            return Result(self.job.name, ResultStatus.FAILED, reason=msg)

    def _run_backup_phase(self) -> Result:
        self.repo.throughput_mbps = self._seed_repo_throughput(self.repo)
        self._check_stale_tmp()
        self._write_status("started", "Backup initiated")

        # Local snapshot retention runs on the snapshot cadence (not backup), so
        # 2h snapshots don't pile up between infrequent backups.
        self._cleanup_snapshots(protected_snapshot=self.delay.protected_snapshot)

        # Create a snapshot on schedule, independent of backup success.
        if self._should_create_snapshot():
            self.create_snapshot()

        # Retry a failed backup from a previous run (same snapshot).
        last_status = self.last_status
        if last_status and last_status.get("status") == "failed":
            failed_name = last_status.get("snapshot_name")
            if not failed_name:
                logger.warning("Previous backup failed but no snapshot name recorded")
                self._write_status(
                    "skipped", "Previous backup failed (no snapshot info)"
                )
                return Result(self.job.name, ResultStatus.SKIPPED)
            failed_path = Path(self.job.snapshot_dir) / failed_name
            if not failed_path.exists():
                logger.warning(f"Failed snapshot {failed_name} no longer exists")
                self._write_status(
                    "skipped", "Previous backup failed but snapshot is gone"
                )
                return Result(self.job.name, ResultStatus.SKIPPED)
            logger.info(f"Retrying failed backup for snapshot: {failed_name}")
            return self._backup_snapshot(failed_path, retry=True)

        decision = self.decide()

        if decision.action == "delayed":
            self._delay(decision.reason)
            return Result(self.job.name, ResultStatus.DELAYED, reason=decision.reason)

        if decision.action == "skip":
            self._write_status(
                "skipped", (decision.reason or "not time for backup").capitalize()
            )
            return Result(self.job.name, ResultStatus.SKIPPED, reason=decision.reason)

        snapshot_path = decision.snapshot or self._candidate_snapshot()
        if snapshot_path is None:
            self._write_status("skipped", "No snapshot found")
            return Result(self.job.name, ResultStatus.SKIPPED)

        # Never back up a snapshot younger than the safety floor; a graceful skip.
        if self._snapshot_too_young(snapshot_path):
            logger.info(
                f"Deferring '{snapshot_path.name}': snapshot younger than "
                f"{config.DEFAULT_SNAPSHOT_AGE_MIN}s; will retry on a later run."
            )
            self._write_status(
                "skipped",
                f"Snapshot {snapshot_path.name} younger than {config.DEFAULT_SNAPSHOT_AGE_MIN}s",
            )
            return Result(self.job.name, ResultStatus.SKIPPED)

        # If the window closed between planning and execution, the slot is missed.
        if not self.within_backup_window():
            self._write_status(
                "skipped", "Backup window closed before backup could run"
            )
            return Result(self.job.name, ResultStatus.SKIPPED, timeout=True)

        return self._backup_snapshot(snapshot_path)
