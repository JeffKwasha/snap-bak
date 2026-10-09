#!/usr/bin/env python3
"""Planning and executing an apply-excludes: one of each, by construction.

There used to be two implementations of "what would a rewrite remove" — one in
the CLI, reachable, and one in Runner, unreachable and subtly different — plus
three copies of the preflight and two of the rewrite. That is how the dry-run
could promise N snapshots and the scheduled run rewrite one.

Now `plan()` is the only thing that decides scope and `execute()` is the only
thing that acts on a plan. A *mode* is what you do with the result:

    show excludes --repo   ->  plan + render      (read-only)
    apply excludes         ->  plan + render      (the dry-run, same call)
    apply excludes --now   ->  plan + execute     (immediately)
    scheduled run          ->  gates, then the same plan + execute

so the dry-run a user approves cannot drift from the run that follows.

Runner is used only through this surface: `job`, `repo`, `tag`, `mount_point`,
`dry_run`, `window`, `repo_snapshots()`, `repo_listing(id)`, `run_command()`.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

from snap_bak import excludes, job, restic, todo

logger = logging.getLogger(__name__)

# restic exit codes we can say something useful about.
LOCKED_ADVICE = (
    "another restic holds the repository lock; "
    "use `apply excludes --later` to rewrite on the next scheduled run"
)

_PREFLIGHT_REASONS = {
    10: "repository does not exist",
    11: "repository is locked by another restic",
    12: "wrong password",
}

# restic codes that mean "not now" rather than "not ever": the repo may well be
# there on the next eligible window, so the task is delayed, not failed.
_RETRYABLE = frozenset({10, 11, 12})


def payload(job_name: str, root: str, found: excludes.ExclusionSet) -> dict:
    """The per-job --json body shared by every show/apply mode.

    One schema for all modes, so an agent can rely on the keys being present:
    `show --live` has no snapshot dimension and says so with empty lists rather
    than by omitting the fields.
    """
    return {
        "job": job_name,
        "root": root,
        "snapshots": sorted(found.per_snapshot, reverse=True),
        "entries": entries_json(found),
        "per_snapshot_totals": {
            s: found.size_in(s) for s in sorted(found.per_snapshot)
        },
        "count": len(found.entries),
        "total_size": found.total_size,
    }


def entries_json(found: excludes.ExclusionSet) -> list[dict]:
    """Excluded entries with their per-snapshot byte breakdown.

    `per_snapshot` maps snapshot label -> this entry's bytes in it, omitting
    snapshots the entry is absent from (which the renderer shows as '-'). The
    live-filesystem walk has no snapshot dimension, so its entries carry none.
    """
    return [
        {
            "path": e.path,
            "size": e.size,
            "is_dir": e.is_dir,
            "per_snapshot": {
                s: found.size_in_of(e.path, s)
                for s in found.per_snapshot
                if s is not None and e.path in found.per_snapshot[s]
            },
        }
        for e in found.ordered_entries()
    ]


@dataclass(frozen=True)
class Plan:
    """What one job's rewrite would touch. Read-only, and complete: printing it
    is the dry-run, and executing it is the mutation."""

    job: str
    root: str
    found: excludes.ExclusionSet
    snapshot_ids: tuple[str, ...] = ()
    command: tuple[str, ...] = field(default=())
    query_error: job.RepoQueryError | None = None

    @property
    def empty(self) -> bool:
        return not self.snapshot_ids

    @property
    def total_size(self) -> int:
        return self.found.total_size

    def to_json(self) -> dict:
        body = payload(self.job, self.root, self.found)
        body["snapshot_ids"] = list(self.snapshot_ids)
        body["command"] = list(self.command)
        # An explicit null, not an omitted key: `snapshot_ids: []` alongside
        # `query_error: null` means "restic said there is nothing", and the same
        # empty list with a non-null error means "restic never answered". An
        # agent must be able to tell those apart without inspecting prose.
        body["query_error"] = (
            None
            if self.query_error is None
            else {
                "path": self.query_error.path,
                "returncode": self.query_error.returncode,
                "retryable": self.query_error.retryable,
                "detail": self.query_error.detail,
            }
        )
        return body


def plan(runner) -> Plan:
    """Scan this job's stored snapshots and decide what a rewrite would remove.

    Read-only. Executes no restic command that can mutate, creates, or delete
    anything; the only restic calls are `snapshots` and `ls --long`, both
    metadata.

    An unreachable or locked repository is recorded on the returned `Plan` as
    `query_error` rather than raised. Raising would abort the whole command when
    one job of several is broken; swallowing it would report "nothing to clean
    up" for a repo nobody ever asked. Carrying it means the caller can print the
    reason next to the job it belongs to and fail the exit code, while the other
    jobs still get their answer.

    A snapshot that dies *mid-scan* is the opposite case and stays non-fatal: a
    target we failed to see is not removed, which is the safe direction.
    """
    patterns = excludes.patterns_for_job(runner.job)
    parsed = excludes.parse_patterns(patterns)
    root = str(runner.mount_point)

    invalid = excludes.validate_patterns(patterns)
    if invalid or not parsed:
        # restic would reject these, so there is nothing coherent to plan.
        return Plan(runner.job.name, root, excludes.ExclusionSet())

    try:
        snaps = runner.repo_snapshots()
    except job.RepoQueryError as e:
        return Plan(runner.job.name, root, excludes.ExclusionSet(), query_error=e)

    found = excludes.ExclusionSet()
    targets: list[str] = []
    for snap in snaps:
        snapshot_id = snap.get("id")
        if not snapshot_id:
            continue
        label = snap.get("short_id") or snapshot_id[:8]
        try:
            listing = runner.repo_listing(snapshot_id)
        except Exception as e:
            logger.error("%s: cannot list snapshot %s: %s", runner.job.name, label, e)
            continue
        excludes.collect_from_listing(listing, label, parsed, into=found)
        # Only snapshots that actually hold a match are rewritten. Restic lists
        # newest-first, and entries are keyed by path, so this must be a
        # per-snapshot membership test: asking whether any *path* is named like
        # the snapshot label would keep only the newest snapshot and silently
        # leave every older copy of the excluded data in place forever.
        if label in found.per_snapshot:
            targets.append(snapshot_id)

    return Plan(
        runner.job.name,
        root,
        found,
        tuple(targets),
        tuple(_rewrite_cmd(runner, targets)),
    )


def execute(runner, p: Plan) -> job.Result:
    """Perform the plan: preflight, then one rewrite. Never raises.

    Ordered so that every failure is harmless: the preflight refuses before
    anything is modified, a failed rewrite leaves the originals in place, and
    the caller's prune leaves the data merely unreclaimed rather than deleted.

    Reports a `job.Result`, the same type a backup produces, so the outcome
    reaches the run's exit-code ladder through exactly one path. A rewrite the
    user asked for and that did not happen is worth a non-zero exit; a rewrite
    that found nothing to do is an ordinary skip.

    Pruning is not done here — rewrite is per job, prune is per repo, and the
    caller merges them into the run's single prune pass.
    """
    name = p.job
    if p.query_error is not None:
        # An unreachable repo makes an empty plan unknowable, not empty. Without
        # this, `--now` against a locked repository reports "skipped: no excluded
        # files" and exits 0 — telling a caller the rewrite found nothing to do
        # when in fact it never looked.
        return job.Result(
            name,
            job.ResultStatus.DELAYED
            if p.query_error.retryable
            else job.ResultStatus.FAILED,
            f"could not read repository: {p.query_error.detail}",
        )
    if p.empty:
        return job.Result(
            name, job.ResultStatus.SKIPPED, "no excluded files in stored snapshots"
        )

    logger.info(
        "%s: %d excluded path(s) (%s across %d snapshot(s))",
        p.job,
        len(p.found.entries),
        excludes.human_bytes(p.total_size),
        len(p.snapshot_ids),
    )
    for entry in p.found.ordered_entries()[:20]:
        logger.info("  would remove: %s (%s)", entry.path, excludes.human_bytes(entry.size))
    if len(p.found.entries) > 20:
        logger.info("  ... and %d more", len(p.found.entries) - 20)

    if runner.dry_run:
        return job.Result(
            name, job.ResultStatus.DRY_RUN, f"would rewrite {len(p.snapshot_ids)} snapshot(s)"
        )

    refused = preflight(runner)
    if refused is not None:
        return refused

    started = time.monotonic()
    try:
        runner.run_command(list(p.command))
    except subprocess.CalledProcessError as e:
        status = (
            job.ResultStatus.DELAYED
            if e.returncode in _RETRYABLE
            else job.ResultStatus.FAILED
        )
        todo.record_attempt(
            runner.state_file, TASK_NAME, rc=e.returncode, error="rewrite failed"
        )
        return job.Result(
            name,
            status,
            f"rewrite failed (rc={e.returncode}); original snapshots intact",
        )
    except subprocess.TimeoutExpired:
        todo.record_attempt(runner.state_file, TASK_NAME, rc=None, error="timed out")
        return job.Result(
            name, job.ResultStatus.DELAYED, "rewrite timed out; originals intact"
        )
    logger.info(
        "%s: rewrote %d snapshot(s) in %ds",
        p.job,
        len(p.snapshot_ids),
        int(time.monotonic() - started),
    )

    return job.Result(
        name,
        job.ResultStatus.SUCCESS,
        f"rewrote {len(p.snapshot_ids)} snapshot(s), "
        f"{excludes.human_bytes(p.total_size)} excluded",
    )


def preflight(runner) -> job.Result | None:
    """Refuse before modifying anything; None when the repo is usable.

    Nothing has changed when this runs, so refusing is always safe. A repo that
    is missing, locked or unreadable is a temporary problem that the next
    eligible window will retry, so it reports as delayed rather than failed.
    """
    try:
        rc = runner.repo_ping()
    except subprocess.TimeoutExpired:
        todo.record_attempt(
            runner.state_file, TASK_NAME, rc=None, error="preflight timed out"
        )
        return job.Result(
            runner.job.name,
            job.ResultStatus.DELAYED,
            "preflight refused: repository unreachable (timed out); nothing changed",
        )
    if rc is None:
        return None
    why = _PREFLIGHT_REASONS.get(rc, "repository not usable")
    status = job.ResultStatus.DELAYED if rc in _RETRYABLE else job.ResultStatus.FAILED
    todo.record_attempt(runner.state_file, TASK_NAME, rc=rc, error=why)
    return job.Result(
        runner.job.name, status, f"preflight refused: {why} (rc={rc}); nothing changed"
    )


def _rewrite_cmd(runner, snapshot_ids) -> list[str]:
    """`restic rewrite <ids> … --forget` for exactly these snapshots.

    `--forget` removes exactly the originals restic just rewrote. A separate
    `forget` would also apply the retention policies and could delete snapshots
    beyond this intent.
    """
    return restic.build(
        "rewrite",
        repo_path=runner.repo.path,
        password_file=runner.repo.password_file,
        options=list(runner.repo.restic_options) + list(runner.job.restic_options),
        exclude_file=runner.job.ignore_file or None,
        excludes=runner.job.excludes,
        forget=True,
        extra_args=list(snapshot_ids),
        warn=lambda opt: logger.warning(
            f"restic_options contains reserved flag '{opt}' which snap-bak "
            "manages; ignoring it."
        ),
    )


def warns_on_newest(runner, p: Plan) -> bool:
    """True when the newest snapshot still holds files the excludes should skip.

    The backup that ran moments before this rewrote using the job's current
    excludes, so its snapshot should hold nothing to remove. Matches there mean
    the backup path is not honouring the excludes — a real bug that would
    otherwise be hidden, because the rewrite quietly cleans up after it.
    """
    if p.query_error is not None:
        # We never reached the repo, so "the newest snapshot is clean" is not a
        # fact we have. Answering either way would be a guess.
        return False
    snaps = runner.repo_snapshots()
    if not snaps or not p.snapshot_ids:
        return False
    return snaps[0]["id"] in p.snapshot_ids

# --- the queued task --------------------------------------------------------

# Named here, once, and declared to the registry by the handler that implements
# it. The name is what a human reads in `<job>.json`, so it says both the restic
# operation and the goal rather than being an internal identifier.
TASK_NAME = "rewrite-drop-excluded-files"


@todo.task(TASK_NAME)
def rewrite_drop_excluded(runner, entry) -> tuple[job.Result, set[str]]:
    """Remove this job's excluded files from snapshots restic already holds.

    Uses the job's *current* excludes, the same list the backup that ran
    moments earlier used, so there is nothing to pin and no variant to carry.
    Returns the repos it made prunable, which the run folds into its single
    prune pass.
    """
    result = execute(runner, plan(runner))
    return result, {runner.repo.path} if result.status == job.ResultStatus.SUCCESS else set()
