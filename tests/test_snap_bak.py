# -*- coding: utf-8 -*-
"""Unit tests for snapbak.py — no remote restic, minimal IO."""

import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.snap_bak as snapbak_module

from pydantic import ValidationError

from scripts.snap_bak import (
    DEFAULT_SNAPSHOT_INTERVAL,
    EXIT_PERM_FAILED,
    EXIT_PERM_TIMEOUT,
    EXIT_PERMISSION_ERROR,
    EXIT_STUCK_WAIT,
    EXIT_SYSTEM,
    EXIT_TEMP_SIZE_LIMIT,
    EXIT_TEMP_UNREACHABLE,
    FAIL_CREDENTIALS,
    FAIL_INTERNAL,
    CredentialsError,
    Job,
    Repo,
    RemoteUnavailableError,
    RepoLockedError,
    FAIL_LOCKED,
    SnapBakConfig,
    _categorize_restic_error,
    _effective_priority,
    _restic_update_due,
    _mounted_source,
    restic_running,
    build_retention_datetimes,
    build_retention_exponential,
    build_snapshot_keeplist,
    load_config,
    parse_duration,
    parse_size,
    Snapshot,
)


# --- Fixtures ---


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Give every test its own STATE_DIR / GLOBAL_LOCK_FILE.

    Without this, run() writes status/lock files into the real /opt/snap-bak
    and tests leak state into each other.
    """
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(snapbak_module, "STATE_DIR", state)
    monkeypatch.setattr(snapbak_module, "GLOBAL_LOCK_FILE", state / "snap-bak.lock")


@pytest.fixture
def sample_repos():
    return [
        Repo(
            name="primary",
            path="rclone:CONFIG_ENTRY:DIRECTORY",
            password_file="/home/user/.restic_password",
            size_limit=1099511627776,
            latency_ms=50,
            throughput_mbps=10,
        ),
        Repo(
            name="secondary",
            path="rclone:CONFIG_ENTRY:DIRECTORY",
            password_file="/home/user/.b2_password",
            size_limit=536870912000,
            latency_ms=200,
            throughput_mbps=5,
        ),
    ]


@pytest.fixture
def sample_job(tmp_path, sample_repos):
    """Create a Job with a tmp_path snapshot directory."""
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    return Job(
        name="test_job",
        snapshot_dir=str(snapshot_dir),
        source_subvolume=str(snapshot_dir),
        repo="primary",
        repos=sample_repos,
        config_path=str(tmp_path / "config.json"),
    )


@pytest.fixture
def create_snapshots(sample_job):
    """Factory to create snapshot directories with given timestamps."""

    def _create(name: str, dt: datetime):
        snap_dir = Path(sample_job.snapshot_dir) / name
        snap_dir.mkdir(exist_ok=True)
        return snap_dir

    return _create


@pytest.fixture
def long_history():
    """400 daily snapshots ending at 'now' plus a few within the last 6h.

    In-memory only (no btrfs): exercises the pure retention math. Spans >= 388d.
    """
    now = datetime.now(timezone.utc)
    times = [now - timedelta(days=d) for d in range(0, 400)]
    times += [now - timedelta(hours=h) for h in (1, 3, 6)]
    times = sorted(set(times))
    return [
        Snapshot(Path(f"/bak/data/{t.strftime('%Y-%m-%d_%H:%MZ')}"), t)
        for t in times
    ]


@contextmanager
def _mock_mount(*args, **kwargs):
    """Context manager replacing Job.mount in run() tests."""
    yield Path("/tmp/fake_mount")


@pytest.fixture
def mocked_backup_job(sample_job):
    """Sample job with all backup sub-steps mocked so run() reaches success.

    create_snapshot/_write_status are mocked too: individual tests override
    their side_effect/return_value as needed.
    """
    sample_job.mount = _mock_mount
    sample_job._preflight = MagicMock(return_value=1_000_000_000)
    sample_job._backup = MagicMock(return_value="restic_snap_id")
    sample_job._forget_only = MagicMock()
    sample_job._update_repo_throughput = MagicMock()
    sample_job._cleanup_snapshots = MagicMock()
    sample_job.create_snapshot = MagicMock()
    sample_job._write_status = MagicMock()
    sample_job._check_repo_reachable = MagicMock(return_value=True)
    sample_job._measure_repo_size = MagicMock(return_value=0)
    # Power-saving gate: a bare {} is the fail-open path (no pool/probe info),
    # preserving the historic "snapshot when due" behavior in run() tests.
    sample_job._wrapper_status = MagicMock(return_value={})
    return sample_job


# --- Snapshot time parsing tests ---


class TestParseSnapshotTime:
    def test_yyyy_mm_dd_hh_mm_utc_format(self, sample_job, tmp_path):
        snap = tmp_path / "snapshots" / "2024-01-15_10:30Z"
        snap.mkdir(parents=True)
        result = sample_job._parse_snapshot_time(snap)
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_legacy_no_z_name_rejected(self, sample_job, tmp_path):
        """The 2026-08-14_02:00 crash: a bare no-Z name must not parse as a
        valid snapshot, or the orchestrator and the sudo-exec wrapper disagree."""
        snap = tmp_path / "snapshots" / "2026-08-14_02:00"
        snap.mkdir(parents=True)
        assert sample_job._parse_snapshot_time(snap) is None

    def test_invalid_format(self, sample_job, tmp_path):
        snap = tmp_path / "snapshots" / "random_name"
        snap.mkdir(parents=True)
        result = sample_job._parse_snapshot_time(snap)
        assert result is None


# --- Exponential cleanup tests ---


class TestExponentialCleanup:
    def test_youngest_local_snapshot_retained(self, sample_job, create_snapshots):
        """_cleanup_snapshots must never delete the youngest local snapshot."""
        now = datetime.now(timezone.utc)
        create_snapshots("old", now - timedelta(hours=3))
        create_snapshots("new", now - timedelta(minutes=20))
        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 1}}
        sample_job._run = MagicMock()
        sample_job._cleanup_snapshots()
        assert (Path(sample_job.snapshot_dir) / "new").exists()

    def test_fewer_than_n_snapshots_keeps_all(self, sample_job, create_snapshots):
        """If fewer than N snapshots exist, keep all."""
        now = datetime.now(timezone.utc)
        create_snapshots("1h_ago", now - timedelta(hours=1))
        create_snapshots("2h_ago", now - timedelta(hours=2))

        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 5}}
        sample_job._run = MagicMock()

        sample_job._cleanup_snapshots()

        btrfs_calls = [
            c for c in sample_job._run.call_args_list if c.args[0][1].endswith("btrfs")
        ]
        assert len(btrfs_calls) == 0

    def test_protected_snapshot_not_deleted(self, sample_job, create_snapshots):
        """The protected snapshot should never be deleted."""
        now = datetime.now(timezone.utc)
        create_snapshots("1h_ago", now - timedelta(hours=1))
        create_snapshots("2h_ago", now - timedelta(hours=2))
        create_snapshots("3h_ago", now - timedelta(hours=3))

        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 2}}
        deleted = []
        sample_job._run = MagicMock(
            side_effect=lambda cmd, **kw: (
                deleted.append(cmd[4]) if cmd[1].endswith("sudo-exec.py") else None
            )
        )

        sample_job._cleanup_snapshots(protected_snapshot="2h_ago")

        assert (Path(sample_job.snapshot_dir) / "2h_ago").exists()

    def test_zero_snapshots_to_keep_skips_cleanup(self, sample_job, create_snapshots):
        """If fn not in RETENTION_FN, skip cleanup entirely."""
        now = datetime.now(timezone.utc)
        create_snapshots("1h_ago", now - timedelta(hours=1))

        sample_job.snapshot_retention = {"fn": "invalid", "config": {}}
        sample_job._run = MagicMock()

        sample_job._cleanup_snapshots()

        btrfs_calls = [
            c for c in sample_job._run.call_args_list if c.args[0][1].endswith("btrfs")
        ]
        assert len(btrfs_calls) == 0

    def test_non_standard_names_skipped(self, sample_job, create_snapshots):
        """Non-standard snapshot names should be skipped (not deleted)."""
        now = datetime.now(timezone.utc)
        create_snapshots("20240115_103000", now - timedelta(hours=1))
        create_snapshots("random_name", now - timedelta(hours=2))

        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 5}}
        deleted = []
        sample_job._run = MagicMock(
            side_effect=lambda cmd, **kw: (
                deleted.append(cmd[4]) if cmd[1].endswith("sudo-exec.py") else None
            )
        )

        sample_job._cleanup_snapshots()

        assert not any("random_name" in d for d in deleted)


class TestSnapshotCleanupCadence:
    def test_cleanup_runs_on_skipped_cycle(self, sample_job):
        """Local snapshot retention must run on the snapshot cadence: invoke
        _cleanup_snapshots in _run_backup_phase even when the cycle is skipped
        (no backup). Previously cleanup only ran after a successful restic
        backup, so 2h snapshots piled up between infrequent backups."""
        sample_job.dry_run = False
        sample_job.protected_snapshot = None
        sample_job._seed_repo_throughput = MagicMock(return_value=10)
        sample_job._check_stale_tmp = MagicMock()
        sample_job._load_delayed_state = MagicMock()
        sample_job._get_last_status = MagicMock(return_value=None)
        sample_job._write_status = MagicMock()
        sample_job._should_create_snapshot = MagicMock(return_value=False)
        sample_job._plan = MagicMock(return_value="skip")
        sample_job.skip_reason = "outside backup window"
        sample_job._cleanup_snapshots = MagicMock()

        result = sample_job._run_backup_phase()

        assert result["status"] == "skipped"
        sample_job._cleanup_snapshots.assert_called_once()
        _, kwargs = sample_job._cleanup_snapshots.call_args
        assert kwargs.get("protected_snapshot") is None

    def test_cleanup_is_noop_when_snapshot_dir_missing(self, sample_job):
        """_cleanup_snapshots must not raise when the snapshot dir is absent
        (e.g. first run, before any snapshot is created)."""
        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 5}}
        sample_job.snapshot_dir = str(Path(sample_job.snapshot_dir) / "does_not_exist")
        sample_job._run = MagicMock()
        sample_job._cleanup_snapshots()
        sample_job._run.assert_not_called()


# --- Config validation tests (pydantic-based) ---


class TestConfigValidation:
    def test_missing_repos_raises(self):
        with pytest.raises(Exception):
            SnapBakConfig(jobs=[])

    def test_empty_repos_raises(self):
        with pytest.raises(Exception):
            SnapBakConfig(repos=[], jobs=[])

    def test_repo_missing_name_raises(self):
        with pytest.raises(Exception):
            SnapBakConfig(repos=[{"path": "rclone:test"}], jobs=[])

    def test_repo_missing_path_raises(self):
        with pytest.raises(Exception):
            SnapBakConfig(repos=[{"name": "primary"}], jobs=[])

    def test_job_missing_name_raises(self):
        with pytest.raises(Exception):
            SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[{"snapshot_dir": "/tmp"}],
            )

    def test_job_missing_snapshot_dir_raises(self):
        with pytest.raises(Exception):
            SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[{"name": "test"}],
            )

    def test_job_missing_repo_raises(self):
        with pytest.raises(Exception):
            SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[{"name": "test", "snapshot_dir": "/tmp"}],
            )

    def test_valid_config_passes(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                }
            ],
        )
        assert len(config.repos) == 1
        assert len(config.jobs) == 1

    def test_no_backward_compat_with_repo_string(self):
        """Old 'repo' string format should not be supported."""
        with pytest.raises(Exception):
            SnapBakConfig(
                repo="rclone:test",
                jobs=[{"name": "test", "snapshot_dir": "/tmp"}],
            )

    def test_unknown_repo_reference_raises(self):
        """Job referencing non-existent repo should raise."""
        with pytest.raises(Exception):
            SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[{"name": "test", "snapshot_dir": "/tmp", "repo": "nonexistent"}],
            )

    def test_password_file_per_repo(self):
        """password_file should be on Repo, not top-level."""
        config = SnapBakConfig(
            repos=[
                {
                    "name": "primary",
                    "path": "rclone:test",
                    "password_file": "/path/to/pass",
                }
            ],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                }
            ],
        )
        assert config.repos[0].password_file == "/path/to/pass"

    def test_source_subvolume_is_required(self, tmp_path, sample_repos):
        """source_subvolume is required in Job config."""
        with pytest.raises(Exception):
            Job(
                name="test",
                snapshot_dir="/tmp",
                repo="primary",
                repos=sample_repos,
            )

    def test_snapshot_dir_must_exist(self, tmp_path, sample_repos):
        """snapshot_dir must exist on the filesystem."""
        with pytest.raises(Exception):
            SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[
                    {
                        "name": "test",
                        "snapshot_dir": "/nonexistent/path",
                        "source_subvolume": "data",
                        "repo": "primary",
                    }
                ],
            )


# --- find_oldest_snapshot_within tests ---


class TestFindOldestSnapshotWithin:
    def test_finds_oldest_within_interval(self, sample_job, create_snapshots):
        now = datetime.now(timezone.utc)
        snap_30m = (now - timedelta(minutes=30)).strftime("%Y-%m-%d_%H:%MZ")
        snap_15m = (now - timedelta(minutes=15)).strftime("%Y-%m-%d_%H:%MZ")
        create_snapshots(snap_30m, now - timedelta(minutes=30))
        create_snapshots(snap_15m, now - timedelta(minutes=15))

        result = sample_job._find_oldest_snapshot_within(3600)  # 1 hour
        assert result is not None
        assert result.name == snap_30m

    def test_returns_none_when_no_snapshots(self, sample_job):
        result = sample_job._find_oldest_snapshot_within(3600)
        assert result is None

    def test_returns_none_when_all_outside_interval(self, sample_job, create_snapshots):
        now = datetime.now(timezone.utc)
        create_snapshots("5h_ago", now - timedelta(hours=5))
        create_snapshots("10h_ago", now - timedelta(hours=10))

        result = sample_job._find_oldest_snapshot_within(3600)  # 1 hour
        assert result is None

    def test_skips_non_standard_names(self, sample_job, create_snapshots):
        now = datetime.now(timezone.utc)
        snap_name = (now - timedelta(minutes=30)).strftime("%Y-%m-%d_%H:%MZ")
        create_snapshots(snap_name, now - timedelta(minutes=30))
        create_snapshots("random_name", now - timedelta(minutes=15))

        result = sample_job._find_oldest_snapshot_within(3600)
        assert result is not None
        assert result.name == snap_name


# --- Throughput calculation tests ---


class TestThroughputCalculation:
    def test_zero_backup_time_no_update(self, sample_job):
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)

        sample_job._update_repo_throughput(repo, 1_000_000_000, 0.0)
        assert repo.throughput_mbps == 10  # unchanged

    def test_zero_total_size_no_update(self, sample_job):
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)

        sample_job._update_repo_throughput(repo, 0, 10.0)
        assert repo.throughput_mbps == 10  # unchanged

    def test_records_history_per_repo(self, sample_job):
        """Observations are appended to the per-repo history in the state file."""
        repo_a = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        repo_b = Repo(name="secondary", path="rclone:test2", throughput_mbps=5)

        for _ in range(3):
            sample_job._update_repo_throughput(repo_a, 1_000_000_000, 10.0)
        sample_job._update_repo_throughput(repo_b, 500_000_000, 10.0)

        data = json.loads(sample_job.throughput_file.read_text())
        assert data["repos"]["primary"] == [800, 800, 800]
        assert data["repos"]["secondary"] == [400]

    def test_history_capped_at_10(self, sample_job):
        """History per repo is capped at THROUGHPUT_HISTORY_SIZE entries."""
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)

        for _ in range(15):
            sample_job._update_repo_throughput(repo, 1_000_000_000, 10.0)

        data = json.loads(sample_job.throughput_file.read_text())
        assert len(data["repos"]["primary"]) == 10

    def test_seed_uses_median_of_history(self, sample_job):
        """Seeding picks the median of observed history, falling back to config."""
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        for mbps in (100, 200, 300, 400, 100000):
            bytes_size = round(mbps * 1_000_000 * 1.0 / 8)
            sample_job._update_repo_throughput(repo, bytes_size, 1.0)

        fresh = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        assert sample_job._seed_repo_throughput(fresh) == 300

        no_history = Repo(name="other", path="rclone:test", throughput_mbps=42)
        assert sample_job._seed_repo_throughput(no_history) == 42

    def test_state_write_failure_continues(self, sample_job, monkeypatch):
        """If the state file cannot be written, log a warning and continue."""
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        monkeypatch.setattr(snapbak_module, "STATE_DIR", Path("/nonexistent_abyss"))

        # Should not raise even when STATE_DIR is unwritable
        sample_job._update_repo_throughput(repo, 1_000_000_000, 10.0)
        assert repo.throughput_mbps == 800


# --- Failed backup retry tests ---


class TestFailedBackupRetry:
    def test_failed_status_triggers_retry(self, mocked_backup_job, create_snapshots):
        """When last status is 'failed' with snapshot_name, retry with that snapshot."""
        now = datetime.now(timezone.utc)
        snap_name = (now - timedelta(hours=1)).strftime("%Y-%m-%d_%H:%MZ")
        create_snapshots(snap_name, now - timedelta(hours=1))

        # Write a failed status file
        status_file = mocked_backup_job.status_file
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(
            json.dumps(
                {
                    "job": "test_job",
                    "status": "failed",
                    "timestamp": (now - timedelta(hours=1)).isoformat(),
                    "message": "Backup failed",
                    "snapshot_name": snap_name,
                }
            )
        )

        # The 1h-old snapshot is due on schedule; fixture mocks create_snapshot.
        result = mocked_backup_job.run()

        assert result["status"] == "success"
        # Snapshot creation fired on schedule and the failed snapshot was retried.
        mocked_backup_job.create_snapshot.assert_called_once()
        mocked_backup_job._backup.assert_called_once()
        # The retry success carries restic's reported snapshot id.
        success_call = mocked_backup_job._write_status.call_args_list[-1]
        assert success_call.kwargs["restic_snapshot_id"] == "restic_snap_id"

    def test_failed_snapshot_gone_skips(self, mocked_backup_job):
        """When failed snapshot no longer exists, skip job."""
        now = datetime.now(timezone.utc)
        snap_name = (now - timedelta(hours=1)).strftime("%Y-%m-%d_%H:%MZ")

        # Write a failed status file
        status_file = mocked_backup_job.status_file
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(
            json.dumps(
                {
                    "job": "test_job",
                    "status": "failed",
                    "timestamp": (now - timedelta(hours=1)).isoformat(),
                    "message": "Backup failed",
                    "snapshot_name": snap_name,
                }
            )
        )

        # Fresh install (no snapshots) -> snapshot_due fires; fixture mocks it.
        result = mocked_backup_job.run()

        assert result["status"] == "skipped"
        mocked_backup_job._backup.assert_not_called()


# --- Snapshot schedule tests ---


class TestSnapshotSchedule:
    def test_fresh_install_creates_snapshot_and_defers(self, mocked_backup_job):
        """Fresh install: no snapshots -> create one on schedule, but it's too
        young to back up yet, so the run ends in a graceful skip."""
        now = datetime.now(timezone.utc)
        created = Path(mocked_backup_job.snapshot_dir) / now.strftime("%Y-%m-%d_%H:%MZ")

        def _fake_create():
            created.mkdir(exist_ok=True)
            return created

        mocked_backup_job.create_snapshot = MagicMock(side_effect=_fake_create)

        result = mocked_backup_job.run()

        mocked_backup_job.create_snapshot.assert_called_once()
        mocked_backup_job._backup.assert_not_called()
        assert result["status"] == "skipped"

    def test_creates_snapshot_when_none_exist_and_continues(self, mocked_backup_job):
        """A snapshot old enough to back up lets the run continue to backup."""
        created = Path(mocked_backup_job.snapshot_dir) / (
            datetime.now(timezone.utc) - timedelta(hours=20)
        ).strftime("%Y-%m-%d_%H:%MZ")

        def _fake_create():
            created.mkdir(exist_ok=True)
            return created

        mocked_backup_job.create_snapshot = MagicMock(side_effect=_fake_create)

        result = mocked_backup_job.run()

        mocked_backup_job.create_snapshot.assert_called_once()
        assert result["status"] == "success"
        success_call = mocked_backup_job._write_status.call_args_list[-1]
        assert success_call.kwargs["restic_snapshot_id"] == "restic_snap_id"

    def test_does_not_create_when_snapshot_within_interval(
        self, mocked_backup_job, create_snapshots
    ):
        """Snapshot younger than snapshot_interval (but old enough to back up) -> no new snapshot."""
        recent = datetime.now(timezone.utc) - timedelta(hours=13)
        create_snapshots(recent.strftime("%Y-%m-%d_%H:%MZ"), recent)
        mocked_backup_job.snapshot_interval = 24 * 3600

        result = mocked_backup_job.run()

        mocked_backup_job.create_snapshot.assert_not_called()
        assert result["status"] == "success"

    def test_too_young_snapshot_defers_backup(
        self, mocked_backup_job, create_snapshots
    ):
        """A snapshot under the minimum age is skipped gracefully, not backed up."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        create_snapshots(recent.strftime("%Y-%m-%d_%H:%MZ"), recent)

        result = mocked_backup_job.run()

        mocked_backup_job._backup.assert_not_called()
        assert result["status"] == "skipped"

    def test_dry_run_does_not_create_snapshot(self, mocked_backup_job):
        mocked_backup_job.dry_run = True
        # Dry-run now reaches create_snapshot (which logs the sudo-exec.py
        # command via `_run`); it must not actually create a snapshot.
        mocked_backup_job._should_create_snapshot = MagicMock(return_value=True)

        result = mocked_backup_job.run()

        mocked_backup_job.create_snapshot.assert_called()
        assert result["status"] in ("skipped", "dry_run")


