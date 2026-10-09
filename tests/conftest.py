# -*- coding: utf-8 -*-
"""Test-wide isolation.

Redirects SNAPBAK_STATE_DIR to a throwaway directory BEFORE snap_bak.config is
imported. config.py calls STATE_DIR.mkdir(...) at module import time, so
without this every test run would try to create the real /opt/snap-bak/state on
the running machine. The directory is removed on interpreter exit.
"""

import atexit
import json
import os
import shutil
import subprocess
import tempfile

_STATE_TMP = tempfile.mkdtemp(prefix="snap-bak-test-state-")
os.environ["SNAPBAK_STATE_DIR"] = _STATE_TMP
atexit.register(lambda: shutil.rmtree(_STATE_TMP, ignore_errors=True))

PASSWORD = "test-password"


def restic(repo, *args, timeout=120):
    """Run restic against a throwaway repo, with the test password in env."""
    env = dict(os.environ, RESTIC_PASSWORD=PASSWORD)
    return subprocess.run(
        ["restic", "-r", repo, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def build_source(root):
    """A small tree with one file that must survive and some that may not.

    keep.txt is never excluded; a.tmp and cache/big.bin are the typical
    exclusions, and cache is a directory so it also covers subtree pruning.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "keep.txt").write_text("keep")
    (root / "a.tmp").write_text("tmp")
    cache = root / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "big.bin").write_bytes(b"z" * 100)


def init_repo(repo):
    result = restic(repo, "init")
    assert result.returncode == 0, result.stderr
    return repo


def backup(repo, source, *extra, tag="Test"):
    """Back up `source`, returning the new snapshot id."""
    result = restic(repo, "backup", source, "--json", "--tag", tag, *extra)
    assert result.returncode == 0, result.stderr
    for line in reversed(result.stdout.splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("message_type") == "summary":
            return obj["snapshot_id"]
    raise AssertionError(f"no summary in backup output: {result.stdout}")


def stored_files(repo, snapshot_id):
    """The files and symlinks restic stored; `restic ls` also lists directories."""
    from snap_bak import excludes

    result = restic(repo, "ls", "--long", snapshot_id)
    assert result.returncode == 0, result.stderr
    return {
        path
        for path, _size, is_dir in excludes.parse_ls_long(result.stdout)
        if not is_dir
    }


def resolved_job(tmp_path, monkeypatch, *, excludes_list):
    """A ResolvedJob over a throwaway source tree and a local restic repo.

    Sets RESTIC_PASSWORD so the CLI's own restic subprocesses can open the repo,
    and points STATE_DIR at a temp dir so no real state is touched. Returns
    (resolved_job, source_dir, repo_path).
    """
    from snap_bak import config

    src = tmp_path / "src"
    build_source(src)
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    repo = str(tmp_path / "repo")

    cfg = {
        "repos": [{"name": "local", "path": repo}],
        "mount_base": str(tmp_path / "bak"),
        "jobs": [
            {
                "name": "Test",
                "snapshot_dir": str(snaps),
                "source_subvolume": str(src),
                "repo": "local",
                "backup_retention": {"daily": 6},
                "excludes": excludes_list,
            }
        ],
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(cfg))
    monkeypatch.setenv("RESTIC_PASSWORD", PASSWORD)
    # config.py reads SNAPBAK_STATE_DIR at import; the module-level redirection
    # above already pointed it at a temp dir, so only the monkeypatched tests
    # need this. Kept so each test's state is fully isolated.
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    rj = config.Config.load(config_file).resolve(str(config_file))[0]
    return rj, src, repo
