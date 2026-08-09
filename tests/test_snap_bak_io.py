# -*- coding: utf-8 -*-
"""Unit tests for the root-owned snap-bak-io wrapper.

Tests the wrapper's pure functions (validation, path confinement, config
loading) by importing it as a module via importlib (the filename contains a
hyphen, so a plain `import` cannot be used). No privileged operations run.
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_WRAPPER_PATH = Path(__file__).parent.parent / "scripts" / "snap-bak-io"
_loader = importlib.machinery.SourceFileLoader("snap_bak_io", str(_WRAPPER_PATH))
_spec = importlib.util.spec_from_loader("snap_bak_io", _loader)
io = importlib.util.module_from_spec(_spec)
_loader.exec_module(io)


def _write_config(tmp_path, jobs=None, mount_base="/bak"):
    """Write a minimal JSON config and return its path."""
    config = {
        "mount_base": mount_base,
        "stuck_threshold": 3600,
        "repos": [{"name": "primary", "path": "rclone:test"}],
        "jobs": jobs or [
            {
                "name": "home",
                "snapshot_dir": str(tmp_path / "snaps"),
                "source_subvolume": "home",
                "repo": "primary",
            }
        ],
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))
    return cfg


class TestStripJsonComments:
    def test_preserves_strings(self):
        src = '{"path": "rclone://host:*/dir", "x": 1}  // comment'
        out = io.strip_json_comments(src)
        assert "rclone://host:*/dir" in out
        assert "comment" not in out

    def test_handles_block_comments(self):
        src = "{\n /* block */\n \"a\": 1\n}"
        out = io.strip_json_comments(src)
        parsed = json.loads(out)
        assert parsed == {"a": 1}


class TestValidateName:
    def test_valid_iso(self):
        assert io._validate_name("2026-08-09_02:00") == "2026-08-09_02:00"

    def test_rejects_traversal(self):
        with pytest.raises(SystemExit):
            io._validate_name("../../etc/passwd")

    def test_rejects_non_iso(self):
        with pytest.raises(SystemExit):
            io._validate_name("home")


class TestJobPath:
    def test_builds_path_within_snapshot_dir(self, tmp_path):
        job = {"snapshot_dir": str(tmp_path / "snaps")}
        target = io._job_path(job, "2026-08-09_02:00")
        assert Path(target) == Path(job["snapshot_dir"]) / "2026-08-09_02:00"

    def test_rejects_escape_attempt(self, tmp_path):
        job = {"snapshot_dir": str(tmp_path / "snaps")}
        with pytest.raises(SystemExit):
            io._job_path(job, "2026-08-09_02:00/../../x")


class TestFindJob:
    def test_finds_existing_job(self, tmp_path):
        cfg_path = _write_config(tmp_path)
        io.CONFIG_PATH = str(cfg_path)
        config = io.load_config()
        job = io._find_job(config, "home")
        assert job["name"] == "home"

    def test_rejects_unknown_job(self, tmp_path):
        cfg_path = _write_config(tmp_path)
        io.CONFIG_PATH = str(cfg_path)
        config = io.load_config()
        with pytest.raises(SystemExit):
            io._find_job(config, "not-a-job")


class TestRun:
    def test_run_execs_real_command(self, tmp_path, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

        monkeypatch.setattr(io.subprocess, "run", fake_run)
        io._run(["/usr/bin/btrfs", "subvolume", "snapshot"])
        assert captured["cmd"][0] == "/usr/bin/btrfs"

    def test_run_propagates_exit_code(self, tmp_path, monkeypatch):
        def fail_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(5, cmd)

        monkeypatch.setattr(io.subprocess, "run", fail_run)
        with pytest.raises(SystemExit) as exc:
            io._run(["btrfs"])
        assert exc.value.code == 5