# --- Power-saving snapshot gate tests ---


class TestShouldCreateSnapshot:
    """Snapshot creation is gated on (a) pool disk state and (b) changes."""

    def test_dry_run_never_creates(self, sample_job):
        sample_job.dry_run = True
        sample_job._wrapper_status = MagicMock(return_value={"any_spun_down": False})
        # In dry-run the create decision still runs, but create_snapshot must
        # NOT actually create a snapshot on disk — the sudo-exec.py command is
        # only logged (see `_run`), never executed.
        snap = sample_job.create_snapshot()
        assert not Path(snap).exists()

    def test_spun_down_disk_skips_even_when_due_and_changed(
        self, sample_job, create_snapshots
    ):
        """ANY spun-down disk in the pool suppresses the snapshot."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        sample_job.skip_snapshot_if_idle = True
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": True,
                "changed": True,
                "source_generation": 5,
                "last_snapshot_generation": 4,
            }
        )
        assert sample_job._should_create_snapshot() is False

    def test_spun_down_ignored_when_opted_out(self, sample_job, create_snapshots):
        """With skip_snapshot_if_idle=False the idle gate is bypassed."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        sample_job.skip_snapshot_if_idle = False
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": True,
                "changed": True,
                "source_generation": 5,
                "last_snapshot_generation": 4,
            }
        )
        assert sample_job._should_create_snapshot() is True

    def test_no_changes_skips(self, sample_job, create_snapshots):
        """Unchanged source (source gen == snapshot gen) -> no snapshot."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": False,
                "changed": False,
                "source_generation": 7,
                "last_snapshot_generation": 7,
            }
        )
        assert sample_job._should_create_snapshot() is False

    def test_no_changes_logs_skip_with_names(
        self, sample_job, create_snapshots, caplog
    ):
        """Unchanged source -> skip log leads with action and names the snapshots."""
        now = datetime.now(timezone.utc)
        snap = create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": False,
                "changed": False,
                "source_generation": 7,
                "last_snapshot_generation": 7,
            }
        )
        with caplog.at_level(logging.INFO):
            assert sample_job._should_create_snapshot() is False
        assert "Skipping" in caplog.text
        assert "source unchanged since" in caplog.text
        assert snap.name in caplog.text  # the most-recent snapshot name (Z)

    def test_changed_creates(self, sample_job, create_snapshots):
        """Source generation advanced -> a new snapshot captures it."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": False,
                "changed": True,
                "source_generation": 8,
                "last_snapshot_generation": 7,
            }
        )
        assert sample_job._should_create_snapshot() is True

    def test_fresh_install_creates(self, sample_job):
        """No snapshot yet -> always create (fresh-install self-start)."""
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": False,
                "changed": True,
                "source_generation": 1,
                "last_snapshot_generation": None,
            }
        )
        assert sample_job._should_create_snapshot() is True

    def test_not_due_skips(self, sample_job, create_snapshots):
        """Within the interval nothing is created even if changed."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        create_snapshots(recent.strftime("%Y-%m-%d_%H:%MZ"), recent)
        sample_job.snapshot_interval = 24 * 3600
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": False,
                "changed": True,
                "source_generation": 9,
                "last_snapshot_generation": 1,
            }
        )
        assert sample_job._should_create_snapshot() is False

    def test_probe_failure_fails_open(self, sample_job):
        """Wrapper error ({}) falls back to the old behavior: create if due."""
        sample_job._wrapper_status = MagicMock(return_value={})
        assert sample_job._should_create_snapshot() is True

    def test_unknown_pool_state_proceeds(self, sample_job):
        """Indeterminate pool/generation state must not suppress a snapshot."""
        sample_job._wrapper_status = MagicMock(
            return_value={
                "any_spun_down": None,
                "changed": None,
                "source_generation": None,
                "last_snapshot_generation": None,
            }
        )
        assert sample_job._should_create_snapshot() is True


# --- Failure category tests ---


class TestFailureCategories:
    def test_restic_exit_code_mapping(self):
        err = subprocess.CalledProcessError(12, "restic", stderr="wrong password")
        assert isinstance(_categorize_restic_error(err), CredentialsError)

        # rc=11 (repository locked) is its own category, not "remote unavailable".
        err = subprocess.CalledProcessError(11, "restic", stderr="locked")
        assert isinstance(_categorize_restic_error(err), RepoLockedError)

        for rc in (1, 3, 10):
            err = subprocess.CalledProcessError(rc, "restic", stderr="boom")
            assert isinstance(_categorize_restic_error(err), RemoteUnavailableError), rc

    def test_failed_backup_records_category(self, mocked_backup_job, create_snapshots):
        """A categorized failure is persisted on the failed status."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        mocked_backup_job._backup = MagicMock(
            side_effect=CredentialsError("wrong password")
        )

        result = mocked_backup_job.run()

        assert result["status"] == "failed"
        assert result["error"] == "wrong password"
        call = mocked_backup_job._write_status.call_args
        assert call.kwargs["failure_category"] == FAIL_CREDENTIALS
        assert "wrong password" in call.kwargs["last_error"]

    def test_unknown_failure_defaults_to_internal(
        self, mocked_backup_job, create_snapshots
    ):
        """Exceptions without a category default to FAIL_INTERNAL."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        mocked_backup_job._backup = MagicMock(side_effect=RuntimeError("mystery"))

        result = mocked_backup_job.run()

        assert result["status"] == "failed"
        call = mocked_backup_job._write_status.call_args
        assert call.kwargs["failure_category"] == FAIL_INTERNAL
        assert "mystery" in call.kwargs["last_error"]


# --- SnapBakConfig tests ---


class TestSnapBakConfig:
    def test_creates_jobs_with_repos(self, tmp_path):
        config = SnapBakConfig(
            repos=[
                {
                    "name": "primary",
                    "path": "rclone:CONFIG_ENTRY:DIRECTORY",
                    "size_limit": 100,
                    "latency_ms": 50,
                    "throughput_mbps": 10,
                },
            ],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                    "snapshot_retention": {"fn": "exponential", "config": {"n": 3}},
                    "backup_interval": 7200,
                },
            ],
        )
        jobs = config.resolve_jobs(str(tmp_path / "config.json"))

        assert len(jobs) == 1
        job = jobs[0]
        assert job.name == "test"
        assert job.repo == "primary"
        assert job.repo_path == "rclone:CONFIG_ENTRY:DIRECTORY"
        assert job.snapshot_retention == {"fn": "exponential", "config": {"n": 3}}
        assert job.backup_interval == 7200
        assert len(job.repos) == 1
        assert job.repos[0].name == "primary"
        assert job.config_path == str(tmp_path / "config.json")

    def test_defaults_applied(self, tmp_path):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                }
            ],
        )
        jobs = config.resolve_jobs(str(tmp_path / "config.json"))

        job = jobs[0]
        assert job.snapshot_retention == {"fn": "exponential", "config": {"n": 5}}
        assert job.backup_interval == 3600
        assert job.snapshot_interval == DEFAULT_SNAPSHOT_INTERVAL
        # Power saving is on by default (opt out per job).
        assert job.skip_snapshot_if_idle is True

    def test_skip_snapshot_if_idle_override(self, tmp_path):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                    "skip_snapshot_if_idle": False,
                }
            ],
        )
        assert config.jobs[0].skip_snapshot_if_idle is False

    def test_repo_path_resolves(self, tmp_path):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:CONFIG_ENTRY:DIRECTORY"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                }
            ],
        )
        jobs = config.resolve_jobs(str(tmp_path / "config.json"))
        assert jobs[0].repo_path == "rclone:CONFIG_ENTRY:DIRECTORY"

    def test_mount_base_default(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                }
            ],
        )
        assert config.mount_base == "/bak"

    def test_stuck_threshold_default(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                }
            ],
        )
        assert config.stuck_threshold == 3600

    def test_restic_update_default(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "data",
                    "repo": "primary",
                }
            ],
        )
        assert config.restic_update == "never"

    def test_restic_update_valid_values(self):
        for v in ("after", "never"):
            config = SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[
                    {
                        "name": "test",
                        "snapshot_dir": "/tmp",
                        "source_subvolume": "data",
                        "repo": "primary",
                    }
                ],
                restic_update=v,
            )
            assert config.restic_update == v

    def test_restic_update_invalid_rejected(self):
        # 'before' is rejected: updating restic rewrites a setcap binary, so it
        # may never run before knowing whether a backup will happen.
        for v in ("before", "sometimes"):
            with pytest.raises(Exception):
                SnapBakConfig(
                    repos=[{"name": "primary", "path": "rclone:test"}],
                    jobs=[
                        {
                            "name": "test",
                            "snapshot_dir": "/tmp",
                            "source_subvolume": "data",
                            "repo": "primary",
                        }
                    ],
                    restic_update=v,
                )

    def test_mount_point_mirrors_source(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "CustomName",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "/mnt/data",
                    "repo": "primary",
                }
            ],
        )
        jobs = config.resolve_jobs()
        assert jobs[0].mount_point == Path("/bak") / "data"

    def test_mount_point_relative_source(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "CustomName",
                    "snapshot_dir": "/tmp",
                    "source_subvolume": "home",
                    "repo": "primary",
                }
            ],
        )
        jobs = config.resolve_jobs()
        assert jobs[0].mount_point == Path("/bak") / "home"


class TestResticUpdateGate:
    """restic updates must never run unless a backup actually happened."""

    def test_not_due_when_never(self):
        assert not _restic_update_due([{"status": "success"}], "never")

    def test_due_only_after_success(self):
        assert _restic_update_due([{"status": "success"}], "after")

    def test_not_due_when_only_skipped(self):
        assert not _restic_update_due(
            [{"status": "skipped"}, {"status": "skipped"}], "after"
        )

    def test_due_when_any_real_backup_succeeded(self):
        # A dry-run job next to a real success still had a backup happen.
        assert _restic_update_due(
            [{"status": "dry_run"}, {"status": "success"}], "after"
        )

    def test_not_due_when_only_dry_run(self):
        assert not _restic_update_due([{"status": "dry_run"}], "after")

    def test_not_due_when_empty(self):
        assert not _restic_update_due([], "after")


class TestResticCmdPolicy:
    """restic_options/excludes apply to backup; global options only elsewhere."""

    def test_backup_cmd_has_options_and_excludes(self, sample_job):
        sample_job.repo_obj.restic_options = ["--compression=better"]
        sample_job.restic_options = ["--one-file-system"]
        sample_job.ignore_file = "/etc/x.ignore"
        sample_job.excludes = ["*.tmp", "*.cache"]
        cmd = sample_job._restic_cmd("backup")
        assert cmd[3] == "backup"
        assert "--compression=better" in cmd  # global option
        assert "--one-file-system" in cmd  # backup-only option
        assert ["--exclude-file", "/etc/x.ignore"] in [
            cmd[i : i + 2] for i in range(len(cmd) - 1)
        ]
        assert "--exclude" in cmd

    def test_forget_cmd_no_backup_only_flags(self, sample_job):
        sample_job.repo_obj.restic_options = ["--compression=better"]
        sample_job.restic_options = ["--one-file-system"]
        sample_job.ignore_file = "/etc/x.ignore"
        sample_job.excludes = ["*.tmp"]
        cmd = sample_job._restic_cmd("forget")
        assert cmd[3] == "forget"
        assert "--compression=better" in cmd  # global option still applies
        assert "--one-file-system" not in cmd
        assert "--exclude" not in cmd
        assert "--exclude-file" not in cmd

    def test_probe_cmd_global_only(self, sample_job):
        sample_job.repo_obj.restic_options = [
            "--compression=better",
            "--option",
            "rclone.program=foo",
        ]
        cmd = sample_job._restic_cmd("cat")
        assert cmd[3] == "cat"
        assert "--compression=better" in cmd
        assert "--option" in cmd
        assert "--exclude" not in cmd

    def test_forget_command_no_prune(self, sample_job):
        """forget carries tag + keep-* but NOT --prune; prune runs separately."""
        sample_job.backup_retention = {"daily": 6, "monthly": 4}
        sample_job.tag = "MyTag"
        sample_job.excludes = ["*.tmp"]
        calls = []
        sample_job._run = lambda cmd, **kw: calls.append(cmd)
        sample_job._forget_only()
        cmd = calls[0]
        assert cmd[3] == "forget"
        assert "--tag" in cmd
        assert ["--keep-daily", "6"] in [cmd[i : i + 2] for i in range(len(cmd) - 1)]
        assert ["--keep-monthly", "4"] in [cmd[i : i + 2] for i in range(len(cmd) - 1)]
        assert "--prune" not in cmd
        assert "--exclude" not in cmd
        assert "--exclude-file" not in cmd

    def test_prune_once_command(self, sample_job):
        """prune_once issues a bare `prune` (no tag/keep-*), default global opts."""
        calls = []
        sample_job._run = lambda cmd, **kw: calls.append(cmd)
        sample_job.prune_once()
        cmd = calls[0]
        assert cmd[3] == "prune"
        assert "--prune" not in cmd
        assert "--tag" not in cmd
        # graceful-timeout defaults from restic.build are present.
        assert "--stuck-request-timeout" in cmd

    def test_reserved_flag_dropped_with_warning(self, sample_job, caplog):
        sample_job.restic_options = ["--tag", "bogus"]
        with caplog.at_level("WARNING"):
            cmd = sample_job._restic_cmd("backup")
        assert "--tag" not in cmd
        assert "reserved flag '--tag'" in caplog.text


class TestHumanReadableQuantities:
    """Duration/size fields accept ints or strings like '2h', '1w', '200GB'."""

    def test_parse_duration_units(self):
        assert parse_duration("90m") == 5400
        assert parse_duration("2h") == 7200
        assert parse_duration("1w") == 604800
        assert parse_duration("1d") == 86400
        assert parse_duration("2H") == 7200
        assert parse_duration(" 2 h ") == 7200

    def test_parse_duration_int_passthrough(self):
        assert parse_duration(7200) == 7200

    def test_parse_size_units(self):
        assert parse_size("200GB") == 200 * 1024**3
        assert parse_size("1.5TiB") == int(1.5 * 1024**4)
        assert parse_size("2gib") == 2 * 1024**3
        assert parse_size("500") == 500

    def test_parse_errors(self):
        with pytest.raises(Exception):
            parse_duration("2x")
        with pytest.raises(Exception):
            parse_duration("abc")
        with pytest.raises(Exception):
            parse_size("many")
        with pytest.raises(Exception):
            parse_size(None)

    def test_config_accepts_human_readable(self, tmp_path):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test", "size_limit": "200GB"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": str(tmp_path),
                    "source_subvolume": "data",
                    "repo": "primary",
                    "snapshot_interval": "2h",
                    "backup_interval": "1w",
                }
            ],
            stuck_threshold="1h",
        )
        assert config.repos[0].size_limit == 200 * 1024**3
        assert config.jobs[0].snapshot_interval == 7200
        assert config.jobs[0].backup_interval == 604800
        assert config.stuck_threshold == 3600

    def test_unknown_timeout_key_is_inert(self, tmp_path):
        """The per-job `timeout` key is gone. A leftover config must still load
        (extra='allow' swallows it without error), and nothing in Job reads it,
        so the value can never reach a subprocess timeout."""
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[
                {
                    "name": "test",
                    "snapshot_dir": str(tmp_path),
                    "source_subvolume": "data",
                    "repo": "primary",
                    "timeout": "90m",
                }
            ],
            stuck_threshold="1h",
        )
        assert config.jobs[0].model_extra["timeout"] == "90m"
        assert not hasattr(config.jobs[0], "snapshot_timeout")  # never applied

    def test_config_rejects_unknown_unit(self, tmp_path):
        with pytest.raises(Exception):
            SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[
                    {
                        "name": "test",
                        "snapshot_dir": str(tmp_path),
                        "source_subvolume": "data",
                        "repo": "primary",
                        "backup_interval": "1fortnight",
                    }
                ],
            )


# --- load_config tests ---


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repos": [{"name": "primary", "path": "rclone:test"}],
                    "jobs": [
                        {
                            "name": "test",
                            "snapshot_dir": "/tmp",
                            "source_subvolume": "data",
                            "repo": "primary",
                        }
                    ],
                }
            )
        )
        result = load_config(config_path)
        assert len(result.repos) == 1
        assert len(result.jobs) == 1

    def test_missing_config_exits(self, tmp_path):
        config_path = tmp_path / "nonexistent.json"
        with pytest.raises(SystemExit) as exc_info:
            load_config(config_path)
        assert exc_info.value.code == 2  # EXIT_CONFIG_ERROR

    def test_invalid_config_exits(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"repos": [], "jobs": []}))
        with pytest.raises(SystemExit) as exc_info:
            load_config(config_path)
        assert exc_info.value.code == 2  # EXIT_CONFIG_ERROR


# --- Backup window tests ---


class TestBackupWindow:
    def test_no_window_always_allowed(self, sample_job):
        """If no window is configured, backups are always allowed."""
        sample_job.backup_window_start = None
        sample_job.backup_window_end = None
        assert sample_job._is_within_backup_window() is True

    def test_same_day_window_inside(self, sample_job):
        """Same-day window: 09:00-17:00, current time 12:00 → allowed."""
        sample_job.backup_window_start = "09:00"
        sample_job.backup_window_end = "17:00"
        with patch("scripts.snap_bak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 12, 0, 0)
            assert sample_job._is_within_backup_window() is True

    def test_same_day_window_outside(self, sample_job):
        """Same-day window: 09:00-17:00, current time 18:00 → not allowed."""
        sample_job.backup_window_start = "09:00"
        sample_job.backup_window_end = "17:00"
        with patch("scripts.snap_bak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 18, 0, 0)
            assert sample_job._is_within_backup_window() is False

    def test_overnight_window_inside_start(self, sample_job):
        """Overnight window: 22:00-06:00, current time 23:00 → allowed."""
        sample_job.backup_window_start = "22:00"
        sample_job.backup_window_end = "06:00"
        with patch("scripts.snap_bak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 23, 0, 0)
            assert sample_job._is_within_backup_window() is True

    def test_overnight_window_inside_end(self, sample_job):
        """Overnight window: 22:00-06:00, current time 03:00 → allowed."""
        sample_job.backup_window_start = "22:00"
        sample_job.backup_window_end = "06:00"
        with patch("scripts.snap_bak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 3, 0, 0)
            assert sample_job._is_within_backup_window() is True

    def test_overnight_window_outside(self, sample_job):
        """Overnight window: 22:00-06:00, current time 12:00 → not allowed."""
        sample_job.backup_window_start = "22:00"
        sample_job.backup_window_end = "06:00"
        with patch("scripts.snap_bak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 12, 0, 0)
            assert sample_job._is_within_backup_window() is False

    def test_invalid_window_format_ignored(self, sample_job):
        """Invalid window format should be ignored (return True)."""
        sample_job.backup_window_start = "invalid"
        sample_job.backup_window_end = "06:00"
        assert sample_job._is_within_backup_window() is True

    def test_window_boundary_exclusive(self, sample_job):
        """Window end is exclusive: 09:00-17:00, current time 17:00 → not allowed."""
        sample_job.backup_window_start = "09:00"
        sample_job.backup_window_end = "17:00"
        with patch("scripts.snap_bak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 17, 0, 0)
            assert sample_job._is_within_backup_window() is False


# --- Retention function tests ---


class TestBuildRetentionExponential:
    def test_generates_correct_tiers(self):
        """n=4 should produce boundaries at 1h, 2h, 4h, 8h."""
        now = datetime.now(timezone.utc)
        result = build_retention_exponential(n=4)
        assert len(result) == 4
        for i, dt in enumerate(result):
            expected = now - timedelta(hours=2**i)
            assert abs((dt - expected).total_seconds()) < 1

    def test_n_1_returns_one_boundary(self):
        result = build_retention_exponential(n=1)
        assert len(result) == 1

    def test_n_0_returns_empty(self):
        result = build_retention_exponential(n=0)
        assert len(result) == 0


class TestBuildRetentionDatetimes:
    def test_hourly_only(self):
        now = datetime.now(timezone.utc)
        result = build_retention_datetimes(hourly=3)
        assert len(result) == 3
        for dt in result:
            assert (now - dt).total_seconds() < 60 * 60 * 3 + 1

    def test_daily_only(self):
        result = build_retention_datetimes(daily=2)
        assert len(result) == 2

    def test_combined_hourly_daily(self):
        """hourly=2, daily=1 → 3 total boundaries."""
        result = build_retention_datetimes(hourly=2, daily=1)
        assert len(result) == 3

    def test_all_zero_returns_empty(self):
        result = build_retention_datetimes()
        assert len(result) == 0


class TestBuildSnapshotKeeplist:
    def test_exponential_keeplist(self, long_history):
        """The youngest snapshot is always kept, across retention sizes.

        Folds in the 'most recent never dropped' regression: with several
        snapshots inside the newest tier, the youngest must still survive.
        """
        youngest = max(long_history, key=lambda s: s.time).path
        for n in (0, 3, 5, 7):
            kl = build_snapshot_keeplist(long_history, build_retention_exponential(n))
            assert youngest in kl

    def test_fewer_snapshots_than_tiers(self):
        """With more tiers than snapshots, keeps what's available."""
        now = datetime.now(timezone.utc)
        snaps = [
            Snapshot(Path("1h"), now - timedelta(hours=1)),
            Snapshot(Path("3h"), now - timedelta(hours=3)),
        ]
        rets = [
            now - timedelta(hours=1),
            now - timedelta(hours=2),
            now - timedelta(hours=4),
            now - timedelta(hours=8),
        ]
        keeps = build_snapshot_keeplist(snaps, rets)
        assert Path("1h") in keeps
        assert Path("3h") in keeps

    def test_empty_snapshots(self):
        keeps = build_snapshot_keeplist([], [datetime.now(timezone.utc)])
        assert keeps == set()

    def test_empty_retentions(self):
        snaps = [Snapshot(Path("a"), datetime.now(timezone.utc))]
        keeps = build_snapshot_keeplist(snaps, [])
        assert keeps == {Path("a")}


