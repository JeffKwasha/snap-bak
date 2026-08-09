#!/usr/bin/env python3
"""
Btrfs Snapshot -> Restic Backup Orchestrator.

Strictly data-driven: jobs are defined entirely in the config file.
Supports CLI overrides, env vars, and preflight checks.

Usage:
  snap_bak.py --help
  snap_bak.py --list-jobs
  snap_bak.py --job <name> --dry-run
  SNAPBAK_JOB=home snap_bak.py

Protection strategy:
  - Snapshots are young (minutes to hours old)
  - Offsite backups are always slightly older
  - After a successful backup, snapshots older than the
    backup time are deleted (data is now offsite)
  - Younger snapshots remain for quick local recovery
  - This provides 3-2-1 coverage:
    3 copies (RAID1 btrfs + offsite restic)
    2 media (RAID1 + offsite)
    1 offsite (restic)
"""
import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typing import ClassVar, NamedTuple

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Constants ---

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "config.json"
DEFAULT_MOUNT_BASE = Path("/bak")
DEFAULT_STUCK_THRESHOLD = 3600  # 1 hour
DEFAULT_TIMEOUT = 86400  # 24 hours
DEFAULT_BACKUP_INTERVAL = 3600  # 1 hour
DEFAULT_SNAPSHOT_INTERVAL = 3600  # 1 hour
DEFAULT_SNAPSHOT_INTERVAL_MIN = 900  # 15 minutes (minimum)
DEFAULT_SNAPSHOT_AGE_MIN = 3600  # 1 hour (minimum snapshot age for backup safety)
# Sudo invocation for privileged (btrfs/mount) operations. snap-bak calls a
# root-owned wrapper (installed by snap-bak-setup.sh) rather than running
# btrfs/mount/umount directly. The wrapper derives every path from the
# root-owned config and validates the job/snapshot name, so a single static
# sudoers rule (NOPASSWD:/opt/snap-bak/snap-bak-io *) works on both sudo-rs
# and sudo.ws. Default /usr/bin/sudo; overridable via SNAPBAK_SUDO for tests.
SUDO = os.environ.get("SNAPBAK_SUDO", "/usr/bin/sudo")
IO_WRAPPER = os.environ.get("SNAPBAK_IO_WRAPPER", "/opt/snap-bak/snap-bak-io")

RETRY_DELAY = 5  # seconds between retry attempts


def strip_json_comments(src: str) -> str:
    """Strip // and /* */ comments from JSON(C), preserving line/column positions.

    Comments are replaced with whitespace (newlines are kept) so that JSON
    error offsets still point at the original text. String literals are left
    untouched, so values like "rclone://host:*/dir" are never treated as
    comments. This mirrors the behaviour of the npm 'strip-json-comments'
    package (which is not available on PyPI).
    """
    out = list(src)
    i, n = 0, len(src)
    in_string = False
    while i < n:
        c = src[i]
        if in_string:
            out[i] = c
            if c == "\\" and i + 1 < n:
                out[i + 1] = src[i + 1]
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out[i] = c
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                out[i] = " " if src[i] != "\n" else "\n"
                i += 1
            if i + 1 < n:
                out[i] = out[i + 1] = " "
                i += 2
            continue
        out[i] = c
        i += 1
    return "".join(out)


def load_config_json(text: str) -> dict:
    """Parse a config string that may contain // and /* */ comments."""
    return json.loads(strip_json_comments(text))


# --- Error codes ---
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_MOUNT_ERROR = 3
EXIT_PERMISSION_ERROR = 4
EXIT_TEMP_UNREACHABLE = 30
EXIT_TEMP_SIZE_LIMIT = 31
EXIT_PERM_TIMEOUT = 40
EXIT_PERM_FAILED = 41
EXIT_SYSTEM = 50
EXIT_STUCK_WAIT = 51

# --- Failure categories (for cheap watchdog triage; snapbak does NOT
#     decide escalation — it only records the cause so the watchdog can).
#     Categories are assigned EXPLICITLY at the call site (which operation
#     failed), never inferred from log text. ---
FAIL_CREDENTIALS = "credentials"        # bad/expired password, auth rejected -> user action
FAIL_PERMISSION = "permission"          # sudo denied, EPERM -> user action
FAIL_MOUNT = "mount"                    # snapshot mount failed -> system problem
FAIL_REMOTE = "remote_unavailable"      # restic repo/network down, timeout -> transient
FAIL_SNAPSHOT = "snapshot"              # btrfs snapshot creation failed -> system problem
FAIL_INTERNAL = "internal"              # unknown


# --- Retention functions ---

class Snapshot(NamedTuple):
    path: Path
    time: datetime


def build_retention_exponential(n: int = 1) -> list[datetime]:
    """Generate tier boundaries at exponential intervals: 1h, 2h, 4h, 8h..."""
    now = datetime.now(timezone.utc)
    return [now - relativedelta(hours=2**i) for i in range(0, n)]


