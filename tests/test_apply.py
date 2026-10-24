"""Tests for the excludes planner/executor and the queue that schedules it.

The point of the planner is that there is only one of each. `plan()` decides what
a rewrite would touch and `execute()` performs it, so the dry-run a user
approves and the run that follows cannot disagree about scope.

The point of the queue is what it *doesn't* hold: there is no record of work
already done, because an empty queue is the only statement needed that nothing
is outstanding. A task that succeeds has its entry removed; a task that fails
keeps its entry and counts the attempt.

The suite this replaced passed 440 tests with `Runner.apply_excludes` having zero
callers and zero tests, and with three integration "apply" tests driving raw
restic over a single snapshot, in which both target predicates agree. A passing
suite is not evidence on its own; the tests that matter are the ones that fail
against the code they replace.
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from conftest import backup, init_repo, resolved_job, restic, stored_files

from snap_bak import apply, config, excludes, job, todo


class FakeRepo:
    def __init__(self, path="/repo"):
        self.path = path
        self.password_file = ""
        self.restic_options = []


class FakeJob:
    def __init__(self, *, name="Test", excludes=(), ignore_file=""):
        self.name = name
        self.excludes = list(excludes)
        self.ignore_file = ignore_file
        self.restic_options = []


class FakeRunner:
    """Exactly the surface apply.py and todo.py are allowed to use.

    Recording `commands` and raising a canned error keeps execute() testable
    without restic; if either module reaches for another Runner attribute, this
    stand-in stops being enough and the test says so.
    """

    def __init__(self, *, job=None, snapshots=(), listings=None, dry_run=False,
                 state_file=None):
        self.job = job or FakeJob()
        self.repo = FakeRepo()
        self.tag = "Test"
        self.mount_point = Path("/bak/sub")
        self.dry_run = dry_run
        self.window = None
        self.state_file = Path(state_file or "/nonexistent/Test.json")
        self._snapshots = list(snapshots)
        self._listings = dict(listings or {})
        self.commands: list[list[str]] = []
        self.run_error: Exception | None = None
        self.ping_error: Exception | None = None
        self.listing_error: Exception | None = None

    def repo_snapshots(self):
        return list(self._snapshots)

    def repo_listing(self, snapshot_id):
        if self.listing_error:
            raise self.listing_error
        return list(self._listings.get(snapshot_id, []))

    def repo_ping(self):
        if isinstance(self.ping_error, subprocess.CalledProcessError):
            return self.ping_error.returncode
        if self.ping_error:
            raise self.ping_error
        return None

    def run_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        if self.run_error:
            raise self.run_error
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point STATE_DIR at this test's own tmp_path, which pytest reaps.

    Snap-bak writes per-job state under STATE_DIR, so a test that touches the
    queue must redirect it or it would write into the real state directory.
    """
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STATE_DIR", d)
    monkeypatch.setattr(job.config, "STATE_DIR", d)
    monkeypatch.setattr(todo.config, "STATE_DIR", d)
    return d


def _snapshots(n):
    """n restic snapshot objects, newest first, as `restic snapshots` gives."""
    return [{"id": f"id{i}", "short_id": f"snap{i}"} for i in range(n)]


def _cache_listing(size=100):
    """A stored excluded directory, as `restic ls --long` reports it."""
    return [("/bak/sub/cache", size, True)]


def _planned(state_file, **kw):
    runner = FakeRunner(
        job=FakeJob(excludes=["cache"]),
        snapshots=_snapshots(1),
        listings={"id0": _cache_listing()},
        state_file=state_file,
        **kw,
    )
    return runner, apply.plan(runner)