class TestDatetimesCleanup:
    def test_datetimes_keeplist(self, long_history):
        """The youngest snapshot is always kept for calendar-based retention.

        Folds in the 'most recent never dropped' regression guard.
        """
        cfg = dict(hourly=7, daily=0, weekly=3, monthly=0, yearly=1)
        youngest = max(long_history, key=lambda s: s.time).path
        kl = build_snapshot_keeplist(long_history, build_retention_datetimes(**cfg))
        assert youngest in kl


# --- Repo planning / delayed-job handling ---


class TestRepoPlanning:
    def test_unreachable_repo_delays_job(self, mocked_backup_job, create_snapshots):
        """Phase 2 reachability failure delays the job, never backs up."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        mocked_backup_job._check_repo_reachable = MagicMock(return_value=False)

        result = mocked_backup_job.run()

        assert result["status"] == "delayed"
        assert result["delayed_reason"] == "unreachable"
        mocked_backup_job._backup.assert_not_called()

    def test_over_size_limit_delays_job(self, mocked_backup_job, create_snapshots):
        """Repo at/over size_limit delays the job (protected snapshot duty)."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        mocked_backup_job._measure_repo_size = MagicMock(
            return_value=2 * 10**12
        )  # 2TB > 1TiB limit

        result = mocked_backup_job.run()

        assert result["status"] == "delayed"
        assert result["delayed_reason"] == "size_limit"
        mocked_backup_job._backup.assert_not_called()

    def test_over_limit_second_job_skipped_not_delayed(self, sample_repos, tmp_path):
        """Two jobs on one over-limit repo: the first (highest effective
        priority) is delayed and allocated; the second is skipped only."""
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        shared: dict = {}
        jobs = []
        for name, prio in (("high", 10), ("low", 5)):
            job = Job(
                name=name,
                snapshot_dir=str(snap_dir),
                source_subvolume=str(snap_dir),
                repo="primary",
                repos=sample_repos,
                config_path=str(tmp_path / "c.json"),
            )
            job.priority = prio
            job.repo_state = shared
            job._check_repo_reachable = MagicMock(return_value=True)
            job._measure_repo_size = MagicMock(return_value=2 * 10**12)
            job._write_status = MagicMock()
            jobs.append(job)

        assert jobs[0]._plan() == "skip"
        assert jobs[0].delayed is True
        assert shared["primary"]["allocated"] is True

        assert jobs[1]._plan() == "skip"
        assert jobs[1].delayed is False
        assert "higher-priority" in (jobs[1].skip_reason or "")

    def test_repo_checks_cached_once_per_run(self, sample_repos, tmp_path):
        """Reachability + size are probed once per repo, shared across jobs."""
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        shared: dict = {}
        jobs = [
            Job(
                name=f"j{i}",
                snapshot_dir=str(snap_dir),
                source_subvolume=str(snap_dir),
                repo="primary",
                repos=sample_repos,
                config_path=str(tmp_path / "c.json"),
            )
            for i in range(3)
        ]
        reachable_mock = MagicMock(return_value=True)
        size_mock = MagicMock(return_value=123)
        for job in jobs:
            job.repo_state = shared
            job._check_repo_reachable = reachable_mock
            job._measure_repo_size = size_mock
            job._plan()

        assert reachable_mock.call_count == 1
        assert size_mock.call_count == 1

    def test_bump_repo_size_reflects_upload(self, mocked_backup_job):
        """Successful uploads grow the cached repo-size estimate."""
        mocked_backup_job.repo_state = {}
        state = mocked_backup_job._repo_state(mocked_backup_job.repo_obj)
        mocked_backup_job._bump_repo_size(mocked_backup_job.repo_obj, 1_000_000_000)
        assert state["size"] == 1_000_000_000


