"""Tests for snap_bak.job: errors, lock helpers, status, Runner decisions."""

import subprocess
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from snap_bak import config
from snap_bak import job as job_mod
from snap_bak.job import (
    CredentialsError,
    DelayState,
    RemoteUnavailableError,
    RepoLockedError,
    Result,
    ResultStatus,
    Runner,
    categorize_restic_error,
    check_password_file,
    delay_from_status,
    mounted_source,
    read_status,
    restic_running,
    write_status,
)


def _runner(
    monkeypatch,
    tmp_path,
    *,
    size_limit=0,
    reachable=True,
    size=0,
    allocated=False,
    delayed=False,
    missed=0,
    protected=None,
    time_for_backup=True,
    in_window=True,
):
    cfg = config.Config(
        repos=[{"name": "r", "path": "x", "size_limit": size_limit}],
        jobs=[
            {
                "name": "j",
                "snapshot_dir": str(tmp_path),
                "source_subvolume": str(tmp_path),
                "repo": "r",
            }
        ],
    )
    rj = cfg.resolve()[0]
    runner = Runner(
        rj,
        dry_run=True,
        repo_state={
            "r": {"reachable": reachable, "size": size, "allocated": allocated}
        },
    )
    runner.delay = DelayState(
        delayed=delayed, missed_slots=missed, protected_snapshot=protected
    )
    monkeypatch.setattr(runner, "_time_for_backup", lambda: time_for_backup)
    monkeypatch.setattr(runner, "within_backup_window", lambda: in_window)
    return runner


# --- error categorization ---


class TestCategorize:
    def _err(self, rc):
        return subprocess.CalledProcessError(rc, "restic", output="", stderr="boom")

    def test_locked(self):
        e = categorize_restic_error(self._err(11))
        assert isinstance(e, RepoLockedError)
        assert e.category == "locked"

    def test_credentials(self):
        assert isinstance(categorize_restic_error(self._err(12)), CredentialsError)

    def test_other_is_remote(self):
        assert isinstance(categorize_restic_error(self._err(3)), RemoteUnavailableError)

    def test_message(self):
        assert "rc=12" in str(categorize_restic_error(self._err(12)))


# --- status persistence ---


