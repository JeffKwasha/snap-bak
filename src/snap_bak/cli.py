#!/usr/bin/env python3
"""snap-bak CLI entry point.

Two entry points share one argument surface:

* ``snap-bak`` (no subcommand) runs the normal orchestrator: validate, plan,
  backup, apply excludes, retain. The systemd service invokes exactly this.
* ``snap-bak show excludes`` / ``snap-bak apply excludes`` inspect and apply
  exclude patterns (see :mod:`snap_bak.excludes`). ``show`` is read-only and
  never creates or removes a snapshot or local file; ``apply`` removes excluded
  files from what restic already stored.

The subcommand words are also the verbs the documentation uses, which is why
they are ``show``/``apply`` rather than a bare noun group.
"""

import argparse
import logging
import os
import subprocess
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

from snap_bak import apply
from snap_bak import config
from snap_bak import excludes
from snap_bak import job
from snap_bak import snapshot as snapshot_model

logger = logging.getLogger(__name__)

SUBCOMMANDS = ("show", "apply")

# Global flags that consume a separate value token; used only to locate the
# subcommand when it follows global flags (`snap-bak -c cfg show excludes ...`).
_VALUE_FLAGS = {"-c", "--config", "-j", "--job", "--log-file", "--limit"}


def _find_subcommand(argv: list[str]) -> int | None:
    """Index of the subcommand in argv, or None.

    Skips global flags and their values so a subcommand may follow them. Stops at
    the first non-flag argument that is not a subcommand, since that is a normal
    run's positional (of which there are none, so it means "not a subcommand").
    """
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in SUBCOMMANDS:
            return i
        if arg.startswith("-"):
            i += 2 if arg in _VALUE_FLAGS else 1
            continue
        return None
    return None


def main() -> None:
    try:
        _main_impl()
    except SystemExit:
        raise
    except Exception as e:
        # No failure in a backup orchestrator should be silent.
        logger.critical(f"FATAL: snap-bak crashed: {e}", exc_info=True)
        sys.exit(config.EXIT_ERROR)


def _restic_update_due(results: list[job.Result], restic_update: str) -> bool:
    """True when the privileged restic update should run after this run.

    restic carries cap_dac_read_search, so it updates only when a backup
    actually completed this run — never on dry-runs, skips, or failures.
    """
    if restic_update != "after":
        return False
    return any(r.status == "success" for r in results)


