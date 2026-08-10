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
import json5
import logging
import os
import re
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
DEFAULT_SNAPSHOT_AGE_MIN = 12 * 3600  # 12 hours (minimum snapshot age for backup safety)
DEFAULT_STUCK_WAIT = 300  # total seconds to keep re-checking a stuck lock
STUCK_POLL_INTERVAL = 60  # seconds between stuck-lock re-checks
REPO_CHECK_TIMEOUT = 30   # restic cat config timeout (repo reachability)
REPO_STATS_TIMEOUT = 300  # restic stats timeout (repo size measurement)
# Sudo invocation for privileged (btrfs/mount) operations. snap-bak calls a
# root-owned wrapper (installed by snap-bak-setup.sh) rather than running
# btrfs/mount/umount directly. The wrapper derives every path from the
# root-owned config and validates the job/snapshot name, so a single static
# sudoers rule (NOPASSWD:/opt/snap-bak/snap-bak-io) works on both sudo-rs and
# sudo.ws: a bare command name matches any arguments (see sudoers(5)), and '*'
# is deliberately avoided because sudo-rs mishandles it. Default /usr/bin/sudo;
# overridable via SNAPBAK_SUDO for tests.
SUDO = os.environ.get("SNAPBAK_SUDO", "/usr/bin/sudo")
IO_WRAPPER = os.environ.get("SNAPBAK_IO_WRAPPER", "/opt/snap-bak/snap-bak-io")

RETRY_DELAY = 5  # seconds between retry attempts


def load_config_json(text: str) -> dict:
    """Parse a config string that may contain // and /* */ comments (JSONC).

    Uses json5, which natively accepts comments while still requiring quoted
    keys and standard JSON literals for values pydantic expects. A future
    switch to strict JSON would only mean removing this one call.
    """
    return json5.loads(text)


# --- Human-readable quantities ---
# Duration/size config fields accept a bare int (seconds/bytes) or a
# human-readable string such as '90m', '2h', '1w' or '200GB'. Parsing is
# lenient about whitespace and case; unknown units raise so config typos are
# caught loudly rather than silently misread.

_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

_SIZE_UNITS = {
    "b": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024**2, "mb": 1024**2, "mib": 1024**2,
    "g": 1024**3, "gb": 1024**3, "gib": 1024**3,
    "t": 1024**4, "tb": 1024**4, "tib": 1024**4,
    "p": 1024**5, "pb": 1024**5, "pib": 1024**5,
}