class TestPlan:
    def test_targets_every_snapshot_holding_an_excluded_file(self):
        """The defect this replaces: restic lists newest-first and the old code
        rewrote only a snapshot that introduced a path it had not seen before, so
        of N snapshots carrying the same excluded file it rewrote one and
        silently kept the rest forever."""
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(3),
            listings={f"id{i}": _cache_listing() for i in range(3)},
        )
        assert apply.plan(runner).snapshot_ids == ("id0", "id1", "id2")

    def test_targets_a_subset_when_only_some_snapshots_match(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(3),
            listings={"id0": _cache_listing(), "id1": _cache_listing(), "id2": []},
        )
        assert apply.plan(runner).snapshot_ids == ("id0", "id1")

    def test_no_match_selects_nothing(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(2),
            listings={"id0": [], "id1": []},
        )
        assert apply.plan(runner).empty is True

    def test_command_rewrites_exactly_the_targets_and_forgets(self):
        """`--forget` removes the originals restic just rewrote; a separate
        `forget` would also apply retention policies and could delete snapshots
        beyond this intent."""
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(2),
            listings={"id0": _cache_listing(), "id1": _cache_listing()},
        )
        cmd = list(apply.plan(runner).command)
        assert "rewrite" in cmd
        assert "--forget" in cmd
        assert {"id0", "id1"} <= set(cmd)

    def test_plan_never_executes_anything(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(1),
            listings={"id0": _cache_listing()},
        )
        apply.plan(runner)
        assert runner.commands == []

    def test_a_failed_listing_yields_fewer_targets_not_an_exception(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(2),
            listings={"id0": _cache_listing()},
        )
        runner.listing_error = OSError("remote hung up")
        assert apply.plan(runner).snapshot_ids == ()


class TestExecute:
    def test_executes_the_rewrite(self, state_dir):
        runner, p = _planned(state_dir / "Test.json")
        result = apply.execute(runner, p)
        assert result.status == job.ResultStatus.SUCCESS
        assert any("rewrite" in c for c in runner.commands)

    def test_dry_run_reports_without_executing(self, state_dir):
        runner, p = _planned(state_dir / "Test.json", dry_run=True)
        assert apply.execute(runner, p).status == job.ResultStatus.DRY_RUN
        assert runner.commands == []

    def test_empty_plan_is_a_skip(self, state_dir):
        """Nothing to remove is an ordinary skip, not a failure: it must not
        perturb the run's exit code."""
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(1),
            listings={"id0": []},
            state_file=state_dir / "Test.json",
        )
        result = apply.execute(runner, apply.plan(runner))
        assert result.status == job.ResultStatus.SKIPPED
        assert not any("rewrite" in c for c in runner.commands)

    def test_preflight_refusal_reports_the_restic_exit_code(self, state_dir):
        """restic: 10 = no repo, 11 = locked, 12 = wrong password. Nothing may be
        modified, and the code has to survive so 10 is distinguishable from 11."""
        runner, p = _planned(state_dir / "Test.json")
        runner.ping_error = subprocess.CalledProcessError(10, "restic")
        result = apply.execute(runner, p)
        assert result.status == job.ResultStatus.DELAYED
        assert "rc=10" in result.reason
        assert not any("rewrite" in c for c in runner.commands)

    def test_unusable_repo_is_delayed_not_failed(self, state_dir):
        """A missing/locked/unreadable repo may well be fine on the next window,
        so it reports as temporary rather than permanent."""
        runner, p = _planned(state_dir / "Test.json")
        runner.ping_error = subprocess.CalledProcessError(11, "restic")
        result = apply.execute(runner, p)
        assert result.status == job.ResultStatus.DELAYED
        assert "rc=11" in result.reason

    def test_rewrite_failure_leaves_originals_and_reports(self, state_dir):
        runner, p = _planned(state_dir / "Test.json")
        runner.run_error = subprocess.CalledProcessError(1, "restic")  # the rewrite, not the ping
        result = apply.execute(runner, p)
        assert result.status == job.ResultStatus.FAILED
        assert "intact" in result.reason

    def test_failure_is_counted_so_it_can_escalate(self, state_dir):
        path = state_dir / "Test.json"
        todo.arm_task(path, apply.TASK_NAME)
        runner, p = _planned(path)
        runner.run_error = subprocess.CalledProcessError(1, "restic")
        apply.execute(runner, p)
        apply.execute(runner, p)
        entry = todo.queued_tasks(path)[0]
        assert entry["attempts"] == 2
        assert entry["last_rc"] == 1

    def test_recording_an_attempt_never_invents_an_entry(self, state_dir):
        """A failure outside the queue must not create queued work; only an
        existing entry is counted."""
        path = state_dir / "Test.json"
        runner, p = _planned(path)
        runner.run_error = subprocess.CalledProcessError(1, "restic")
        apply.execute(runner, p)
        assert todo.queued_tasks(path) == []