def _run_restic_update() -> None:
    """Run the privileged 'update-restic' wrapper op after a successful backup.

    snap-bak never runs `restic self-update` itself (the binary carries
    cap_dac_read_search); only root-owned sudo-exec.py may replace it. A failure
    never aborts the run (rc 0 = updated, rc 2 = old binary retained).
    """
    cmd = [config.SUDO, config.SUDO_EXEC, "update-restic"]
    logger.info(f"running {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        logger.error("restic update timed out; continuing with existing restic")
        return
    if result.returncode == 0:
        logger.info("restic update: OK (binary up to date, setcap applied)")
    elif result.returncode == 2:
        logger.warning(
            "restic self-update failed but existing restic is retained and usable; "
            f"continuing ({result.stderr.strip() or 'see wrapper log'})"
        )
    else:
        logger.error(
            f"restic update failed (rc={result.returncode}): "
            f"{result.stderr.strip() or 'see wrapper log'}"
        )


def exit_code(results: list[job.Result]) -> int:
    """Map results to a process exit code via an ordered priority ladder.

    First matching predicate wins: failed > timeout > unreachable > size_limit.
    """

    def has(status, reason=None):
        return any(
            r.status == status and (reason is None or r.reason == reason)
            for r in results
        )

    def any_timeout():
        return any(r.timeout for r in results)

    ladder = [
        (lambda: has("failed"), config.EXIT_PERM_FAILED),
        (any_timeout, config.EXIT_PERM_TIMEOUT),
        (lambda: has("delayed", "unreachable"), config.EXIT_TEMP_UNREACHABLE),
        (lambda: has("delayed"), config.EXIT_TEMP_SIZE_LIMIT),
    ]
    for predicate, code in ladder:
        if predicate():
            return code
    return config.EXIT_SUCCESS


# --------------------------------------------------------------------------
# show / apply excludes
# --------------------------------------------------------------------------


def _selected_jobs(cfg: config.Config, name: str | None, path: str) -> list:
    """Resolve jobs, optionally filtered by name.

    Reusing the orchestrator's own resolution means the inspection commands see
    exactly the repos, mount points and tags a real run would use — a snapshot
    the user is shown is a snapshot a run would act on.
    """
    jobs = cfg.resolve(path)
    if not name:
        return jobs
    chosen = [j for j in jobs if j.job.name == name]
    if not chosen:
        logger.error(f"Job '{name}' not found in config.")
        sys.exit(config.EXIT_ERROR)
    return chosen


def _patterns_for(rj) -> tuple[list[str], list[excludes.Pattern], str]:
    """The job's pattern list, parsed, plus the path prefix restic stored.

    Every mode goes through here: they must all answer the same question
    ("what would the next backup skip?"), so they must all read the patterns the
    same way and match them against the same path basis.
    """
    raw = excludes.patterns_for_job(rj.job)
    return raw, excludes.parse_patterns(raw), rj.backup_root


def _do_lint(jobs: list, as_json: bool) -> int:
    """Report malformed patterns, and anchored ones that can never match.

    Always run and silent when clean, so there is no `--lint` flag: it is not
    optional, and offering it would imply that the modes skip it.
    """
    found = []
    for rj in jobs:
        raw, _, root = _patterns_for(rj)
        for issue in excludes.lint(raw, root):
            found.append({"job": rj.job.name, **vars(issue)})

    if as_json:
        print(json.dumps({"lint": {"issues": found}}, indent=2))
        return config.EXIT_SUCCESS

    for issue in found:
        print(f"[{issue['job']}] {issue['pattern']}: {issue['message']}")
    if not found:
        logger.info(f"excludes OK ({len(jobs)} job(s), nothing to report)")
    return config.EXIT_SUCCESS


def _scan_live(rj) -> tuple[excludes.ExclusionSet, str]:
    """The live source subvolume: no mount, no restic, no writes."""
    _, patterns, root = _patterns_for(rj)
    found = excludes.ExclusionSet()
    for path, size, is_dir in excludes.walk_excluded(
        Path(rj.job.source_subvolume), root, patterns
    ):
        found.add(None, path, size, is_dir)
    return found, root


def _scan_snaps(rj) -> tuple[excludes.ExclusionSet, str]:
    """Each local btrfs snapshot dir, labelled by its snapshot name."""
    _, patterns, root = _patterns_for(rj)
    found = excludes.ExclusionSet()
    for snap in snapshot_model.Snapshot.scan(rj.job.snapshot_dir):
        for path, size, is_dir in excludes.walk_excluded(snap.path, root, patterns):
            found.add(snap.name, path, size, is_dir)
    return found, root


def _do_live(jobs: list, limit: int | None, as_json: bool) -> int:
    """Walk each job's live source subvolume and list what the rules exclude.

    Read-only, and local: nothing is mounted and restic is never invoked, so
    this answers "what would the next backup skip?" without touching the repo.
    """
    scans = [(rj, *_scan_live(rj)) for rj in jobs]
    if as_json:
        print(json.dumps({"live": [apply.payload(r.job.name, root, f) for r, f, root in scans]}, indent=2))
        return config.EXIT_SUCCESS
    for rj, found, root in scans:
        print(f"=== {rj.job.name} ({root}): {len(found.entries)} excluded, "
              f"{excludes.human_bytes(found.total_size)} ===")
        if not found.entries:
            continue
        for line in excludes.format_entries(found, limit):
            print(f"  {line}")
    return config.EXIT_SUCCESS


def _do_snaps(jobs: list, limit: int | None, as_json: bool) -> int:
    """Per-snapshot table across each job's local btrfs snapshot dirs."""
    scans = [(rj, *_scan_snaps(rj)) for rj in jobs]
    if as_json:
        print(json.dumps({"snaps": [apply.payload(r.job.name, root, f) for r, f, root in scans]}, indent=2))
        return config.EXIT_SUCCESS
    for rj, found, root in scans:
        print(f"=== {rj.job.name} ({root}) ===")
        if not found.per_snapshot:
            print("  no snapshots found")
            continue
        print(excludes.format_table(found, limit))
    return config.EXIT_SUCCESS


def _do_repo(jobs: list, limit: int | None, as_json: bool) -> int:
    """What restic already stores, per snapshot (network, metadata only).

    This is the same plan `apply` would execute, from the same `plan()` call, so
    the table a user reads here is by construction the table the rewrite acts on.
    """
    plans = [apply.plan(job.Runner(rj)) for rj in jobs]
    if as_json:
        print(json.dumps({"repo": [p.to_json() for p in plans]}, indent=2))
        return config.EXIT_SUCCESS
    for p in plans:
        print(f"=== {p.job} ({p.root}) ===")
        if not p.found.per_snapshot:
            print("  no snapshots matched")
            continue
        print(excludes.format_table(p.found, limit))
    return config.EXIT_SUCCESS


def _do_apply(jobs: list, now: bool, later: bool, as_json: bool, limit: int | None) -> int:
    """Plan (default), execute (`--now`), or arm (`--later`) the rewrite.

    All three are restic-only. Nothing here reads or removes a live file or a
    local snapshot: excluded data is removed from what restic already stored.
    """
    if now and later:
        logger.error("--now and --later are mutually exclusive.")
        return config.EXIT_ERROR
    if now:
        return _apply_now(jobs, as_json)
    if later:
        return _arm(jobs, as_json)
    return _plan(jobs, as_json, limit)


def _plan(jobs: list, as_json: bool, limit: int | None) -> int:
    """The dry-run: which snapshots would change, how much they hold, and the
    exact command that would run. Read-only, and the same plan `--now` uses."""
    plans = [apply.plan(job.Runner(rj)) for rj in jobs]
    if as_json:
        print(json.dumps({"plan": [p.to_json() for p in plans]}, indent=2))
        return config.EXIT_SUCCESS
    for p in plans:
        print(f"=== {p.job} ({p.root}) ===")
        if p.empty:
            print("  nothing to remove")
            continue
        print(excludes.format_table(p.found, limit))
        print(f"  would rewrite {len(p.snapshot_ids)} snapshot(s): "
              f"{', '.join(p.snapshot_ids)}")
        print(f"  $ {' '.join(p.command)}")
    return config.EXIT_SUCCESS


def _arm(jobs: list, as_json: bool) -> int:
    """Record a one-shot request for the next scheduled run to act on.

    Lives in the state dir rather than config so it needs no write access to the
    root-owned config file, and is consumed (never reused) by that run.
    """
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    pending = job.arm_file()
    armed = {
        "jobs": [rj.job.name for rj in jobs],
        "armed_at": datetime.now(timezone.utc).isoformat(),
    }
    pending.write_text(json.dumps(armed, indent=2))
    logger.info(f"armed: the next scheduled run will apply excludes ({pending})")
    if as_json:
        print(json.dumps({"armed": armed}, indent=2))
    return config.EXIT_SUCCESS


def _apply_now(jobs: list, as_json: bool) -> int:
    """Execute the rewrite for each job, serially, then one prune per repo.

    Each job is independent: one failing does not stop the others, and every
    outcome is reported rather than raised. No lock is taken — restic serializes
    on the repository's own lock, so a contended run fails instantly below and
    the answer is `--later`, which rides the next scheduled run.
    """
    results = []
    pruned: dict[str, config.ResolvedJob] = {}
    for rj in jobs:
        runner = job.Runner(rj)
        outcome = apply.execute(runner, apply.plan(runner))
        results.append(_outcome_json(rj.job.name, outcome))
        if outcome.status == apply.APPLIED:
            pruned.setdefault(runner.repo.path, rj)

    # Prune is a repo-level concern: one pass reclaims everything every rewrite
    # left unreferenced, rather than one pass per job on a shared repo.
    for repo_path, rj in sorted(pruned.items()):
        prune = job.Runner(rj).prune()
        results.append(
            {
                "job": rj.job.name,
                "status": "pruned"
                if prune.status == job.ResultStatus.RETENTION_OK
                else "prune-failed",
                "detail": prune.reason or "reclaimed the rewritten space",
                "rc": None,
            }
        )

    if as_json:
        print(json.dumps({"applied": results}, indent=2))
    else:
        for entry in results:
            print(f"[{entry['job']}] {entry['status']}: {entry['detail']}")

    return (
        config.EXIT_ERROR
        if any(r["status"] in ("failed", "prune-failed") for r in results)
        else config.EXIT_SUCCESS
    )


def _outcome_json(job_name: str, outcome: apply.Outcome) -> dict:
    detail = outcome.detail
    if outcome.rc == 11:
        detail = f"{detail}; {apply.LOCKED_ADVICE}"
    return {"job": job_name, "status": outcome.status, "detail": detail, "rc": outcome.rc}


def _apply_excludes_phase(runs: list, *, armed: set[str]) -> set[str]:
    """Retroactively drop excluded files from stored snapshots, gated per job.

    Returns the repos whose space the run's single prune must reclaim.

    Every gate is a *skip*, never a failure: removing files the user has not
    inspected must not be able to change a backup result or the run's exit code.
    Cheapest first:

      1. armed: `apply_excludes: true`, or named by a `--later` arm
      2. the job has excludes at all
      3. inside the backup window (a job with no window is never time-boxed)
      4. at least half the window remains — "ample time"
      5. the completion stamp is not already current (nothing newer to remove)

    Takes Runners, not ResolvedJobs, so the only thing available here is a
    resolved Runner's public surface — which is what made the previous version
    confuse Job with ResolvedJob and raise AttributeError on first use.
    """
    to_prune: set[str] = set()
    for runner in runs:
        name = runner.job.name
        if not (runner.job.apply_excludes or name in armed):
            continue
        if not (runner.job.excludes or runner.job.ignore_file):
            continue
        if not runner.within_backup_window():
            logger.info("%s: apply excludes skipped (outside backup window)", name)
            continue
        if not job.half_window_remaining(runner):
            logger.info(
                "%s: apply excludes skipped (less than half the backup window remains)",
                name,
            )
            continue
        patterns = excludes.patterns_for_job(runner.job)
        if job.stamp_current(
            runner.job.name, runner.repo.path, patterns, apply.newest_id(runner)
        ):
            logger.info("%s: apply excludes skipped (already applied)", name)
            continue

        outcome = apply.execute(runner, apply.plan(runner))
        logger.info("%s: apply excludes %s: %s", name, outcome.status, outcome.detail)
        if outcome.status == apply.APPLIED:
            to_prune.add(runner.repo.path)
    return to_prune


class _RefuseSelector(argparse.Action):
    """An argument a verb accepts only to explain why it will not take it.

    `apply` removes files from what restic stored. A selector aimed at the live
    filesystem or the local snapshots is a mistake worth naming, not an
    unrecognized-flag mystery — and never something to silently discard.
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(
            f"{option_string} inspects the live filesystem or the local snapshots, "
            "but `apply excludes` only removes files from restic. To inspect what "
            "restic stores, use `snap-bak show excludes --repo`."
        )


def excludes_parser(verb: str) -> argparse.ArgumentParser:
    """The argument parser for one verb.

    Split per verb on purpose: `show` is read-only and may inspect anything,
    while `apply` mutates restic and refuses the selectors that would imply
    otherwise.
    """
    parser = argparse.ArgumentParser(
        prog=f"snap-bak {verb} excludes",
        description=(
            "Inspect restic exclude patterns (show), or remove excluded files "
            "from existing restic snapshots (apply)."
        ),
        epilog=(
            "Cost: patterns are linted on every `show`, silently when clean. "
            "--live and --snaps are local and instant; --repo reaches the network "
            "for metadata only. `apply --now` prunes after rewriting, which "
            "repacks and re-uploads (minutes to hours); prefer --later."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("object", choices=("excludes",))
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("-j", "--job", default=None)
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Bound displayed rows (totals still cover all)."
    )

    if verb == "show":
        parser.add_argument("--live", action="store_true", help="Match the live filesystem.")
        parser.add_argument(
            "--snaps", action="store_true", help="Match local btrfs snapshots (table)."
        )
        parser.add_argument(
            "--repo", action="store_true", help="Match files already in restic (network)."
        )
        parser.add_argument("--all", action="store_true", help="Every selector.")
    else:
        parser.add_argument(
            "--now", action="store_true", help="Execute the rewrite now."
        )
        parser.add_argument(
            "--later", action="store_true", help="Arm the next scheduled run."
        )
        for refused in ("--live", "--snaps", "--all"):
            parser.add_argument(
                refused, action=_RefuseSelector, help=argparse.SUPPRESS
            )
    return parser


def _excludes_main(argv: list[str], config_path: str) -> None:
    """Dispatch one verb, then exit with its status.

    Exits here rather than returning: `show` and `apply` report failures through
    their return code, and a verb whose status is discarded is a verb that
    always looks successful to whatever is monitoring it.
    """
    verb = argv[0]
    args = excludes_parser(verb).parse_args(argv[1:])
    cfg_path = args.config or config_path
    jobs = _selected_jobs(config.Config.load(Path(cfg_path)), args.job, cfg_path)

    if verb == "show":
        _do_lint(jobs, args.json)  # always; silent when clean
        for mode in _selected_modes(args):
            if mode == "live":
                _do_live(jobs, args.limit, args.json)
            elif mode == "snaps":
                _do_snaps(jobs, args.limit, args.json)
            elif mode == "repo":
                _do_repo(jobs, args.limit, args.json)
        sys.exit(config.EXIT_SUCCESS)

    sys.exit(_do_apply(jobs, args.now, args.later, args.json, args.limit))


def _selected_modes(args) -> tuple[str, ...]:
    """Which listing modes to run.

    `--all` runs everything; explicit selectors run just those; and no selector
    at all defaults to `--live` — the fastest mode — so the common question
    ("are my patterns sane, and what do they hit?") costs nothing.
    """
    if args.all:
        return ("live", "snaps", "repo")
    chosen = tuple(m for m in ("live", "snaps", "repo") if getattr(args, m))
    return chosen or ("live",)


def _main_parser() -> argparse.ArgumentParser:
    """The normal-run argument parser (also handles --list-jobs)."""
    parser = argparse.ArgumentParser(
        description="Btrfs Snapshot -> Restic Backup Orchestrator",
        epilog="Config: %(default)s | Env: SNAPBAK_CONFIG_FILE, SNAPBAK_JOB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("SNAPBAK_CONFIG_FILE", str(config.DEFAULT_CONFIG)),
        help="Path to config JSON file.",
    )
    parser.add_argument(
        "-j",
        "--job",
        default=os.environ.get("SNAPBAK_JOB"),
        help="Run a specific job by name (default: all jobs).",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Simulate backup. Runs preflight but skips upload & deletion.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Also write logs to this file (default: stderr only).",
    )
    parser.add_argument(
        "-l",
        "--list-jobs",
        action="store_true",
        help="List all configured jobs and exit.",
    )
    return parser


def _add_file_log(path: str) -> None:
    """Also write logs to `path` (default is stderr only)."""
    fh = logging.FileHandler(path)
    fh.setFormatter(logging.Formatter(config._FILE_FORMAT))
    logging.getLogger().addHandler(fh)


def _main_impl() -> None:
    argv = sys.argv[1:]

    # A subcommand may follow global flags. Locating it first means `--help`
    # after the subcommand reaches the subparser, not this one.
    sub = _find_subcommand(argv)
    if sub is not None:
        args, _ = _main_parser().parse_known_args(argv[:sub])
        if args.log_file:
            _add_file_log(args.log_file)
        _excludes_main(argv[sub:], args.config)
        return

    args = _main_parser().parse_args(argv)

    if args.log_file:
        _add_file_log(args.log_file)

    _run_orchestrator(args)


def _run_orchestrator(args) -> None:
    """The normal run: validate, plan, backup, retain."""
    cfg = config.Config.load(Path(args.config))
    jobs = cfg.resolve(str(args.config))

    if args.list_jobs:
        print("Configured jobs:")
        for j in jobs:
            print(
                f"  - {j.job.name}: {j.job.snapshot_dir} -> {j.job.repo} ({j.repo.path})"
            )
        sys.exit(0)

    if args.job:
        jobs = [j for j in jobs if j.job.name == args.job]
        if not jobs:
            logger.error(f"Job '{args.job}' not found in config.")
            sys.exit(config.EXIT_ERROR)

    # Check mount base exists before acquiring the lock.
    mount_base = Path(cfg.mount_base)
    if not mount_base.exists():
        logger.error(f"Mount base {mount_base} does not exist")
        sys.exit(config.EXIT_MOUNT_ERROR)

    # Phase 1: password-file permissions are a hard gate (exit 4).
    for j in jobs:
        job.check_password_file(j.repo)

    # Acquire the global lock to prevent multiple snap-bak instances.
    with job.GlobalLock(cfg.stuck_threshold, cfg.stuck_wait):
        # One Runner per resolved job sharing one per-run repo-state cache;
        # ordered by effective priority (delayed jobs escalate each missed slot).
        repo_state: dict = {}
        runners = [
            job.Runner(j, dry_run=args.dry_run, repo_state=repo_state) for j in jobs
        ]
        runners.sort(key=lambda r: r.effective_priority(), reverse=True)

        # Phase A — backups run SERIALLY (single process, single systemd oneshot).
        results = []
        for r in runners:
            results.append(r.run())
            job.touch_global_lock()

        # Phase A2 — retroactively drop excluded files from stored snapshots, for
        # the jobs that asked for it (`apply_excludes: true`, or a `--later` arm,
        # consumed here whatever the outcome). Its outcomes are logged rather
        # than appended to `results`: removing data a user has not inspected must
        # not be able to change a backup result or this run's exit code.
        rewritten_repos = _apply_excludes_phase(
            runners, armed=set(job.consume_pending())
        )

        # Phase B — retention serially per job.
        forgotten_repos: set[str] = set()
        for r in runners:
            if not r.job.backup_retention:
                continue
            res = r.retention()
            if res and res.status == "retention_ok":
                forgotten_repos.add(r.repo.path)

        # Phase C — ONE prune per distinct repo, covering both retention's
        # forgotten snapshots and the space Phase A2 made unreferenced. Prune is
        # by far the most expensive restic operation here, so a repo is pruned
        # once per run whatever asked for it.
        for repo_path in sorted(rewritten_repos | forgotten_repos):
            rep = next(r for r in runners if r.repo.path == repo_path)
            results.append(rep.prune())

        # Exit codes for monitoring: a *backup* failure is permanent; retention
        # failure alone is not (data is safe, prune retries next run). The
        # retention/update side effects only run on the success path.
        code = exit_code(results)
        if code == config.EXIT_SUCCESS:
            if any(r.status == "retention_failed" for r in results):
                logger.error(
                    "One or more jobs completed backup but failed retention "
                    "(repository locked by an orphaned restic?). See per-job status "
                    "files; retention is retried on the next run."
                )
            if _restic_update_due(results, cfg.restic_update):
                _run_restic_update()
        sys.exit(code)


if __name__ == "__main__":
    main()
