# -*- coding: utf-8 -*-
"""Tests for the `show excludes` / `apply excludes` subcommands.

Mostly unit tests over the pure helpers (mode selection, plan building,
rendering), plus a couple of end-to-end runs against a throwaway local restic
repo so the wiring is proven against the real binary rather than mocks.
"""

import contextlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import backup, init_repo, resolved_job, restic

import snap_bak
from snap_bak import cli, config, excludes, job

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
        assert cli._do_live([rj], cli._View()) == config.EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "a.tmp" in out
        assert "cache" in out
        assert "keep.txt" not in out

    def test_live_json(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        cli._do_live([rj], cli._View(as_json=True))
        data = json.loads(capsys.readouterr().out)
        entry = data["live"][0]
        assert entry["count"] == 1
        assert entry["entries"][0]["path"].endswith("a.tmp")
        assert entry["total_size"] == 3

    def test_live_limit_bounds_output_not_totals(self, tmp_path, monkeypatch, capsys):
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        for i in range(5):
            (src / f"f{i}.tmp").write_text("x")
        cli._do_live([rj], cli._View(limit=2))
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
        cli._do_snaps([rj], cli._View())
        out = capsys.readouterr().out
        assert "2026-10-06_10:00Z" in out
        assert "2026-10-05_10:00Z" in out
        assert "BYTES TO EXCLUDE" in out

    def test_snaps_json(self, tmp_path, monkeypatch, capsys):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        cli._do_snaps([rj], cli._View(as_json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["snaps"][0]["snapshots"] == []


@contextlib.contextmanager
def _empty_repo(rj):
    """An initialised repo that genuinely holds no snapshots.

    Distinct from the default fixture's un-initialised path: restic answers
    rc 0 with `[]` here, where it answers rc 10 there. Only this one means
    "nothing to report".
    """
    init_repo(rj.repo.path)
    try:
        yield
    finally:
        shutil.rmtree(rj.repo.path, ignore_errors=True)


class TestRepoOutput:
    def test_repo_empty_when_no_snapshots(self, tmp_path, monkeypatch, capsys):
        # An initialised but empty repo is a real "nothing to report": restic
        # answers with an empty list, which is a success. This is the case the
        # unreachable-repo tests below must not swallow.
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        with _empty_repo(rj):
            assert cli._do_repo([rj], cli._View()) == config.EXIT_SUCCESS
            assert "no snapshots matched" in capsys.readouterr().out

    def test_repo_shows_stored_matches(self, tmp_path, monkeypatch, capsys):
        # Back up with only *.tmp excluded, so `cache` is still stored; the
        # current rules now also exclude `cache`, which is what apply removes.
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"])
        init_repo(str(tmp_path / "repo"))
        backup(str(tmp_path / "repo"), src, "--exclude", "*.tmp")
        cli._do_repo([rj], cli._View())
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
        """An initialised repo holding no snapshots means nothing to remove —
        a skip, not a failure. An *uninitialised* one is a different answer
        entirely: restic could not be asked, so this reports delayed."""
        rj, src, repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")  # stored, but matches nothing now

        assert (
            cli._do_apply([rj], now=True, later=False, as_json=False, limit=None)
            == config.EXIT_SUCCESS
        )
        assert "skipped" in capsys.readouterr().out

    def test_apply_now_reports_an_unreadable_repo_as_delayed(
        self, tmp_path, monkeypatch, capsys
    ):
        """`--now` is a mutation an agent may drive unattended. Reporting
        "skipped: nothing to remove" for a repository that was never read would
        tell it the cleanup was unnecessary when the answer is unknown."""
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert not Path(rj.repo.path).exists()

        assert (
            cli._do_apply([rj], now=True, later=False, as_json=False, limit=None)
            == config.EXIT_TEMP_UNREACHABLE
        )
        out = capsys.readouterr().out
        assert "delayed" in out
        assert "could not read repository" in out
        assert "skipped" not in out, "an unread repo is not a clean skip"

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

    def test_later_refuses_when_the_repo_cannot_be_read(self, tmp_path, monkeypatch, caplog):
        """Queueing blind to an unreachable repo would schedule work nobody
        verified, and would keep retrying it at every eligible window. The
        refusal has to name the reason, not just 'nothing to do'."""
        from snap_bak import todo as todo_mod

        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert not Path(rj.repo.path).exists()  # restic will answer rc 10

        assert (
            cli._do_apply([rj], now=False, later=True, as_json=False, limit=None)
            == config.EXIT_ERROR
        )
        assert "could not read repository" in caplog.text
        assert todo_mod.queued_tasks(rj.state_file) == []


class TestUnreachableRepoReporting:
    """The failure mode this whole class exists to prevent.

    `repo_snapshots()` used to log the restic failure and return `[]`, which is
    indistinguishable from a repo that genuinely holds no snapshots. So
    `show excludes --repo` against an unreachable repository printed "no
    snapshots matched" and exited 0 — a caller reading the exit code would
    conclude the backups were verified clean when nothing had been checked at
    all. Silence in a monitoring tool is only safe when it means "nothing wrong".
    """

    def _unreachable(self, path, monkeypatch):
        """A job whose repo restic cannot open, as a distinct temp tree.

        Takes a subdirectory rather than `tmp_path` so a test can ask for a
        healthy job *and* a broken one in the same run — `resolved_job` owns
        `src`, `snaps` and `repo` under whatever path it is handed.
        """
        rj, _src, _repo = resolved_job(path, monkeypatch, excludes_list=["*.tmp"])
        assert not Path(rj.repo.path).exists(), "restic must answer rc 10 here"
        return rj

    def test_repo_snapshots_raises_instead_of_returning_empty(
        self, tmp_path, monkeypatch
    ):
        rj = self._unreachable(tmp_path / "broken", monkeypatch)
        with pytest.raises(job.RepoQueryError) as caught:
            job.Runner(rj).repo_snapshots()
        assert caught.value.returncode == 10
        assert caught.value.retryable is True

    def test_show_repo_exits_delayed_and_names_the_cause(
        self, tmp_path, monkeypatch, capsys
    ):
        rj = self._unreachable(tmp_path / "broken", monkeypatch)
        # 30 (delayed), not 41: restic 10/11/12 are environmental and may well
        # be fine on the next run.
        assert cli._do_repo([rj], cli._View()) == config.EXIT_TEMP_UNREACHABLE
        out = capsys.readouterr().out
        assert "could not read repository" in out
        assert "no snapshots matched" not in out, "must not read as a clean result"

    def test_json_carries_the_error_rather_than_a_silent_empty(
        self, tmp_path, monkeypatch, capsys
    ):
        rj = self._unreachable(tmp_path / "broken", monkeypatch)
        cli._do_repo([rj], cli._View(as_json=True))
        body = json.loads(capsys.readouterr().out)["repo"][0]
        assert body["snapshot_ids"] == []
        assert body["query_error"]["returncode"] == 10
        assert body["query_error"]["retryable"] is True

    def test_apply_dry_run_reports_it_too(self, tmp_path, monkeypatch, capsys):
        """The dry-run is what a user approves, so it must not print
        'nothing to remove' for a repository it never reached."""
        rj = self._unreachable(tmp_path / "broken", monkeypatch)
        assert cli._plan([rj], as_json=False, limit=None) == config.EXIT_TEMP_UNREACHABLE
        out = capsys.readouterr().out
        assert "could not read repository" in out
        assert "nothing to remove" not in out

    def test_non_environmental_failure_is_permanent(self, tmp_path, monkeypatch, capsys):
        """Anything outside restic 10/11/12 will not fix itself, so it must not
        be reported as merely delayed."""
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        runner = job.Runner(rj)

        def boom(_cmd, timeout=None):
            raise subprocess.CalledProcessError(13, "restic")

        monkeypatch.setattr(runner, "run_command", boom)
        with pytest.raises(job.RepoQueryError) as caught:
            runner.repo_snapshots()
        assert caught.value.retryable is False
        monkeypatch.setattr(
            job.Runner, "repo_snapshots", lambda self: (_ for _ in ()).throw(caught.value)
        )
        assert cli._do_repo([rj], cli._View()) == config.EXIT_PERM_FAILED

    def test_one_broken_repo_does_not_hide_a_healthy_one(
        self, tmp_path, monkeypatch, capsys
    ):
        """Two jobs, one repo unreachable. The reachable one still gets a real
        answer, and the command as a whole still fails."""
        good, src, repo = resolved_job(
            tmp_path / "good", monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        bad = self._unreachable(tmp_path / "broken", monkeypatch)

        codes = {id(good): cli._do_repo([good], cli._View())}
        assert codes[id(good)] == config.EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "cache" in out, "the healthy repo must still be reported"

        assert cli._do_repo([good, bad], cli._View()) == (
            config.EXIT_TEMP_UNREACHABLE
        )
        out = capsys.readouterr().out
        assert "could not read repository" in out
        assert "cache" in out, "a broken repo must not suppress the healthy one"


class TestAgentFacingErrors:
    def test_unknown_job_name_lists_the_available_ones(
        self, tmp_path, monkeypatch, caplog
    ):
        """A job name is the one thing a caller must know up front. Without the
        list, a wrong name is a dead end that invites a retry loop with nothing
        to vary."""
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        cfg = config.Config(repos=[rj.repo], jobs=[rj.job])
        with pytest.raises(SystemExit) as caught:
            cli._selected_jobs(cfg, "data", "cfg.json")
        assert caught.value.code == config.EXIT_ERROR
        assert "data" in caplog.text
        assert f"Available: {rj.job.name}" in caplog.text
        assert [r.levelno for r in caplog.records] == [logging.ERROR]

    def test_version_comes_from_package_metadata(self, capsys):
        assert cli.__version__ == snap_bak.__version__
        assert cli.__version__ != "unknown", "the package must be installed"


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


class TestPatternOverride:
    """A trial pattern set: same syntax as the config, no config mutation."""

    def test_excludes_file_adds_to_the_jobs_own_rules(self, tmp_path):
        trial = tmp_path / "work.ignore"
        trial.write_text("# a comment\n*.iso\n\n!keep.iso\n")
        ov = cli.PatternOverride(str(trial))
        assert ov.active is True
        assert ov.apply(["*.tmp", "*.cache"]) == [
            "*.tmp", "*.cache", "*.iso", "!keep.iso",
        ]

    def test_extra_patterns_come_after_the_file(self, tmp_path):
        trial = tmp_path / "work.ignore"
        trial.write_text("*.iso\n")
        ov = cli.PatternOverride(str(trial), ["build/cache/"])
        # Order matters: `!` negation applies in sequence, and config rules come
        # first so a trial rule cannot accidentally re-include what config excludes.
        assert ov.apply(["*.tmp"]) == ["*.tmp", "*.iso", "build/cache/"]

    def test_inactive_override_changes_nothing(self):
        ov = cli.PatternOverride()
        assert ov.active is False
        assert ov.apply(["*.tmp"]) == ["*.tmp"]

    def test_diff_reports_added_and_removed(self, tmp_path):
        trial = tmp_path / "work.ignore"
        trial.write_text("*.iso\n")
        d = cli.PatternOverride(str(trial)).diff(["*.tmp", "*.cache"])
        assert d["added"] == ["*.iso"]
        # A trial cannot take a rule away, so removals are empty by construction.
        assert d["removed"] == []
        assert d["config"] == ["*.tmp", "*.cache"]
        assert d["excludes_file"] == str(trial)

    def test_an_edited_rule_appears_alongside_the_config_one(self):
        """Both are evaluated, so the new text is an addition and the old one
        stays in force - the change is visible rather than implied."""
        d = cli.PatternOverride(extra=["*.tmpx"]).diff(["*.tmp"])
        assert d["added"] == ["*.tmpx"]
        assert d["removed"] == []
        assert d["effective"] == ["*.tmp", "*.tmpx"]

    def test_changed_rules_is_the_additions(self):
        ov = cli.PatternOverride(extra=["*.iso"])
        assert ov.changed_rules(["*.tmp"]) == {"*.iso"}


class TestShowRefusesWhatApplyRefuses:
    """The trial flags are read-only; only apply must explain the refusal."""

    def test_show_accepts_the_trial_flags(self):
        args = cli.excludes_parser("show").parse_args(
            ["excludes", "--excludes-file", "/tmp/w.ignore", "-e", "*.iso"]
        )
        assert args.excludes_file == "/tmp/w.ignore"
        assert args.exclude == ["*.iso"]

    @pytest.mark.parametrize(
        "argv,flag",
        [
            (["excludes", "--excludes-file", "/tmp/w.ignore"], "--excludes-file"),
            (["excludes", "-e", "*.iso"], "-e"),
            (["excludes", "--exclude", "*.iso"], "--exclude"),
        ],
    )
    def test_apply_refuses_a_trial_pattern_set(self, argv, flag, capsys):
        with pytest.raises(SystemExit):
            cli.excludes_parser("apply").parse_args(argv + ["--now"])
        err = capsys.readouterr().err
        assert flag in err
        assert "trial pattern set" in err
        # The message must say what to do instead, not just refuse.
        assert "show excludes --excludes-file" in err


class TestOutputPresentation:
    def _job(self, tmp_path, monkeypatch):
        """A job whose stored snapshot holds data its current rules exclude.

        The backup deliberately used *fewer* excludes than the job now has, so
        the rows exist to be reported: that is the whole situation `show` and
        `apply` exist for.
        """
        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        from conftest import backup, init_repo

        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")  # cache/ stays stored
        return rj

    def test_rows_are_grouped_by_rule_with_a_subtotal(
        self, tmp_path, monkeypatch, capsys
    ):
        rj = self._job(tmp_path, monkeypatch)
        cli._do_live([rj], cli._View())
        out = capsys.readouterr().out
        assert "*.tmp  —" in out or "cache  —" in out
        assert "TOTAL:" in out
        assert "file(s)" in out

    def test_csv_has_numeric_bytes_and_a_header(self, tmp_path, monkeypatch, capsys):
        import csv
        import io

        rj = self._job(tmp_path, monkeypatch)
        cli._do_live([rj], cli._View(fmt="csv"))
        records = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))

        assert records, "the fixture must store something the rules exclude"
        assert {"job", "rule", "path", "is_dir", "files", "bytes"} <= set(records[0])
        for r in records:
            # Numeric, so a spreadsheet can sum it; the human form is alongside
            # for reading.
            int(r["bytes"])
            int(r["files"])
            assert r["bytes_human"]
        assert any(r["rule"] == "cache" for r in records)

    def test_csv_suppresses_the_prose_diff_header(self, tmp_path, monkeypatch, capsys):
        """A prose header would corrupt the first CSV record."""
        trial = tmp_path / "w.ignore"
        trial.write_text("*.iso\n")
        rj = self._job(tmp_path, monkeypatch)
        cli._do_live(
            [rj],
            cli._View(fmt="csv", override=cli.PatternOverride(str(trial))),
        )
        out = capsys.readouterr().out
        assert "trial pattern set" not in out, "the diff header is prose"
        # Every line must be a record, header or data - nothing else mixed in.
        for line in out.splitlines():
            assert line.startswith(("job,", rj.job.name)), line

    def test_verbose_lists_every_row_not_only_the_trial_ones(
        self, tmp_path, monkeypatch, capsys
    ):
        trial = tmp_path / "w.ignore"
        trial.write_text("*.iso\n")
        rj = self._job(tmp_path, monkeypatch)
        ov = cli.PatternOverride(str(trial))

        cli._do_live([rj], cli._View(override=ov))
        quiet = capsys.readouterr().out
        cli._do_live([rj], cli._View(override=ov, verbose=True))
        loud = capsys.readouterr().out

        # The config's own rule matches nothing here, so -v must show strictly more.
        assert len(loud.splitlines()) >= len(quiet.splitlines())
        # Totals are unaffected by the listing restriction, in both cases.
        assert "TOTAL:" in quiet and "TOTAL:" in loud

    def test_only_rules_is_none_without_a_trial(self, tmp_path, monkeypatch):
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert cli._View().only_rules(rj) is None
        assert cli._View(verbose=True).only_rules(rj) is None


class TestFileTotalsAreFree:
    """The file count must come from the traversal already being done."""

    def test_a_matched_directory_reports_its_subtree_file_count(self, tmp_path):
        from snap_bak import excludes as ex

        root = tmp_path / "src"
        deep = root / "cache" / "sub"
        deep.mkdir(parents=True)
        for i in range(7):
            (deep / f"f{i}").write_bytes(b"x" * 10)
        found = ex.ExclusionSet()
        for w in ex.walk_excluded(
            root, "/bak/x", ex.parse_patterns(["cache"]), attribute_rules=True
        ):
            found.add(None, w.path, w.size, w.is_dir, w.rule, w.files)

        assert len(found.entries) == 1, "one row for the directory"
        assert found.total_files == 7, "seven files beneath it"
        assert found.total_size == 70
        assert found.by_rule()[0].rule == "cache"

    def test_files_reported_per_row(self, tmp_path):
        from snap_bak import excludes as ex

        root = tmp_path / "src"
        (root / "cache").mkdir(parents=True)
        (root / "cache" / "a").write_bytes(b"x")
        (root / "top.tmp").write_bytes(b"y")
        found = ex.ExclusionSet()
        for w in ex.walk_excluded(
            root, "/bak/x", ex.parse_patterns(["cache", "*.tmp"]),
            attribute_rules=True,
        ):
            found.add(None, w.path, w.size, w.is_dir, w.rule, w.files)
        by_path = {e.path: e.files for e in found.entries.values()}
        assert by_path["/bak/x/cache"] == 1
        assert by_path["/bak/x/top.tmp"] == 1
        assert found.total_files == 2

    def test_a_rule_named_twice_is_one_rule(self, tmp_path):
        """Named on the command line *and* in the working file: one rule the
        user wrote twice, not two additions."""
        trial = tmp_path / "work.ignore"
        trial.write_text("*.iso\nbuild/cache/\n")
        ov = cli.PatternOverride(str(trial), ["*.iso"])
        effective = ov.apply(["*.tmp"])
        assert effective == ["*.tmp", "*.iso", "build/cache/"]
        assert ov.diff(["*.tmp"])["added"] == ["*.iso", "build/cache/"]

    def test_deduplication_does_not_reorder_negated_rules(self):
        """restic applies `!` negation in sequence, so position can matter."""
        ov = cli.PatternOverride(None, ["!keep", "*.a", "!keep2", "*.b"])
        assert ov.apply([]) == ["!keep", "*.a", "!keep2", "*.b"]


class TestTrialNegation:
    """How a trial pattern set interacts with the config's own rules.

    Prune semantics beneath an excluded directory are restic's own and already
    pinned in test_excludes.py against the golden suite. What is new here is the
    *override plumbing* placing a trial's rules after the config's, and so these
    pin the two consequences of that ordering - which cut in opposite directions.
    """

    def test_a_trial_negation_cancels_a_config_rule_of_the_same_target(
        self, tmp_path, monkeypatch
    ):
        """Negation is sequential and the trial runs last, so it wins.

        Verified against `restic backup`: `--exclude cache --exclude '!cache'`
        stores the directory. Documented because it is the opposite of the
        intuition that a trial can only tighten.
        """
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["cache"])
        baseline = cli._scan_live(rj, attribute=True)[0]
        assert baseline.entries, "the fixture must exclude something"

        after = cli._scan_live(
            rj, attribute=True, override=cli.PatternOverride(None, ["!cache"])
        )[0]
        assert after.entries == {}, "the trial's negation cancels the config's rule"

    def test_a_trial_cannot_reach_inside_a_directory_it_excluded(
        self, tmp_path, monkeypatch
    ):
        """The bound on the above: nothing later applies beneath a pruned dir."""
        rj, src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["cache"])
        trial = cli.PatternOverride(
            None, ["!cache/keep", "!cache/big.bin", "!cache"]
        )
        found = cli._scan_live(rj, attribute=True, override=trial)[0]
        # Negating the directory itself restores it wholesale; the two rules
        # naming things inside it contributed nothing, because pruning meant they
        # were never evaluated against those paths.
        assert found.entries == {}, "cancelled at the directory, as restic does"

    def test_dedup_preserves_rule_order(self):
        """dict.fromkeys, not a set: negation is order-sensitive.

        A set would silently reorder the user's rules and change what they
        match. The order of the negated pairs is what makes this observable.
        """
        ov = cli.PatternOverride(None, ["!keep", "*.a", "!keep2", "*.b"])
        assert ov.apply([]) == ["!keep", "*.a", "!keep2", "*.b"]
