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
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from snap_bak import __version__, apply, config, excludes, job, todo
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

    def any_unreachable():
        # Matched by text, not by a sentinel string: `apply --now` reports the
        # restic error verbatim so the reason survives into the log, and routing
        # that through the ladder is what keeps an unreachable repo from exiting
        # 0 as if the rewrite had simply found nothing to do.
        return any(r.status == "delayed" and "could not read repository" in (r.reason or "")
                   for r in results)

    ladder = [
        (lambda: has("failed"), config.EXIT_PERM_FAILED),
        (any_timeout, config.EXIT_PERM_TIMEOUT),
        (lambda: has("delayed", "unreachable") or any_unreachable(),
         config.EXIT_TEMP_UNREACHABLE),
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
        # Names are the one thing an agent has to know before it can ask
        # anything, so the error names them. Guessing a job name otherwise ends
        # in a bare "not found" and a retry loop with nothing to vary.
        available = ", ".join(j.job.name for j in jobs)
        logger.error(f"Job '{name}' not found in config. Available: {available}")
        sys.exit(config.EXIT_ERROR)
    return chosen


class PatternOverride:
    """A trial pattern set for one `show` invocation, and how it differs.

    Lets a working file be evaluated against the real config without editing it.
    `--excludes-file` and `--exclude` accept exactly the syntax the config's own
    `ignore_file` uses, so a pattern can be copied between them unchanged; both
    are added to the config's patterns rather than replacing them, because the
    question being asked is "what would these *extra* rules exclude, and what
    would they cost".

    Order is preserved and meaningful: restic applies `!` negation in sequence,
    and config patterns come first so an added rule cannot accidentally re-
    include something the config excludes.
    """

    def __init__(
        self,
        excludes_file: str | None = None,
        extra: list[str] | None = None,
    ):
        self.excludes_file = excludes_file
        self.extra = list(extra or [])

    @property
    def active(self) -> bool:
        return bool(self.excludes_file or self.extra)

    def apply(self, config_patterns: list[str]) -> list[str]:
        """The patterns to evaluate: config rules, then the trial's own."""
        added: list[str] = []
        if self.excludes_file:
            added.extend(excludes.read_pattern_file(self.excludes_file))
        added.extend(self.extra)
        return list(config_patterns) + added

    def diff(self, config_patterns: list[str]) -> dict:
        """Which of the effective patterns are not in the config's set.

        `added` is the whole point: these are the rules the trial adds, and they
        are the ones whose rows get listed. `removed` is empty by construction
        — a trial never takes a rule away, since it cannot edit the config — and
        is kept in the schema so a caller can see that explicitly rather than
        inferring it.

        An edited rule therefore reads as one addition (`*.tmpx`) alongside the
        config's `*.tmp` still in force, which is what actually happens: both are
        evaluated. Set difference on the text, so the change is visible rather
        than implied.
        """
        effective = self.apply(config_patterns)
        added = [p for p in effective if p not in config_patterns]
        removed = [p for p in config_patterns if p not in effective]
        return {
            "excludes_file": self.excludes_file,
            "effective": effective,
            "config": list(config_patterns),
            "added": added,
            "removed": removed,
        }

    def changed_rules(self, config_patterns: list[str]) -> set[str]:
        """The rule texts a caller would want called out: the additions.

        Removals cannot match anything, so they appear in the diff header and
        never as output rows.
        """
        return set(self.diff(config_patterns)["added"])


def _patterns_for(
    rj, override: PatternOverride | None = None
) -> tuple[list[str], list[excludes.Pattern], str]:
    """The job's pattern list, parsed, plus the path prefix restic stored.

    Every mode goes through here: they must all answer the same question
    ("what would the next backup skip?"), so they must all read the patterns the
    same way and match them against the same path basis.
    """
    raw = excludes.patterns_for_job(rj.job)
    if override is not None and override.active:
        raw = override.apply(raw)
    return raw, excludes.parse_patterns(raw), rj.backup_root


def _do_lint(
    jobs: list, as_json: bool, override: PatternOverride | None = None
) -> int:
    """Report malformed patterns, and anchored ones that can never match.

    Always run and silent when clean, so there is no `--lint` flag: it is not
    optional, and offering it would imply that the modes skip it.

    Linted against the patterns actually being evaluated, so a trial file's own
    broken pattern is reported rather than quietly ignored.
    """
    found = []
    for rj in jobs:
        raw, _, root = _patterns_for(rj, override)
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


def _scan_live(
    rj,
    attribute: bool = False,
    override: PatternOverride | None = None,
    progress=None,
) -> tuple[excludes.ExclusionSet, str]:
    """The live source subvolume: no mount, no restic, no writes."""
    _, patterns, root = _patterns_for(rj, override)
    found = excludes.ExclusionSet()
    for walked in excludes.walk_excluded(
        Path(rj.job.source_subvolume), root, patterns,
        attribute_rules=attribute, progress=progress,
    ):
        found.add(
            None, walked.path, walked.size, walked.is_dir, walked.rule, walked.files
        )
    return found, root


def _scan_snaps(
    rj,
    attribute: bool = False,
    override: PatternOverride | None = None,
    progress=None,
) -> tuple[excludes.ExclusionSet, str]:
    """Each local btrfs snapshot dir, labelled by its snapshot name."""
    _, patterns, root = _patterns_for(rj, override)
    found = excludes.ExclusionSet()
    for snap in snapshot_model.Snapshot.scan(rj.job.snapshot_dir):
        for walked in excludes.walk_excluded(
            snap.path, root, patterns,
            attribute_rules=attribute, progress=progress,
        ):
            found.add(
                snap.name, walked.path, walked.size, walked.is_dir,
                walked.rule, walked.files,
            )
    return found, root


@dataclass
class _View:
    """How one `show` invocation should be rendered.

    Bundles the presentation choices so the three modes cannot drift on what
    "text output" means — the size column, the rule grouping and the totals
    footer have to appear consistently whether the reader asked for `--live` or
    `--snaps`.
    """

    limit: int | None = None
    as_json: bool = False
    fmt: str = "text"
    verbose: bool = False
    override: PatternOverride | None = None
    # Off unless the caller asks or stderr is a terminal: a piped or journalled
    # run wants results, not a running commentary.
    progress: bool = False

    @classmethod
    def build(cls, args, override) -> "_View":
        return cls(
            limit=args.limit,
            as_json=args.json,
            fmt=args.format,
            verbose=args.verbose,
            override=override,
            # Never while emitting a machine-readable document.
            progress=args.verbose or (
                args.format == "text"
                and not args.json
                and sys.stderr.isatty()
            ),
        )

    @property
    def csv(self) -> bool:
        return self.fmt == "csv"

    def only_rules(self, rj) -> set[str] | None:
        """Which rows to list, or None for all of them.

        A trial set normally lists only what its own new rules matched: the
        config's rules are already in effect, so re-listing thousands of rows
        they account for buries the answer. `-v` asks for everything.
        """
        if self.verbose or self.override is None or not self.override.active:
            return None
        base = excludes.patterns_for_job(rj.job)
        changed = self.override.changed_rules(base)
        return changed or None

    def totals_line(self, found: excludes.ExclusionSet) -> str:
        """Bytes and files for the whole set, never just the shown rows."""
        return (
            f"{excludes.human_bytes(found.total_size)} in "
            f"{found.total_files} file(s), {len(found.entries)} row(s)"
        )


def _progress_reporter(rj, enabled: bool):
    """A walk callback that reports progress on stderr, throttled.

    Only when stderr is a terminal or the caller asked for it: on a pipe or in a
    journal, an unending stream of counts is noise, and in CSV or JSON output it
    would corrupt the document. Written to stderr so `--format csv` piped into a
    spreadsheet stays clean either way.

    Throttled to whole seconds, because the callback fires per excluded file and
    a 40k-file tree would otherwise emit 40k lines.
    """
    if not enabled:
        return None
    state = {"last": 0.0}

    def report(files: int, nbytes: int) -> None:
        now = time.monotonic()
        if now - state["last"] < 1.0:
            return
        state["last"] = now
        print(
            f"  scanning {rj.job.name}: {files} file(s), "
            f"{excludes.human_bytes(nbytes)} so far...",
            file=sys.stderr,
            flush=True,
        )

    return report


def _do_live(jobs: list, view: _View) -> int:
    """Walk each job's live source subvolume and list what the rules exclude.

    Read-only, and local: nothing is mounted and restic is never invoked, so
    this answers "what would the next backup skip?" without touching the repo.
    """
    scans = [
        (rj, *_scan_live(rj, attribute=True, override=view.override,
                          progress=_progress_reporter(rj, view.progress)))
        for rj in jobs
    ]
    if view.as_json:
        print(json.dumps({"live": [apply.payload(r.job.name, root, f) for r, f, root in scans]}, indent=2))
        return config.EXIT_SUCCESS
    if view.csv:
        rows = []
        for rj, found, _root in scans:
            rows.extend(_csv_rows(rj, found, view))
        print(excludes.to_csv(rows), end="")
        return config.EXIT_SUCCESS

    for rj, found, root in scans:
        print(f"=== {rj.job.name} ({root}): {view.totals_line(found)} ===")
        if not found.entries:
            print("  nothing excluded by these rules")
            continue
        for line in excludes.format_entries(
            found, view.limit, only_rules=view.only_rules(rj)
        ):
            print(line)
        print(f"  TOTAL: {view.totals_line(found)}")
    return config.EXIT_SUCCESS


def _csv_rows(rj, found: excludes.ExclusionSet, view: _View) -> list[dict]:
    """One record per excluded row, for a spreadsheet.

    `bytes` stays numeric so a sum works; `bytes_human` is there for reading.
    `files` is the count the row stands for, so a directory row carrying a
    subtree reports the subtree's file count rather than 1.
    """
    only = view.only_rules(rj)
    rows = []
    for entry in found.ordered_entries():
        if only is not None and entry.rule not in only:
            continue
        rows.append(
            {
                "job": rj.job.name,
                "rule": entry.rule or "",
                "path": entry.path,
                "is_dir": entry.is_dir,
                "files": entry.files,
                "bytes": entry.size,
                "bytes_human": excludes.human_bytes(entry.size),
            }
        )
    return rows


def _do_snaps(jobs: list, view: _View) -> int:
    """Per-snapshot table across each job's local btrfs snapshot dirs."""
    scans = [
        (rj, *_scan_snaps(rj, attribute=True, override=view.override,
                          progress=_progress_reporter(rj, view.progress)))
        for rj in jobs
    ]
    if view.as_json:
        print(json.dumps({"snaps": [apply.payload(r.job.name, root, f) for r, f, root in scans]}, indent=2))
        return config.EXIT_SUCCESS
    if view.csv:
        rows = []
        for rj, found, _root in scans:
            rows.extend(_csv_rows(rj, found, view))
        print(excludes.to_csv(rows), end="")
        return config.EXIT_SUCCESS

    for rj, found, root in scans:
        print(f"=== {rj.job.name} ({root}): {view.totals_line(found)} ===")
        if not found.per_snapshot:
            print("  no snapshots found")
            continue
        print(excludes.format_table(found, view.limit))
        print()
        for line in excludes.format_entries(
            found, view.limit, only_rules=view.only_rules(rj)
        ):
            print(line)
        print(f"  TOTAL: {view.totals_line(found)}")
    return config.EXIT_SUCCESS


def _do_repo(jobs: list, view: _View) -> int:
    """What restic already stores, per snapshot (network, metadata only).

    This is the same plan `apply` would execute, from the same `plan()` call, so
    the table a user reads here is by construction the table the rewrite acts on.
    """
    plans = []
    for rj in jobs:
        runner = job.Runner(rj)
        p = apply.plan(runner, patterns=_patterns_for(rj, view.override)[1])
        plans.append((rj, p))

    if view.as_json:
        print(json.dumps({"repo": [p.to_json() for _r, p in plans]}, indent=2))
        return _plan_exit_code([p for _r, p in plans])
    if view.csv:
        rows = []
        for rj, p in plans:
            if p.query_error is not None:
                continue
            rows.extend(_csv_rows(rj, p.found, view))
        print(excludes.to_csv(rows), end="")
        return _plan_exit_code([p for _r, p in plans])

    for rj, p in plans:
        print(f"=== {p.job} ({p.root}): {view.totals_line(p.found)} ===")
        if p.query_error is not None:
            print(f"  {describe_query_error(p.query_error)}")
            continue
        if not p.found.per_snapshot:
            print("  no snapshots matched")
            continue
        print(excludes.format_table(p.found, view.limit))
        print()
        for line in excludes.format_entries(
            p.found, view.limit, only_rules=view.only_rules(rj)
        ):
            print(line)
        print(f"  would rewrite {len(p.snapshot_ids)} snapshot(s)")
        print(f"  TOTAL: {view.totals_line(p.found)}")
    return _plan_exit_code([p for _r, p in plans])


def describe_query_error(e: job.RepoQueryError) -> str:
    """One sentence naming what restic said and what to do about it.

    An agent reading a log has no way to infer from a bare exit code whether to
    fix a path, wait for a lock, or repair credentials, so the restic code is
    mapped to the action it implies.
    """
    reasons: dict[int | None, str] = {
        10: "the repository does not exist at that path",
        11: "another restic holds the repository lock",
        12: "the password file is wrong or unreadable",
    }
    reason = reasons.get(e.returncode, "restic could not be queried")
    advice = (
        "Retry later; the repo may be reachable or unlocked by then."
        if e.retryable
        else "This will not resolve on its own — fix the config or repository."
    )
    return f"could not read repository: {reason} (restic exit {e.returncode}). {advice}"


def _plan_exit_code(plans: list) -> int:
    """Success unless a repo could not be read at all.

    A plan that scanned successfully and found nothing is a success, and says so.
    A plan that never reached the repository is not: reporting 0 there would tell
    an operator — or an agent reading the exit code — that a backup was verified
    clean when in fact nothing was checked. restic 10/11/12 mean "not now"
    (delayed, 30); anything else means the setup is wrong (failed, 41).
    """
    if all(p.query_error is None for p in plans):
        return config.EXIT_SUCCESS
    if any(not p.query_error.retryable for p in plans if p.query_error):
        return config.EXIT_PERM_FAILED
    return config.EXIT_TEMP_UNREACHABLE


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
        return _plan_exit_code(plans)
    for p in plans:
        print(f"=== {p.job} ({p.root}) ===")
        if p.query_error is not None:
            print(f"  {describe_query_error(p.query_error)}")
            continue
        if p.empty:
            print("  nothing to remove")
            continue
        print(excludes.format_table(p.found, limit))
        print(f"  would rewrite {len(p.snapshot_ids)} snapshot(s): "
              f"{', '.join(p.snapshot_ids)}")
        print(f"  $ {' '.join(p.command)}")
    return _plan_exit_code(plans)


def _arm(jobs: list, as_json: bool) -> int:
    """Queue the rewrite for each job's next scheduled run.

    Validates before queueing. The user has just been told by `show excludes
    --repo` what would be removed, but there is no reason to queue work that
    provably has nothing to do — an empty repo means the store went empty or
    retention took everything, which is a problem to notice, not a rewrite to
    schedule. The plan is the same read-only call the dry-run already makes.

    Writes to the state dir, never the config: the config is root-owned and
    declarative, and it must never carry a request.
    """
    queued, refused = [], []
    for rj in jobs:
        runner = job.Runner(rj)
        p = apply.plan(runner)
        if p.query_error is not None:
            detail = describe_query_error(p.query_error)
            logger.error("%s: refusing to queue: %s", rj.job.name, detail)
            refused.append({"job": rj.job.name, "reason": detail})
            continue
        if p.empty:
            detail = (
                "no snapshots hold files matching this job's excludes"
                if not runner.repo_snapshots()
                else "no stored file matches this job's excludes"
            )
            logger.error("%s: refusing to queue: %s", rj.job.name, detail)
            refused.append({"job": rj.job.name, "reason": detail})
            continue
        try:
            # The global lock is refused, not waited on: queueing during a run
            # would race the run reading this same file.
            with job.GlobalLock():
                entry = todo.arm_task(rj.state_file, apply.TASK_NAME)
            queued.append({"job": rj.job.name, **entry})
        except ValueError as e:
            logger.error("%s: %s", rj.job.name, e)
            refused.append({"job": rj.job.name, "reason": str(e)})

    for entry in queued:
        logger.info(
            "queued %s for %s; the next scheduled run will do it",
            entry["task"], entry["job"],
        )
    if as_json:
        print(json.dumps({"queued": queued, "refused": refused}, indent=2))
    if refused and not queued:
        return config.EXIT_ERROR
    return config.EXIT_SUCCESS


def _apply_now(jobs: list, as_json: bool) -> int:
    """Execute the rewrite for each job, serially, then one prune per repo.

    Each job is independent: one failing does not stop the others, and every
    outcome is reported rather than raised. No lock is taken — restic serializes
    on the repository's own lock, so a contended run fails instantly below and
    the answer is `--later`, which rides the next scheduled run.

    A matching queue entry is cleared as the rewrite succeeds, so the queue only
    ever holds work that has not been done.
    """
    results = []
    pruned: dict[str, config.ResolvedJob] = {}
    for rj in jobs:
        runner = job.Runner(rj)
        result = apply.execute(runner, apply.plan(runner))
        if result.status == job.ResultStatus.SUCCESS:
            pruned.setdefault(runner.repo.path, rj)
            if any(e["task"] == apply.TASK_NAME for e in todo.queued_tasks(rj.state_file)):
                todo.clear_task(rj.state_file, apply.TASK_NAME)
                logger.info("cleared the queued rewrite: it just ran")
        results.append(_result_json(result))

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
            }
        )

    if as_json:
        print(json.dumps({"applied": results}, indent=2))
    else:
        for entry in results:
            print(f"[{entry['job']}] {entry['status']}: {entry['detail']}")

    # Through the same ladder a scheduled run uses, rather than a hand-rolled
    # check: `--now` is a mutation an agent may well drive unattended, so its
    # outcome has to be classified exactly as the orchestrator would classify it.
    statuses = [
        job.Result(r["job"], r["status"], reason=r["detail"]) for r in results
    ]
    code = exit_code(statuses)
    return config.EXIT_ERROR if code == config.EXIT_PERM_FAILED else code


