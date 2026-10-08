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

from snap_bak import excludes, job, restic

logger = logging.getLogger(__name__)

# Outcome.status values. Plain strings, and deliberately not job.ResultStatus:
# an apply outcome is never fed to exit_code(), so this phase cannot move the
# run's exit code.
APPLIED = "applied"
NOOP = "noop"
FAILED = "failed"
DRY_RUN = "dry-run"

LOCKED_ADVICE = (
    "another restic holds the repository lock; "
    "use `apply excludes --later` to rewrite on the next scheduled run"
)

# restic exit codes we can say something useful about.
_PREFLIGHT_REASONS = {
    10: "repository does not exist",
    11: "repository is locked by another restic",
    12: "wrong password",
}


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
        return body


@dataclass(frozen=True)
class Outcome:
    """What executing a plan did.

    Not a `job.Result`: that is the type exit_code() consumes, and keeping the
    types apart makes "apply cannot change the run's exit code" structural
    rather than a property of how each status happens to fall through the
    ladder. `rc` is the restic exit code when there was one, so a caller can
    distinguish a locked repo (11) from a missing one (10).
    """

    status: str
    detail: str
    rc: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in (APPLIED, NOOP, DRY_RUN)


def plan(runner) -> Plan:
    """Scan this job's stored snapshots and decide what a rewrite would remove.

    Read-only. Executes no restic command that can mutate, creates, or delete
    anything; the only restic calls are `snapshots` and `ls --long`, both
    metadata. A repo that dies mid-scan yields fewer targets rather than an
    exception, which is the safe direction: a target we failed to see is not
    removed.
    """
    patterns = excludes.patterns_for_job(runner.job)
    parsed = excludes.parse_patterns(patterns)
    root = str(runner.mount_point)

    invalid = excludes.validate_patterns(patterns)
    if invalid or not parsed:
        # restic would reject these, so there is nothing coherent to plan.
        return Plan(runner.job.name, root, excludes.ExclusionSet())

    found = excludes.ExclusionSet()
    targets: list[str] = []
    for snap in runner.repo_snapshots():
        snapshot_id = snap.get("id")
        if not snapshot_id:
            continue
        label = snap.get("short_id") or snapshot_id[:8]
        try:
            listing = runner.repo_listing(snapshot_id)
        except Exception as e:  # noqa: BLE001 - one bad snapshot is not fatal
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


def execute(runner, p: Plan) -> Outcome:
    """Perform the plan: preflight, then one rewrite. Never raises.

    Ordered so that every failure is harmless: the preflight refuses before
    anything is modified, a failed rewrite leaves the originals in place, and
    the caller's prune leaves the data merely unreclaimed rather than deleted.

    Pruning is not done here — rewrite is per job, prune is per repo, and the
    caller merges them into the run's single prune pass.
    """
    if p.empty:
        return Outcome(
            NOOP, f"no excluded files in {p.job}'s snapshots; nothing to remove"
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
        return Outcome(
            DRY_RUN, f"would rewrite {len(p.snapshot_ids)} snapshot(s)"
        )

    refused = preflight(runner)
    if refused is not None:
        return refused

    started = time.monotonic()
    try:
        runner.run_command(list(p.command))
    except subprocess.CalledProcessError as e:
        return Outcome(
            FAILED,
            f"rewrite failed (rc={e.returncode}); original snapshots intact",
            rc=e.returncode,
        )
    except subprocess.TimeoutExpired:
        return Outcome(FAILED, "rewrite timed out; original snapshots intact")
    logger.info(
        "%s: rewrote %d snapshot(s) in %ds",
        p.job,
        len(p.snapshot_ids),
        int(time.monotonic() - started),
    )

    # Stamp it so the next run skips the scan. The stamp records the snapshot
    # id *after* the rewrite, because `rewrite --forget` replaces the originals
    # with new ids: stamping the pre-rewrite id would make every subsequent run
    # rescan in full and restamp.
    if not runner.dry_run:
        job.write_stamp(
            runner.job.name,
            runner.repo.path,
            excludes.patterns_for_job(runner.job),
            newest_id(runner),
        )
    return Outcome(
        APPLIED,
        f"rewrote {len(p.snapshot_ids)} snapshot(s), "
        f"{excludes.human_bytes(p.total_size)} excluded",
    )


def preflight(runner) -> Outcome | None:
    """Refuse before modifying anything; None when the repo is usable.

    Nothing has changed when this runs, so refusing is always safe.
    """
    try:
        rc = runner.repo_ping()
    except subprocess.TimeoutExpired:
        return Outcome(
            FAILED,
            "preflight refused: repository unreachable (timed out); nothing changed",
        )
    if rc is None:
        return None
    why = _PREFLIGHT_REASONS.get(rc, "repository not usable")
    return Outcome(
        FAILED, f"preflight refused: {why} (rc={rc}); nothing changed", rc=rc
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


def newest_id(runner) -> str | None:
    snaps = runner.repo_snapshots()
    return snaps[0]["id"] if snaps else None