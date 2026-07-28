#!/usr/bin/env python3
"""
Btrfs Snapshot -> Restic Backup Orchestrator.

Strictly data-driven: jobs are defined entirely in the config file.
Supports CLI overrides, env vars, and preflight checks.

Usage:
  snapbak.py --help
  snapbak.py --list-jobs
  snapbak.py --job <name> --dry-run
  SNAPBAK_JOB=home snapbak.py

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
from datetime import datetime, timedelta
from pathlib import Path

from typing import NamedTuple

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Constants ---

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "config.json"
DEFAULT_MOUNT_BASE = Path("/bak")
DEFAULT_STUCK_THRESHOLD = 3600  # 1 hour
DEFAULT_TIMEOUT = 86400  # 24 hours
DEFAULT_BACKUP_RETRIES = 3
DEFAULT_BACKUP_INTERVAL = 3600  # 1 hour
RETRY_DELAY = 5  # seconds between retry attempts

# --- Error codes ---
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_MOUNT_ERROR = 3
EXIT_PERMISSION_ERROR = 4


# --- Retention functions ---

class Snapshot(NamedTuple):
    path: Path
    time: datetime


def build_retention_exponential(n: int = 1) -> list[datetime]:
    """Generate tier boundaries at exponential intervals: 1h, 2h, 4h, 8h..."""
    now = datetime.now()
    return [now - relativedelta(hours=2**i) for i in range(0, n)]


def build_retention_datetimes(
    hourly: int = 0, daily: int = 0, weekly: int = 0,
    monthly: int = 0, yearly: int = 0,
) -> list[datetime]:
    """Generate calendar-based retention boundaries.

    Each tier starts after the previous tiers end.
    E.g., hourly=2, daily=1 → 2 hourly slots + 1 daily slot starting 2h ago.
    """
    now = datetime.now()
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
    return keeps


RETENTION_FN = {
    "exponential": build_retention_exponential,
    "datetimes": build_retention_datetimes,
}


def _get_state_dir() -> Path:
    """Get XDG state directory for snapbak."""
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "snapbak"
    return Path.home() / ".local" / "state" / "snapbak"


STATE_DIR = _get_state_dir()
LOG_FILE = STATE_DIR / "snapbak.log"
GLOBAL_LOCK_FILE = STATE_DIR / "snapbak.lock"

# Ensure state directory exists before logging setup
STATE_DIR.mkdir(parents=True, exist_ok=True)

# --- Logging ---
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


class LockError(Exception):
    """Raised when a job lock cannot be acquired."""


class StuckInstanceError(Exception):
    """Raised when a previous snap-bak instance appears stuck."""


# --- Global lock ---

def _write_lock_info(job_name: str | None = None, snapshot_name: str | None = None):
    """Write current job info to the lock file as JSON."""
    info = {
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(),
        "current_job": job_name,
        "snapshot_name": snapshot_name,
        "backup_started_at": datetime.now().isoformat() if job_name else None,
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


# --- Job model ---

class Job(BaseModel):
    """A single backup job."""
    model_config = ConfigDict(extra='allow')

    name: str
    repo: str
    snapshot_dir: str
    backup_retention: dict = Field(default_factory=dict)
    snapshot_retention: dict = Field(default_factory=lambda: {"fn": "exponential", "config": {"n": 5}})
    backup_interval: int = Field(default=DEFAULT_BACKUP_INTERVAL, ge=1)
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list = Field(default_factory=list)
    backup_retries: int = Field(default=DEFAULT_BACKUP_RETRIES, ge=1)
    timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1)
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

        Returns None if the name doesn't match our expected formats:
        - ISO 8601: 2024-01-15T10:30:00
        - Compact:  20240115_103000
        """
        name = snapshot.name
        try:
            if "T" in name:
                return datetime.fromisoformat(name)
            return datetime.strptime(name, "%Y%m%d_%H%M%S")
        except ValueError:
            return None

    def _find_oldest_snapshot_within(self, interval_seconds: int) -> Path | None:
        """Find the oldest snapshot newer than (now - interval)."""
        cutoff = datetime.now() - timedelta(seconds=interval_seconds)
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
                return datetime.fromisoformat(data["timestamp"])
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

    def _write_status(self, status: str, message: str,
                      snapshot_name: str | None = None,
                      snapshot_time: datetime | None = None):
        """Atomically write status to per-job status file.

        Uses tmp file + rename for atomicity. If a .tmp file exists
        from a previous interrupted run, it indicates a stuck process.
        """
        if self.dry_run:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "job": self.name,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "message": message,
        }
        if snapshot_name:
            data["snapshot_name"] = snapshot_name
        if snapshot_time:
            data["snapshot_time"] = snapshot_time.isoformat()
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
        """Warn if password file is readable by group or others."""
        repo = self.repo_obj
        if not repo.password_file:
            return
        try:
            stat = os.stat(repo.password_file)
            mode = stat.st_mode
            if mode & 0o077:
                logger.warning(
                    f"Password file {repo.password_file} is readable by group or others "
                    f"(mode {oct(mode & 0o777)}). Consider: chmod 600 {repo.password_file}"
                )
        except OSError as e:
            logger.warning(f"Cannot stat password file {repo.password_file}: {e}")

    # --- Decision ---

    def should_run(self) -> bool:
        """Return True if the latest snapshot is newer than the last backup."""
        try:
            snap_time = self.latest_snapshot_time
        except (ValueError, OSError) as e:
            logger.warning(f"Job {self.name}: cannot determine snapshot time: {e}")
            return True

        last_backup = self._get_last_backup_time()
        if last_backup is None:
            logger.info(f"Job {self.name}: no previous backup, will run")
            return True

        if snap_time > last_backup:
            logger.info(
                f"Job {self.name}: new snapshot ({snap_time}) "
                f"since last backup ({last_backup})"
            )
            return True

        logger.info(f"Job {self.name}: no new snapshots, skipping")
        return False

    def _time_since_last_success(self) -> float | None:
        """Return seconds since last successful backup, or None."""
        last_backup = self._get_last_backup_time()
        if last_backup is None:
            return None
        return (datetime.now() - last_backup).total_seconds()

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

    def _build_restic_cmd(self, subcommand: str, repo: Repo | None = None) -> list[str]:
        """Build a restic command with common arguments."""
        if repo is None:
            repo = self.repo_obj
        cmd = ["restic", "-r", repo.path, subcommand]
        if repo.password_file:
            cmd.extend(["--password-file", repo.password_file])
        if self.ignore_file:
            cmd.extend(["--exclude-file", self.ignore_file])
        for pattern in self.excludes:
            cmd.extend(["--exclude", pattern])
        return cmd

    # --- Backup steps ---

    def _preflight(self, mount_point: Path, repo: Repo | None = None) -> int:
        """Run a restic dry-run to estimate size and check remote connectivity."""
        logger.info(f"Job {self.name}: running preflight...")
        cmd = self._build_restic_cmd("backup", repo)
        cmd.extend([
            str(mount_point),
            "--no-symlinks",
            "--tag", self.name,
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
            raise RuntimeError(
                f"Preflight failed (remote connectivity or config issue): {e.stderr}"
            )
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse preflight output: {e}")
            return 0

    def _backup(self, mount_point: Path, repo: Repo | None = None):
        """Run the actual restic backup with retry logic."""
        cmd = self._build_restic_cmd("backup", repo)
        cmd.extend([str(mount_point), "--no-symlinks", "--tag", self.name])

        for attempt in range(self.backup_retries + 1):
            try:
                self._run(cmd, timeout=self.timeout)
                return
            except subprocess.CalledProcessError as e:
                if attempt < self.backup_retries:
                    logger.warning(
                        f"Backup attempt {attempt + 1}/{self.backup_retries + 1} "
                        f"failed for {self.name}. Retrying in {RETRY_DELAY}s... "
                        f"Error: {e.stderr}"
                    )
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error(
                        f"Backup failed after {self.backup_retries + 1} attempts "
                        f"for {self.name}: {e.stderr}"
                    )
                    raise

    def _forget_and_prune(self, repo: Repo | None = None):
        """Apply retention policy via forget + prune."""
        if not self.backup_retention:
            return
        cmd = self._build_restic_cmd("forget", repo)
        cmd.extend(["--tag", self.name])
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
                self._run(["btrfs", "subvolume", "delete", str(s.path)])

    # --- Throughput learning ---

    def _update_repo_throughput(self, repo: Repo, total_size: int, backup_time: float):
        """Update repo throughput based on observed performance."""
        if backup_time > 0 and total_size > 0:
            throughput_mbps = round((total_size * 8) / (backup_time * 1_000_000))
            if throughput_mbps > 0:
                repo.throughput_mbps = throughput_mbps
                logger.info(f"Updated {repo.name} throughput to {throughput_mbps} Mbps")
                self._save_config_throughput(repo.name, throughput_mbps)

    def _save_config_throughput(self, repo_name: str, throughput_mbps: int):
        """Update throughput_mbps in the config file."""
        if not self.config_path:
            return
        try:
            config_path = Path(self.config_path)
            config = json.loads(config_path.read_text())
            for repo in config.get("repos", []):
                if repo.get("name") == repo_name:
                    repo["throughput_mbps"] = throughput_mbps
                    break
            tmp = config_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(config, indent=2))
            tmp.replace(config_path)
        except OSError as e:
            logger.warning(f"Could not update config file for throughput: {e}")

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
                self._run(["umount", "-l", str(mount_point)])
            self._run(["mount", "-o", "ro", str(snapshot_path), str(mount_point)])
            yield mount_point
        finally:
            if mount_point.exists() and os.path.ismount(mount_point):
                try:
                    self._run(["umount", "-l", str(mount_point)])
                except subprocess.CalledProcessError:
                    logger.warning(f"Failed to unmount {mount_point}")
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
            self.lock_file.unlink(missing_ok=True)

    # --- Main entry point ---

    def run(self, config_overrides: dict | None = None) -> dict:
        """Execute the backup job. Returns a result dict."""
        if config_overrides:
            for key, val in config_overrides.items():
                if hasattr(self, key):
                    setattr(self, key, val)

        logger.info(f"--- Starting Job: {self.name} ---")
        self._check_stale_tmp()
        self._write_status("started", "Backup initiated")

        # 1. Check for failed backup from previous run
        last_status = self._get_last_status()
        if last_status and last_status.get("status") == "failed":
            failed_snapshot_name = last_status.get("snapshot_name")
            if failed_snapshot_name:
                failed_snapshot_path = Path(self.snapshot_dir) / failed_snapshot_name
                if failed_snapshot_path.exists():
                    logger.info(f"Retrying failed backup for snapshot: {failed_snapshot_name}")
                    touch_global_lock(self.name, failed_snapshot_name)
                    try:
                        with self.mount_and_lock(failed_snapshot_path) as mount_point:
                            total_size = self._preflight(mount_point)
                            if self.dry_run:
                                logger.info(f"DRY-RUN: Would run backup for {self.name}")
                                self._write_status("dry_run", "Preflight passed (dry-run)")
                                return {"job": self.name, "status": "dry_run"}

                            backup_start = time.time()
                            selected_repo = self.repo_obj
                            self._backup(mount_point, selected_repo)
                            self._forget_and_prune(selected_repo)
                            backup_time = time.time() - backup_start

                            self._update_repo_throughput(selected_repo, total_size, backup_time)

                            snap_time = self._parse_snapshot_time(failed_snapshot_path)
                            self._write_status(
                                "success",
                                f"Backup completed (retry of failed snapshot)",
                                failed_snapshot_name,
                                snap_time,
                            )
                            self._cleanup_snapshots(protected_snapshot=failed_snapshot_name)
                            return {"job": self.name, "status": "success"}
                    except LockError:
                        self._write_status("skipped", "Another instance is running")
                        return {"job": self.name, "status": "skipped"}
                    except Exception as e:
                        logger.error(f"Job {self.name} failed: {e}", exc_info=True)
                        self._write_status("failed", str(e), failed_snapshot_name)
                        return {"job": self.name, "status": "failed", "error": str(e)}
                    finally:
                        logger.info(f"--- Finished Job: {self.name} ---")
                else:
                    logger.warning(f"Failed snapshot {failed_snapshot_name} no longer exists")
                    self._write_status("skipped", "Previous backup failed but snapshot is gone")
                    return {"job": self.name, "status": "skipped"}
            else:
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

        snapshot_name = snapshot_path.name
        touch_global_lock(self.name, snapshot_name)

        try:
            with self.mount_and_lock(snapshot_path) as mount_point:
                total_size = self._preflight(mount_point)

                if self.dry_run:
                    logger.info(f"DRY-RUN: Would run backup for {self.name}")
                    self._write_status("dry_run", "Preflight passed (dry-run)")
                    return {"job": self.name, "status": "dry_run"}

                backup_start = time.time()
                selected_repo = self.repo_obj
                self._backup(mount_point, selected_repo)
                self._forget_and_prune(selected_repo)
                backup_time = time.time() - backup_start

                self._update_repo_throughput(selected_repo, total_size, backup_time)

                snap_time = self._parse_snapshot_time(snapshot_path)
                self._write_status(
                    "success",
                    f"Backup completed at {datetime.now().isoformat()}",
                    snapshot_name,
                    snap_time,
                )
                self._cleanup_snapshots(protected_snapshot=snapshot_name)
                return {"job": self.name, "status": "success"}

        except LockError:
            self._write_status("skipped", "Another instance is running")
            return {"job": self.name, "status": "skipped"}
        except Exception as e:
            logger.error(f"Job {self.name} failed: {e}", exc_info=True)
            self._write_status("failed", str(e), snapshot_name)
            return {"job": self.name, "status": "failed", "error": str(e)}
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
        return SnapBakConfig(**json.loads(config_path.read_text()))
    except Exception as e:
        logger.error(f"Config validation error: {e}")
        sys.exit(EXIT_CONFIG_ERROR)


# --- CLI ---

def main():
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
    parser.add_argument("-l", "--list-jobs", action="store_true",
                        help="List all configured jobs and exit.")
    args = parser.parse_args()

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