def _result_json(result: job.Result) -> dict:
    detail = result.reason or ""
    if "rc=11" in detail:
        detail = f"{detail}; {apply.LOCKED_ADVICE}"
    return {"job": result.name, "status": result.status, "detail": detail}


def _apply_excludes_phase(runs: list) -> tuple[list, set[str]]:
    """Run whatever work each job has queued, and say what to prune.

    Returns the task results (folded into the run's own results, so a rewrite the
    user asked for and that failed shows up in the exit code) and the repos whose
    space the run's single prune must reclaim.

    Every gate is a *skip*, never a failure: removing files the user has not
    inspected must not be able to change a backup result. Cheapest first:

      1. inside the backup window (a job with no window is never time-boxed)
      2. at least half the window remains — "ample time"

    The queue is the only arming mechanism, so there is no record of completed
    work to consult and nothing accumulates here.
    """
    results: list = []
    to_prune: set[str] = set()
    for runner in runs:
        if not todo.queued_tasks(runner.state_file):
            continue
        if not (runner.job.excludes or runner.job.ignore_file):
            logger.info("%s: queued work skipped (no excludes configured)", runner.job.name)
            continue
        if not runner.within_backup_window():
            logger.info("%s: queued work skipped (outside backup window)", runner.job.name)
            continue
        if not job.half_window_remaining(runner):
            logger.info(
                "%s: queued work skipped (less than half the backup window remains)",
                runner.job.name,
            )
            continue

        # One plan, used twice. It costs one `restic ls --long` per stored
        # snapshot, which on a remote repository is the slowest thing this tool
        # does; planning separately for the check and the rewrite did that work
        # twice for every job with queued work.
        planned = apply.plan(runner)
        if apply.warns_on_newest(runner, planned):
            logger.warning(
                "%s: the newest snapshot still holds excluded files, so the "
                "backup that just ran is not applying this job's excludes",
                runner.job.name,
            )

        result, repos = todo.dispatch(runner.state_file, runner, plan=planned)
        if result is None:
            continue
        logger.info("%s: %s: %s", runner.job.name, result.status, result.reason)
        results.append(result)
        to_prune |= repos
    return results, to_prune


