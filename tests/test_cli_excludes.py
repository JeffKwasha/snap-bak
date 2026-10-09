# -*- coding: utf-8 -*-
"""Tests for the `show excludes` / `apply excludes` subcommands.

Mostly unit tests over the pure helpers (mode selection, plan building,
rendering), plus a couple of end-to-end runs against a throwaway local restic
repo so the wiring is proven against the real binary rather than mocks.
"""

import json
import shutil

import pytest

from conftest import backup, init_repo, resolved_job, restic

from snap_bak import cli
from snap_bak import config
from snap_bak import excludes

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not available"
)


class TestFindSubcommand:
    def test_no_subcommand(self):
        assert cli._find_subcommand([]) is None
        assert cli._find_subcommand(["-n"]) is None
        assert cli._find_subcommand(["-c", "cfg"]) is None

    def test_subcommand_first(self):
        assert cli._find_subcommand(["show", "excludes"]) == 0
        assert cli._find_subcommand(["apply", "excludes", "--now"]) == 0

    def test_subcommand_after_global_flags(self):
        assert cli._find_subcommand(["-c", "cfg", "show", "excludes"]) == 2
        assert cli._find_subcommand(["-j", "Job", "--json", "apply", "excludes"]) == 3

    def test_stops_at_unknown_positional(self):
        assert cli._find_subcommand(["backup", "show"]) is None


class TestSelectedModes:
    def test_all_selectors(self):
        class A:
            all = True
            live = False
            snaps = False
            repo = False

        assert cli._selected_modes(A()) == ("live", "snaps", "repo")

    def test_explicit_selectors(self):
        class A:
            all = False
            live = False
            snaps = True
            repo = True

        assert cli._selected_modes(A()) == ("snaps", "repo")

    def test_no_selector_defaults_to_live(self):
        class A:
            all = False
            live = False
            snaps = False
            repo = False

        assert cli._selected_modes(A()) == ("live",)