class TestDelayedHandling:
    def test_delay_persists_and_restores(self, sample_job, create_snapshots):
        """_delay() writes the delayed group; _load_delayed_state() restores it."""
        now = datetime.now(timezone.utc)
        snap = (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ")
        create_snapshots(snap, now - timedelta(hours=20))
        sample_job._delay("size_limit")

        fresh = Job(
            name=sample_job.name,
            snapshot_dir=sample_job.snapshot_dir,
            source_subvolume=sample_job.source_subvolume,
            repo="primary",
            repos=sample_job.repos,
            config_path=sample_job.config_path,
        )
        fresh._load_delayed_state()
        assert fresh.delayed is True
        assert fresh.delayed_reason == "size_limit"
        assert fresh.protected_snapshot == snap
        assert fresh.missed_slots == 1

    def test_retry_protected_snapshot_when_not_time(self, sample_job, create_snapshots):
        """A recovered over-limit job retries the exact protected snapshot."""
        now = datetime.now(timezone.utc)
        snap = (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ")
        create_snapshots(snap, now - timedelta(hours=20))
        sample_job._delay("size_limit")
        sample_job._check_repo_reachable = MagicMock(return_value=True)
        sample_job._measure_repo_size = MagicMock(return_value=0)
        sample_job._time_since_last_success = MagicMock(return_value=1)

        plan = sample_job._plan()

        assert plan == "backup"
        assert sample_job.planned_snapshot is not None
        assert sample_job.planned_snapshot.name == snap

    def test_success_clears_delayed_state(self, mocked_backup_job, create_snapshots):
        """A successful backup resets the delayed group."""
        now = datetime.now(timezone.utc)
        create_snapshots(
            (now - timedelta(hours=20)).strftime("%Y-%m-%d_%H:%MZ"),
            now - timedelta(hours=20),
        )
        mocked_backup_job.delayed = True
        mocked_backup_job.delayed_reason = "size_limit"
        mocked_backup_job.missed_slots = 3

        result = mocked_backup_job.run()

        assert result["status"] == "success"
        assert mocked_backup_job.delayed is False
        assert mocked_backup_job.delayed_reason is None
        assert mocked_backup_job.missed_slots == 0
        assert mocked_backup_job.protected_snapshot is None


class TestPriority:
    def test_effective_priority_escalates_with_missed_slots(self, sample_job):
        sample_job.priority = 5
        sample_job.missed_slots = 0
        assert _effective_priority(sample_job) == 5
        sample_job.missed_slots = 2
        assert _effective_priority(sample_job) == 15

    def test_missed_slots_break_priority_ties(self, sample_repos, tmp_path):
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        jobs = [
            Job(
                name=f"j{i}",
                snapshot_dir=str(snap_dir),
                source_subvolume=str(snap_dir),
                repo="primary",
                repos=sample_repos,
                config_path=str(tmp_path / "c.json"),
            )
            for i in range(2)
        ]
        jobs[0].priority = 10
        jobs[1].priority = 10
        jobs[1].missed_slots = 1
        ordered = sorted(jobs, key=_effective_priority, reverse=True)
        assert ordered[0].name == "j1"


class TestNameValidation:
    def test_invalid_job_name_rejected(self):
        with pytest.raises(ValidationError):
            SnapBakConfig(
                repos=[{"name": "primary", "path": "rclone:test"}],
                jobs=[
                    {
                        "name": "Bad Name!",
                        "snapshot_dir": "/tmp",
                        "source_subvolume": "home",
                        "repo": "primary",
                    }
                ],
            )


class TestPasswordFilePermissions:
    @pytest.mark.parametrize(
        ("mode", "wrong_gid", "expect_exit"),
        [
            (0o644, False, True),  # other-readable
            (0o660, False, True),  # group-writable
            (0o640, True, True),  # group-read but not the service group
            (0o640, False, False),  # group-read by the service group
            (0o600, False, False),  # owner-only
        ],
    )
    def test_password_file_permissions(
        self, sample_repos, tmp_path, monkeypatch, mode, wrong_gid, expect_exit
    ):
        secret = tmp_path / "secret"
        secret.write_text("pw\n")
        os.chown(secret, -1, os.getgid())
        secret.chmod(mode)
        if wrong_gid:
            real_gid = os.getgid()
            monkeypatch.setattr(snapbak_module.os, "getgid", lambda: real_gid + 1000)
        job = Job(
            name="t",
            snapshot_dir=str(tmp_path),
            source_subvolume=str(tmp_path),
            repo="primary",
            repos=sample_repos,
            config_path=str(tmp_path / "c.json"),
        )
        job.repo_obj.password_file = str(secret)

        if expect_exit:
            with pytest.raises(SystemExit) as ei:
                job._check_password_file_permissions()
            assert ei.value.code == EXIT_PERMISSION_ERROR
        else:
            job._check_password_file_permissions()


class TestGlobalLock:
    def test_acquire_and_release(self):
        fh = snapbak_module.acquire_global_lock(3600, 300)
        try:
            assert snapbak_module.GLOBAL_LOCK_FILE.exists()
            info = snapbak_module._read_lock_info()
            assert info is not None
            assert info["pid"] == os.getpid()
        finally:
            snapbak_module.release_global_lock(fh)
        assert not snapbak_module.GLOBAL_LOCK_FILE.exists()

    def test_write_lock_info_preserves_inode(self):
        """The lock is flock()ed on the file's inode, so writing must never
        replace the file (no tmp+rename) or the lock would be orphaned."""
        snapbak_module.GLOBAL_LOCK_FILE.write_text("{}")
        ino_before = snapbak_module.GLOBAL_LOCK_FILE.stat().st_ino
        snapbak_module._write_lock_info("data", "2026-01-01_00:00")
        assert snapbak_module.GLOBAL_LOCK_FILE.stat().st_ino == ino_before

        info = snapbak_module._read_lock_info()
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["current_job"] == "data"
        assert info["snapshot_name"] == "2026-01-01_00:00"

    def test_fresh_lock_contention_exits_system(self):
        """A fresh lock (< stuck_threshold) means another instance is live.
        The parent holds it via the real acquire_global_lock(); a second
        acquire in the same process hits a separate open file description."""
        fh = snapbak_module.acquire_global_lock(3600, 300)
        try:
            with pytest.raises(SystemExit) as ei:
                snapbak_module.acquire_global_lock(3600, 300)
            assert ei.value.code == EXIT_SYSTEM
        finally:
            snapbak_module.release_global_lock(fh)

    def test_another_process_cannot_acquire_held_lock(self):
        """The critical test: one process holds the lock via the real
        acquire_global_lock(), and a *separate process* is denied it. flock()
        locks are tied to the open file description, so even the fork-inherited
        fd must not allow a new acquisition."""
        fh = snapbak_module.acquire_global_lock(3600, 300)
        try:
            pid = os.fork()
            if pid == 0:
                # Child: the lock is genuinely held by the parent, so
                # acquire must refuse (EXIT_SYSTEM), not succeed.
                try:
                    snapbak_module.acquire_global_lock(3600, 300)
                    os._exit(1)  # wrongly acquired the held lock
                except SystemExit as e:
                    os._exit(0 if e.code == EXIT_SYSTEM else 2)
                except Exception:
                    os._exit(3)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        finally:
            snapbak_module.release_global_lock(fh)

    def test_stale_lock_holder_dead_breaks_lock(self, monkeypatch):
        """A stale lock whose holder PID is gone is broken and acquisition retried."""
        snapbak_module._write_lock_info()
        old = time.time() - 7200
        os.utime(snapbak_module.GLOBAL_LOCK_FILE, (old, old))
        monkeypatch.setattr(snapbak_module, "_pid_alive", MagicMock(return_value=False))

        assert snapbak_module._resolve_lock_contention(3600, 300) is True
        assert not snapbak_module.GLOBAL_LOCK_FILE.exists()

    def test_stuck_alive_holder_exits_stuck_wait(self, monkeypatch):
        """Holder still alive past stuck_wait: give up with EXIT_STUCK_WAIT."""
        monkeypatch.setattr(snapbak_module, "_pid_alive", MagicMock(return_value=True))
        monkeypatch.setattr(snapbak_module, "STUCK_POLL_INTERVAL", 0)
        monkeypatch.setattr(snapbak_module.time, "sleep", lambda s: None)
        held = snapbak_module.acquire_global_lock(3600, 300)
        old = time.time() - 7200
        os.utime(snapbak_module.GLOBAL_LOCK_FILE, (old, old))
        try:
            with pytest.raises(SystemExit) as ei:
                snapbak_module._resolve_lock_contention(3600, 0)
            assert ei.value.code == EXIT_STUCK_WAIT
        finally:
            snapbak_module.release_global_lock(held)

    def test_stuck_lock_released_during_poll_proceeds(self, monkeypatch):
        """If the lock clears during the stuck-wait poll, proceed normally."""
        snapbak_module._write_lock_info()
        old = time.time() - 7200
        os.utime(snapbak_module.GLOBAL_LOCK_FILE, (old, old))
        monkeypatch.setattr(snapbak_module, "_pid_alive", MagicMock(return_value=True))
        monkeypatch.setattr(snapbak_module, "STUCK_POLL_INTERVAL", 0)
        monkeypatch.setattr(snapbak_module.time, "sleep", lambda s: None)

        assert snapbak_module._resolve_lock_contention(3600, 300) is True
        assert snapbak_module.GLOBAL_LOCK_FILE.exists()

    def test_touch_updates_lock_info(self):
        fh = snapbak_module.acquire_global_lock()
        try:
            snapbak_module.touch_global_lock("data", "2026-01-01_00:00")
            info = snapbak_module._read_lock_info()
            assert info is not None
            assert info["current_job"] == "data"
            assert info["snapshot_name"] == "2026-01-01_00:00"
        finally:
            snapbak_module.release_global_lock(fh)

    def test_touch_updates_mtime(self):
        fh = snapbak_module.acquire_global_lock()
        try:
            time.sleep(0.02)
            before = snapbak_module.GLOBAL_LOCK_FILE.stat().st_mtime
            snapbak_module.touch_global_lock()
            after = snapbak_module.GLOBAL_LOCK_FILE.stat().st_mtime
            assert after > before
        finally:
            snapbak_module.release_global_lock(fh)


class TestPidAlive:
    def test_dead_pid(self):
        assert snapbak_module._pid_alive(99999999) is False

    def test_alive_pid(self):
        assert snapbak_module._pid_alive(os.getpid()) is True

    def test_none(self):
        assert snapbak_module._pid_alive(None) is False

    def test_permission_error_means_alive(self, monkeypatch):
        def fake_kill(pid, sig):
            raise PermissionError

        monkeypatch.setattr(os, "kill", fake_kill)
        assert snapbak_module._pid_alive(12345) is True


class TestMainExitCodes:
    def _make_fake_job(self, name, result, priority=10, missed_slots=0):
        job = MagicMock()
        job.name = name
        job.priority = priority
        job.missed_slots = missed_slots
        job.run.return_value = result
        job._load_delayed_state = MagicMock()
        job._check_password_file_permissions = MagicMock()
        job.dry_run = False
        job.repo_state = {}
        return job

    def _run_main(self, jobs, tmp_path, monkeypatch):
        config = MagicMock()
        config.mount_base = str(tmp_path)
        config.stuck_threshold = 3600
        config.stuck_wait = 300
        config.restic_update = "never"
        config.resolve_jobs.return_value = jobs
        monkeypatch.setattr(snapbak_module, "load_config", lambda p: config)
        monkeypatch.setattr(
            snapbak_module, "acquire_global_lock", lambda *a, **k: object()
        )
        monkeypatch.setattr(snapbak_module, "release_global_lock", lambda fh: None)
        monkeypatch.setattr(snapbak_module, "_run_restic_update", lambda: None)
        monkeypatch.setattr(
            sys, "argv", ["snap-bak", "-c", str(tmp_path / "config.json")]
        )
        try:
            snapbak_module._main_impl()
        except SystemExit as ei:
            return ei.code
        return None

    def test_failed_exits_permanent(self, tmp_path, monkeypatch):
        code = self._run_main(
            [self._make_fake_job("a", {"status": "failed"})], tmp_path, monkeypatch
        )
        assert code == EXIT_PERM_FAILED

    def test_timeout_exits_permanent(self, tmp_path, monkeypatch):
        code = self._run_main(
            [self._make_fake_job("a", {"status": "skipped", "timeout": True})],
            tmp_path,
            monkeypatch,
        )
        assert code == EXIT_PERM_TIMEOUT

    def test_unreachable_delay_exits_temp(self, tmp_path, monkeypatch):
        code = self._run_main(
            [
                self._make_fake_job(
                    "a", {"status": "delayed", "delayed_reason": "unreachable"}
                )
            ],
            tmp_path,
            monkeypatch,
        )
        assert code == EXIT_TEMP_UNREACHABLE

    def test_size_limit_delay_exits_temp(self, tmp_path, monkeypatch):
        code = self._run_main(
            [
                self._make_fake_job(
                    "a", {"status": "delayed", "delayed_reason": "size_limit"}
                )
            ],
            tmp_path,
            monkeypatch,
        )
        assert code == EXIT_TEMP_SIZE_LIMIT

    def test_jobs_ordered_by_effective_priority(self, tmp_path, monkeypatch):
        """_main_impl schedules delayed (higher effective priority) jobs first.

        Backups run in parallel, so we make the low-priority job sleep to make
        the high-priority one deterministically finish first.
        """
        order: list[str] = []
        low = self._make_fake_job(
            "low", {"status": "skipped"}, priority=10, missed_slots=0
        )
        high = self._make_fake_job(
            "high", {"status": "skipped"}, priority=10, missed_slots=1
        )

        def low_run():
            time.sleep(0.05)
            order.append("low")
            return {"status": "skipped"}

        low.run.side_effect = low_run
        high.run.side_effect = lambda: order.append("high") or {"status": "skipped"}
        self._run_main([low, high], tmp_path, monkeypatch)
        assert order == ["high", "low"]

    def test_prune_runs_once_per_repo(self, tmp_path, monkeypatch):
        """forget runs per job; prune runs once even for multiple jobs/repo."""
        a = self._make_fake_job("a", {"status": "success"})
        b = self._make_fake_job("b", {"status": "success"})
        a.retention.return_value = {"status": "retention_ok"}
        b.retention.return_value = {"status": "retention_ok"}
        shared_repo = MagicMock()
        shared_repo.path = "rclone:shared:repo"
        a.repo_obj = shared_repo
        b.repo_obj = shared_repo
        self._run_main([a, b], tmp_path, monkeypatch)
        assert a.retention.called and b.retention.called
        prune_calls = a.prune_once.call_count + b.prune_once.call_count
        assert prune_calls == 1


class TestRetention:
    def test_retention_ok(self, sample_job):
        sample_job.backup_retention = {"daily": 6}
        sample_job._run = MagicMock()
        sample_job._write_status = MagicMock()
        res = sample_job.retention()
        assert res == {"job": sample_job.name, "status": "retention_ok"}
        sample_job._run.assert_called_once()
        # forget must NOT carry --prune (prune runs separately, once).
        assert "--prune" not in sample_job._run.call_args[0][0]

    def test_retention_failed_lock(self, sample_job):
        sample_job.backup_retention = {"daily": 6}
        sample_job._run = MagicMock(side_effect=RepoLockedError("locked"))
        sample_job._write_status = MagicMock()
        res = sample_job.retention()
        assert res["status"] == "retention_failed"
        # Distinct status (not generic 'failed'); backup data is safe.
        status_call = sample_job._write_status.call_args
        assert status_call.args[0] == "retention_failed"
        assert status_call.kwargs["failure_category"] == FAIL_LOCKED

    def test_retention_no_policy(self, sample_job):
        sample_job.backup_retention = {}
        sample_job._run = MagicMock()
        assert sample_job.retention() is None
        sample_job._run.assert_not_called()


class TestMountHelpers:
    def test_mounted_source_matches(self, monkeypatch):
        mi = "23 32 0:21 /snap /mnt/bak/data ro - btrfs /dev/sda1\n"
        monkeypatch.setattr(snapbak_module, "_mountinfo_text", lambda: mi)
        src = _mounted_source(Path("/mnt/bak/data"))
        assert src == Path("/dev/sda1")

    def test_mounted_source_none(self, monkeypatch):
        mi = "23 32 0:21 /other /mnt/elsewhere ro - btrfs /dev/sda1\n"
        monkeypatch.setattr(snapbak_module, "_mountinfo_text", lambda: mi)
        assert _mounted_source(Path("/mnt/bak/data")) is None

    def test_restic_running_true(self, monkeypatch, tmp_path):
        p = tmp_path / "1" / "cmdline"
        p.parent.mkdir()
        p.write_bytes(b"restic\0backup\0")
        monkeypatch.setattr(snapbak_module, "_proc_dir", lambda: tmp_path)
        assert restic_running() is True

    def test_restic_running_false_for_orchestrator(self, monkeypatch, tmp_path):
        p = tmp_path / "1" / "cmdline"
        p.parent.mkdir()
        p.write_bytes(b"python3\0snap_bak.py\0")
        monkeypatch.setattr(snapbak_module, "_proc_dir", lambda: tmp_path)
        assert restic_running() is False


class TestRunCacheLogging:
    """restic cache complaints in stderr must be surfaced at WARNING (not
    buried at debug), since the cache dir is provisioned by setup rather than
    checked at runtime."""

    class _FakeResult:
        def __init__(self, stdout="", stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = 0

    def _run_restic(self, monkeypatch, sample_job, stderr):
        def fake_run(cmd, **kw):
            return self._FakeResult(stderr=stderr)

        monkeypatch.setattr(snapbak_module.subprocess, "run", fake_run)
        sample_job._run(["restic", "-r", "x:dir", "snapshots"])

    def test_restic_cache_complaint_surfaced(self, monkeypatch, sample_job, caplog):
        with caplog.at_level("WARNING"):
            self._run_restic(
                monkeypatch,
                sample_job,
                "warning: unable to locate cache directory /x (disabling caching)\n",
            )
        assert any("restic cache" in r.message for r in caplog.records)

    def test_restic_benign_cache_line_not_surfaced(
        self, monkeypatch, sample_job, caplog
    ):
        with caplog.at_level("WARNING"):
            self._run_restic(
                monkeypatch,
                sample_job,
                "using cache dir /opt/snap-bak/cache\n",
            )
        assert not any("restic cache" in r.message for r in caplog.records)

    def test_non_restic_cache_stderr_not_surfaced(
        self, monkeypatch, sample_job, caplog
    ):
        def fake_run(cmd, **kw):
            return self._FakeResult(stderr="unable to open cache\n")

        monkeypatch.setattr(snapbak_module.subprocess, "run", fake_run)
        with caplog.at_level("WARNING"):
            sample_job._run(["echo", "hello"])
        assert not any("restic cache" in r.message for r in caplog.records)