class _RefuseSelector(argparse.Action):
    """An argument a verb accepts only to explain why it will not take it.

    `apply` removes files from what restic stored. A selector aimed at the live
    filesystem or the local snapshots is a mistake worth naming, not an
    unrecognized-flag mystery — and never something to silently discard.

    A trial pattern set is refused for a different reason: rewriting real
    backups against rules the deployment does not use would remove data on the
    strength of an experiment. The trial belongs to `show`, and `apply` acts on
    what is actually configured.
    """

    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        super().__init__(option_strings, dest, nargs=nargs, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        if option_string in ("--excludes-file", "-e", "--exclude"):
            parser.error(
                f"{option_string} tries the rewrite against a trial pattern set, "
                "but `apply excludes` rewrites real backups and must use the "
                "rules the job is actually configured with. Review the trial "
                "with `snap-bak show excludes --excludes-file ...`, then edit "
                "the config's ignore_file and apply against the real config."
            )
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
        "--format",
        choices=("text", "csv"),
        default="text",
        help="Output format for the row listing (default: text).",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Bound displayed rows (totals still cover all)."
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="List every excluded path, not just those from changed rules.",
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
        parser.add_argument(
            "--excludes-file",
            metavar="PATH",
            default=None,
            help=(
                "Evaluate this pattern file's rules in addition to the job's. "
                "Same syntax as a job's ignore_file: one restic exclude pattern "
                "per line, '#' comments, '!' negation. The config is not "
                "modified, and this cannot remove the job's own rules."
            ),
        )
        parser.add_argument(
            "-e",
            "--exclude",
            action="append",
            default=[],
            metavar="PATTERN",
            help=(
                "Add one restic exclude pattern, in the same syntax the "
                "excludes file uses. Repeatable; added after --excludes-file."
            ),
        )
    else:
        parser.add_argument(
            "--now", action="store_true", help="Execute the rewrite now."
        )
        parser.add_argument(
            "--later", action="store_true", help="Arm the next scheduled run."
        )
        for refused in (
            "--live",
            "--snaps",
            "--all",
            "--excludes-file",
            "-e",
            "--exclude",
        ):
            parser.add_argument(
                refused,
                nargs=0 if refused.startswith("--") else 1,
                action=_RefuseSelector,
                help=argparse.SUPPRESS,
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
        override = PatternOverride(
            getattr(args, "excludes_file", None), getattr(args, "exclude", None)
        )
        if override.active:
            missing = _missing_trial_file(override, jobs)
            if missing:
                logger.error(
                    "Cannot read the trial excludes file: %s. A job's own "
                    "ignore_file needs read access to the snap-bak group; a "
                    "trial file must be readable by whoever runs this command.",
                    missing,
                )
                sys.exit(config.EXIT_ERROR)
            _report_override(override, jobs, as_csv=args.format == "csv")

        view = _View.build(args, override)
        _do_lint(jobs, args.json, override)  # always; silent when clean
        # A `show` that cannot reach what it was asked about must not exit 0.
        # The worst outcome for a caller is a clean report that was never
        # actually checked, so each mode's code is accumulated, never overwritten.
        code = config.EXIT_SUCCESS
        for mode in _selected_modes(args):
            if mode == "live":
                _do_live(jobs, view)
            elif mode == "snaps":
                _do_snaps(jobs, view)
            elif mode == "repo":
                # Accumulate rather than overwrite: `--all` includes --repo, so a
                # last-wins assignment would let an unreachable repository be
                # reported as a clean success by the modes that ran after it.
                code = max(code, _do_repo(jobs, view))
        sys.exit(code)

    sys.exit(_do_apply(jobs, args.now, args.later, args.json, args.limit))


def _missing_trial_file(override: PatternOverride, jobs: list) -> str | None:
    """The trial pattern file that cannot be read, if any."""
    if not override.excludes_file:
        return None
    try:
        excludes.read_pattern_file(override.excludes_file)
    except OSError as e:
        return f"{override.excludes_file} ({e.strerror or e})"
    return None


def _report_override(
    override: PatternOverride, jobs: list, as_csv: bool = False
) -> None:
    """Print how the trial pattern set differs from the config, per job.

    Printed before the report rather than buried in it: the whole risk of a
    trial is acting on numbers that came from a different rule set, so which
    rules those were belongs at the top.

    Suppressed under `--format csv`, since a CSV consumer is piping this into a
    spreadsheet and a prose header would corrupt the first record. The same
    facts remain in the `added`/`removed` lists a `--json` caller can read.
    """
    for rj in jobs:
        base = excludes.patterns_for_job(rj.job)
        d = override.diff(base)
        if (not d["added"] and not d["removed"]) or as_csv:
            continue
        print(f"=== {rj.job.name}: trial pattern set ===")
        if override.excludes_file:
            print(f"  from: {override.excludes_file}")
        for rule in d["added"]:
            print(f"  + {rule}")
        for rule in d["removed"]:
            # Removed rules cannot match anything, so they are named here and
            # never appear as result rows.
            print(f"  - {rule}  (matches nothing; listed for completeness)")
        if not d["added"]:
            print("  no new rules, so nothing extra would be excluded")


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
        epilog=(
            "Subcommands (run with no subcommand for a normal backup run):\n"
            "  show excludes [SELECTOR]   inspect exclude patterns; read-only.\n"
            "                             --live (default, local) --snaps (local)\n"
            "                             --repo (network) --all\n"
            "                             --excludes-file PATH  try a working\n"
            "                             pattern file against the real config\n"
            "  apply excludes [--now|--later]\n"
            "                           rewrite excluded files out of restic\n"
            "\n"
            "Config: see -c, or SNAPBAK_CONFIG_FILE / SNAPBAK_JOB"
        ),
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
    parser.add_argument(
        "--version",
        action="version",
        version=f"snap-bak {__version__}",
        help="Print the installed version and exit.",
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

        # Phase A2 — carry out whatever work each job has queued. Runs after
        # backups so the newest snapshot is already in the repo, which is also
        # what makes it a check: that snapshot was taken with the job's current
        # excludes, so if it still holds excluded files the backup path is not
        # honouring them.
        task_results, rewritten_repos = _apply_excludes_phase(runners)
        results.extend(task_results)

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

        # Housekeeping last: only ever removes state for jobs that have left the
        # config, and never queued work. Failure is logged, never fatal.
        try:
            todo.reap(cfg, {r.job.name for r in runners})
        except OSError as e:
            logger.warning("Could not reap stale state: %s", e)

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