def _parse_quantity(value, units: dict, what: str) -> int:
    """Accept an int or a '<number><unit>' string; return the base-unit int."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{what} must be an integer or a human-readable value")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{what} must be an integer or a human-readable value")
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", value, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"{what}: cannot parse {value!r} "
            f"(expected int or e.g. '90m', '2h', '200GB')"
        )
    unit = m.group(2).lower()
    if unit == "":
        return int(float(m.group(1)))
    if unit not in units:
        raise ValueError(f"{what}: unknown unit {unit!r} in {value!r}")
    return int(float(m.group(1)) * units[unit])


def parse_duration(value) -> int:
    """Parse a duration: bare int seconds or e.g. '90m', '2h', '1w'."""
    return _parse_quantity(value, _DURATION_UNITS, "duration")


def parse_size(value) -> int:
    """Parse a size: bare int bytes or e.g. '200GB', '1.5TiB'."""
    return _parse_quantity(value, _SIZE_UNITS, "size")


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
    """Get the state directory for snap-bak.

    Everything lives under /opt/snap-bak, so the state directory is hardcoded
    there (no XDG/.local). Overridable via SNAPBAK_STATE_DIR for tests and
    ad-hoc runs.
    """
    return Path(os.environ.get("SNAPBAK_STATE_DIR", "/opt/snap-bak/state"))


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
    """Write current job info to the lock file as JSON, IN PLACE.

    The lock is held by flock() on the file's inode. Writing must never replace
    the inode (no tmp + rename): replacing the file would orphan the flock and
    let a second instance acquire a fresh lock on the new inode.
    """
    info = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "current_job": job_name,
        "snapshot_name": snapshot_name,
        "backup_started_at": datetime.now(timezone.utc).isoformat() if job_name else None,
    }
    try:
        try:
            fh = open(GLOBAL_LOCK_FILE, "r+")
        except FileNotFoundError:
            fh = open(GLOBAL_LOCK_FILE, "w")
        with fh:
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(info, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass  # best-effort heartbeat; lock integrity is unaffected


def _read_lock_info() -> dict | None:
    """Read job info from the lock file."""
    try:
        return json.loads(GLOBAL_LOCK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int | None) -> bool:
    """True if the process with the given pid still exists."""
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


def _resolve_lock_contention(stuck_threshold: int, stuck_wait: int) -> bool:
    """Resolve lock contention per SPEC. Returns True to retry acquiring."""
    try:
        age = time.time() - GLOBAL_LOCK_FILE.stat().st_mtime
    except OSError:
        age = 0
    lock_info = _read_lock_info()
    pid = lock_info.get("pid") if lock_info else None

    if age <= stuck_threshold:
        # Too fresh to be stale: another instance is genuinely running.
        if lock_info:
            logger.info(
                f"Another snap-bak instance is running "
                f"(PID {pid}, job: {lock_info.get('current_job')}, "
                f"lock age: {age:.0f}s). Exiting."
            )
        else:
            logger.info(f"Another snap-bak instance is running (lock age: {age:.0f}s). Exiting.")
        sys.exit(EXIT_SYSTEM)

    # Lock is older than stuck_threshold — check whether the holder is stuck.
    if _pid_alive(pid):
        logger.error(
            f"Previous snap-bak instance appears stuck "
            f"(PID {pid}, job: {lock_info.get('current_job') if lock_info else None}, "
            f"lock age: {age:.0f}s > {stuck_threshold}s). "
            f"Re-checking every {STUCK_POLL_INTERVAL}s for up to {stuck_wait}s..."
        )
        deadline = time.time() + stuck_wait
        while time.time() < deadline:
            time.sleep(STUCK_POLL_INTERVAL)
            probe = open(GLOBAL_LOCK_FILE, "a+")
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                probe.close()
                logger.info("Stuck lock released; proceeding.")
                return True
            except IOError:
                probe.close()
        logger.error(
            f"STUCK WAIT EXCEEDED: lock not released within {stuck_wait}s. Giving up."
        )
        sys.exit(EXIT_STUCK_WAIT)

    logger.warning(
        f"Breaking stale lock (age: {age:.0f}s > {stuck_threshold}s, "
        f"pid={pid or 'unknown'})."
    )
    try:
        GLOBAL_LOCK_FILE.unlink()
    except OSError:
        pass
    return True


def acquire_global_lock(stuck_threshold: int = DEFAULT_STUCK_THRESHOLD,
                        stuck_wait: int = DEFAULT_STUCK_WAIT) -> object:
    """Acquire the global lock to prevent multiple snap-bak instances.

    Opens the lock file WITHOUT truncation ('a+'), takes an exclusive
    non-blocking flock on the inode, and writes lock info in place (never
    replacing the file, which would orphan the flock). On contention:
      - lock fresh (< stuck_threshold)      -> EXIT_SYSTEM (50)
      - lock stale, holder alive            -> poll up to stuck_wait, then EXIT_STUCK_WAIT (51)
      - lock stale, holder gone             -> break the lock and continue
    """
    while True:
        lock_fh = open(GLOBAL_LOCK_FILE, "a+")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            lock_fh.close()
            if _resolve_lock_contention(stuck_threshold, stuck_wait):
                continue
        else:
            break

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
        try:
            GLOBAL_LOCK_FILE.touch()
        except OSError:
            pass


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

    @field_validator('size_limit', mode='before')
    @classmethod
    def _parse_size_limit(cls, v):
        return parse_size(v)


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

    @field_validator('snapshot_interval', 'backup_interval', 'timeout', mode='before')
    @classmethod
    def _parse_interval(cls, v):
        return parse_duration(v)

    # Runtime fields (not from config)
    repos: list = Field(default_factory=list, exclude=True)
    mount_base: str = "/bak"
    dry_run: bool = False
    config_path: str | None = None

    # Phase 2 planning / delayed-job state (not from config)
    delayed: bool = False
    delayed_reason: str | None = None
    protected_snapshot: str | None = None
    missed_slots: int = 0
    backup_planned: bool = False
    planned_snapshot: Path | None = None
    repo_state: dict | None = None
    skip_reason: str | None = None

    @field_validator('snapshot_dir')
    @classmethod
    def snapshot_dir_must_exist(cls, v):
        if not Path(v).exists():
            raise ValueError(f'snapshot_dir does not exist: {v}')
        return v

    @field_validator('name', 'tag')
    @classmethod
    def name_must_be_safe(cls, v):
        """name/tag must be filesystem-safe: they drive <state>/<name>_status.json
        and the restic tag."""
        if v is None:
            return v
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError(f"{v!r} must match [A-Za-z0-9_-]")
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
        """Stable read-only mount point for this job's snapshot.

        The mount point mirrors the real filesystem path: the final path
        component of source_subvolume (e.g. /mnt/data -> data, so
        <mount_base>/data). It is always derived this way and is NOT
        configurable. restic's change tracking keys on this path, so it must
        be stable across backups.
        """
        return Path(self.mount_base) / Path(self.source_subvolume).name

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
                f"Expected YYYY-MM-DD_HH:MM."
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
                    last = datetime.fromisoformat(ts)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    return last
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
        data: dict = {
            "job": self.name,
            "status": status,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M"),
            "message": message,
        }
        # Delayed group: carries the delayed-job state across runs (SPEC §Status File).
        data["delayed"] = bool(self.delayed)
        data["delayed_reason"] = self.delayed_reason
        data["protected_snapshot"] = self.protected_snapshot
        data["missed_slots"] = self.missed_slots
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
        """Exit EXIT_PERMISSION_ERROR if a password file is readable/writable
        by group or other.

        The password file is a restic credential: any access beyond the owning
        service user is a leak and must be fixed before backups proceed.
        """
        repo = self.repo_obj
        if not repo.password_file:
            return
        try:
            mode = os.stat(repo.password_file).st_mode
            if mode & 0o077:
                logger.error(
                    f"Password file {repo.password_file} is accessible by group/other "
                    f"(mode {oct(mode & 0o777)}). Fix with: chmod 600 {repo.password_file}"
                )
                sys.exit(EXIT_PERMISSION_ERROR)
        except FileNotFoundError:
            logger.warning(f"Password file {repo.password_file} does not exist")
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
        means 22:00-23:59 and 00:00-06:00). Times are compared in the
        server's local timezone.

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

        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes < end_minutes:
            # Same-day window (e.g., 09:00 to 17:00)
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight window (e.g., 22:00 to 06:00)
            return current_minutes >= start_minutes or current_minutes < end_minutes

    # --- Repo checks (Phase 2) ---

    def _probe_cmd(self, subcommand: str, repo: Repo) -> list[str]:
        """Minimal restic command for read-only repo probes (cat/stats).

        Only -r and --password-file are safe for every subcommand; repo/job
        restic_options (e.g. --compression=better) are backup-oriented and
        would make 'restic cat config' / 'restic stats' fail.
        """
        cmd = ["restic", "-r", repo.path, subcommand]
        if repo.password_file:
            cmd.extend(["--password-file", repo.password_file])
        return cmd

    def _check_repo_reachable(self, repo: Repo) -> bool:
        """Probe repo reachability with 'restic cat config' (30s timeout)."""
        try:
            self._run(self._probe_cmd("cat", repo) + ["config"], timeout=REPO_CHECK_TIMEOUT)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Repo {repo.name} unreachable: {e}")
            return False

    def _measure_repo_size(self, repo: Repo) -> int:
        """Measure raw repo size in bytes ('restic stats --mode raw-data')."""
        try:
            result = self._run(
                self._probe_cmd("stats", repo) + ["--mode", "raw-data", "--json"],
                timeout=REPO_STATS_TIMEOUT,
            )
            line = result.stdout.strip().splitlines()[-1]
            return int(json.loads(line).get("total_size", 0))
        except Exception as e:
            logger.warning(f"Could not measure repo {repo.name} size ({e}); assuming 0")
            return 0

    def _repo_state(self, repo: Repo) -> dict:
        """Per-run repo check cache: reachability + size, computed once per repo."""
        if self.repo_state is None:
            self.repo_state = {}
        state = self.repo_state.setdefault(
            repo.name, {"reachable": None, "size": None, "allocated": False}
        )
        if state["reachable"] is None:
            state["reachable"] = self._check_repo_reachable(repo)
            state["size"] = self._measure_repo_size(repo) if state["reachable"] else None
        return state

    def _bump_repo_size(self, repo: Repo, used_bytes: int):
        """Reflect freshly-uploaded bytes in the cached repo size estimate.

        SPEC Phase 3 step 6: "subtract actual backup size used from
        repo_available_space". We cache used size (available = size_limit - size),
        so uploads grow the cached size by the bytes written.
        """
        if self.repo_state is None:
            return
        state = self.repo_state.get(repo.name)
        if state and state.get("size") is not None:
            state["size"] = state["size"] + used_bytes

    # --- Phase 2 planning / delayed-job handling ---

    def _candidate_snapshot(self) -> Path | None:
        """The snapshot a normal backup would pick (oldest within interval, else latest)."""
        snap = self._find_oldest_snapshot_within(self.backup_interval)
        if snap is None:
            try:
                snap = self._get_latest_snapshot()
            except (ValueError, OSError):
                snap = None
        return snap

    def _select_snapshot_for_backup(self) -> Path | None:
        return self._candidate_snapshot()

    def _time_for_backup(self) -> bool:
        """True when backup_interval has elapsed since the last success."""
        t = self._time_since_last_success()
        if t is None:
            return True
        return t >= self.backup_interval

    def _delay(self, reason: str):
        """Mark this job as delayed (SPEC §Delayed Job Handling)."""
        if not self.delayed:
            cand = self._candidate_snapshot()
            self.protected_snapshot = cand.name if cand else None
        self.delayed = True
        self.delayed_reason = reason
        self.missed_slots += 1
        self.backup_planned = False
        self._write_status(
            "delayed",
            f"{reason}: {self.protected_snapshot or 'no snapshot'}",
            snapshot_name=self.protected_snapshot,
        )

    def _load_delayed_state(self):
        """Load persisted delayed/missed_slots state from the status file."""
        data = self._get_last_status()
        if not data:
            return
        try:
            self.missed_slots = int(data.get("missed_slots") or 0)
        except (TypeError, ValueError):
            self.missed_slots = 0
        if data.get("status") == "delayed":
            self.delayed = True
            self.delayed_reason = data.get("delayed_reason")
            self.protected_snapshot = data.get("protected_snapshot")

    def _plan(self) -> str:
        """Phase 2 planning. Returns 'backup' or 'skip'.

        Sets backup_planned/planned_snapshot and the delayed group. Repo
        reachability and size checks are fresh per run, cached in repo_state.
        """
        repo = self.repo_obj
        state = self._repo_state(repo)
        was_delayed = self.delayed

        # 1. Repo reachability (once per repo per run).
        if state.get("reachable") is False:
            self._delay("unreachable")
            return "skip"

        # 2. Repo size limit. When a repo is over its limit, the highest-
        #    effective-priority job using it is the one marked delayed (it
        #    carries the protected snapshot / retry duty); the remaining jobs
        #    are skipped (not delayed) and accumulate missed_slots.
        if repo.size_limit > 0 and (state.get("size") or 0) >= repo.size_limit:
            if state.get("allocated"):
                self.missed_slots += 1
                self.backup_planned = False
                self.skip_reason = "repo over size_limit (higher-priority job running)"
                return "skip"
            state["allocated"] = True
            self._delay("size_limit")
            return "skip"

        # 3. Backup window + time-for-backup planning.
        if not self._is_within_backup_window():
            self.backup_planned = False
            self.skip_reason = "outside backup window"
            return "skip"

        if self._time_for_backup():
            if was_delayed:
                self.missed_slots += 1
            self.delayed = False
            self.delayed_reason = None
            self.protected_snapshot = None
            self.backup_planned = True
            self.planned_snapshot = None
            return "backup"

        if was_delayed and self.protected_snapshot:
            # Not time yet — retry the exact protected snapshot.
            self.backup_planned = True
            self.planned_snapshot = Path(self.snapshot_dir) / self.protected_snapshot
            return "backup"

        self.backup_planned = False
        self.skip_reason = "not time for backup"
        return "skip"

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
            logger.warning(
                f"Job {self.name}: backup_retention is empty — no restic "
                f"forget/prune will run; remote snapshots are kept forever."
            )
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
        Never deletes in dry-run mode.
        """
        if self.dry_run:
            logger.info(f"Job {self.name}: dry-run — skipping snapshot cleanup")
            return
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
    def mount(self, snapshot_path: Path):
        """Mount the snapshot read-only, yield mount point, unmount on exit.

        The global lock (acquired once per run in _main_impl) already prevents
        concurrent snap-bak instances, so no per-job lock is needed here. The
        status file is touched to record when the job started and expose its
        mtime to stuck-process detection.
        """
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not self.dry_run:
            self.status_file.touch()
            logger.info(f"Job {self.name}: status file touched")

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

        # On success the delayed group resets (SPEC §Delayed Job Handling) and
        # the uploaded bytes are reflected in the cached repo size estimate.
        self.delayed = False
        self.delayed_reason = None
        self.protected_snapshot = None
        self.missed_slots = 0
        self._bump_repo_size(selected_repo, total_size)

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
        """Mount a snapshot and run the backup flow with failure handling."""
        snapshot_name = snapshot_path.name
        try:
            touch_global_lock(self.name, snapshot_name)
            with self.mount(snapshot_path) as mount_point:
                return self._run_backup(mount_point, snapshot_path, snapshot_name, retry=retry)
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
        # otherwise the failed-backup retry / delayed-state load below could
        # never trigger.
        self._load_delayed_state()
        last_status = self._get_last_status()
        self._write_status("started", "Backup initiated")

        # Phase 4 (executed first for cadence): create a snapshot on schedule,
        # independent of backup success, so fresh installs self-start and long
        # backups don't stall cadence. The 12h minimum age guarantees this new
        # snapshot is never selected for a backup in this same run.
        if self.snapshot_due:
            self.create_snapshot()

        try:
            # 1. Retry a failed backup from a previous run (same snapshot).
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

            # 2. Phase 2 planning (window, time, repo checks, delay recovery).
            plan = self._plan()

            if plan == "skip":
                if self.delayed:
                    # Delayed jobs already wrote their status in _delay(); the
                    # persisted state from a prior run needs no rewrite.
                    return {"job": self.name, "status": "delayed",
                            "delayed_reason": self.delayed_reason}
                self._write_status("skipped", (self.skip_reason or "not time for backup").capitalize())
                return {"job": self.name, "status": "skipped"}

            # 3. Phase 3 execution — select snapshot, then back up.
            snapshot_path = self.planned_snapshot or self._select_snapshot_for_backup()
            if snapshot_path is None:
                self._write_status("skipped", "No snapshot found")
                return {"job": self.name, "status": "skipped"}

            # Safety rule: never back up a snapshot newer than 12 hours old.
            # A graceful skip, not a failure — picked up on a later run.
            if self._snapshot_too_young(snapshot_path):
                logger.info(f"Job {self.name}: snapshot {snapshot_path.name} is younger than "
                            f"{DEFAULT_SNAPSHOT_AGE_MIN}s; deferring to a later run.")
                self._write_status(
                    "skipped",
                    f"Snapshot {snapshot_path.name} younger than {DEFAULT_SNAPSHOT_AGE_MIN}s",
                )
                return {"job": self.name, "status": "skipped"}

            # If the window closed between planning and execution, the slot is
            # missed (EXIT_PERM_TIMEOUT) rather than forced through.
            if not self._is_within_backup_window():
                self._write_status("skipped", "Backup window closed before backup could run")
                return {"job": self.name, "status": "skipped", "timeout": True}

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
    stuck_wait: int = Field(default=DEFAULT_STUCK_WAIT, ge=1)
    restic_update: str = "never"

    def __init__(self, **data):
        known_keys = {'repos', 'jobs', 'mount_base', 'stuck_threshold',
                      'stuck_wait', 'restic_update'}
        unknown = set(data.keys()) - known_keys
        if unknown:
            warnings.warn(f"Unknown config keys: {unknown}")
        super().__init__(**data)

    @field_validator('stuck_threshold', 'stuck_wait', mode='before')
    @classmethod
    def _parse_stuck(cls, v):
        return parse_duration(v)

    @field_validator('restic_update')
    @classmethod
    def validate_restic_update(cls, v):
        if v not in ("after", "never"):
            raise ValueError(
                f"restic_update must be one of 'after', 'never'; got {v!r}"
            )
        return v

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


