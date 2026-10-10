"""Config: constants, env paths, quantity parsing, logging, retention, models.

Imports only `snapshot` (plus stdlib + pydantic + dateutil + json5); never
`job`, `excludes` or `cli`.
"""

import contextvars
import logging
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import json5
from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Snapshot naming/format and the btrfs command line live in snapshot.py.
from snap_bak import snapshot

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.json"
DEFAULT_MOUNT_BASE = Path("/bak")
DEFAULT_STUCK_THRESHOLD = 3600
DEFAULT_BACKUP_INTERVAL = 3600
DEFAULT_SNAPSHOT_INTERVAL = 3600
DEFAULT_SNAPSHOT_INTERVAL_MIN = 900
DEFAULT_SNAPSHOT_AGE_MIN = 12 * 3600
DEFAULT_STUCK_WAIT = 300
STUCK_POLL_INTERVAL = 60
REPO_CHECK_TIMEOUT = 30
REPO_STATS_TIMEOUT = 300
# Sudo runs a root-owned wrapper (installed by snap-bak-setup.sh) rather than
# btrfs/mount directly; a bare sudoers command name matches any args.
SUDO = os.environ.get("SNAPBAK_SUDO", "/usr/bin/sudo")
SUDO_EXEC = os.environ.get("SUDO_EXEC_PATH", "/opt/snap-bak/sudo-exec.py")

# SNAPBAK_STATE_DIR overrides the hardcoded path for tests; the mkdir at import
# is why tests must set that env var before importing this module.
STATE_DIR = Path(os.environ.get("SNAPBAK_STATE_DIR", "/opt/snap-bak/state"))
GLOBAL_LOCK_FILE = STATE_DIR / "snap-bak.lock"
STATE_DIR.mkdir(parents=True, exist_ok=True)

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


def load_config_json(text: str) -> dict:
    """Parse a config string; json5 accepts // and /* */ comments (JSONC)."""
    return json5.loads(text)


# A bare int (seconds/bytes) or a string like '90m'/'2h'/'200GB'; unknown units
# raise so config typos fail loudly rather than being silently misread.
_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}

_SIZE_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
    "p": 1024**5,
    "pb": 1024**5,
    "pib": 1024**5,
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


# stderr only (systemd journals it); a file copy is opt-in via --log-file. Each
# line carries the current job via contextvars so multi-job runs stay readable.
_JOB_VAR = contextvars.ContextVar("snapbak_job", default="-")