def build_retention_datetimes(
    hourly: int = 0, daily: int = 0, weekly: int = 0,
    monthly: int = 0, yearly: int = 0,
) -> list[datetime]:
    """Generate calendar-based retention boundaries.

    Each tier starts after the previous tiers end.
    E.g., hourly=2, daily=1 → 2 hourly slots + 1 daily slot starting 2h ago.
    """
    now = datetime.now(timezone.utc)
    hours = [now - relativedelta(hours=i) for i in range(0, hourly)]
    days = [now - relativedelta(hours=hourly, days=i) for i in range(0, daily)]
    weeks = [now - relativedelta(hours=hourly, days=daily, weeks=i) for i in range(0, weekly)]
    months = [now - relativedelta(hours=hourly, days=daily, weeks=weekly, months=i) for i in range(0, monthly)]
    years = [now - relativedelta(hours=hourly, days=daily, weeks=weekly, months=monthly, years=i) for i in range(0, yearly)]
    return list(set(hours + days + weeks + months + years))


def build_snapshot_keeplist(snapshots: list[Snapshot], retentions: list[datetime]) -> set[Path]:
    """Determine which snapshots to keep based on retention tier boundaries.

    For each retention boundary (oldest to newest), keeps the oldest
    snapshot that is still newer than that boundary.
    """
    if not snapshots or not retentions:
        return {s.path for s in snapshots}

    keeps = set()
    for boundary in sorted(retentions):
        candidates = [s for s in snapshots if s.time >= boundary]
        if candidates:
            oldest = min(candidates, key=lambda s: s.time)
            keeps.add(oldest.path)
    return keeps


RETENTION_FN = {
    "exponential": build_retention_exponential,
    "datetimes": build_retention_datetimes,
}


def _get_state_dir() -> Path:
    """Get XDG state directory for snap-bak."""
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "snap-bak"
    return Path.home() / ".local" / "state" / "snap-bak"


STATE_DIR = _get_state_dir()
GLOBAL_LOCK_FILE = STATE_DIR / "snap-bak.lock"

# Ensure state directory exists before logging setup
STATE_DIR.mkdir(parents=True, exist_ok=True)

# --- Logging ---
# stderr ONLY. For a systemd service, systemd captures stderr into the journal
# (journalctl) which is the canonical, always-available log. We deliberately do
# NOT write a separate log file by default: that would duplicate every line and
# is redundant. A file copy is OPT-IN via --log-file for ad-hoc/non-service use.
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_stream_handler = logging.StreamHandler(sys.stderr)
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.basicConfig(level=logging.INFO, handlers=[_stream_handler])
logger = logging.getLogger(__name__)


class LockError(Exception):
    """Raised when a job lock cannot be acquired."""


class StuckInstanceError(Exception):
    """Raised when a previous snap-bak instance appears stuck."""


class BackupError(Exception):
    """Base class for failures carrying a category (see FAIL_* constants)."""
    category = FAIL_INTERNAL


class CredentialsError(BackupError):
    """Authentication/credential problem; requires user action."""
    category = FAIL_CREDENTIALS


class RemoteUnavailableError(BackupError):
    """Remote repository/network unavailable; transient."""
    category = FAIL_REMOTE


class MountError(BackupError):
    """Snapshot could not be mounted."""
    category = FAIL_MOUNT


def _categorize_restic_error(e: subprocess.CalledProcessError) -> BackupError:
    """Map a restic subprocess failure to a categorized BackupError.

    Uses restic's documented exit codes (cmd/restic/main.go):
      3  invalid source data, 10 repository not found,
      11 repository locked, 12 wrong key/password.
    """
    msg = f"restic failed (rc={e.returncode}): {e.stderr or ''}".strip()
    if e.returncode == 12:
        return CredentialsError(msg)
    return RemoteUnavailableError(msg)


# --- Global lock ---

def _write_lock_info(job_name: str | None = None, snapshot_name: str | None = None):
    """Write current job info to the lock file as JSON."""
    info = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "current_job": job_name,
        "snapshot_name": snapshot_name,
        "backup_started_at": datetime.now(timezone.utc).isoformat() if job_name else None,
    }
    tmp = GLOBAL_LOCK_FILE.with_suffix(".lock.tmp")
    tmp.write_text(json.dumps(info, indent=2))
    tmp.replace(GLOBAL_LOCK_FILE)


