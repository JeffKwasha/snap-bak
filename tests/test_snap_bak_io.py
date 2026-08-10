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

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.snap_bak as sb

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


class TestLoadConfigJsonc:
    def test_parses_line_comments(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text('{"repos": [], "jobs": []}  // trailing comment\n')
        monkeypatch.setattr(io, "CONFIG_PATH", str(cfg))
        assert io.load_config() == {"repos": [], "jobs": []}

    def test_parses_block_comments(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text('{\n /* block */\n "repos": [], "jobs": []\n}\n')
        monkeypatch.setattr(io, "CONFIG_PATH", str(cfg))
        assert io.load_config() == {"repos": [], "jobs": []}

    def test_preserves_slashes_inside_strings(self, tmp_path, monkeypatch):
        # rclone:// must not be treated as a comment.
        cfg = tmp_path / "config.json"
        cfg.write_text('{"path": "rclone://host:*/dir", "x": 1}  // comment\n')
        monkeypatch.setattr(io, "CONFIG_PATH", str(cfg))
        parsed = io.load_config()
        assert parsed["path"] == "rclone://host:*/dir"


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


class TestMountSubpath:
    def test_defaults_to_source_basename(self):
        job = {"source_subvolume": "/mnt/data"}
        assert io._job_mount_point({"mount_base": "/mnt/bak"}, job) == "/mnt/bak/data"

    def test_defaults_relative_source(self):
        job = {"source_subvolume": "home"}
        assert io._job_mount_point({"mount_base": "/mnt/bak"}, job) == "/mnt/bak/home"

    def test_defaults_absolute_source(self):
        job = {"source_subvolume": "/mnt/data"}
        assert io._job_mount_point({"mount_base": "/bak"}, job) == "/bak/data"

    def test_matches_orchestrator_mount_point(self, tmp_path):
        """Wrapper and orchestrator must derive the mount point identically.

        The rule lives in two places (the wrapper cannot import the
        orchestrator), so this parity test is what keeps them in lock-step.
        """
        cases = [
            ("/bak", "/mnt/data"),
            ("/bak", "home"),
            ("/bak", "/mnt/data/"),
            ("/bak", "/mnt/data/deep/sub"),
            ("/mnt/bak", "/mnt/data"),
        ]
        for mount_base, source in cases:
            wrapper = io._job_mount_point({"mount_base": mount_base}, {"source_subvolume": source})
            model = sb.Job(
                name="n",
                snapshot_dir=str(tmp_path),
                source_subvolume=source,
                repo="primary",
                repos=[sb.Repo(name="primary", path="rclone:x")],
                mount_base=mount_base,
            ).mount_point
            assert str(model) == wrapper, (mount_base, source)


class TestUpdateRestic:
    def test_rejects_non_root_owned(self, tmp_path, monkeypatch):
        fake = tmp_path / "restic"
        fake.write_text("")
        fake.chmod(0o755)
        fake_stat = fake.stat()
        monkeypatch.setattr(io, "RESTIC", str(fake))
        monkeypatch.setattr(io, "SETCAP", "/bin/true")
        # Force uid != 0 by faking os.stat on the binary path
        import types

        def fake_stat_result(p):
            st = fake_stat
            if str(p) == str(fake):
                return types.SimpleNamespace(
                    st_uid=1000, st_mode=st.st_mode, st_gid=st.st_gid,
                )
            return st

        monkeypatch.setattr(io.os, "stat", fake_stat_result)
        with pytest.raises(SystemExit) as exc:
            io._check_restic_trusted()
        assert exc.value.code == 3

    def test_update_runs_setcap(self, tmp_path, monkeypatch):
        fake = tmp_path / "restic"
        fake.write_text("")
        fake.chmod(0o755)
        fake_stat = fake.stat()
        monkeypatch.setattr(io, "RESTIC", str(fake))
        monkeypatch.setattr(io, "SETCAP", "/usr/bin/setcap")
        import types

        def fake_stat_result(p):
            return types.SimpleNamespace(
                st_uid=0, st_mode=fake_stat.st_mode, st_gid=0,
            )

        monkeypatch.setattr(io.os, "stat", fake_stat_result)
        calls = []
        monkeypatch.setattr(
            io.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(io.os.path, "islink", lambda p: False)
        io._update_restic()
        assert any(c[0] == "/usr/bin/setcap" for c in calls)
        assert any("self-update" in c for c in calls)
        assert any(c[0] == "/usr/bin/setcap" and c[1] == "cap_dac_read_search=+ep" for c in calls)