class _ContextFilter(logging.Filter):
    """Inject the current job (from contextvars) into each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.job = _JOB_VAR.get()
        return True


@contextmanager
def job_log_context(name: str) -> Iterator[None]:
    """Tag subsequent log lines with the job name (e.g. 'data')."""
    token = _JOB_VAR.set(name)
    try:
        yield
    finally:
        _JOB_VAR.reset(token)


# systemd already stamps the date/time, so the stream format omits it; the
# opt-in --log-file handler uses _FILE_FORMAT, which keeps a timestamp.
_STREAM_FORMAT = "[%(levelname)s] [%(job)s] %(message)s"
_FILE_FORMAT = "%(asctime)s [%(levelname)s] [%(job)s] %(message)s"
_stream_handler = logging.StreamHandler(sys.stderr)
_stream_handler.setFormatter(logging.Formatter(_STREAM_FORMAT))
_stream_handler.addFilter(_ContextFilter())
logging.basicConfig(level=logging.INFO, handlers=[_stream_handler])
logger = logging.getLogger(__name__)


# The domain Snapshot (path + parsed time) is shared with the root wrapper.
Snapshot = snapshot.Snapshot


def build_retention_exponential(n: int = 1) -> list[datetime]:
    """Generate tier boundaries at exponential intervals: 1h, 2h, 4h, 8h..."""
    now = datetime.now(timezone.utc)
    return [now - relativedelta(hours=2**i) for i in range(0, n)]


def build_retention_datetimes(
    hourly: int = 0,
    daily: int = 0,
    weekly: int = 0,
    monthly: int = 0,
    yearly: int = 0,
) -> list[datetime]:
    """Calendar-based retention boundaries; each tier starts after the previous."""
    now = datetime.now(timezone.utc)
    hours = [now - relativedelta(hours=i) for i in range(0, hourly)]
    days = [now - relativedelta(hours=hourly, days=i) for i in range(0, daily)]
    weeks = [
        now - relativedelta(hours=hourly, days=daily, weeks=i) for i in range(0, weekly)
    ]
    months = [
        now - relativedelta(hours=hourly, days=daily, weeks=weekly, months=i)
        for i in range(0, monthly)
    ]
    years = [
        now
        - relativedelta(hours=hourly, days=daily, weeks=weekly, months=monthly, years=i)
        for i in range(0, yearly)
    ]
    return list(set(hours + days + weeks + months + years))


def build_snapshot_keeplist(
    snapshots: list[Snapshot], retentions: list[datetime]
) -> set[Path]:
    """Keep the oldest snapshot per slot plus the most recent; a kept snapshot
    leaves the pool so an older slot can't re-claim it."""
    # Only parsed snapshots (time set) are candidates; a source subvolume
    # (time=None) has no slot to protect.
    timed = [(s, s.time) for s in snapshots if s.time is not None]
    if not timed:
        return set()
    avail = list(timed)
    keeps: set[Path] = set()
    for boundary in sorted(retentions, reverse=True):
        candidates = [(s, t) for s, t in avail if t >= boundary]
        if candidates:
            chosen = min(candidates, key=lambda pair: pair[1])
            keeps.add(chosen[0].path)
            avail.remove(chosen)
    keeps.add(max(timed, key=lambda pair: pair[1])[0].path)
    return keeps


RETENTION_FN: dict[str, Callable[..., list[datetime]]] = {
    "exponential": build_retention_exponential,
    "datetimes": build_retention_datetimes,
}


class Repo(BaseModel):
    """A restic repository with metadata."""

    # "forbid" rather than "ignore": a mistyped key must be a loud config
    # error, not a setting that silently never takes effect.
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    password_file: str = ""
    size_limit: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=100, ge=0)
    throughput_mbps: int = Field(default=1, ge=0)
    restic_options: list[str] = Field(default_factory=list)

    @field_validator("size_limit", mode="before")
    @classmethod
    def _parse_size_limit(cls, v):
        return parse_size(v)


class Window:
    """A local-time backup window in minutes since midnight.

    Overnight windows (start >= end) span midnight. `parse` fails open to None
    (backups always allowed) so a misconfigured window never disables backups.
    """

    def __init__(self, start_min: int, end_min: int):
        self.start_min = start_min
        self.end_min = end_min

    def contains(self, now: datetime) -> bool:
        current_minutes = now.hour * 60 + now.minute
        if self.start_min < self.end_min:
            return self.start_min <= current_minutes < self.end_min
        return current_minutes >= self.start_min or current_minutes < self.end_min

    @classmethod
    def parse(cls, start: str | None, end: str | None) -> "Window | None":
        if not start or not end:
            return None
        try:
            start_h, start_m = map(int, start.split(":"))
            end_h, end_m = map(int, end.split(":"))
        except (ValueError, AttributeError):
            logger.warning("Ignoring invalid backup window format")
            return None
        return cls(start_h * 60 + start_m, end_h * 60 + end_m)