class TestQueue:
    """One file per job holds the work still to do. Nothing else does."""

    def test_arming_records_the_task(self, state_dir):
        path = state_dir / "data.json"
        entry = todo.arm_task(path, apply.TASK_NAME)
        assert entry["task"] == apply.TASK_NAME
        assert entry["attempts"] == 0
        assert [e["task"] for e in todo.queued_tasks(path)] == [apply.TASK_NAME]

    def test_arming_twice_is_refused_not_deduped_silently(self, state_dir):
        """A second `--later` usually means the user is unsure the first took;
        saying so beats pretending to add work."""
        path = state_dir / "data.json"
        todo.arm_task(path, apply.TASK_NAME)
        with pytest.raises(ValueError, match="already queued"):
            todo.arm_task(path, apply.TASK_NAME)
        assert len(todo.queued_tasks(path)) == 1

    def test_different_tasks_can_coexist(self, state_dir):
        path = state_dir / "data.json"
        todo.arm_task(path, "rewrite-drop-excluded-files")
        todo.arm_task(path, "prune-now")
        assert len(todo.queued_tasks(path)) == 2

    def test_success_clears_the_entry(self, state_dir):
        path = state_dir / "data.json"
        todo.arm_task(path, apply.TASK_NAME)
        todo.clear_task(path, apply.TASK_NAME)
        assert todo.queued_tasks(path) == []

    def test_jobs_do_not_share_a_queue(self, state_dir):
        """Three jobs on one repo each queue their own work; there is no shared
        file for one job's state to land in another's."""
        for name in ("data", "home", "Immich"):
            todo.arm_task(state_dir / f"{name}.json", apply.TASK_NAME)
        for name in ("data", "home", "Immich"):
            assert len(todo.queued_tasks(state_dir / f"{name}.json")) == 1

    def test_a_missing_file_means_nothing_queued(self, state_dir):
        assert todo.queued_tasks(state_dir / "NeverSeen.json") == []

    def test_a_corrupt_file_means_nothing_queued(self, state_dir):
        path = state_dir / "data.json"
        path.write_text("{not json")
        assert todo.queued_tasks(path) == []

    def test_queued_survives_a_status_write(self, state_dir):
        """write_status rewrites the whole file on every status transition, so
        outstanding work has to survive it or it would be silently dropped."""
        path = state_dir / "data.json"
        todo.arm_task(path, apply.TASK_NAME)
        job.write_status(
            path, job="data", status="success", message="ok", delay=job.DelayState()
        )
        assert [e["task"] for e in todo.queued_tasks(path)] == [apply.TASK_NAME]

    def test_status_write_still_updates_the_status(self, state_dir):
        path = state_dir / "data.json"
        todo.arm_task(path, apply.TASK_NAME)
        job.write_status(
            path, job="data", status="delayed", message="nope", delay=job.DelayState()
        )
        assert job.read_status(path)["status"] == "delayed"


