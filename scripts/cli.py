#!/usr/bin/env python3
"""snap-bak CLI entry point.

Composes ``config`` and ``job``: argparse, password gate, backup/retention
phases under the global lock, then a results -> exit-code mapping.
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

try:
    from scripts import config
except ImportError:  # installed standalone under /opt/snap-bak/
    import config

try:
    from scripts import job
except ImportError:  # installed standalone under /opt/snap-bak/
    import job

logger = logging.getLogger(__name__)


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


def _main_impl() -> None:
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
    args = parser.parse_args()

    # Optional file log: reconfigure root logger to ALSO write to args.log_file
    # (default is stderr-only, persisted to the journal by systemd).
    if args.log_file:
        fh = logging.FileHandler(args.log_file)
        fh.setFormatter(logging.Formatter(config._FILE_FORMAT))
        logging.getLogger().addHandler(fh)

    cfg = config.load_config(Path(args.config))
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

        # Phase B — retention serially per job, then ONE prune per distinct repo.
        forgotten_repos: set[str] = set()
        for r in runners:
            if not r.job.backup_retention:
                continue
            res = r.retention()
            if res and res.status == "retention_ok":
                forgotten_repos.add(r.repo.path)

        for repo_path in sorted(forgotten_repos):
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