def _read_lock_info() -> dict | None:
    """Read job info from the lock file."""
    try:
        return json.loads(GLOBAL_LOCK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def acquire_global_lock(stuck_threshold: int = DEFAULT_STUCK_THRESHOLD) -> object:
    """Acquire the global lock to prevent multiple snap-bak instances.

    Writes PID and job info to the lock file as JSON and touches it as a heartbeat.
    If another instance is running, checks the lock file mtime to
    detect stuck processes.
    """
    lock_fh = open(GLOBAL_LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        lock_fh.close()
        try:
            mtime = GLOBAL_LOCK_FILE.stat().st_mtime
            age = time.time() - mtime
            lock_info = _read_lock_info()
            if age > stuck_threshold:
                if lock_info:
                    logger.error(
                        f"Previous snap-bak instance appears stuck "
                        f"(PID {lock_info.get('pid')}, job: {lock_info.get('current_job')}, "
                        f"snapshot: {lock_info.get('snapshot_name')}, "
                        f"lock age: {age:.0f}s > {stuck_threshold}s). "
                        f"Another instance cannot start."
                    )
                else:
                    logger.error(
                        f"Previous snap-bak instance appears stuck "
                        f"(lock age: {age:.0f}s > {stuck_threshold}s). "
                        f"Another instance cannot start."
                    )
            else:
                if lock_info:
                    logger.info(
                        f"Another snap-bak instance is running "
                        f"(PID {lock_info.get('pid')}, job: {lock_info.get('current_job')}, "
                        f"lock age: {age:.0f}s). Exiting."
                    )
                else:
                    logger.info(
                        f"Another snap-bak instance is running "
                        f"(lock age: {age:.0f}s). Exiting."
                    )
        except OSError:
            logger.info("Another snap-bak instance is running. Exiting.")
        sys.exit(0)

    _write_lock_info()
    logger.info("Global lock acquired.")
    return lock_fh


def release_global_lock(lock_fh):
    """Release the global lock."""
    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()
    GLOBAL_LOCK_FILE.unlink(missing_ok=True)
    logger.info("Global lock released.")


def touch_global_lock(job_name: str | None = None, snapshot_name: str | None = None):
    """Update the global lock file mtime and job info to show we're alive."""
    if job_name or snapshot_name:
        _write_lock_info(job_name, snapshot_name)
    else:
        GLOBAL_LOCK_FILE.touch()


# --- Repo model ---

class Repo(BaseModel):
    """A restic repository with metadata."""
    model_config = ConfigDict(extra='ignore')

    name: str
    path: str
    password_file: str = ""
    size_limit: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=100, ge=0)
    throughput_mbps: int = Field(default=1, ge=0)
    restic_options: list = Field(default_factory=list)


# --- Job model ---

class Job(BaseModel):
    """A single backup job."""
    model_config = ConfigDict(extra='allow')

    # Config fields
    name: str
    snapshot_dir: str
    source_subvolume: str
    repo: str
    priority: int = Field(default=100, ge=1)
    snapshot_interval: int = Field(default=DEFAULT_SNAPSHOT_INTERVAL, ge=DEFAULT_SNAPSHOT_INTERVAL_MIN)
    backup_retention: dict = Field(default_factory=dict)
    snapshot_retention: dict = Field(default_factory=lambda: {"fn": "exponential", "config": {"n": 5}})
    backup_interval: int = Field(default=DEFAULT_BACKUP_INTERVAL, ge=1)
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list = Field(default_factory=list)
    timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1)
    tag: str | None = None
    restic_options: list = Field(default_factory=list)

    # Runtime fields (not from config)
    repos: list = Field(default_factory=list, exclude=True)
    mount_base: str = "/bak"
    dry_run: bool = False
    config_path: str | None = None

    @field_validator('snapshot_dir')
    @classmethod
    def snapshot_dir_must_exist(cls, v):
        if not Path(v).exists():
            raise ValueError(f'snapshot_dir does not exist: {v}')
        return v

    @property
    def repo_name(self) -> str:
        """Alias for repo field (the repo name this job uses)."""
        return self.repo

    @property
    def repo_obj(self) -> Repo:
        """Resolve repo name to the Repo object."""
        for r in self.repos:
            if r.name == self.repo:
                return r
        raise ValueError(f"Repo '{self.repo}' not found in configured repos")

    @property
    def repo_path(self) -> str:
        """Resolve repo name to the Repo path string."""
        return self.repo_obj.path

    @property
    def mount_point(self) -> Path:
        return Path(self.mount_base) / self.name

    @property
    def lock_file(self) -> Path:
        return STATE_DIR / f"{self.name}.lock"

    @property
    def status_file(self) -> Path:
        return STATE_DIR / f"{self.name}_status.json"

    # --- Snapshot helpers ---

    def _get_latest_snapshot(self) -> Path:
        """Find the latest Btrfs snapshot by sorting subvolume names.

        Only snapshots matching our naming format are considered.
        Non-matching snapshots are ignored.
        """
        subs = [p for p in Path(self.snapshot_dir).iterdir() if p.is_dir()]
        if not subs:
            raise ValueError(f"No snapshots found in {self.snapshot_dir}")
        # Filter to snapshots with valid naming format
        valid = [(p, self._parse_snapshot_time(p)) for p in subs]
        valid = [(p, t) for p, t in valid if t is not None]
        if not valid:
            raise ValueError(
                f"No snapshots with valid naming format in {self.snapshot_dir}. "
                f"Expected ISO 8601 (YYYY-MM-DDTHH:MM:SS) or YYYYMMDD_HHMMSS."
            )
        return sorted(valid, key=lambda x: x[1])[-1][0]

    def _parse_snapshot_time(self, snapshot: Path) -> datetime | None:
        """Parse timestamp from snapshot name.

        Only accepts YYYY-MM-DD_HH:MM format.
        Returns None if the name doesn't match.
        """
        try:
            return datetime.strptime(snapshot.name, "%Y-%m-%d_%H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _snapshot_format_name(self, dt: datetime) -> str:
        """Format a datetime as a snapshot name: YYYY-MM-DD_HH:MM."""
        return dt.strftime("%Y-%m-%d_%H:%M")

    def _find_oldest_snapshot_within(self, interval_seconds: int) -> Path | None:
        """Find the oldest snapshot newer than (now - interval)."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval_seconds)
        subs = [p for p in Path(self.snapshot_dir).iterdir() if p.is_dir()]
        valid = [(p, self._parse_snapshot_time(p)) for p in subs]
        valid = [(p, t) for p, t in valid if t is not None and t >= cutoff]
        if not valid:
            return None
        return sorted(valid, key=lambda x: x[1])[0][0]

    @property
    def latest_snapshot_time(self) -> datetime:
        """Timestamp of the latest snapshot."""
        latest = self._get_latest_snapshot()
        snap_time = self._parse_snapshot_time(latest)
        if snap_time is None:
            raise ValueError(f"Snapshot {latest.name} has invalid naming format")
        return snap_time

    # --- Status tracking ---

    def _get_last_backup_time(self) -> datetime | None:
        """Read last successful backup time from per-job status file."""
        if not self.status_file.exists():
            return None
        try:
            data = json.loads(self.status_file.read_text())
            if data.get("status") == "success":
                ts = data["timestamp"]
                if "T" in ts:
                    return datetime.fromisoformat(ts)
                return datetime.strptime(ts, "%Y-%m-%d_%H:%M").replace(tzinfo=timezone.utc)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
        return None

    def _get_last_status(self) -> dict | None:
        """Read the full last status dict from per-job status file."""
        if not self.status_file.exists():
            return None
        try:
            return json.loads(self.status_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def create_snapshot(self) -> Path:
        """Create a new btrfs snapshot of source_subvolume.

        Returns the path to the created snapshot.
        Exits with fatal error if source_subvolume is missing or creation fails.
        """
        snap_name = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M")
        snap_path = Path(self.snapshot_dir) / snap_name

        if not snap_path.parent.exists():
            logger.error(f"Snapshot directory does not exist: {snap_path.parent}")
            sys.exit(EXIT_CONFIG_ERROR)

        source = Path(self.source_subvolume)
        if not source.exists():
            logger.error(f"Source subvolume does not exist: {source}")
            sys.exit(EXIT_CONFIG_ERROR)

        cmd = [SUDO, IO_WRAPPER, "snapshot", self.name, snap_name]
        logger.info(f"Creating snapshot: {' '.join(cmd)}")
        try:
            self._run(cmd)
        except subprocess.CalledProcessError as e:
            msg = f"Snapshot creation failed: {e.stderr}"
            logger.error(msg)
            # Record the failure as a scheduled-task error rather than die
            # silently with status stuck at 'started'. snapbak failed to
            # perform a scheduled task -> it failed.
            self._write_status(
                "failed", msg, failure_category=FAIL_SNAPSHOT, last_error=msg
            )
            sys.exit(EXIT_ERROR)

        logger.info(f"Snapshot created: {snap_path}")
        return snap_path

    @property
    def snapshot_due(self) -> bool:
        """True when a snapshot should be created on this run.

        Fresh installs (no valid snapshot yet) and snapshots older than
        snapshot_interval both count as due. Never fires in dry-run mode.
        """
        if self.dry_run:
            return False
        try:
            last_time = self._parse_snapshot_time(self._get_latest_snapshot())
        except (ValueError, OSError):
            return True
        if last_time is None:
            return True
        return (datetime.now(timezone.utc) - last_time).total_seconds() >= self.snapshot_interval

    def _snapshot_too_young(self, snapshot_path: Path) -> bool:
        """True when the snapshot is younger than the minimum backup age."""
        snap_time = self._parse_snapshot_time(snapshot_path)
        if snap_time is None:
            return True
        age = (datetime.now(timezone.utc) - snap_time).total_seconds()
        return age < DEFAULT_SNAPSHOT_AGE_MIN

    def _write_status(self, status: str, message: str,
                      snapshot_name: str | None = None,
                      snapshot_time: datetime | None = None,
                      restic_snapshot_id: str | None = None,
                      failure_category: str | None = None,
                      last_error: str | None = None):
        """Atomically write status to per-job status file.

        Uses tmp file + rename for atomicity. If a .tmp file exists
        from a previous interrupted run, it indicates a stuck process.
        failure_category/last_error record the cause of a 'failed' status so
        consumers can judge severity even for categories they don't know.
        """
        if self.dry_run:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "job": self.name,
            "status": status,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M"),
            "message": message,
        }
        if snapshot_name:
            data["snapshot_name"] = snapshot_name
        if snapshot_time:
            data["snapshot_time"] = snapshot_time.strftime("%Y-%m-%d_%H:%M")
        if restic_snapshot_id:
            data["restic_snapshot_id"] = restic_snapshot_id
        if failure_category:
            data["failure_category"] = failure_category
        if last_error:
            data["last_error"] = last_error
        tmp = self.status_file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.status_file)
        logger.info(f"STATUS: {self.name} - {status} - {message}")

    def _check_stale_tmp(self):
        """Check for stale .tmp files from interrupted runs."""
        tmp = self.status_file.with_suffix('.json.tmp')
        if tmp.exists():
            try:
                mtime = tmp.stat().st_mtime
                age = time.time() - mtime
                logger.warning(
                    f"Stale temp status file found for {self.name} "
                    f"(age: {age:.0f}s). Previous run may have been interrupted."
                )
            except OSError:
                pass

    # --- Security ---

    def _check_password_file_permissions(self):
        """Warn if password file is readable/writable by OTHER (untrusted users).

        Group access is intentionally allowed: the file is owned by the
        service account (read) with the admin group granted write so the
        administrator can rotate credentials. Only 'other' (any user who is
        neither the owner nor in the group) access is a real leak.
        """
        repo = self.repo_obj
        if not repo.password_file:
            return
        try:
            stat = os.stat(repo.password_file)
            mode = stat.st_mode
            if mode & 0o007:
                logger.warning(
                    f"Password file {repo.password_file} is accessible by OTHER "
                    f"(mode {oct(mode & 0o777)}). Restrict with: chmod o-rwx {repo.password_file}"
                )
        except OSError as e:
            logger.warning(f"Cannot stat password file {repo.password_file}: {e}")

    # --- Decision ---

    def _time_since_last_success(self) -> float | None:
        """Return seconds since last successful backup, or None."""
        last_backup = self._get_last_backup_time()
        if last_backup is None:
            return None
        return (datetime.now(timezone.utc) - last_backup).total_seconds()

    def _is_within_backup_window(self) -> bool:
        """Check if current time is within the backup window.

        Supports overnight windows (e.g., start="22:00", end="06:00"
        means 22:00-23:59 and 00:00-06:00).

        If no window is configured, returns True (backups always allowed).
        """
        if not self.backup_window_start or not self.backup_window_end:
            return True

        try:
            start_h, start_m = map(int, self.backup_window_start.split(":"))
            end_h, end_m = map(int, self.backup_window_end.split(":"))
        except (ValueError, AttributeError):
            logger.warning(f"Job {self.name}: invalid backup window format, ignoring")
            return True

        now = datetime.now(timezone.utc)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes < end_minutes:
            # Same-day window (e.g., 09:00 to 17:00)
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight window (e.g., 22:00 to 06:00)
            return current_minutes >= start_minutes or current_minutes < end_minutes

    # --- Command execution ---

    def _run(self, cmd, check=True, timeout=None):
        """Run a shell command and log output."""
        logger.info(f"CMD: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=check, timeout=timeout
            )
            if result.stdout:
                logger.debug(f"STDOUT: {result.stdout}")
            if result.stderr:
                logger.debug(f"STDERR: {result.stderr}")
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed (rc={e.returncode}): {e.cmd} | stderr: {e.stderr}")
            raise

    # restic flags that snap-bak sets itself; config restic_options may not
    # override them (they would collide and break summary parsing/retention).
    _RESERVED_RESTIC_FLAGS = {
        "-r", "--repo", "--password-file", "--json", "--dry-run",
        "--tag", "--keep-last", "--keep-hourly", "--keep-daily",
        "--keep-weekly", "--keep-monthly", "--keep-yearly", "--prune",
        "--exclude", "--exclude-file",
    }

    def _filter_restic_options(self, options: list, source: str) -> list:
        """Drop reserved restic flags (with a warning) from config options.

        Reserved flags are ones snap-bak sets itself for bookkeeping (repo
        location, password, JSON output, tags, retention, excludes). Config
        authors may specify any other restic flag via restic_options.
        """
        kept = []
        for opt in options:
            flag = opt.split("=", 1)[0] if isinstance(opt, str) else opt
            if flag in self._RESERVED_RESTIC_FLAGS:
                logger.warning(
                    f"{source} restic_options contains reserved flag '{opt}' "
                    f"which snap-bak manages; ignoring it."
                )
                continue
            kept.append(opt)
        return kept

    def _build_restic_cmd(self, subcommand: str, repo: Repo | None = None) -> list[str]:
        """Build a restic command with common arguments."""
        if repo is None:
            repo = self.repo_obj
        cmd = ["restic", "-r", repo.path, subcommand]
        cmd.extend(self._filter_restic_options(repo.restic_options, f"repo '{repo.name}'"))
        cmd.extend(self._filter_restic_options(self.restic_options, f"job '{self.name}'"))
        if repo.password_file:
            cmd.extend(["--password-file", repo.password_file])
        if self.ignore_file:
            cmd.extend(["--exclude-file", self.ignore_file])
        for pattern in self.excludes:
            cmd.extend(["--exclude", pattern])
        return cmd

    @property
    def tag_arg(self) -> str:
        """Restic tag used for this job (config override or job name)."""
        return self.tag if self.tag else self.name

    # --- Backup steps ---

    def _preflight(self, mount_point: Path, repo: Repo | None = None) -> int:
        """Run a restic dry-run to estimate size and check remote connectivity."""
        logger.info(f"Job {self.name}: running preflight...")
        cmd = self._build_restic_cmd("backup", repo)
        cmd.extend([
            str(mount_point),
            "--tag", self.tag_arg,
            "--json",
            "--dry-run",
        ])
        try:
            result = self._run(cmd)
            lines = [l for l in result.stdout.strip().split('\n') if l.strip()]
            summary = json.loads(lines[-1])
            total_size = summary.get("total_size", 0)
            total_files = summary.get("total_files", 0)
            logger.info(f"PREFLIGHT: {total_files} files, {total_size / 1e9:.2f} GB")
            return total_size
        except subprocess.CalledProcessError as e:
            raise _categorize_restic_error(e) from e
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse preflight output: {e}")
            return 0

    def _backup(self, mount_point: Path, repo: Repo | None = None) -> str | None:
        """Run the actual restic backup.

        Trusts restic's contract: a non-zero exit means failure (raised by
        _run() and mapped to a category via restic's exit codes); a zero exit
        means success. We additionally capture restic's --json summary so the
        recorded "success" is backed by restic's reported snapshot id. Any
        restic stderr (warnings included) is surfaced at WARNING level so it
        can never be silently lost.
        """
        cmd = self._build_restic_cmd("backup", repo)
        cmd.extend([str(mount_point), "--tag", self.tag_arg, "--json"])
        try:
            result = self._run(cmd, timeout=self.timeout)
        except subprocess.CalledProcessError as e:
            raise _categorize_restic_error(e) from e
        # Surface restic's own stderr as a warning (it may note incomplete paths,
        # changed repository, etc.). We believe what restic reports.
        if result.stderr and result.stderr.strip():
            for line in result.stderr.strip().splitlines():
                logger.warning(f"restic: {line}")
        # Parse restic's own success report for the snapshot id. A zero exit with
        # no usable summary is still success per restic's contract.
        snapshot_id = None
        for line in reversed(result.stdout.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                summary = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(summary, dict) and summary.get("message_type") == "summary":
                snapshot_id = summary.get("snapshot_id")
                break
        return snapshot_id

    def _forget_and_prune(self, repo: Repo | None = None):
        """Apply retention policy via forget + prune."""
        if not self.backup_retention:
            return
        cmd = self._build_restic_cmd("forget", repo)
        cmd.extend(["--tag", self.tag_arg])
        for key, val in self.backup_retention.items():
            cmd.extend([f"--keep-{key}", str(val)])
        cmd.append("--prune")
        self._run(cmd, timeout=self.timeout)

    def _cleanup_snapshots(self, protected_snapshot: str | None = None):
        """Delete snapshots using configured retention strategy.

        Supports exponential and datetimes (calendar-based) retention.
        The protected snapshot (currently being backed up) is never deleted.
        """
        cfg = self.snapshot_retention
        if not cfg or cfg.get("fn") not in RETENTION_FN:
            return

        fn = RETENTION_FN[cfg["fn"]]
        retentions = fn(**cfg.get("config", {}))

        subs = [p for p in Path(self.snapshot_dir).iterdir() if p.is_dir()]
        valid = [Snapshot(p, t) for p, t in
                 ((p, self._parse_snapshot_time(p)) for p in subs)
                 if t is not None]

        if not valid:
            return

        keep_paths = build_snapshot_keeplist(valid, retentions)

        if protected_snapshot:
            keep_paths.add(Path(self.snapshot_dir) / protected_snapshot)

        for s in valid:
            if s.path not in keep_paths:
                logger.info(f"Deleting old snapshot: {s.path.name}")
                self._run([SUDO, IO_WRAPPER, "delete", self.name, s.path.name])

    # --- Throughput learning ---

    THROUGHPUT_HISTORY_SIZE: ClassVar[int] = 10

    @property
    def throughput_file(self) -> Path:
        return STATE_DIR / "throughput.json"

    def _load_throughput_history(self) -> dict:
        """Load observed throughput history from the state file.

        Format: {"repos": {"<repo_name>": [<mbps>, ...]}}.
        """
        try:
            if self.throughput_file.exists():
                data = load_config_json(self.throughput_file.read_text())
                return data.get("repos", {})
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read throughput history: {e}")
        return {}

    def _seed_repo_throughput(self, repo: Repo) -> int:
        """Seed repo.throughput_mbps with the median of observed history.

        The median is robust to a single bad run. Falls back to the config
        value when there is no history.
        """
        history = self._load_throughput_history().get(repo.name, [])
        if not history:
            return repo.throughput_mbps
        ordered = sorted(history)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) // 2
        return max(median, 1)

    def _record_throughput(self, repo_name: str, throughput_mbps: int):
        """Append an observation to the rolling per-repo history in state."""
        history = self._load_throughput_history()
        values = history.get(repo_name, [])
        values.append(throughput_mbps)
        del values[:-self.THROUGHPUT_HISTORY_SIZE]
        history[repo_name] = values
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.throughput_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"repos": history}, indent=2))
            tmp.replace(self.throughput_file)
        except OSError as e:
            logger.warning(f"Could not write throughput history: {e}")

    def _update_repo_throughput(self, repo: Repo, total_size: int, backup_time: float):
        """Update repo throughput based on observed performance."""
        if backup_time > 0 and total_size > 0:
            throughput_mbps = round((total_size * 8) / (backup_time * 1_000_000))
            if throughput_mbps > 0:
                repo.throughput_mbps = throughput_mbps
                logger.info(f"Updated {repo.name} throughput to {throughput_mbps} Mbps")
                self._record_throughput(repo.name, throughput_mbps)

    # --- Context manager ---

    @contextmanager
    def mount_and_lock(self, snapshot_path: Path):
        """Acquire per-job lock, mount snapshot, yield mount point, cleanup.

        The lock file is also used as a status indicator. When the lock is
        acquired, the status file is touched to record the start time.
        Other processes can check the lock file to detect running jobs
        and read the status file mtime to detect stuck processes.
        """
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        lock_fh = open(self.lock_file, 'w')
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            lock_fh.close()
            raise LockError(f"Job {self.name}: another instance is running")

        # Touch the status file to record when this job started
        self.status_file.touch()
        logger.info(f"Job {self.name}: lock acquired, status file touched")

        mount_point = self.mount_point
        try:
            mount_point.mkdir(parents=True, exist_ok=True)
            if os.path.ismount(mount_point):
                logger.warning(f"{mount_point} already mounted. Unmounting first.")
                self._run([SUDO, IO_WRAPPER, "umount", self.name])
            try:
                self._run([SUDO, IO_WRAPPER, "mount", self.name, snapshot_path.name])
            except subprocess.CalledProcessError as e:
                raise MountError(f"Failed to mount {snapshot_path}: {e.stderr}") from e
            yield mount_point
        finally:
            if mount_point.exists() and os.path.ismount(mount_point):
                try:
                    self._run([SUDO, IO_WRAPPER, "umount", self.name])
                except subprocess.CalledProcessError:
                    logger.warning(f"Failed to unmount {mount_point}")
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
            self.lock_file.unlink(missing_ok=True)

    # --- Main entry point ---

    def _run_backup(self, mount_point: Path, snapshot_path: Path,
                    snapshot_name: str, retry: bool = False) -> dict:
        """Shared backup body for the normal and retry flows."""
        total_size = self._preflight(mount_point)

        if self.dry_run:
            logger.info(f"DRY-RUN: Would run backup for {self.name}")
            self._write_status("dry_run", "Preflight passed (dry-run)")
            return {"job": self.name, "status": "dry_run"}

        backup_start = time.time()
        selected_repo = self.repo_obj
        restic_snapshot_id = self._backup(mount_point, selected_repo)
        self._forget_and_prune(selected_repo)
        backup_time = time.time() - backup_start

        self._update_repo_throughput(selected_repo, total_size, backup_time)

        snap_time = self._parse_snapshot_time(snapshot_path)
        message = (
            f"Backup completed at {datetime.now(timezone.utc).isoformat()}"
            if not retry else "Backup completed (retry of failed snapshot)"
        )
        self._write_status(
            "success", message, snapshot_name, snap_time,
            restic_snapshot_id=restic_snapshot_id,
        )
        self._cleanup_snapshots(protected_snapshot=snapshot_name)
        return {"job": self.name, "status": "success"}

    def _backup_snapshot(self, snapshot_path: Path, retry: bool = False) -> dict:
        """Mount a snapshot and run the backup flow with lock + failure handling."""
        snapshot_name = snapshot_path.name
        try:
            touch_global_lock(self.name, snapshot_name)
            with self.mount_and_lock(snapshot_path) as mount_point:
                return self._run_backup(mount_point, snapshot_path, snapshot_name, retry=retry)
        except LockError:
            self._write_status("skipped", "Another instance is running")
            return {"job": self.name, "status": "skipped"}
        except Exception as e:
            category = getattr(e, "category", FAIL_INTERNAL)
            msg = str(e)
            logger.error(f"Job {self.name} failed: {msg}", exc_info=True)
            self._write_status(
                "failed", msg, snapshot_name,
                failure_category=category, last_error=msg,
            )
            return {"job": self.name, "status": "failed", "error": msg}

    def run(self, config_overrides: dict | None = None) -> dict:
        """Execute the backup job. Returns a result dict."""
        if config_overrides:
            for key, val in config_overrides.items():
                if hasattr(self, key):
                    setattr(self, key, val)

        logger.info(f"--- Starting Job: {self.name} ---")
        self.repo_obj.throughput_mbps = self._seed_repo_throughput(self.repo_obj)
        self._check_stale_tmp()
        # Read the previous run's status BEFORE overwriting it with "started";
        # otherwise the failed-backup retry below could never trigger.
        last_status = self._get_last_status()
        self._write_status("started", "Backup initiated")

        # 0. Create a snapshot on schedule — independent of backup success, so
        #    fresh installs self-start and long backups don't stall cadence.
        if self.snapshot_due:
            self.create_snapshot()

        try:
            # 1. Check for failed backup from previous run
            if last_status and last_status.get("status") == "failed":
                failed_snapshot_name = last_status.get("snapshot_name")
                if failed_snapshot_name:
                    failed_snapshot_path = Path(self.snapshot_dir) / failed_snapshot_name
                    if failed_snapshot_path.exists():
                        logger.info(f"Retrying failed backup for snapshot: {failed_snapshot_name}")
                        return self._backup_snapshot(failed_snapshot_path, retry=True)
                    logger.warning(f"Failed snapshot {failed_snapshot_name} no longer exists")
                    self._write_status("skipped", "Previous backup failed but snapshot is gone")
                    return {"job": self.name, "status": "skipped"}
                logger.warning("Previous backup failed but no snapshot name recorded")
                self._write_status("skipped", "Previous backup failed (no snapshot info)")
                return {"job": self.name, "status": "skipped"}

            # 2. Check if it's time to backup
            time_since = self._time_since_last_success()
            if time_since is not None and time_since < self.backup_interval:
                logger.info(f"Job {self.name}: {time_since:.0f}s since last backup "
                            f"(interval: {self.backup_interval}s). Skipping backup, running cleanup.")
                self._cleanup_snapshots()
                self._write_status("skipped", "Not time for backup yet")
                return {"job": self.name, "status": "skipped"}

            # 3. Check if within backup window
            if not self._is_within_backup_window():
                logger.info(f"Job {self.name}: outside backup window "
                            f"({self.backup_window_start}-{self.backup_window_end}). Skipping backup.")
                self._cleanup_snapshots()
                self._write_status("skipped", "Outside backup window")
                return {"job": self.name, "status": "skipped"}

            # 4. Time to backup — find oldest snapshot within interval
            snapshot_path = self._find_oldest_snapshot_within(self.backup_interval)
            if snapshot_path is None:
                # Fall back to latest snapshot if none within interval
                try:
                    snapshot_path = self._get_latest_snapshot()
                except (ValueError, OSError) as e:
                    logger.warning(f"Job {self.name}: cannot find snapshot: {e}")
                    self._write_status("skipped", f"No snapshot found: {e}")
                    return {"job": self.name, "status": "skipped"}

            # Refuse to back up snapshots younger than the minimum age. This is
            # a graceful skip, not a failure: the snapshot will be picked up on
            # a later run (e.g. the fresh-install case where we just created it).
            if self._snapshot_too_young(snapshot_path):
                logger.info(f"Job {self.name}: snapshot {snapshot_path.name} is younger than "
                            f"{DEFAULT_SNAPSHOT_AGE_MIN}s; deferring to a later run.")
                self._write_status(
                    "skipped",
                    f"Snapshot {snapshot_path.name} younger than {DEFAULT_SNAPSHOT_AGE_MIN}s",
                )
                return {"job": self.name, "status": "skipped"}

            return self._backup_snapshot(snapshot_path)
        finally:
            logger.info(f"--- Finished Job: {self.name} ---")


# --- Config loading ---

class SnapBakConfig(BaseModel):
    """Top-level config model."""
    model_config = ConfigDict(extra='ignore')

    repos: list[Repo]
    jobs: list[Job]
    mount_base: str = "/bak"
    stuck_threshold: int = Field(default=DEFAULT_STUCK_THRESHOLD, ge=1)

    def __init__(self, **data):
        known_keys = {'repos', 'jobs', 'mount_base', 'stuck_threshold'}
        unknown = set(data.keys()) - known_keys
        if unknown:
            warnings.warn(f"Unknown config keys: {unknown}")
        super().__init__(**data)

    @model_validator(mode='after')
    def validate_repos_and_jobs(self):
        if not self.repos:
            raise ValueError("Config must contain at least one repo")
        repo_names = {r.name for r in self.repos}
        for job in self.jobs:
            if job.repo not in repo_names:
                raise ValueError(
                    f"Job '{job.name}' references unknown repo '{job.repo}'. "
                    f"Available repos: {repo_names}"
                )
        return self

    def resolve_jobs(self, config_path: str | None = None) -> list[Job]:
        """Create Job objects with repos and config_path attached."""
        for job in self.jobs:
            job.repos = self.repos
            job.mount_base = self.mount_base
            job.config_path = config_path
        return self.jobs


def load_config(config_path: Path) -> SnapBakConfig:
    """Load and validate JSON config file using pydantic."""
    if not config_path.exists():
        logger.error(f"Config not found: {config_path}")
        sys.exit(EXIT_CONFIG_ERROR)
    try:
        return SnapBakConfig(**load_config_json(config_path.read_text()))
    except Exception as e:
        logger.error(f"Config validation error: {e}")
        sys.exit(EXIT_CONFIG_ERROR)


# --- CLI ---

def main():
    try:
        _main_impl()
    except SystemExit:
        raise
    except Exception as e:
        # No failure in a backup orchestrator should be silent.
        logger.critical(f"FATAL: snap-bak crashed: {e}", exc_info=True)
        sys.exit(EXIT_ERROR)


def _main_impl():
    parser = argparse.ArgumentParser(
        description="Btrfs Snapshot -> Restic Backup Orchestrator",
        epilog="Config: %(default)s | Env: SNAPBAK_CONFIG_FILE, SNAPBAK_JOB",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-c", "--config",
                        default=os.environ.get("SNAPBAK_CONFIG_FILE", str(DEFAULT_CONFIG)),
                        help="Path to config JSON file.")
    parser.add_argument("-j", "--job",
                        default=os.environ.get("SNAPBAK_JOB"),
                        help="Run a specific job by name (default: all jobs).")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Simulate backup. Runs preflight but skips upload & deletion.")
    parser.add_argument("--log-file", default=None,
                        help="Also write logs to this file (default: stderr only).")
    parser.add_argument("-l", "--list-jobs", action="store_true",
                        help="List all configured jobs and exit.")
    args = parser.parse_args()

    # Optional file log: reconfigure root logger to ALSO write to args.log_file.
    # Default (no --log-file) is stderr-only, persisted to the journal by systemd.
    if args.log_file:
        _fh = logging.FileHandler(args.log_file)
        _fh.setFormatter(logging.Formatter(_LOG_FORMAT))
        logging.getLogger().addHandler(_fh)

    config = load_config(Path(args.config))
    jobs = config.resolve_jobs(str(args.config))

    if args.list_jobs:
        print("Configured jobs:")
        for j in jobs:
            print(f"  - {j.name}: {j.snapshot_dir} -> {j.repo} ({j.repo_obj.path})")
        sys.exit(0)

    if args.job:
        jobs = [j for j in jobs if j.name == args.job]
        if not jobs:
            logger.error(f"Job '{args.job}' not found in config.")
            sys.exit(EXIT_ERROR)

    # Check mount base exists before acquiring lock
    mount_base = Path(config.mount_base)
    if not mount_base.exists():
        logger.error(f"Mount base {mount_base} does not exist")
        sys.exit(EXIT_MOUNT_ERROR)

    # Check password file permissions before lock (just warnings)
    for job in jobs:
        job._check_password_file_permissions()

    # Acquire global lock to prevent multiple snap-bak instances
    lock_fh = acquire_global_lock(config.stuck_threshold)
    try:
        results = []
        for job in jobs:
            job.dry_run = args.dry_run
            result = job.run()
            results.append(result)
            # Update heartbeat to show we're still alive
            touch_global_lock()

        failed = [r for r in results if r.get("status") == "failed"]
        if failed:
            sys.exit(EXIT_ERROR)
    finally:
        release_global_lock(lock_fh)


if __name__ == "__main__":
    main()
