"""Tests for snap_bak.job: errors, lock helpers, status, Runner decisions."""

import json
import logging
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import install_fake_restic, resolved_job

from snap_bak import __version__, config
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
    cfg = config.Config.model_validate({
              "repos": [{"name": "r", "path": "x", "size_limit": size_limit}],
              "jobs": [
            {
                "name": "j",
                "snapshot_dir": str(tmp_path),
                "source_subvolume": str(tmp_path),
                "repo": "r",
            }
        ],
          })
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

    def test_wrapper_recovery_text_reaches_the_status_reason(self):
        stderr = (
            "sudo-exec: /usr/local/bin/restic has no effective "
            "cap_dac_read_search (absent); run update-restic\n"
        )
        err = categorize_restic_error(
            subprocess.CalledProcessError(5, "restic", stderr=stderr),
            Path("/mnt/bak/home"),
        )
        assert "run update-restic" in str(err)

    def test_permission_errors_are_summarized(self):  # [[R-CLI-37]]
        stderr = (
            "error: open /mnt/bak/home/jk/a: permission denied\n"
            "error: open /mnt/bak/home/jk/b: permission denied"
        )
        err = categorize_restic_error(
            subprocess.CalledProcessError(3, "restic", stderr=stderr),
            Path("/mnt/bak/home"),
        )
        assert "lack permission to read home/jk (2 paths)" in str(err)

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
        assert data is not None
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
        t = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        write_status(
            p,
            job="j",
            status="success",
            message="m",
            delay=DelayState(),
            snapshot_time=t,
        )
        data = read_status(p)
        assert data is not None
        assert data["snapshot_time"] == "2026-01-02_03:04Z"


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
        return config.Repo.model_validate({
                   "name": "r",
                   "path": "x",
                   "password_file": password_file,
               })

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

    def test_detects_restic_reading_a_snap_bak_password_file(self, tmp_path, monkeypatch):
        proc = _fake_proc(
            tmp_path,
            {
                "1": b"restic\0-r\0rclone:filen:restic\0--password-file\0"
                b"/opt/snap-bak/passwords/restic_rclone_filen\0backup\0/bak/home\0"
            },
        )
        monkeypatch.setattr(job_mod, "_proc_dir", lambda: proc)
        assert restic_running() is True

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


class TestBackupReport:
    def test_reports_what_restic_counted(self, monkeypatch, tmp_path, caplog):
        runner = _runner(monkeypatch, tmp_path)
        summary = {
            "message_type": "summary",
            "files_new": 12,
            "files_changed": 3,
            "files_unmodified": 184203,
            "data_added": 41_200_000,
            "snapshot_id": "abc123",
        }
        monkeypatch.setattr(
            runner,
            "run_command",
            lambda cmd, **kw: types.SimpleNamespace(
                stdout=json.dumps(summary), stderr="", returncode=0
            ),
        )
        with caplog.at_level(logging.INFO):
            assert runner._backup(tmp_path / "mnt") == "abc123"
        assert "12 new" in caplog.text
        assert "3 changed" in caplog.text
        assert "184203 unmodified" in caplog.text
        assert "41.2 MB added" in caplog.text


class TestSnapshotGate:
    def test_dry_run_reports_no_wrapper_failure(self, monkeypatch, tmp_path, caplog):
        runner = _runner(monkeypatch, tmp_path)
        with caplog.at_level(logging.DEBUG):
            runner._should_create_snapshot()
        assert "power-saving gate not evaluated" in caplog.text
        assert "wrapper status unavailable" not in caplog.text


class TestRun:
    def test_retries_failed_backup(self, monkeypatch, tmp_path):
        cfg = config.Config.model_validate({
                  "repos": [{"name": "r", "path": "x"}],
                  "jobs": [
                {
                    "name": "j",
                    "snapshot_dir": str(tmp_path),
                    "source_subvolume": str(tmp_path),
                    "repo": "r",
                }
            ],
              })
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


