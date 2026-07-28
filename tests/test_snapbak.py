#!/usr/bin/env python3
"""Unit tests for snapbak.py — no remote restic, minimal IO."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.snapbak import (
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL_MIN,
    Job,
    Repo,
    SnapBakConfig,
    build_retention_datetimes,
    build_retention_exponential,
    build_snapshot_keeplist,
    load_config,
    Snapshot,
)


# --- Fixtures ---

@pytest.fixture
def sample_repos():
    return [
        Repo(name="primary", path="rclone:CONFIG_ENTRY:DIRECTORY", password_file="/home/user/.restic_password", size_limit=1099511627776, latency_ms=50, throughput_mbps=10),
        Repo(name="secondary", path="rclone:CONFIG_ENTRY:DIRECTORY", password_file="/home/user/.b2_password", size_limit=536870912000, latency_ms=200, throughput_mbps=5),
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


# --- Snapshot time parsing tests ---

class TestParseSnapshotTime:
    def test_yyyy_mm_dd_hh_mm_format(self, sample_job, tmp_path):
        snap = tmp_path / "snapshots" / "2024-01-15_10:30"
        snap.mkdir(parents=True)
        result = sample_job._parse_snapshot_time(snap)
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_invalid_format(self, sample_job, tmp_path):
        snap = tmp_path / "snapshots" / "random_name"
        snap.mkdir(parents=True)
        result = sample_job._parse_snapshot_time(snap)
        assert result is None

    def test_empty_name(self, sample_job, tmp_path):
        snap = tmp_path / "snapshots" / ""
        result = sample_job._parse_snapshot_time(snap)
        assert result is None


# --- Exponential cleanup tests ---

class TestExponentialCleanup:
    def test_keeps_correct_snapshots(self, sample_job, create_snapshots):
        """With snapshot_retention n=4, keep oldest in each tier."""
        now = datetime.now(timezone.utc)
        snapshot_times = {
            "20m": now - timedelta(minutes=20),
            "90m": now - timedelta(minutes=90),
            "3h": now - timedelta(hours=3),
            "7h": now - timedelta(hours=7),
            "12h": now - timedelta(hours=12),
            "20h": now - timedelta(hours=20),
        }
        snapshot_names = {}
        for label, dt in snapshot_times.items():
            name = dt.strftime("%Y-%m-%d_%H:%M")
            snapshot_names[label] = name
            create_snapshots(name, dt)

        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 4}}

        deleted = []
        sample_job._run = MagicMock(side_effect=lambda cmd, **kw: deleted.append(cmd[3]) if cmd[0] == "btrfs" else None)

        sample_job._cleanup_snapshots()

        kept_names = {snapshot_names["20m"], snapshot_names["90m"], snapshot_names["3h"], snapshot_names["7h"]}
        deleted_names = {snapshot_names["12h"], snapshot_names["20h"]}

        deleted_str = set(str(d) for d in deleted)
        for name in deleted_names:
            assert any(name in d for d in deleted_str), f"Expected {name} to be deleted"

        for name in kept_names:
            assert (Path(sample_job.snapshot_dir) / name).exists(), f"Expected {name} to be kept"

    def test_fewer_than_n_snapshots_keeps_all(self, sample_job, create_snapshots):
        """If fewer than N snapshots exist, keep all."""
        now = datetime.now(timezone.utc)
        create_snapshots("1h_ago", now - timedelta(hours=1))
        create_snapshots("2h_ago", now - timedelta(hours=2))

        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 5}}
        sample_job._run = MagicMock()

        sample_job._cleanup_snapshots()

        btrfs_calls = [c for c in sample_job._run.call_args_list if c.args[0][0] == "btrfs"]
        assert len(btrfs_calls) == 0

    def test_protected_snapshot_not_deleted(self, sample_job, create_snapshots):
        """The protected snapshot should never be deleted."""
        now = datetime.now(timezone.utc)
        create_snapshots("1h_ago", now - timedelta(hours=1))
        create_snapshots("2h_ago", now - timedelta(hours=2))
        create_snapshots("3h_ago", now - timedelta(hours=3))

        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 2}}
        deleted = []
        sample_job._run = MagicMock(side_effect=lambda cmd, **kw: deleted.append(cmd[2]) if cmd[0] == "btrfs" else None)

        sample_job._cleanup_snapshots(protected_snapshot="2h_ago")

        assert (Path(sample_job.snapshot_dir) / "2h_ago").exists()

    def test_zero_snapshots_to_keep_skips_cleanup(self, sample_job, create_snapshots):
        """If fn not in RETENTION_FN, skip cleanup entirely."""
        now = datetime.now(timezone.utc)
        create_snapshots("1h_ago", now - timedelta(hours=1))

        sample_job.snapshot_retention = {"fn": "invalid", "config": {}}
        sample_job._run = MagicMock()

        sample_job._cleanup_snapshots()

        btrfs_calls = [c for c in sample_job._run.call_args_list if c.args[0][0] == "btrfs"]
        assert len(btrfs_calls) == 0

    def test_non_standard_names_skipped(self, sample_job, create_snapshots):
        """Non-standard snapshot names should be skipped (not deleted)."""
        now = datetime.now(timezone.utc)
        create_snapshots("20240115_103000", now - timedelta(hours=1))
        create_snapshots("random_name", now - timedelta(hours=2))

        sample_job.snapshot_retention = {"fn": "exponential", "config": {"n": 5}}
        deleted = []
        sample_job._run = MagicMock(side_effect=lambda cmd, **kw: deleted.append(cmd[2]) if cmd[0] == "btrfs" else None)

        sample_job._cleanup_snapshots()

        assert not any("random_name" in d for d in deleted)


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
            jobs=[{"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary"}],
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
            repos=[{"name": "primary", "path": "rclone:test", "password_file": "/path/to/pass"}],
            jobs=[{"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary"}],
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
                jobs=[{"name": "test", "snapshot_dir": "/nonexistent/path", "source_subvolume": "data", "repo": "primary"}],
            )


# --- Repo object tests ---

class TestRepo:
    def test_repo_has_password_file(self):
        repo = Repo(name="primary", path="rclone:test", password_file="/path/to/pass")
        assert repo.password_file == "/path/to/pass"

    def test_repo_password_file_default_empty(self):
        repo = Repo(name="primary", path="rclone:test")
        assert repo.password_file == ""

    def test_repo_size_limit_default_zero(self):
        repo = Repo(name="primary", path="rclone:test")
        assert repo.size_limit == 0


# --- Job object tests ---

class TestJob:
    def test_job_repo_property_returns_repo_obj(self, sample_job):
        """self.repo_obj should return the Repo object."""
        repo = sample_job.repo_obj
        assert repo.name == "primary"
        assert repo.path == "rclone:CONFIG_ENTRY:DIRECTORY"

    def test_job_repo_path_returns_string(self, sample_job):
        """repo_path should return the path string."""
        assert sample_job.repo_path == "rclone:CONFIG_ENTRY:DIRECTORY"

    def test_job_repo_name_alias(self, sample_job):
        """repo_name should be an alias for repo field."""
        assert sample_job.repo_name == "primary"


# --- find_oldest_snapshot_within tests ---

class TestFindOldestSnapshotWithin:
    def test_finds_oldest_within_interval(self, sample_job, create_snapshots):
        now = datetime.now(timezone.utc)
        snap_30m = (now - timedelta(minutes=30)).strftime("%Y-%m-%d_%H:%M")
        snap_15m = (now - timedelta(minutes=15)).strftime("%Y-%m-%d_%H:%M")
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
        snap_name = (now - timedelta(minutes=30)).strftime("%Y-%m-%d_%H:%M")
        create_snapshots(snap_name, now - timedelta(minutes=30))
        create_snapshots("random_name", now - timedelta(minutes=15))

        result = sample_job._find_oldest_snapshot_within(3600)
        assert result is not None
        assert result.name == snap_name


# --- Throughput calculation tests ---

class TestThroughputCalculation:
    def test_basic_calculation(self, sample_job, tmp_path):
        """total_size=1GB, backup_time=10s → 800 Mbps."""
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        sample_job._save_config_throughput = MagicMock()

        sample_job._update_repo_throughput(repo, 1_000_000_000, 10.0)

        expected = round((1_000_000_000 * 8) / (10.0 * 1_000_000))
        assert repo.throughput_mbps == expected
        assert repo.throughput_mbps == 800

    def test_zero_backup_time_no_update(self, sample_job):
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        sample_job._save_config_throughput = MagicMock()

        sample_job._update_repo_throughput(repo, 1_000_000_000, 0.0)
        assert repo.throughput_mbps == 10  # unchanged

    def test_zero_total_size_no_update(self, sample_job):
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        sample_job._save_config_throughput = MagicMock()

        sample_job._update_repo_throughput(repo, 0, 10.0)
        assert repo.throughput_mbps == 10  # unchanged

    def test_saves_to_config(self, sample_job, tmp_path):
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "repos": [{"name": "primary", "path": "rclone:test", "throughput_mbps": 10}],
            "jobs": [],
        }))
        sample_job.config_path = str(config_path)

        sample_job._update_repo_throughput(repo, 1_000_000_000, 10.0)

        config = json.loads(config_path.read_text())
        assert config["repos"][0]["throughput_mbps"] == 800

    def test_config_not_writable_continues(self, sample_job, tmp_path):
        """If config file is not writable, log warning and continue."""
        repo = Repo(name="primary", path="rclone:test", throughput_mbps=10)
        config_path = tmp_path / "nonexistent_dir" / "config.json"
        sample_job.config_path = str(config_path)

        # Should not raise
        sample_job._update_repo_throughput(repo, 1_000_000_000, 10.0)
        assert repo.throughput_mbps == 800


# --- Failed backup retry tests ---

class TestFailedBackupRetry:
    def test_failed_status_triggers_retry(self, sample_job, create_snapshots, tmp_path):
        """When last status is 'failed' with snapshot_name, retry with that snapshot."""
        now = datetime.now(timezone.utc)
        snap_name = "20240115_103000"
        create_snapshots(snap_name, now - timedelta(hours=1))

        # Write a failed status file
        status_file = sample_job.status_file
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(json.dumps({
            "job": "test_job",
            "status": "failed",
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "message": "Backup failed",
            "snapshot_name": snap_name,
        }))

        # Mock the backup steps
        sample_job._preflight = MagicMock(return_value=1_000_000_000)
        sample_job._backup = MagicMock()
        sample_job._forget_and_prune = MagicMock()
        sample_job._cleanup_snapshots_exponential = MagicMock()
        sample_job._update_repo_throughput = MagicMock()
        sample_job._write_status = MagicMock()

        # Mock mount_and_lock to avoid actual mounting
        from contextlib import contextmanager
        @contextmanager
        def mock_mount(*args, **kwargs):
            yield Path("/tmp/fake_mount")
        sample_job.mount_and_lock = mock_mount

        result = sample_job.run()

        assert result["status"] == "success"
        # Verify backup was called (retry happened)
        sample_job._backup.assert_called_once()

    def test_failed_snapshot_gone_skips(self, sample_job, tmp_path):
        """When failed snapshot no longer exists, skip job."""
        now = datetime.now(timezone.utc)
        snap_name = "20240115_103000"

        # Write a failed status file
        status_file = sample_job.status_file
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(json.dumps({
            "job": "test_job",
            "status": "failed",
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "message": "Backup failed",
            "snapshot_name": snap_name,
        }))

        result = sample_job.run()

        assert result["status"] == "skipped"


# --- SnapBakConfig tests ---

class TestSnapBakConfig:
    def test_creates_jobs_with_repos(self, tmp_path):
        config = SnapBakConfig(
            repos=[
                {"name": "primary", "path": "rclone:CONFIG_ENTRY:DIRECTORY", "size_limit": 100, "latency_ms": 50, "throughput_mbps": 10},
            ],
            jobs=[
                {"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary",
                 "snapshot_retention": {"fn": "exponential", "config": {"n": 3}},
                 "backup_interval": 7200},
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
            jobs=[{"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary"}],
        )
        jobs = config.resolve_jobs(str(tmp_path / "config.json"))

        job = jobs[0]
        assert job.snapshot_retention == {"fn": "exponential", "config": {"n": 5}}
        assert job.backup_interval == 3600
        assert job.snapshot_interval == DEFAULT_SNAPSHOT_INTERVAL

    def test_repo_path_resolves(self, tmp_path):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:CONFIG_ENTRY:DIRECTORY"}],
            jobs=[{"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary"}],
        )
        jobs = config.resolve_jobs(str(tmp_path / "config.json"))
        assert jobs[0].repo_path == "rclone:CONFIG_ENTRY:DIRECTORY"

    def test_mount_base_default(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[{"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary"}],
        )
        assert config.mount_base == "/bak"

    def test_stuck_threshold_default(self):
        config = SnapBakConfig(
            repos=[{"name": "primary", "path": "rclone:test"}],
            jobs=[{"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary"}],
        )
        assert config.stuck_threshold == 3600


# --- load_config tests ---

class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "repos": [{"name": "primary", "path": "rclone:test"}],
            "jobs": [{"name": "test", "snapshot_dir": "/tmp", "source_subvolume": "data", "repo": "primary"}],
        }))
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
        with patch("scripts.snapbak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 12, 0, 0)
            assert sample_job._is_within_backup_window() is True

    def test_same_day_window_outside(self, sample_job):
        """Same-day window: 09:00-17:00, current time 18:00 → not allowed."""
        sample_job.backup_window_start = "09:00"
        sample_job.backup_window_end = "17:00"
        with patch("scripts.snapbak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 18, 0, 0)
            assert sample_job._is_within_backup_window() is False

    def test_overnight_window_inside_start(self, sample_job):
        """Overnight window: 22:00-06:00, current time 23:00 → allowed."""
        sample_job.backup_window_start = "22:00"
        sample_job.backup_window_end = "06:00"
        with patch("scripts.snapbak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 23, 0, 0)
            assert sample_job._is_within_backup_window() is True

    def test_overnight_window_inside_end(self, sample_job):
        """Overnight window: 22:00-06:00, current time 03:00 → allowed."""
        sample_job.backup_window_start = "22:00"
        sample_job.backup_window_end = "06:00"
        with patch("scripts.snapbak.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 15, 3, 0, 0)
            assert sample_job._is_within_backup_window() is True

    def test_overnight_window_outside(self, sample_job):
        """Overnight window: 22:00-06:00, current time 12:00 → not allowed."""
        sample_job.backup_window_start = "22:00"
        sample_job.backup_window_end = "06:00"
        with patch("scripts.snapbak.datetime") as mock_dt:
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
        with patch("scripts.snapbak.datetime") as mock_dt:
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
        now = datetime.now(timezone.utc)
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
    def test_keeps_oldest_in_each_tier(self):
        """With 4 tier boundaries, keeps the oldest snapshot in each tier."""
        now = datetime.now(timezone.utc)
        snaps = [
            Snapshot(Path("20m"), now - timedelta(minutes=20)),
            Snapshot(Path("90m"), now - timedelta(minutes=90)),
            Snapshot(Path("3h"), now - timedelta(hours=3)),
            Snapshot(Path("7h"), now - timedelta(hours=7)),
            Snapshot(Path("12h"), now - timedelta(hours=12)),
        ]
        rets = [
            now - timedelta(hours=1),
            now - timedelta(hours=2),
            now - timedelta(hours=4),
            now - timedelta(hours=8),
        ]
        keeps = build_snapshot_keeplist(snaps, rets)
        assert keeps == {Path("20m"), Path("90m"), Path("3h"), Path("7h")}

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
    def test_datetimes_retention_keeps_correct_snapshots(self, sample_job, create_snapshots):
        """Calendar-based retention keeps oldest in each tier."""
        now = datetime.now(timezone.utc)
        snapshot_times = {
            "15m": now - timedelta(minutes=15),
            "90m": now - timedelta(minutes=90),
            "3h": now - timedelta(hours=3),
            "15d": now - timedelta(days=15),
            "60d": now - timedelta(days=60),
        }
        for label, dt in snapshot_times.items():
            name = dt.strftime("%Y-%m-%d_%H:%M")
            create_snapshots(name, dt)

        sample_job.snapshot_retention = {
            "fn": "datetimes",
            "config": {"hourly": 2, "daily": 1},
        }
        deleted = []
        sample_job._run = MagicMock(side_effect=lambda cmd, **kw: deleted.append(cmd[3]) if cmd[0] == "btrfs" else None)

        sample_job._cleanup_snapshots()

        # hourly=2, daily=1 → boundaries at now, now-1h, now-2h
        # Boundary now: no candidates. Boundary now-1h: keeps 15m. Boundary now-2h: keeps 90m.
        # 3h, 15d, 60d should be deleted
        deleted_names = {Path(str(d)).name for d in deleted}
        assert len(deleted) == 3
        for label, dt in snapshot_times.items():
            name = dt.strftime("%Y-%m-%d_%H:%M")
            if label in ("3h", "15d", "60d"):
                assert name in deleted_names, f"Expected {name} ({label}) to be deleted"
            else:
                assert (Path(sample_job.snapshot_dir) / name).exists(), f"Expected {name} ({label}) to be kept"