class Job(BaseModel):
    """A job's config fields (validated); resolve() binds it to a ResolvedJob."""

    model_config = ConfigDict(extra="forbid")

    name: str
    snapshot_dir: Path
    source_subvolume: Path
    repo: str  # repo NAME, bound to a Repo in Config.resolve
    priority: int = Field(default=100, ge=1)
    snapshot_interval: int = Field(
        default=DEFAULT_SNAPSHOT_INTERVAL, ge=DEFAULT_SNAPSHOT_INTERVAL_MIN
    )
    backup_retention: dict = Field(default_factory=dict)
    snapshot_retention: dict = Field(
        default_factory=lambda: {"fn": "exponential", "config": {"n": 5}}
    )
    backup_interval: int = Field(default=DEFAULT_BACKUP_INTERVAL, ge=1)
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list[str] = Field(default_factory=list)
    tag: str | None = None
    restic_options: list[str] = Field(default_factory=list)
    skip_snapshot_if_idle: bool = True

    @field_validator("snapshot_interval", "backup_interval", mode="before")
    @classmethod
    def _parse_interval(cls, v):
        return parse_duration(v)

    # `snapshot_dir` existence is deliberately not checked here. It looks like a
    # config fact and is not one: it is machine state, and nothing creates this
    # directory, so a new job's is absent until an admin makes it. Checking it
    # made the config that would have named that job unloadable, took down every
    # other job along with it, and blocked read-only commands that never touch
    # it. It is checked where it is used, by `Runner.create_snapshot`. Readers
    # (`Snapshot.scan`, `_cleanup_snapshots`) already treat it as optional.

    @field_validator("name", "tag")
    @classmethod
    def name_must_be_safe(cls, v):
        # name/tag drive <state>/<name>_status.json and the restic tag.
        if v is None:
            return v
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError(f"{v!r} must match [A-Za-z0-9_-]")
        return v


@dataclass(frozen=True)
class ResolvedJob:
    """A Job bound to its Repo and derived paths, computed once at resolve()."""

    job: Job
    repo: Repo
    mount_point: Path
    state_file: Path
    tag: str
    window: Window | None
    config_path: str | None

    @property
    def backup_root(self) -> str:
        """The path prefix restic saw: the mount point it was backed up from.

        Exclude patterns are matched against this (see excludes.py), not
        against the live source path, so inspection answers "what would the next
        backup skip?" rather than "what matches this string?".
        """
        return str(self.mount_point)


class Config(BaseModel):
    """Top-level config model."""

    model_config = ConfigDict(extra="forbid")

    repos: list[Repo]
    jobs: list[Job]
    mount_base: str = "/bak"
    stuck_threshold: int = Field(default=DEFAULT_STUCK_THRESHOLD, ge=1)
    stuck_wait: int = Field(default=DEFAULT_STUCK_WAIT, ge=1)
    restic_update: str = "never"
    # How long a job's state file survives without being touched, and how old a
    # file for a job no longer in the config may be. Only *state* is reaped:
    # queued work is intent and is never aged out.
    state_retention_days: int = 90

    @field_validator("stuck_threshold", "stuck_wait", mode="before")
    @classmethod
    def _parse_stuck(cls, v):
        return parse_duration(v)

    @field_validator("restic_update")
    @classmethod
    def validate_restic_update(cls, v):
        if v not in ("after", "never"):
            raise ValueError(
                f"restic_update must be one of 'after', 'never'; got {v!r}"
            )
        return v

    @model_validator(mode="after")
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

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Load and validate a JSON config file using pydantic."""
        if not path.exists():
            logger.error(f"Config not found: {path}")
            sys.exit(EXIT_CONFIG_ERROR)
        try:
            return cls(**load_config_json(path.read_text()))
        except Exception as e:
            logger.error(f"Config validation error: {e}")
            sys.exit(EXIT_CONFIG_ERROR)

    def resolve(self, config_path: str | None = None) -> list[ResolvedJob]:
        """Bind each job to its repo and derived paths; return resolved jobs."""
        by_name = {r.name: r for r in self.repos}
        return [
            ResolvedJob(
                job=job,
                repo=by_name[job.repo],
                mount_point=Path(self.mount_base) / job.source_subvolume.name,
                state_file=STATE_DIR / f"{job.name}.json",
                tag=job.tag or job.name,
                window=Window.parse(job.backup_window_start, job.backup_window_end),
                config_path=config_path,
            )
            for job in self.jobs
        ]

    def get(self, name: str) -> ResolvedJob | None:
        """Look up a resolved job by name, or None."""
        for rj in self.resolve():
            if rj.job.name == name:
                return rj
        return None

    def __getitem__(self, name: str) -> ResolvedJob:
        rj = self.get(name)
        if rj is None:
            raise KeyError(name)
        return rj