class TestGlobalLockLog:
    def test_both_lines_name_the_version_and_the_build_time(  # [[R-LOCK-02]] [[R-CLI-38]]
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(config, "GLOBAL_LOCK_FILE", tmp_path / "snap-bak.lock")
        (tmp_path / "BUILD").write_text("2026-11-16T12:00:00Z\n")
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        job_mod._build_time.cache_clear()
        with caplog.at_level(logging.DEBUG):
            lock = job_mod.GlobalLock()
            lock.__enter__()
            lock.__exit__(None, None, None)
        job_mod._build_time.cache_clear()

        lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("Global lock")
        ]
        assert len(lines) == 2
        for line in lines:
            assert __version__ in line
            assert "build 2026-11-16T12:00:00Z" in line

    def test_without_a_stamp_it_falls_back_to_the_package_mtime(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        job_mod._build_time.cache_clear()
        job_mod._program_mtime.cache_clear()
        package = Path(job_mod.__file__).resolve().parent
        newest = max(p.stat().st_mtime for p in package.glob("*.py"))
        expected = datetime.fromtimestamp(newest, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert job_mod._build_time() == expected
        job_mod._build_time.cache_clear()
        job_mod._program_mtime.cache_clear()


class TestWrapperResticOps:
    def _record(self, runner):
        commands = []
        runner.dry_run = False
        runner.run_command = lambda cmd, **kw: (
            commands.append(cmd)
            or types.SimpleNamespace(stdout="", stderr="", returncode=0)
        )
        return commands

    def test_preflight_asks_the_wrapper(self, monkeypatch, tmp_path):
        runner = _runner(monkeypatch, tmp_path)
        commands = self._record(runner)
        runner._preflight(tmp_path / "mnt")
        assert commands == [
            [config.SUDO, config.SUDO_EXEC, "restic-backup-dry-run", "j"]
        ]

    def test_backup_asks_the_wrapper(self, monkeypatch, tmp_path):
        runner = _runner(monkeypatch, tmp_path)
        commands = self._record(runner)
        runner._backup(tmp_path / "mnt")
        assert commands == [[config.SUDO, config.SUDO_EXEC, "restic-backup", "j"]]

    def test_forget_asks_the_wrapper(self, monkeypatch, tmp_path):
        runner = _runner(monkeypatch, tmp_path)
        runner.job.backup_retention = {"daily": 1}
        commands = self._record(runner)
        runner._forget_only()
        assert commands == [[config.SUDO, config.SUDO_EXEC, "restic-forget", "j"]]

    def test_prune_asks_the_wrapper(self, monkeypatch, tmp_path):
        runner = _runner(monkeypatch, tmp_path)
        commands = self._record(runner)
        runner.prune()
        assert commands == [[config.SUDO, config.SUDO_EXEC, "restic-prune", "j"]]


class TestFakeResticHarness:
    """The fake behaves as a working restic, so callers can be tested fast."""

    def test_read_only_queries_succeed_without_a_real_repo(  # [[R-PRIV-23]]
        self, monkeypatch, tmp_path
    ):
        fake = install_fake_restic(tmp_path, monkeypatch)
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=[])
        runner = job_mod.Runner(rj)

        assert runner.repo_ping() is None
        assert runner.repo_snapshots() == []
        assert runner.repo_listing("f" * 64) == []

        subcommands = [r["argv"][3] for r in fake.records()]
        assert subcommands == ["cat", "snapshots", "ls"]

    def test_scenario_supplies_repository_data(  # [[R-PRIV-23]]
        self, monkeypatch, tmp_path
    ):
        fake = install_fake_restic(tmp_path, monkeypatch)
        fake.scenario(
            snapshots=[{"id": "a" * 64, "short_id": "abc", "tags": ["Test"]}],
            listing=[
                "-rw-r--r-- 0 0 4 2026-01-01 00:00 /bak/Test/keep.txt",
            ],
        )
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=[])
        runner = job_mod.Runner(rj)

        assert runner.repo_snapshots()[0]["short_id"] == "abc"
        listing = runner.repo_listing("a" * 64)
        assert listing == [("/bak/Test/keep.txt", 4, False)]


class TestLogSummary:
    def test_summarizes_permission_denials_by_top_directory(self, tmp_path):  # [[R-CLI-37]]
        stderr = (
            "error: open /mnt/bak/home/jk/a: permission denied\n"
            "error: open /mnt/bak/home/jk/b: permission denied\n"
            "error: open /mnt/bak/home/claude/snap: permission denied\n"
            "error: something else"
        )
        summary = job_mod._summarize_restic_stderr(stderr, Path("/mnt/bak/home"))
        assert summary == (
            "lack permission to read home/claude (1 path), home/jk (2 paths)"
        )

    def test_job_end_reports_the_verdict_and_the_snapshot(  # [[R-CLI-36]]
        self, monkeypatch, tmp_path, caplog
    ):
        runner = _runner(monkeypatch, tmp_path)
        runner.state_file.unlink(missing_ok=True)
        runner._run_backup_phase = lambda: Result(
            "j", ResultStatus.SKIPPED, reason="outside backup window"
        )
        with caplog.at_level(logging.INFO):
            runner.run()
        assert "JOB end status=skipped rc=0" in caplog.text
        assert "reason='outside backup window'" in caplog.text
        assert "snapshot=-" in caplog.text

    def test_job_end_reports_a_failed_code(  # [[R-CLI-36]]
        self, monkeypatch, tmp_path, caplog
    ):
        runner = _runner(monkeypatch, tmp_path)
        runner._run_backup_phase = lambda: Result(
            "j", ResultStatus.FAILED, reason="permission denied (rc=3)"
        )
        with caplog.at_level(logging.INFO):
            runner.run()
        assert "JOB end status=failed rc=41" in caplog.text

    def test_status_probe_logs_at_debug(self, monkeypatch, tmp_path):  # [[R-CLI-38]]
        runner = _runner(monkeypatch, tmp_path)
        runner.dry_run = False
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw)
            return types.SimpleNamespace(stdout="{}", stderr="", returncode=0)

        monkeypatch.setattr(runner, "run_command", fake_run)
        runner._wrapper_status()
        assert seen["log_level"] == logging.DEBUG