class TestDispatch:
    def _armed(self, state_dir, name="Test"):
        path = state_dir / f"{name}.json"
        todo.arm_task(path, apply.TASK_NAME)
        return path, FakeRunner(
            job=FakeJob(name=name, excludes=["cache"]),
            snapshots=_snapshots(1),
            listings={"id0": _cache_listing()},
            state_file=path,
        )

    def test_successful_task_clears_its_entry(self, state_dir):
        path, runner = self._armed(state_dir)
        result, repos = todo.dispatch(path, runner)
        assert result.status == job.ResultStatus.SUCCESS
        assert todo.queued_tasks(path) == []
        assert repos == {"/repo"}

    def test_failed_task_keeps_its_entry_and_counts(self, state_dir):
        path, runner = self._armed(state_dir)
        runner.run_error = subprocess.CalledProcessError(1, "restic")
        result, repos = todo.dispatch(path, runner)
        assert result.status == job.ResultStatus.FAILED
        assert len(todo.queued_tasks(path)) == 1, "the request is still outstanding"
        assert todo.queued_tasks(path)[0]["attempts"] == 1
        assert repos == set()

    def test_skipped_task_clears_its_entry(self, state_dir):
        """Nothing to remove means the request was satisfied: leaving it queued
        would rescan for ever."""
        path, runner = self._armed(state_dir)
        runner._listings = {"id0": []}
        result, _ = todo.dispatch(path, runner)
        assert result.status == job.ResultStatus.SKIPPED
        assert todo.queued_tasks(path) == []

    def test_an_unknown_task_is_dropped_rather_than_looped_on(self, state_dir):
        path = state_dir / "Test.json"
        todo.arm_task(path, "a-task-this-version-never-heard-of")
        result, _ = todo.dispatch(path, FakeRunner(state_file=path))
        assert result.status == job.ResultStatus.FAILED
        assert "no handler" in result.reason
        assert todo.queued_tasks(path) == []

    def test_a_raising_handler_becomes_a_failed_result(self, state_dir):
        path = state_dir / "Test.json"

        @todo.task("boom-for-test")
        def _boom(runner, entry):
            raise RuntimeError("kaboom")

        try:
            todo.arm_task(path, "boom-for-test")
            result, repos = todo.dispatch(path, FakeRunner(state_file=path))
        finally:
            del todo.TASKS["boom-for-test"]
        assert result.status == job.ResultStatus.FAILED
        assert "kaboom" in result.reason
        assert len(todo.queued_tasks(path)) == 1
        assert repos == set()

    def test_nothing_queued_dispatches_nothing(self, state_dir):
        path = state_dir / "Test.json"
        assert todo.dispatch(path, FakeRunner(state_file=path)) == (None, set())

    def test_the_task_name_says_what_it_does(self):
        """It is stored verbatim in <job>.json and read by a human wondering
        what a queued run will do."""
        assert apply.TASK_NAME == "rewrite-drop-excluded-files"
        assert apply.TASK_NAME in todo.TASKS

    def test_a_duplicate_task_name_is_rejected(self):
        with pytest.raises(ValueError, match="duplicate task name"):

            @todo.task(apply.TASK_NAME)
            def _dup(runner, entry):
                raise AssertionError("must not be registered")


class TestReap:
    def _cfg(self, days=90):
        class _C:
            state_retention_days = days

        return _C()

    def test_orphaned_state_is_removed_once_old_enough(self, state_dir):
        old = state_dir / "Gone.json"
        old.write_text("{}")
        _age(old, 100)
        assert todo.reap(self._cfg(), {"data"}) == ["Gone.json"]
        assert not old.exists()

    def test_a_recent_orphan_is_kept(self, state_dir):
        """A job removed from the config and put back should not lose its state."""
        recent = state_dir / "Gone.json"
        recent.write_text("{}")
        assert todo.reap(self._cfg(), {"data"}) == []
        assert recent.exists()

    def test_a_live_job_is_never_reaped(self, state_dir):
        """Reaping it would reset the missed-slot escalation that drives priority."""
        live = state_dir / "data.json"
        live.write_text("{}")
        _age(live, 10_000)
        assert todo.reap(self._cfg(), {"data"}) == []
        assert live.exists()

    def test_queued_work_is_never_reaped_for_its_age(self, state_dir):
        """An entry is a request from a human; discarding it for being old would
        be worse than leaving the file. Only the job leaving the config retires
        a request, which is a different and deliberate thing."""
        path = state_dir / "data.json"
        todo.arm_task(path, apply.TASK_NAME)
        _age(path, 10_000)
        assert todo.reap(self._cfg(), {"data"}) == []
        assert len(todo.queued_tasks(path)) == 1

    def test_repo_level_state_is_never_reaped(self, state_dir):
        for name in todo.NOT_JOB_STATE:
            if name.endswith(".json"):
                p = state_dir / name
                p.write_text("{}")
                _age(p, 10_000)
        assert todo.reap(self._cfg(), set()) == []
        assert (state_dir / "throughput.json").exists()


def _age(path, days):
    import os

    t = path.stat().st_mtime - days * 86400
    os.utime(path, (t, t))