def _restic_update_due(results: list[dict], restic_update: str) -> bool:
    """True when the privileged restic update should run after this run.

    Updating restic rewrites a binary carrying cap_dac_read_search — the
    read-anything capability — so it must only run when a backup actually
    completed this run, and never on dry-runs, skipped jobs, or failures.
    """
    if restic_update != "after":
        return False
    return any(r.get("status") == "success" for r in results)


def _run_restic_update() -> None:
    """Run the privileged 'update-restic' wrapper op after a successful backup.

    Updating restic rewrites a binary that carries cap_dac_read_search — the
    read-anything capability — so it must only run when a backup actually
    happened this run (the caller guarantees this). A failure never aborts the
    run: the existing binary remains usable (rc 0 = updated, rc 2 = self-update
    failed but old binary retained + setcap applied).
    """
    cmd = [SUDO, IO_WRAPPER, "update-restic"]
    logger.info(f"running {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        logger.error("restic update timed out; continuing with existing restic")
        return
    if result.returncode == 0:
        logger.info("restic update: OK (binary up to date, setcap applied)")
    elif result.returncode == 2:
        logger.warning(
            "restic self-update failed but existing restic is retained and usable; "
            f"continuing ({result.stderr.strip() or 'see wrapper log'})"
        )
    else:
        logger.error(
            f"restic update failed (rc={result.returncode}): "
            f"{result.stderr.strip() or 'see wrapper log'}"
        )


def _effective_priority(job: Job) -> int:
    """Priority with delay escalation: priority × (missed_slots + 1).

    A delayed job's effective priority rises each missed slot, so the same job
    keeps winning the allocation when its repo is over the size limit.
    """
    return job.priority * (job.missed_slots + 1)


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

    # Phase 1: restic cache dir. Restic recreates it, so a missing dir is a
    # warning, not a fatal config error.
    cache_dir = os.environ.get("RESTIC_CACHE_DIR", "/opt/snap-bak/cache")
    if not os.path.isdir(cache_dir):
        logger.warning(f"Restic cache dir {cache_dir} does not exist (restic will create it)")

    # Phase 1: password file permissions are a hard gate (exit 4).
    for job in jobs:
        job._check_password_file_permissions()

    # Acquire global lock to prevent multiple snap-bak instances
    lock_fh = acquire_global_lock(config.stuck_threshold, config.stuck_wait)
    try:
        # Load persisted delayed/missed_slots state for effective-priority
        # ordering, and share one per-run repo-state cache across jobs.
        for job in jobs:
            job._load_delayed_state()
        repo_state: dict = {}
        for job in jobs:
            job.repo_state = repo_state

        # Priority system: effective_priority = priority × (missed_slots + 1).
        ordered = sorted(jobs, key=_effective_priority, reverse=True)

        results = []
        for job in ordered:
            job.dry_run = args.dry_run
            result = job.run()
            results.append(result)
            # Update heartbeat to show we're still alive
            touch_global_lock()

        # Exit codes for monitoring: permanent failures beat temporary delays.
        statuses = [r.get("status") for r in results]
        if "failed" in statuses:
            sys.exit(EXIT_PERM_FAILED)
        if any(r.get("timeout") for r in results):
            sys.exit(EXIT_PERM_TIMEOUT)
        if any(r.get("status") == "delayed" and r.get("delayed_reason") == "unreachable"
               for r in results):
            sys.exit(EXIT_TEMP_UNREACHABLE)
        if any(r.get("status") == "delayed" for r in results):
            sys.exit(EXIT_TEMP_SIZE_LIMIT)
        # restic_update='after': update only when a backup actually completed
        # this run (never on skipped/dry-run/failed runs).
        if _restic_update_due(results, config.restic_update):
            _run_restic_update()
    finally:
        release_global_lock(lock_fh)


if __name__ == "__main__":
    main()
