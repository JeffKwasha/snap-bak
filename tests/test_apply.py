"""Tests for `apply.py` — the single planner and executor for apply-excludes.

The point of this module is that there is only one of each. `plan()` decides
what a rewrite would touch and `execute()` performs it, so the dry-run a user
approves and the run that follows cannot disagree about scope. Most tests here
are unit tests over a fake Runner, which keeps them fast and pins the exact
target-selection semantics; the scheduled-phase tests use a real restic repo,
because what they guard is that the wiring exists at all.

The suite this replaced passed 440 tests with `Runner.apply_excludes` having
zero callers and zero tests, and with the three integration "apply" tests
driving raw restic instead of snap-bak. A passing suite is not evidence on its
own; the tests that matter are the ones that fail against the old code.
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from conftest import backup, init_repo, resolved_job, restic, stored_files

from snap_bak import apply, config, excludes, job


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
    """Exactly the read API apply.py is allowed to use.

    Recording `commands` and raising a canned error keeps execute() testable
    without restic; if apply.py ever reaches for another Runner attribute, this
    stand-in stops being enough and the test says so.
    """

    def __init__(self, *, job=None, snapshots=(), listings=None, dry_run=False):
        self.job = job or FakeJob()
        self.repo = FakeRepo()
        self.tag = "Test"
        self.mount_point = Path("/bak/sub")
        self.dry_run = dry_run
        self.window = None
        self._snapshots = list(snapshots)
        self._listings = dict(listings or {})
        self.commands: list[list[str]] = []
        self.run_error: Exception | None = None
        self.listing_error: Exception | None = None

    def repo_snapshots(self):
        return list(self._snapshots)

    def repo_listing(self, snapshot_id):
        if self.listing_error:
            raise self.listing_error
        return list(self._listings.get(snapshot_id, []))

    def repo_ping(self):
        if isinstance(self.run_error, subprocess.CalledProcessError):
            return self.run_error.returncode
        if self.run_error:
            raise self.run_error
        return None

    def run_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        if self.run_error:
            raise self.run_error
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _snapshots(n):
    """n restic snapshot objects, newest first, as `restic snapshots` gives."""
    return [{"id": f"id{i}", "short_id": f"snap{i}"} for i in range(n)]


def _cache_listing(size=100):
    """A stored excluded directory, as `restic ls --long` reports it."""
    return [("/bak/sub/cache", size, True)]


class TestPlan:
    def test_targets_every_snapshot_holding_an_excluded_file(self):
        """The defect this replaces: restic lists newest-first and the old code
        rewrote only a snapshot that introduced a path it had not seen before,
        so of N snapshots carrying the same excluded file it rewrote one and
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
    def _planned(self, **kw):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(1),
            listings={"id0": _cache_listing()},
            **kw,
        )
        return runner, apply.plan(runner)

    def test_executes_the_rewrite(self):
        runner, p = self._planned()
        assert apply.execute(runner, p).status == "applied"
        assert any("rewrite" in c for c in runner.commands)

    def test_dry_run_reports_without_executing(self):
        runner, p = self._planned(dry_run=True)
        assert apply.execute(runner, p).status == "dry-run"
        assert runner.commands == []

    def test_empty_plan_is_a_noop(self):
        runner = FakeRunner(
            job=FakeJob(excludes=["cache"]),
            snapshots=_snapshots(1),
            listings={"id0": []},
        )
        out = apply.execute(runner, apply.plan(runner))
        assert out.status == "noop"
        assert not any("rewrite" in c for c in runner.commands)

    def test_preflight_refusal_reports_the_restic_exit_code(self):
        """restic: 10 = no repo, 11 = locked, 12 = wrong password. Nothing may be
        modified, and the reason must name the code so 10 is distinguishable
        from 11 and 12."""
        runner, p = self._planned()
        runner.run_error = subprocess.CalledProcessError(10, "restic")
        out = apply.execute(runner, p)
        assert out.status == "failed"
        assert out.rc == 10
        assert not any("rewrite" in c for c in runner.commands)

    def test_locked_repo_reports_rc_11(self):
        """rc 11 is restic's own lock. `--now` turns that into advice to use
        `--later`; the scheduled path renders the same reason without advice."""
        runner, p = self._planned()
        runner.run_error = subprocess.CalledProcessError(11, "restic")
        out = apply.execute(runner, p)
        assert (out.status, out.rc) == ("failed", 11)

    def test_rewrite_failure_leaves_originals_and_reports(self):
        runner, p = self._planned()
        runner.run_error = subprocess.CalledProcessError(1, "restic")
        out = apply.execute(runner, p)
        assert (out.status, out.rc) == ("failed", 1)

    def test_outcome_is_not_a_job_result(self):
        """Structural guarantee: apply can never move the run's exit code,
        because its outcome is not the type exit_code() consumes."""
        assert not issubclass(apply.Outcome, job.Result)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point STATE_DIR at this test's own tmp_path, which pytest reaps.

    The rest of the suite relies on conftest redirecting STATE_DIR to a
    directory removed at interpreter exit; this goes one better for the tests
    that actually write stamps.
    """
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(job.config, "STATE_DIR", d)
    return d


class TestCompletionStamp:
    """The record that a job's patterns were already applied to its repo.

    The stamp is a per-(repo, job) fact, and the three jobs in the production
    config all share one rclone repo with different excludes and different
    ignore_files. A single shared file meant each job's stamp invalidated the
    last, so every run fell back to a full per-snapshot `restic ls` scan — the
    network cost the stamp exists to avoid, paid on every single run.
    """

    def _stamp_files(self):
        return sorted(p.name for p in job.done_dir().iterdir())

    def test_two_jobs_on_one_repo_keep_independent_stamps(self, state_dir):
        repo = "rclone:CONFIG_ENTRY:DIRECTORY"
        a, b = ["*.tmp"], ["*.tmp", ".immich_db"]

        job.write_stamp("data", repo, a, "snapA")
        job.write_stamp("Immich", repo, b, "snapB")

        assert job.stamp_current("data", repo, a, "snapA") is True
        assert job.stamp_current("Immich", repo, b, "snapB") is True, (
            "one job's apply must not invalidate another's stamp"
        )

    def test_three_jobs_each_get_their_own_file(self, state_dir):
        for name in ("data", "home", "Immich"):
            job.write_stamp(name, "rclone:CONFIG_ENTRY:DIRECTORY", ["*.tmp"], "snap")
        assert self._stamp_files() == ["data.json", "Immich.json", "home.json"]

    def test_stamp_records_the_job_name(self, state_dir):
        job.write_stamp("data", "r", ["*.tmp"], "snapA")
        body = json.loads(
            (job.done_dir() / "data.json").read_text()
        )
        assert body["job"] == "data"
        assert body["repo"] == "r"
        assert body["newest_snapshot"] == "snapA"

    def test_one_jobs_patterns_do_not_satisfy_another(self, state_dir):
        job.write_stamp("data", "r", ["*.tmp"], "snapA")
        assert job.stamp_current("data", "r", ["*.tmp", "cache"], "snapA") is False

    def test_a_newer_snapshot_invalidates(self, state_dir):
        job.write_stamp("data", "r", ["*.tmp"], "snapA")
        assert job.stamp_current("data", "r", ["*.tmp"], "snapB") is False

    def test_a_different_repo_invalidates(self, state_dir):
        job.write_stamp("data", "repoA", ["*.tmp"], "snapA")
        assert job.stamp_current("data", "repoB", ["*.tmp"], "snapA") is False

    def test_missing_stamp_is_not_current(self, state_dir):
        assert job.stamp_current("data", "r", ["*.tmp"], "snapA") is False

    def test_corrupt_stamp_is_not_current(self, state_dir):
        job.write_stamp("data", "r", ["*.tmp"], "snapA")
        (job.done_dir() / "data.json").write_text("{not json")
        assert job.stamp_current("data", "r", ["*.tmp"], "snapA") is False

    def test_job_names_are_safe_as_filenames(self, tmp_path):
        """A job name becomes a path component, so the config validator is what
        makes that safe — it already gates `<name>_status.json` the same way."""
        import pydantic

        from snap_bak import config

        with pytest.raises(pydantic.ValidationError):
            config.Job(
                name="../escape",
                snapshot_dir=tmp_path,
                source_subvolume=tmp_path,
                repo="r",
            )


class TestArmFile:
    def test_arm_and_stamp_are_distinct_facts(self, state_dir):
        """The arm is a request, consumed once whatever happens; the stamp is a
        record of completion that outlives the run. Distinct lifecycles, so
        distinct files."""
        job.arm_file().write_text(json.dumps({"jobs": ["data"]}))
        job.write_stamp("data", "r", ["*.tmp"], "snapA")

        assert job.arm_file() != job.completion_stamp("data")
        assert job.read_pending() == ["data"]  # the arm outlives the stamp
        assert job.stamp_current("data", "r", ["*.tmp"], "snapA") is True


needs_restic = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not available"
)


@needs_restic
class TestScheduledPhase:
    """The wiring that did not exist: `apply_excludes: true` and `--later` were
    silent no-ops, the phase function had no caller, and it would have raised
    AttributeError on first use because it confused Job with ResolvedJob."""

    @staticmethod
    def _phase(runs, *, armed):
        from snap_bak import cli

        return cli._apply_excludes_phase(runs, armed=armed)

    def test_apply_excludes_true_rewrites_history(self, tmp_path, monkeypatch):
        rj, src, repo = resolved_job(
            tmp_path,
            monkeypatch,
            excludes_list=["*.tmp", "cache"],
            apply_excludes=True,
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        pruned = self._phase([job.Runner(rj)], armed=set())

        assert pruned == {repo}, "the rewritten repo must be queued for prune"
        snap = json.loads(restic(repo, "snapshots", "--json").stdout)[0]
        stored = stored_files(repo, snap["id"])
        assert not any(p.endswith("cache/big.bin") for p in stored), stored
        assert any(p.endswith("keep.txt") for p in stored), stored

    def test_without_arming_nothing_is_rewritten(self, tmp_path, monkeypatch):
        """Default off: editing `excludes` must never silently rewrite history."""
        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        assert self._phase([job.Runner(rj)], armed=set()) == set()

        snap = json.loads(restic(repo, "snapshots", "--json").stdout)[0]
        assert any(p.endswith("cache/big.bin") for p in stored_files(repo, snap["id"]))

    def test_later_arm_applies_and_is_consumed_once(self, tmp_path, monkeypatch):
        from snap_bak import cli

        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        assert cli._arm([rj], as_json=False) == config.EXIT_SUCCESS
        armed = set(job.consume_pending())
        assert armed == {"Test"}

        assert self._phase([job.Runner(rj)], armed=armed) == {repo}
        snap = json.loads(restic(repo, "snapshots", "--json").stdout)[0]
        assert not any(
            p.endswith("cache/big.bin") for p in stored_files(repo, snap["id"])
        )
        # One-shot: the arm is gone, so the next run is a no-op.
        assert job.consume_pending() == []

    def test_out_of_window_is_skipped(self, tmp_path, monkeypatch):
        from snap_bak.config import Window

        rj, src, repo = resolved_job(
            tmp_path,
            monkeypatch,
            excludes_list=["*.tmp", "cache"],
            apply_excludes=True,
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        runner = job.Runner(rj)
        # A one-minute window six hours from now: never inside it.
        now = datetime.now()
        runner.window = Window(((now.hour + 6) % 24) * 60, ((now.hour + 6) % 24) * 60 + 1)

        assert self._phase([runner], armed=set()) == set()

    def test_plan_reports_every_real_snapshot(self, tmp_path, monkeypatch):
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

        p = apply.plan(job.Runner(rj))
        assert len(p.snapshot_ids) == 3


class TestReporting:
    """`--json` and the human printer. The JSON path had a latent NameError
    (Plan.to_json shadowed the module-level payload function) that no test
    touched, which is the same failure mode this module exists to end."""

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
        live = apply.payload("Test", "/bak/sub", _live_set())
        repo = self._plan().to_json()
        assert set(live) == set(repo) - {"snapshot_ids", "command"}
        assert live["snapshots"] == [], "no snapshot dimension, but present"

    def test_live_json_reports_entries_without_a_snapshot_dimension(self):
        found = _live_set()
        body = apply.payload("Test", "/bak/sub", found)
        assert body["entries"][0]["per_snapshot"] == {}
        assert body["count"] == 1


class TestLockedAdvice:
    def test_now_recommends_later_when_restic_holds_the_lock(self):
        """`--now` takes no snap-bak lock and adds no --retry-lock: restic
        serializes on its own, so contention is an instant rc 11 whose only
        useful answer is to wait for the scheduled run."""
        detail = cli_outcome(11)["detail"]
        assert apply.LOCKED_ADVICE in detail
        assert "--later" in detail

    def test_the_scheduled_path_gets_the_reason_without_the_advice(self):
        """The scheduled run *is* the recommended path, so it must not tell the
        user to go and use --later."""
        assert "--later" not in apply.Outcome("failed", "repository is locked", rc=11).detail


def _live_set():
    """An ExclusionSet shaped the way the live-filesystem walk produces one:
    entries with no snapshot dimension."""
    found = excludes.ExclusionSet()
    found.add(None, "/bak/sub/cache", 100, True)
    return found


def cli_outcome(rc) -> dict:
    from snap_bak import cli

    return cli._outcome_json("Test", apply.Outcome("failed", "preflight refused", rc=rc))


@needs_restic
class TestOrchestratorWiring:
    """The run reaches the phase, and its repos join the run's single prune.

    The phase tests above call `_apply_excludes_phase` directly. This one
    proves the orchestrator actually invokes it — which, when the whole feature
    shipped dead, was the difference between 440 green tests and a working
    `apply_excludes` key.
    """

    def _run(self, tmp_path, monkeypatch, *, argv, prune_calls):
        from snap_bak import cli

        (tmp_path / "bak").mkdir(exist_ok=True)
        monkeypatch.setattr(job, "check_password_file", lambda repo: None)
        monkeypatch.setattr(
            job.Runner,
            "run",
            lambda self: job.Result(self.job.name, job.ResultStatus.SKIPPED),
        )
        monkeypatch.setattr(job.Runner, "retention", lambda self: None)
        monkeypatch.setattr(
            job.Runner, "prune", lambda self: prune_calls.append(self.repo.path)
        )
        monkeypatch.setattr(sys, "argv", ["snap-bak", "-c", str(tmp_path / "config.json"), *argv])
        with pytest.raises(SystemExit):
            cli.main()

    def test_apply_excludes_true_prunes_its_repo_in_the_same_run(
        self, tmp_path, monkeypatch
    ):
        rj, src, repo = resolved_job(
            tmp_path,
            monkeypatch,
            excludes_list=["*.tmp", "cache"],
            apply_excludes=True,
        )
        assert rj.job.apply_excludes is True
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        prune_calls: list[str] = []
        self._run(tmp_path, monkeypatch, argv=[], prune_calls=prune_calls)

        assert prune_calls == [repo]
        snap = json.loads(restic(repo, "snapshots", "--json").stdout)[0]
        assert not any(
            p.endswith("cache/big.bin") for p in stored_files(repo, snap["id"])
        )

    def test_a_run_without_arming_prunes_nothing(
        self, tmp_path, monkeypatch
    ):
        _rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")

        prune_calls: list[str] = []
        self._run(tmp_path, monkeypatch, argv=[], prune_calls=prune_calls)

        assert prune_calls == []

    def test_an_armed_run_consumes_the_arm(self, tmp_path, monkeypatch):
        from snap_bak import cli

        rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        assert cli._arm([rj], as_json=False) == config.EXIT_SUCCESS

        prune_calls: list[str] = []
        self._run(tmp_path, monkeypatch, argv=[], prune_calls=prune_calls)

        assert prune_calls == [repo]
        assert job.read_pending() == [], "the arm is one-shot"


class TestVerbSurface:

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

        parser = cli.excludes_parser("show")
        args = parser.parse_args(["excludes", "--live", "--snaps", "--repo"])
        assert (args.live, args.snaps, args.repo) == (True, True, True)

    def test_rejected_selector_exits_nonzero_and_changes_nothing(
        self, tmp_path, monkeypatch
    ):
        """End to end: the refusal has to reach the exit code, and it has to
        leave the repo alone."""
        from snap_bak import cli

        _rj, src, repo = resolved_job(
            tmp_path, monkeypatch, excludes_list=["*.tmp", "cache"]
        )
        init_repo(repo)
        backup(repo, str(src), "--exclude", "*.tmp")
        before = restic(repo, "snapshots", "--json").stdout

        monkeypatch.setattr(
            sys, "argv", ["snap-bak", "-c", str(tmp_path / "config.json"),
                          "apply", "excludes", "--live", "--now"]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code != 0
        assert restic(repo, "snapshots", "--json").stdout == before