class TestReporting:
    """`--json` and the human printer."""

    def _plan(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(2),
            listings={"id0": _cache_listing(), "id1": _cache_listing()},
        )
        return apply.plan(runner)

    def test_plan_json_carries_scope_and_the_exact_command(self):
        body = self._plan().to_json()
        assert body["snapshot_ids"] == ["id0", "id1"]
        assert "rewrite" in body["command"]
        assert body["count"] == 1
        # Totals accumulate across snapshots — this is the space reclaimable by
        # rewriting all of them, where `--live` totals the bytes on disk.
        assert body["total_size"] == 200
        assert body["per_snapshot_totals"] == {"snap0": 100, "snap1": 100}

    def test_every_mode_emits_the_same_keys(self):
        """One schema for --live, --snaps and --repo: an agent can rely on the
        keys being there rather than on which mode produced them."""
        found = excludes.ExclusionSet()
        found.add(None, "/bak/sub/cache", 100, True)
        live = apply.payload("Test", "/bak/sub", found)
        repo = self._plan().to_json()
        assert set(live) == set(repo) - {"snapshot_ids", "command", "query_error"}
        assert live["snapshots"] == [], "no snapshot dimension, but present"
        assert live["entries"][0]["per_snapshot"] == {}

    def test_query_error_is_explicit_null_not_absent(self, tmp_path, monkeypatch):
        """`snapshot_ids: []` is ambiguous on its own: an unreachable repo and a
        clean repo both look empty. `query_error` is always present so an agent
        can tell them apart without reading prose."""
        rj, _src, _repo = resolved_job(tmp_path, monkeypatch, excludes_list=["*.tmp"])
        assert not Path(rj.repo.path).exists(), "fixture repo must not exist"
        body = apply.plan(job.Runner(rj)).to_json()
        assert body["snapshot_ids"] == []
        assert body["query_error"] is not None
        assert body["query_error"]["returncode"] == 10
        assert body["query_error"]["retryable"] is True


class TestNewestSnapshotCheck:
    """Backups run before the rewrite using the same excludes, so the newest
    snapshot should hold nothing to remove. Matches there mean the backup path
    is not honouring the excludes."""

    def test_warns_when_the_newest_snapshot_still_matches(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(2),
            listings={"id0": _cache_listing(), "id1": _cache_listing()},
        )
        assert apply.warns_on_newest(runner, apply.plan(runner)) is True

    def test_quiet_when_only_older_snapshots_match(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(2),
            listings={"id0": [], "id1": _cache_listing()},
        )
        assert apply.warns_on_newest(runner, apply.plan(runner)) is False

    def test_quiet_when_nothing_matches(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(1),
            listings={"id0": []},
        )
        assert apply.warns_on_newest(runner, apply.plan(runner)) is False


needs_restic = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not available"
)


@needs_restic
class TestScheduledWork:
    """The wiring that did not exist: arming was a silent no-op, the phase had no
    caller, and it would have raised AttributeError on first use."""

    @staticmethod
    def _phase(runs):
        from snap_bak import cli

        return cli._apply_excludes_phase(runs)

    def _armed(self, tmp_path, monkeypatch, state_dir):
        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        todo.arm_task(rj.state_file, apply.TASK_NAME)
        return job.Runner(rj), repo

    def test_queued_rewrite_removes_excluded_files(self, tmp_path, monkeypatch, state_dir):
        runner, repo = self._armed(tmp_path, monkeypatch, state_dir)

        results, pruned = self._phase([runner])

        assert pruned == {repo}, "the rewritten repo must be queued for prune"
        assert results[0].status == job.ResultStatus.SUCCESS
        snap = json.loads(restic(repo, "snapshots", "--json").stdout)[0]
        stored = stored_files(repo, snap["id"])
        assert not any(p.endswith("cache/big.bin") for p in stored), stored
        assert any(p.endswith("keep.txt") for p in stored), stored

    def test_success_clears_the_entry_so_it_never_runs_again(self, tmp_path, monkeypatch, state_dir):
        runner, _repo = self._armed(tmp_path, monkeypatch, state_dir)
        self._phase([runner])
        assert todo.queued_tasks(runner.state_file) == []

    def test_nothing_queued_does_nothing(self, tmp_path, monkeypatch, state_dir):
        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        results, pruned = self._phase([job.Runner(rj)])

        assert (results, pruned) == ([], set())
        snap = json.loads(restic(repo, "snapshots", "--json").stdout)[0]
        assert any(p.endswith("cache/big.bin") for p in stored_files(repo, snap["id"]))

    def test_out_of_window_is_skipped_and_stays_queued(self, tmp_path, monkeypatch, state_dir):
        from snap_bak.config import Window

        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        todo.arm_task(rj.state_file, apply.TASK_NAME)
        runner = job.Runner(rj)
        now = datetime.now()
        runner.window = Window(((now.hour + 6) % 24) * 60, ((now.hour + 6) % 24) * 60 + 1)

        results, pruned = self._phase([runner])

        assert (results, pruned) == ([], set())
        assert len(todo.queued_tasks(runner.state_file)) == 1, "still outstanding"

    def test_plan_reports_every_real_snapshot(self, tmp_path, monkeypatch, state_dir):
        """Three real snapshots all holding `cache`, against real restic.

        The unit test above pins the predicate; this proves the planner's restic
        plumbing feeds it every snapshot rather than just the newest. With one
        snapshot the old broken predicate looked correct, which is why the
        earlier integration tests missed this.
        """
        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        for i in range(3):
            # restic skips an identical tree, so each backup must differ.
            (src / f"marker{i}.txt").write_text(str(i))
            backup(repo, str(src), "--exclude", "*.tmp")
        assert len(json.loads(restic(repo, "snapshots", "--json").stdout)) == 3

        assert len(apply.plan(job.Runner(rj)).snapshot_ids) == 3


