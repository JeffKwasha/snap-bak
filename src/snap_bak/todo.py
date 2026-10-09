#!/usr/bin/env python3
"""Queued work: things a human asked for that a later run should carry out.

One file per job, `<state>/<job>.json`, holds both that job's last status and
the work queued against it. Keeping them together is the point: an entry in
`queued` *is* the record that the work was requested, so nothing else has to
remember it. When a task succeeds its entry is removed; when it fails the entry
stays and counts the attempt, because the request is still outstanding.

There is no "already done" marker anywhere. An empty queue is the only thing
that means "nothing left to do", which is why nothing here accumulates a record
of completed work in the first place.

A task name is declared once, by the handler that implements it, via @task. The
registry is built from those declarations, so a name can never drift away from
the function it names.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from snap_bak import config, job

logger = logging.getLogger(__name__)

# A handler runs one queued task and reports (Result, repos it made prunable).
# It must not raise; dispatch turns an exception into a failed result so that
# no task can take down a backup run.
Handler = Callable[..., tuple[job.Result, set[str]]]
TASKS: dict[str, Handler] = {}

# `<state>/snap-bak.lock` and `<state>/throughput.json` share the directory but
# belong to no job, so the reaper must never mistake either for a job's state.
NOT_JOB_STATE = ("snap-bak.lock", "throughput.json")

# The one key write_status carries forward. The rest of the document reports the
# run that just happened; `queued` records work that has not happened yet, so a
# status transition must not discard it.
QUEUED_KEY = "queued"


def task(name: str) -> Callable[[Handler], Handler]:
    """Register a task handler under `name`, declared once, at the handler.

    The name is stored verbatim in `<job>.json` and read by humans wondering
    what a queued run will do, so it should read as the restic operation plus
    its goal rather than as an internal identifier.
    """

    def register(fn: Handler) -> Handler:
        if name in TASKS:
            raise ValueError(f"duplicate task name: {name}")
        TASKS[name] = fn
        return fn

    return register


# --- the per-job state file ------------------------------------------------


def _load(path) -> dict:
    return job.read_status(path) or {}


def _store(path, data: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def queued_tasks(path) -> list[dict]:
    """Tasks outstanding for this job, oldest first.

    Tolerates a missing or corrupt file: nothing queued simply means nothing
    queued, and a mangled file must never wedge a run.
    """
    entries = _load(path).get(QUEUED_KEY)
    if not isinstance(entries, list):
        return []
    return [
        e for e in entries if isinstance(e, dict) and isinstance(e.get("task"), str)
    ]


def arm_task(path, task_name: str, *, now: datetime | None = None) -> dict:
    """Queue a task for this job's next run, refusing a duplicate.

    Refusing rather than deduping silently: a second `--later` almost always
    means the user is unsure the first took, and saying so is more useful than
    pretending to add work.
    """
    if any(e["task"] == task_name for e in queued_tasks(path)):
        raise ValueError(f"{task_name} is already queued for {path.stem}")

    data = _load(path)
    entry = {
        "task": task_name,
        "armed_at": (now or datetime.now(timezone.utc)).isoformat(),
        "attempts": 0,
        "last_rc": None,
        "last_error": None,
    }
    data.setdefault(QUEUED_KEY, []).append(entry)
    _store(path, data)
    return entry


def clear_task(path, task_name: str) -> None:
    """Forget a task: it succeeded, or it can never be attempted."""
    data = _load(path)
    data[QUEUED_KEY] = [e for e in data.get(QUEUED_KEY, []) if e.get("task") != task_name]
    _store(path, data)


def record_attempt(path, task_name: str, *, rc: int | None, error: str | None) -> int:
    """Note a failed attempt and return how many there have been.

    The count lets a persistent failure escalate in the log instead of being
    retried nightly in silence. It deliberately does not gate the retry: the
    window gates already bound how often a task runs (at most once per eligible
    window), and every task is idempotent, so re-running is safe.
    """
    data = _load(path)
    attempts = 0
    for entry in data.get(QUEUED_KEY, []):
        if entry.get("task") == task_name:
            attempts = int(entry.get("attempts") or 0) + 1
            entry["attempts"] = attempts
            entry["last_rc"] = rc
            entry["last_error"] = error
            entry["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    data[QUEUED_KEY] = data.get(QUEUED_KEY, [])
    _store(path, data)
    return attempts


# --- running queued work ---------------------------------------------------


def dispatch(path, runner) -> tuple[job.Result | None, set[str]]:
    """Run whatever this job has queued; return its result and repos to prune.

    A task that failed or was delayed keeps its entry and is retried at the next
    eligible window; one that succeeded or found nothing to do has its entry
    removed. Nothing here raises: a queued task must never be able to take down a
    backup run.

    The prune set is returned alongside the Result rather than derived from it,
    because "succeeded" and "left data unreferenced" are different facts — a
    future task could prune without rewriting, or rewrite without pruning.

    A dry run reports and returns without touching the queue at all: simulating a
    run must not consume the request the real run is waiting for.
    """
    dry_run = bool(getattr(runner, "dry_run", False))
    for entry in queued_tasks(path):
        name = entry["task"]
        handler = TASKS.get(name)
        if handler is None:
            detail = f"no handler for task {name!r}; dropping it"
            logger.error("%s: %s", runner.job.name, detail)
            if not dry_run:
                clear_task(path, name)
            return job.Result(runner.job.name, job.ResultStatus.FAILED, detail), set()

        try:
            result, repos = handler(runner, entry)
        except Exception as e:
            # Deliberately blind: a task must never be able to take down a
            # backup run, whatever it did on the way.
            detail = f"{name} raised {e!r}"
            if not dry_run:
                attempts = record_attempt(path, name, rc=None, error=repr(e))
                detail += f" (attempt {attempts})"
            logger.exception("%s: %s", runner.job.name, detail)
            return job.Result(runner.job.name, job.ResultStatus.FAILED, detail), set()

        if not dry_run and _settles(result):
            clear_task(path, name)
        return result, set(repos)
    return None, set()


# A task that failed or was delayed has not done the work, so its request stands.
# Everything else — applied, or nothing to apply — has satisfied it. An entry
# left behind for a task that keeps finding nothing would rescan for ever.
_UNSETTLED = (job.ResultStatus.FAILED, job.ResultStatus.DELAYED, job.ResultStatus.DRY_RUN)


def _settles(result: job.Result) -> bool:
    return result.status not in _UNSETTLED


# --- reaping ---------------------------------------------------------------


def reap(cfg, job_names: set[str]) -> list[str]:
    """Delete state for jobs that have left the config, once they are old enough.

    Only ever touches files for jobs that are *not* in the config, and only once
    they have been that way for `state_retention_days`. The grace period matters:
    a job temporarily removed from the config and put back should not lose its
    delay state. A job still in the config is never reaped, because its file is
    rewritten by every run and reaping it would silently reset the missed-slot
    escalation that drives priority.

    Queued work is never reaped. An entry is a request from a human; discarding
    it because enough time passed would be worse than leaving the file.
    """
    if not config.STATE_DIR.exists():
        return []
    cutoff = (
        datetime.now(timezone.utc).timestamp() - cfg.state_retention_days * 86400
    )
    removed = []
    for path in sorted(config.STATE_DIR.glob("*.json")):
        if path.name in NOT_JOB_STATE or path.stem in job_names:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
        except OSError as e:
            logger.warning("Could not remove stale state %s: %s", path.name, e)
            continue
        removed.append(path.name)
    for name in removed:
        logger.info("Reaped state for a job no longer in the config: %s", name)
    return removed