class TestStatus:
    def test_read_missing(self, tmp_path):
        assert read_status(tmp_path / "nope.json") is None

    def test_read_invalid(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{not json")
        assert read_status(p) is None

    def test_round_trip(self, tmp_path):
        delay = DelayState(
            delayed=True, reason="size_limit", protected_snapshot="snap", missed_slots=3
        )
        p = tmp_path / "s.json"
        write_status(
            p,
            job="j",
            status="delayed",
            message="m",
            delay=delay,
            snapshot_name="snap",
            restic_snapshot_id="abc",
        )
        data = read_status(p)
        assert data["job"] == "j"
        assert data["status"] == "delayed"
        assert data["snapshot_name"] == "snap"
        assert data["restic_snapshot_id"] == "abc"
        assert data["delayed"] is True
        assert data["missed_slots"] == 3

    def test_delay_from_status(self):
        delay = delay_from_status(
            {
                "status": "delayed",
                "delayed_reason": "unreachable",
                "protected_snapshot": "s",
                "missed_slots": 2,
            }
        )
        assert delay.delayed is True
        assert delay.reason == "unreachable"
        assert delay.protected_snapshot == "s"
        assert delay.missed_slots == 2

    def test_delay_from_missing(self):
        assert delay_from_status(None) == DelayState()

    def test_delay_reset(self):
        d = DelayState(delayed=True, reason="x", protected_snapshot="s", missed_slots=9)
        d.reset()
        assert d == DelayState()

    def test_snapshot_time_serialized(self, tmp_path):
        p = tmp_path / "s.json"
        t = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        write_status(
            p,
            job="j",
            status="success",
            message="m",
            delay=DelayState(),
            snapshot_time=t,
        )
        assert read_status(p)["snapshot_time"] == "2026-01-02_03:04Z"


# --- Result ---


class TestResult:
    def test_status_string_equality(self):
        assert Result("x", ResultStatus.SKIPPED).status == "skipped"

    def test_defaults(self):
        r = Result("x", ResultStatus.SUCCESS)
        assert r.reason is None and r.timeout is False


# --- password gate ---


class TestCheckPasswordFile:
    def _repo(self, password_file):
        return config.Repo(name="r", path="x", password_file=password_file)

    def test_no_password_file_is_noop(self, monkeypatch):
        check_password_file(self._repo(""))

    def _patch_stat(self, monkeypatch, mode, gid):
        monkeypatch.setattr(job_mod.os, "getgid", lambda: 1000)
        monkeypatch.setattr(
            job_mod.os,
            "stat",
            lambda p: types.SimpleNamespace(st_mode=mode, st_gid=gid),
        )

    def test_owner_only_is_safe(self, monkeypatch):
        self._patch_stat(monkeypatch, 0o600, 1000)
        check_password_file(self._repo("/p"))

    def test_group_read_matching_gid_is_safe(self, monkeypatch):
        self._patch_stat(monkeypatch, 0o640, 1000)
        check_password_file(self._repo("/p"))

    def test_other_read_rejected(self, monkeypatch):
        self._patch_stat(monkeypatch, 0o644, 1000)
        with pytest.raises(SystemExit) as exc:
            check_password_file(self._repo("/p"))
        assert exc.value.code == config.EXIT_PERMISSION_ERROR

    def test_group_mismatch_rejected(self, monkeypatch):
        self._patch_stat(monkeypatch, 0o640, 2000)
        with pytest.raises(SystemExit) as exc:
            check_password_file(self._repo("/p"))
        assert exc.value.code == config.EXIT_PERMISSION_ERROR

    def test_group_write_rejected(self, monkeypatch):
        self._patch_stat(monkeypatch, 0o660, 1000)
        with pytest.raises(SystemExit):
            check_password_file(self._repo("/p"))


# --- /proc helpers ---


def _fake_proc(tmp_path, entries):
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, cmdline in entries.items():
        d = proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(cmdline)
    return proc


class TestResticRunning:
    def test_detects_restic(self, tmp_path, monkeypatch):
        proc = _fake_proc(tmp_path, {"1": b"restic\0backup\0", "2": b"python3\0x\0"})
        monkeypatch.setattr(job_mod, "_proc_dir", lambda: proc)
        assert restic_running() is True

    def test_ignores_orchestrator(self, tmp_path, monkeypatch):
        proc = _fake_proc(tmp_path, {"1": b"python3\0/opt/snap-bak/cli.py\0restic\0"})
        monkeypatch.setattr(job_mod, "_proc_dir", lambda: proc)
        assert restic_running() is False

    def test_no_restic(self, tmp_path, monkeypatch):
        proc = _fake_proc(tmp_path, {"1": b"python3\0x\0"})
        monkeypatch.setattr(job_mod, "_proc_dir", lambda: proc)
        assert restic_running() is False

    def test_no_proc_dir_fails_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(job_mod, "_proc_dir", lambda: tmp_path / "missing")
        assert restic_running() is True


class TestMountedSource:
    def test_finds_source(self, monkeypatch):
        monkeypatch.setattr(
            job_mod,
            "_mountinfo_text",
            lambda: (
                "22 28 0:20 / /snap/mount rw,relatime shared:1 - btrfs /dev/sda1 rw\n"
            ),
        )
        assert mounted_source(Path("/snap/mount")) == Path("/dev/sda1")

    def test_no_match(self, monkeypatch):
        monkeypatch.setattr(
            job_mod,
            "_mountinfo_text",
            lambda: "22 28 0:20 / /other rw - btrfs /dev/sda1 rw\n",
        )
        assert mounted_source(Path("/snap/mount")) is None

    def test_read_error_is_none(self, monkeypatch):
        def boom():
            raise OSError

        monkeypatch.setattr(job_mod, "_mountinfo_text", boom)
        assert mounted_source(Path("/snap/mount")) is None


# --- Runner.decide ---


class TestDecide:
    def test_unreachable_delays(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, reachable=False)
        d = r.decide()
        assert d.action == "delayed" and d.reason == "unreachable"

    def test_size_limit_delays_and_allocates(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, size_limit=100, size=100)
        d = r.decide()
        assert d.action == "delayed" and d.reason == "size_limit"
        assert r.repo_state["r"]["allocated"] is True

    def test_size_limit_allocated_skips(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, size_limit=100, size=100, allocated=True)
        d = r.decide()
        assert d.action == "skip"
        assert r.delay.missed_slots == 1

    def test_outside_window_skips(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, in_window=False)
        d = r.decide()
        assert d.action == "skip" and d.reason == "outside backup window"

    def test_outside_window_skips_remote_checks(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, in_window=False)
        calls = {"reachable": 0, "size": 0}

        def spy_reachable(repo):
            calls["reachable"] += 1
            return True

        def spy_size(repo):
            calls["size"] += 1
            return 0

        monkeypatch.setattr(r, "_check_repo_reachable", spy_reachable)
        monkeypatch.setattr(r, "_measure_repo_size", spy_size)
        d = r.decide()
        assert d.action == "skip" and d.reason == "outside backup window"
        assert calls["reachable"] == 0 and calls["size"] == 0

    def test_time_for_backup(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, time_for_backup=True)
        d = r.decide()
        assert d.action == "backup" and d.snapshot is None
        assert r.delay.delayed is False

    def test_delayed_retry_returns_protected_snapshot(self, monkeypatch, tmp_path):
        r = _runner(
            monkeypatch, tmp_path, time_for_backup=False, delayed=True, protected="snap"
        )
        d = r.decide()
        assert d.action == "backup"
        assert d.snapshot == Path(str(tmp_path)) / "snap"

    def test_not_time_skips(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, time_for_backup=False)
        d = r.decide()
        assert d.action == "skip" and d.reason == "not time for backup"


# --- Runner.run orchestration ---


class TestRun:
    def test_retries_failed_backup(self, monkeypatch, tmp_path):
        cfg = config.Config(
            repos=[{"name": "r", "path": "x"}],
            jobs=[
                {
                    "name": "j",
                    "snapshot_dir": str(tmp_path),
                    "source_subvolume": str(tmp_path),
                    "repo": "r",
                }
            ],
        )
        rj = cfg.resolve()[0]
        (tmp_path / "snap").mkdir()
        write_status(
            rj.state_file,
            job="j",
            status="failed",
            message="x",
            delay=DelayState(),
            snapshot_name="snap",
        )

        runner = Runner(rj, dry_run=True)
        calls = {}

        def fake_backup(sp, retry=False):
            calls["retry"] = retry
            calls["path"] = sp
            return Result("j", ResultStatus.SUCCESS)

        monkeypatch.setattr(runner, "_backup_snapshot", fake_backup)
        res = runner.run()
        assert res.status == "success"
        assert calls["retry"] is True
        assert calls["path"] == tmp_path / "snap"

    def test_skip_returns_skipped(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, time_for_backup=False)
        res = r.run()
        assert res.status == "skipped"

    def test_effective_priority(self, monkeypatch, tmp_path):
        r = _runner(monkeypatch, tmp_path, missed=2)
        assert r.effective_priority() == 100 * (2 + 1)



def test_a_missing_snapshot_dir_is_caught_before_anything_is_written(
    tmp_path, monkeypatch, caplog
):
    """Config no longer checks `snapshot_dir` at load time, so this is the one
    place it has to be caught: it is the only code that writes into it."""
    import dataclasses

    from conftest import resolved_job

    rj, _src, _repo = resolved_job(
        tmp_path, monkeypatch, excludes_list=["*.tmp"]
    )
    broken = dataclasses.replace(
        rj, job=rj.job.model_copy(update={"snapshot_dir": tmp_path / "absent"})
    )
    with pytest.raises(SystemExit) as caught:
        Runner(broken).create_snapshot()
    assert caught.value.code == config.EXIT_CONFIG_ERROR
    assert "Snapshot directory does not exist" in caplog.text
