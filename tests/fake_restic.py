#!/usr/bin/env python3
"""Stand-in for restic at the exec boundary.

Records the argv and environment that crossed the boundary, then answers as a
working restic would, assuming the binary and the backend are healthy. It never
touches a repository and never needs root.

A test may place a scenario JSON at $RESTIC_CACHE_DIR/fake-restic-scenario.json
to supply repository data. Without one, the answer is a healthy, empty repo.
The scenario is a test input, not a simulation of restic's decisions.
"""

import json
import os
import sys
from pathlib import Path

HEADER = "mode uid gid size date time path"


def scenario(cache: Path) -> dict:
    path = cache / "fake-restic-scenario.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def answer(subcommand: str, argv: list[str], data: dict) -> str:
    if subcommand == "cat":
        return json.dumps({"id": "fake-repo", "version": 2})
    if subcommand == "stats":
        return json.dumps(
            {"message_type": "stats", "total_size": data.get("stats_total_size", 0)}
        )
    if subcommand == "snapshots":
        return json.dumps(data.get("snapshots", []))
    if subcommand == "ls":
        return "\n".join(data.get("listing", []))
    if subcommand == "backup":
        summary = {
            "message_type": "summary",
            "snapshot_id": data.get("snapshot_id", "f" * 64),
            "files_new": 0,
            "files_changed": 0,
            "files_unmodified": 0,
            "data_added": 0,
            "total_size": data.get("backup_total_size", 0),
        }
        return json.dumps(summary)
    if subcommand in ("forget", "prune", "rewrite"):
        return ""
    return ""


def main() -> None:
    cache = os.environ.get("RESTIC_CACHE_DIR")
    if not cache:
        raise SystemExit("fake restic: RESTIC_CACHE_DIR is unset")
    cache_dir = Path(cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "argv": sys.argv,
        "env": {
            key: os.environ.get(key)
            for key in ("HOME", "RCLONE_CONFIG", "RESTIC_CACHE_DIR", "PATH")
        },
        "uid": os.getuid(),
        "gid": os.getgid(),
    }
    (cache_dir / f"fake-restic-{os.getpid()}.json").write_text(
        json.dumps(record, indent=2)
    )

    data = scenario(cache_dir)
    subcommand = sys.argv[3] if len(sys.argv) > 3 and sys.argv[1] == "-r" else ""
    stdout = answer(subcommand, sys.argv, data)
    if stdout:
        print(stdout)
    if data.get("stderr"):
        print(data["stderr"], file=sys.stderr)
    raise SystemExit(int(data.get("exit_code", 0)))


if __name__ == "__main__":
    main()