@needs_restic
class TestOrchestratorWiring:
    """The run reaches the queued work, and its repos join the run's single prune.

    The tests above call the phase directly. This one proves the orchestrator
    actually invokes it — which, when the whole feature shipped dead, was the
    difference between a green suite and a working command.
    """

    def _run(self, tmp_path, monkeypatch, prune_calls):
        from snap_bak import cli

        (tmp_path / "bak").mkdir(exist_ok=True)
        monkeypatch.setattr(job, "check_password_file", lambda repo: None)
        monkeypatch.setattr(
            job.Runner, "run",
            lambda self: job.Result(self.job.name, job.ResultStatus.SKIPPED),
        )
        monkeypatch.setattr(job.Runner, "retention", lambda self: None)
        monkeypatch.setattr(
            job.Runner, "prune", lambda self: prune_calls.append(self.repo.path)
        )
        monkeypatch.setattr(
            sys, "argv", ["snap-bak", "-c", str(tmp_path / "config.json")]
        )
        with pytest.raises(SystemExit):
            cli.main()

    def test_queued_work_runs_and_prunes_its_repo(self, tmp_path, monkeypatch, state_dir):
        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        todo.arm_task(rj.state_file, apply.TASK_NAME)

        prune_calls: list[str] = []
        self._run(tmp_path, monkeypatch, prune_calls)

        assert prune_calls == [repo]
        snap = json.loads(restic(repo, "snapshots", "--json").stdout)[0]
        assert not any(p.endswith("cache/big.bin") for p in stored_files(repo, snap["id"]))

    def test_a_run_with_nothing_queued_prunes_nothing(self, tmp_path, monkeypatch, state_dir):
        _rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        prune_calls: list[str] = []
        self._run(tmp_path, monkeypatch, prune_calls)

        assert prune_calls == []


class TestVerbSurface:
    """`apply` removes files from restic only, so a selector aimed at the live
    filesystem or local snapshots is a mistake worth refusing rather than
    silently discarding."""

    @pytest.mark.parametrize("flag", ["--live", "--snaps", "--all"])
    def test_apply_parser_rejects_live_and_snaps_selectors(self, flag):
        from snap_bak import cli

        parser = cli.excludes_parser("apply")
        with pytest.raises(SystemExit):
            parser.parse_args(["excludes", flag])

    def test_apply_parser_accepts_now_and_later(self):
        from snap_bak import cli

        parser = cli.excludes_parser("apply")
        assert parser.parse_args(["excludes", "--now"]).now is True
        assert parser.parse_args(["excludes", "--later"]).later is True

    def test_lint_is_not_a_flag(self):
        """Linting runs on every `show` and is silent when clean, so offering it
        as a flag only implies it is optional."""
        from snap_bak import cli

        with pytest.raises(SystemExit):
            cli.excludes_parser("show").parse_args(["excludes", "--lint"])

    def test_show_parser_still_offers_the_three_selectors(self):
        from snap_bak import cli

        args = cli.excludes_parser("show").parse_args(
            ["excludes", "--live", "--snaps", "--repo"]
        )
        assert (args.live, args.snaps, args.repo) == (True, True, True)

    def test_rejected_selector_exits_nonzero_and_changes_nothing(
        self, tmp_path, monkeypatch
    ):
        from snap_bak import cli

        _rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        before = restic(repo, "snapshots", "--json").stdout

        monkeypatch.setattr(
            sys, "argv",
            ["snap-bak", "-c", str(tmp_path / "config.json"),
             "apply", "excludes", "--live", "--now"],
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code != 0
        assert restic(repo, "snapshots", "--json").stdout == before