class TestLintOutput:
    def test_lint_silent_when_clean(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert cli._do_lint([rj], as_json=False) == config.EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "*.tmp" not in out

    def test_lint_reports_invalid_and_anchored(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["bad/[", "/home/user/x", "*.tmp"]
        )
        cli._do_lint([rj], as_json=False)
        out = capsys.readouterr().out
        assert "bad/[" in out
        assert "/home/user/x" in out
        assert "*.tmp" not in out

    def test_lint_json_structure(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["bad/["])
        cli._do_lint([rj], as_json=True)
        data = json.loads(capsys.readouterr().out)
        assert data["lint"]["issues"][0]["kind"] == "invalid"


class TestLiveOutput:
    def test_live_lists_excluded_with_sizes(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        assert cli._do_live([rj], limit=None, as_json=False) == config.EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "a.tmp" in out
        assert "cache" in out
        assert "keep.txt" not in out

    def test_live_json(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        cli._do_live([rj], limit=None, as_json=True)
        data = json.loads(capsys.readouterr().out)
        entry = data["live"][0]
        assert entry["count"] == 1
        assert entry["entries"][0]["path"].endswith("a.tmp")
        assert entry["total_size"] == 3

    def test_live_limit_bounds_output_not_totals(self, tmp_path, monkeypatch, capsys):
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        for i in range(5):
            (src / f"f{i}.tmp").write_text("x")
        cli._do_live([rj], limit=2, as_json=False)
        out = capsys.readouterr().out
        assert "... 4 more" in out
        assert "f0.tmp" in out


class TestSnapsOutput:
    def test_snaps_table_with_per_snapshot_columns(self, tmp_path, monkeypatch, capsys):
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])

        for name in ("2026-10-05_10:00Z", "2026-10-06_10:00Z"):
            d = src.parent / "snaps" / name
            d.mkdir()
            (d / "a.tmp").write_text("x" * 10)
            (d / "keep.txt").write_text("k")
        cli._do_snaps([rj], limit=None, as_json=False)
        out = capsys.readouterr().out
        assert "2026-10-06_10:00Z" in out
        assert "2026-10-05_10:00Z" in out
        assert "BYTES TO EXCLUDE" in out

    def test_snaps_json(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        cli._do_snaps([rj], limit=None, as_json=True)
        data = json.loads(capsys.readouterr().out)
        assert data["snaps"][0]["snapshots"] == []


class TestRepoOutput:
    def test_repo_empty_when_no_snapshots(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert cli._do_repo([rj], limit=None, as_json=False) == config.EXIT_SUCCESS
        assert "no snapshots matched" in capsys.readouterr().out

    def test_repo_shows_stored_matches(self, tmp_path, monkeypatch, capsys):
        # Back up with only *.tmp excluded, so `cache` is still stored; the
        # current rules now also exclude `cache`, which is what apply removes.
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        init_repo(str(tmp_path / "repo"))
        backup(str(tmp_path / "repo"), src, "--exclude", "*.tmp")
        cli._do_repo([rj], limit=None, as_json=False)
        out = capsys.readouterr().out
        assert "cache" in out
        assert "BYTES TO EXCLUDE" in out


class TestApplyDryRun:
    def test_dry_run_reports_plan_without_touching_repo(
        self, tmp_path, monkeypatch, capsys
    ):
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        init_repo(str(tmp_path / "repo"))
        backup(str(tmp_path / "repo"), src, "--exclude", "*.tmp")
        before = restic(str(tmp_path / "repo"), "snapshots", "--json").stdout

        assert (
            cli._do_apply([rj], now=False, later=False, as_json=False, limit=None)
            == config.EXIT_SUCCESS
        )
        out = capsys.readouterr().out
        assert "would rewrite" in out
        assert "cache" in out

        # The repo is untouched by a dry run.
        after = restic(str(tmp_path / "repo"), "snapshots", "--json").stdout
        assert before == after


class TestApplyNow:
    def test_apply_now_rewrites_and_prunes(self, tmp_path, monkeypatch, capsys):
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        init_repo(str(tmp_path / "repo"))
        backup(str(tmp_path / "repo"), src, "--exclude", "*.tmp")

        assert (
            cli._do_apply([rj], now=True, later=False, as_json=False, limit=None)
            == config.EXIT_SUCCESS
        )
        out = capsys.readouterr().out
        assert "rewrote 1 snapshot(s)" in out

        # The stored snapshot no longer holds the excluded directory.
        snaps = json.loads(restic(str(tmp_path / "repo"), "snapshots", "--json").stdout)
        result = restic(str(tmp_path / "repo"), "ls", "--long", snaps[0]["id"])
        stored = {p for p, _s, _d in excludes.parse_ls_long(result.stdout)}
        assert not any(p.endswith("cache/big.bin") for p in stored)

        assert any(p.endswith("keep.txt") for p in stored)

    def test_apply_now_noop_when_nothing_to_remove(
        self, tmp_path, monkeypatch, capsys
    ):
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        init_repo(str(tmp_path / "repo"))
        backup(str(tmp_path / "repo"), src, "--exclude", "*.tmp", "--exclude", "cache")
        assert (
            cli._do_apply([rj], now=True, later=False, as_json=False, limit=None)
            == config.EXIT_SUCCESS
        )
        assert "skipped" in capsys.readouterr().out

    def test_apply_now_noop_when_repo_has_no_snapshots(
        self, tmp_path, monkeypatch, capsys
    ):
        """No repo means no snapshots, which means nothing to remove — a noop,
        not a failure. The preflight only matters once there is work to do."""
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert (
            cli._do_apply([rj], now=True, later=False, as_json=False, limit=None)
            == config.EXIT_SUCCESS
        )
        assert "skipped" in capsys.readouterr().out

    def test_preflight_refuses_cleanly_when_repo_missing(
        self, tmp_path, monkeypatch, capsys
    ):
        """The preflight must refuse before anything is modified, naming the
        restic exit code so a caller can tell why."""
        from snap_bak import apply as apply_mod
        from snap_bak import job as job_mod

        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        result = apply_mod.preflight(job_mod.Runner(rj))
        assert result is not None
        # A repo that is missing may well be there on the next window, so this is
        # a temporary problem rather than a permanent one.
        assert result.status == job_mod.ResultStatus.DELAYED
        assert "nothing changed" in result.reason
        # restic: 10 = repository does not exist.
        assert "rc=10" in result.reason


class TestQueueing:
    def test_later_queues_the_rewrite(self, tmp_path, monkeypatch, capsys):
        from conftest import backup, init_repo

        from snap_bak import apply as apply_mod
        from snap_bak import todo as todo_mod

        rj, src, repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        assert (
            cli._do_apply([rj], now=False, later=True, as_json=False, limit=None)
            == config.EXIT_SUCCESS
        )
        queued = todo_mod.queued_tasks(rj.state_file)
        assert [e["task"] for e in queued] == [apply_mod.TASK_NAME]

    def test_later_refuses_when_nothing_would_be_removed(
        self, tmp_path, monkeypatch, caplog
    ):
        """An empty repo means the store went empty or retention took everything.
        That is a problem to notice, not a rewrite to schedule — and the user
        would have seen it from `show excludes --repo` already."""
        from conftest import init_repo

        rj, _src, repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        init_repo(repo)

        assert (
            cli._do_apply([rj], now=False, later=True, as_json=False, limit=None)
            == config.EXIT_ERROR
        )
        assert "refusing to queue" in caplog.text

    def test_later_twice_is_an_error(self, tmp_path, monkeypatch, capsys):
        from conftest import backup, init_repo

        from snap_bak import todo as todo_mod

        rj, src, repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        cli._do_apply([rj], now=False, later=True, as_json=False, limit=None)
        capsys.readouterr()

        assert (
            cli._do_apply([rj], now=False, later=True, as_json=False, limit=None)
            == config.EXIT_ERROR
        )
        assert len(todo_mod.queued_tasks(rj.state_file)) == 1

    def test_now_and_later_are_mutually_exclusive(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert cli._do_apply([rj], now=True, later=True, as_json=False, limit=None)
        assert config.EXIT_ERROR


class TestHalfWindowRemaining:
    """The gate is 'at least half the window remains', evaluated against the
    current local time — so the clock is pinned rather than assumed."""

    class _RJ:
        def __init__(self, window):
            self.window = window

    def test_no_window_is_always_true(self):
        assert cli.job.half_window_remaining(self._RJ(None)) is True

    def _at(self, monkeypatch, hour, minute):
        from datetime import datetime as real_datetime

        class FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 1, hour, minute)

        monkeypatch.setattr(cli.job, "datetime", FakeDatetime)

    def test_half_remaining_is_true(self, monkeypatch):
        from snap_bak.config import Window

        self._at(monkeypatch, 6, 0)
        # 00:00-12:00 window at 06:00: half elapsed, half remains.
        assert cli.job.half_window_remaining(self._RJ(Window(0, 12 * 60))) is True

    def test_mostly_elapsed_is_false(self, monkeypatch):
        from snap_bak.config import Window

        self._at(monkeypatch, 6, 0)
        # 00:00-08:00 window at 06:00: 6h elapsed of 8h, only 2h left.
        assert cli.job.half_window_remaining(self._RJ(Window(0, 8 * 60))) is False

    def test_overnight_window(self, monkeypatch):
        from snap_bak.config import Window

        # 22:00-06:00 window. At 23:00, 1h elapsed of 8h -> 7h left -> True.
        self._at(monkeypatch, 23, 0)
        assert cli.job.half_window_remaining(self._RJ(Window(22 * 60, 6 * 60))) is True
        # At 06:00 the window has just ended: nothing left -> False.
        self._at(monkeypatch, 6, 0)
        assert cli.job.half_window_remaining(self._RJ(Window(22 * 60, 6 * 60))) is False
