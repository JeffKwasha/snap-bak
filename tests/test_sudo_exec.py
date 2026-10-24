"""Unit tests for the root-owned sudo-exec.py wrapper.

Tests the wrapper's pure functions (validation, path confinement, config
loading) by importing it as a module via importlib (the filename contains a
hyphen, so a plain `import` cannot be used). No privileged operations run.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

from snap_bak import config

_WRAPPER_PATH = Path(__file__).parent.parent / "src" / "sudo-exec.py"
_loader = importlib.machinery.SourceFileLoader("sudo_exec", str(_WRAPPER_PATH))
_spec = importlib.util.spec_from_loader("sudo_exec", _loader)
assert _spec is not None
io = importlib.util.module_from_spec(_spec)
_loader.exec_module(io)


def _write_config(tmp_path, jobs=None, mount_base="/bak"):
    """Write a minimal JSON config and return its path."""
    (tmp_path / "snaps").mkdir(exist_ok=True)
    config = {
        "mount_base": mount_base,
        "stuck_threshold": 3600,
        "repos": [{"name": "primary", "path": "rclone:test"}],
        "jobs": jobs
        or [
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


class TestValidateName:
    def test_valid_iso(self):
        assert io._validate_name("2026-08-09_02:00Z") == "2026-08-09_02:00Z"

    def test_rejects_traversal(self):
        with pytest.raises(SystemExit):
            io._validate_name("../../etc/passwd")

    def test_rejects_non_iso(self):
        with pytest.raises(SystemExit):
            io._validate_name("home")


class TestJobPath:
    def test_builds_path_within_snapshot_dir(self, tmp_path):
        snap_dir = tmp_path / "snaps"
        target = io._job_path(snap_dir, "2026-08-09_02:00Z")
        assert Path(target) == snap_dir / "2026-08-09_02:00Z"

    def test_rejects_escape_attempt(self, tmp_path):
        snap_dir = tmp_path / "snaps"
        with pytest.raises(SystemExit):
            io._job_path(snap_dir, "2026-08-09_02:00Z/../../x")


class TestConfigLookup:
    def test_finds_existing_job(self, tmp_path):
        cfg = config.Config.load(_write_config(tmp_path))
        rj = cfg.get("home")
        assert rj is not None
        assert rj.job.name == "home"

    def test_rejects_unknown_job(self, tmp_path):
        cfg = config.Config.load(_write_config(tmp_path))
        assert cfg.get("not-a-job") is None


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
                    st_uid=1000,
                    st_mode=st.st_mode,
                    st_gid=st.st_gid,
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
                st_uid=0,
                st_mode=fake_stat.st_mode,
                st_gid=0,
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
        assert any(
            c[0] == "/usr/bin/setcap" and c[1] == "cap_dac_read_search=+ep"
            for c in calls
        )


# --- status op: pool disk state + generation (power saving) ---


class TestClassifyAnySpunDown:
    """ANY spun-down rotational disk in a pool suppresses the snapshot."""

    def test_all_running_not_idle(self):
        disks = [
            {"device": "sda", "state": "running", "rotational": "1"},
            {"device": "sdb", "state": "running", "rotational": "1"},
        ]
        assert io._classify_any_spun_down(disks) is False

    def test_mixed_running_standby_is_idle(self):
        """A single spun-down drive in an otherwise running pool must skip."""
        disks = [
            {"device": "sda", "state": "running", "rotational": "1"},
            {"device": "sdb", "state": "standby", "rotational": "1"},
        ]
        assert io._classify_any_spun_down(disks) is True

    def test_all_spun_down(self):
        disks = [
            {"device": "sda", "state": "standby", "rotational": "1"},
            {"device": "sdb", "state": "slumber", "rotational": "1"},
        ]
        assert io._classify_any_spun_down(disks) is True

    def test_unknown_state_fails_open(self):
        """An unreadable state is never treated as proof of spin-down."""
        disks = [
            {"device": "sda", "state": "running", "rotational": "1"},
            {"device": "sdb", "state": None, "rotational": "1"},
        ]
        assert io._classify_any_spun_down(disks) is None

    def test_parked_is_not_spun_down(self):
        """'parked' (dm/layer) must not be mistaken for a physically spun-down
        disk; it stays unknown and fails open."""
        disks = [{"device": "sdb", "state": "parked", "rotational": "1"}]
        assert io._classify_any_spun_down(disks) is None

    def test_non_rotational_ignored(self):
        disks = [
            {"device": "nvme0n1", "state": "live", "rotational": "0"},
            {"device": "sda", "state": "running", "rotational": "1"},
        ]
        assert io._classify_any_spun_down(disks) is False

    def test_no_rotational_disks(self):
        disks = [{"device": "nvme0n1", "state": "live", "rotational": "0"}]
        assert io._classify_any_spun_down(disks) is False


class TestSubvolumeGeneration:
    """Generation reads moved to the shared snap_bak.snapshot module; the
    wrapper reaches them through snapshot.Snapshot (memoized)."""

    def _result(self, stdout, rc=0):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=rc)

    def test_parses_generation(self, monkeypatch):
        out = (
            "/mnt/data\n\tName: data\n"
            "\tGeneration: \t\t\t192381\n\tGen at creation: \t\t192100\n"
        )
        monkeypatch.setattr(
            io.snapshot.subprocess, "run", lambda *a, **k: self._result(out)
        )
        s = io.snapshot.Snapshot("/mnt/data")
        assert s.generation == 192381

    def test_nonzero_rc_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            io.snapshot.subprocess, "run", lambda *a, **k: self._result("boom", rc=1)
        )
        assert io.snapshot.Snapshot("/mnt/data").generation is None

    def test_missing_field_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            io.snapshot.subprocess,
            "run",
            lambda *a, **k: self._result("/mnt/data\n\tName: x\n"),
        )
        assert io.snapshot.Snapshot("/mnt/data").generation is None


class TestLatestSnapshotName:
    def test_picks_most_recent_valid(self, tmp_path):
        for n in (
            "2026-08-09_02:00Z",
            "2026-08-10_03:00Z",
            "2026-08-10_04:00Z",
            "random",
        ):
            (tmp_path / n).mkdir()
        assert io.snapshot.Snapshot.latest(str(tmp_path)).name == "2026-08-10_04:00Z"

    def test_empty_dir(self, tmp_path):
        assert io.snapshot.Snapshot.latest(str(tmp_path)) is None

    def test_ignores_non_matching(self, tmp_path):
        (tmp_path / "random").mkdir()
        assert io.snapshot.Snapshot.latest(str(tmp_path)) is None


class TestBtrfsMountDevice:
    MOUNTINFO = (
        "164 32 0:63 /@/data /mnt/data rw,noatime - "
        "btrfs /dev/dm-3[/@/data] rw\n"
        "165 32 0:15 / / rw,noatime - btrfs /dev/dm-7 rw"
        "\n"
    )

    def _set_mountinfo(self, tmp_path, monkeypatch, text):
        f = tmp_path / "mountinfo"
        f.write_text(text)
        monkeypatch.setattr(io, "_MOUNTINFO_PATH", str(f))

    def test_finds_device_node(self, tmp_path, monkeypatch):
        self._set_mountinfo(tmp_path, monkeypatch, self.MOUNTINFO)
        assert io._btrfs_mount_device("/mnt/data") == "/dev/dm-3"

    def test_strips_subvol_suffix(self, tmp_path, monkeypatch):
        self._set_mountinfo(tmp_path, monkeypatch, self.MOUNTINFO)
        assert io._btrfs_mount_device("/mnt/data/data") == "/dev/dm-3"

    def test_deepest_match_wins(self, tmp_path, monkeypatch):
        text = (
            "a b c d /mnt rw - btrfs /dev/sda rw\n"
            "e f g h /mnt/data rw - btrfs /dev/dm-3 rw\n"
        )
        self._set_mountinfo(tmp_path, monkeypatch, text)
        assert io._btrfs_mount_device("/mnt/data") == "/dev/dm-3"

    def test_non_btrfs_ignored(self, tmp_path, monkeypatch):
        self._set_mountinfo(
            tmp_path, monkeypatch, "a b c d /mnt/data rw - ext4 /dev/sda1 rw\n"
        )
        assert io._btrfs_mount_device("/mnt/data") is None


class TestPoolDeviceNames:
    def test_discovers_all_pool_devices(self, tmp_path, monkeypatch):
        fs = tmp_path / "btrfs" / "c84c2c15" / "devices"
        for name, majmin in (("dm-3", "252:3"), ("dm-4", "252:4"), ("dm-11", "252:11")):
            (fs / name).mkdir(parents=True)
            (fs / name / "dev").write_text(majmin)
        monkeypatch.setattr(io, "_SYSFS_BTRFS", str(tmp_path / "btrfs"))
        monkeypatch.setattr(io, "_btrfs_mount_device", lambda src: "/dev/dm-3")

        # The mount device comes FIRST in listing order. Returning as soon as
        # it matches would then yield only ['dm-3']; the whole pool is wanted.
        real_listdir = os.listdir

        def fake_listdir(path):
            if str(path) == str(tmp_path / "btrfs"):
                return ["c84c2c15"]
            if str(path) == str(fs):
                return ["dm-3", "dm-11", "dm-4"]
            return real_listdir(path)

        monkeypatch.setattr(io.os, "listdir", fake_listdir)

        real_stat = os.stat

        def fake_stat(p):
            if str(p) == "/dev/dm-3":
                return types.SimpleNamespace(st_rdev=os.makedev(252, 3))
            return real_stat(p)

        monkeypatch.setattr(io.os, "stat", fake_stat)
        assert set(io._pool_device_names("/mnt/data")) == {
            "dm-11",
            "dm-3",
            "dm-4",
        }

    def test_unmapped_device_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(io, "_SYSFS_BTRFS", str(tmp_path / "btrfs"))
        monkeypatch.setattr(io, "_btrfs_mount_device", lambda src: "/dev/dm-3")

        def fake_stat(p):
            return types.SimpleNamespace(st_rdev=os.makedev(9, 9))

        monkeypatch.setattr(io.os, "stat", fake_stat)
        assert io._pool_device_names("/mnt/data") == []


class TestLeafDisks:
    def test_resolves_stacked_to_physical(self, tmp_path, monkeypatch):
        """dm-3 -> slaves/sdd1 -> whole disk sdd."""
        cb = tmp_path / "block"
        dm = cb / "dm-3"
        dm.mkdir(parents=True)
        (dm / "slaves").mkdir()
        (dm / "slaves" / "sdd1").mkdir()
        (cb / "sdd").mkdir()
        (cb / "sdd" / "device").symlink_to("x")
        monkeypatch.setattr(io, "_CLASS_BLOCK", str(cb))
        assert io._leaf_disks(str(dm)) == {"sdd"}

    def test_partition_climbs_to_disk(self, tmp_path, monkeypatch):
        cb = tmp_path / "block"
        cb.mkdir(parents=True)
        (cb / "sdd").mkdir()
        (cb / "sdd" / "device").symlink_to("x")
        (cb / "sdd1").mkdir()
        monkeypatch.setattr(io, "_CLASS_BLOCK", str(cb))
        assert io._leaf_disks(str(cb / "sdd1")) == {"sdd"}

    def test_whole_disk_is_leaf(self, tmp_path, monkeypatch):
        cb = tmp_path / "block"
        cb.mkdir(parents=True)
        (cb / "sda").mkdir()
        (cb / "sda" / "device").symlink_to("x")
        monkeypatch.setattr(io, "_CLASS_BLOCK", str(cb))
        assert io._leaf_disks(str(cb / "sda")) == {"sda"}


class TestStatusOp:
    """End-to-end status: idle pools make NO btrfs/dir I/O; live pools do."""

    def _build(self, tmp_path, monkeypatch, disk_state):
        """Fabricated pool: dm-3 (pool) backed by disk sdb (rotational)."""
        cb = tmp_path / "block"
        disk = cb / "sdb"
        disk.mkdir(parents=True)
        (disk / "scsi").mkdir()
        (disk / "device").symlink_to("scsi")  # sysfs: device -> host dir
        (disk / "queue").mkdir()
        (disk / "queue" / "rotational").write_text("1")
        (disk / "scsi" / "state").write_text(disk_state)
        dm = cb / "dm-3"
        dm.mkdir()
        (dm / "slaves").mkdir()
        (dm / "slaves" / "sdb").mkdir()
        (dm / "dev").write_text("252:3")

        fs = tmp_path / "btrfs" / "c84c2c15" / "devices"
        (fs / "dm-3").mkdir(parents=True)
        (fs / "dm-3" / "dev").write_text("252:3")

        mif = tmp_path / "mountinfo"
        mif.write_text("1 1 0:63 /@/root /mnt/data rw - btrfs /dev/dm-3 rw\n")

        monkeypatch.setattr(io, "_SYSFS_BTRFS", str(tmp_path / "btrfs"))
        monkeypatch.setattr(io, "_CLASS_BLOCK", str(cb))
        monkeypatch.setattr(io, "_MOUNTINFO_PATH", str(mif))

        real_stat = os.stat

        def fake_stat(p):
            if str(p) == "/dev/dm-3":
                return types.SimpleNamespace(st_rdev=os.makedev(252, 3))
            return real_stat(p)

        monkeypatch.setattr(io.os, "stat", fake_stat)

    def test_idle_pool_issues_no_disk_io(self, tmp_path, monkeypatch, capsys):
        self._build(tmp_path, monkeypatch, "standby")
        calls = []
        monkeypatch.setattr(
            io.subprocess,
            "run",
            lambda *a, **k: (
                calls.append(a[0])
                or types.SimpleNamespace(stdout="", stderr="", returncode=0)
            ),
        )
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "2026-08-10_04:00Z").mkdir()

        io._op_status(
            config.Job.model_validate({
                "name": "x",
                "snapshot_dir": snap,
                "source_subvolume": "/mnt/data",
                "repo": "primary",
            })
        )
        parsed = json.loads(capsys.readouterr().out)

        assert parsed["any_spun_down"] is True
        assert parsed["source_generation"] is None
        assert parsed["last_snapshot_generation"] is None
        assert parsed["changed"] is None
        assert calls == []  # no btrfs subvolume calls, no dir-level work

    def test_running_pool_reports_generations(self, tmp_path, monkeypatch, capsys):
        self._build(tmp_path, monkeypatch, "running")
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            path = argv[-1]
            gen = 300 if path == "/mnt/data" else 299
            return types.SimpleNamespace(
                stdout=(f"{path}\n\tName: x\n\tGeneration: \t\t\t{gen}\n"),
                stderr="",
                returncode=0,
            )

        monkeypatch.setattr(io.subprocess, "run", fake_run)
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "2026-08-10_04:00Z").mkdir()

        io._op_status(
            config.Job.model_validate({
                "name": "x",
                "snapshot_dir": snap,
                "source_subvolume": "/mnt/data",
                "repo": "primary",
            })
        )
        parsed = json.loads(capsys.readouterr().out)

        assert parsed["any_spun_down"] is False
        assert parsed["source_generation"] == 300
        assert parsed["last_snapshot_generation"] == 299
        assert parsed["changed"] is True
        assert len(calls) == 2  # source + latest snapshot
        assert all(c[1] == "subvolume" for c in calls)

    def test_running_pool_no_snapshot_means_changed(
        self, tmp_path, monkeypatch, capsys
    ):
        self._build(tmp_path, monkeypatch, "running")

        def fake_run(argv, **kw):
            return types.SimpleNamespace(
                stdout=(f"{argv[-1]}\n\tName: x\n\tGeneration: \t\t\t300\n"),
                stderr="",
                returncode=0,
            )

        monkeypatch.setattr(io.subprocess, "run", fake_run)
        snap = tmp_path / "snap"
        snap.mkdir()  # empty snapshot dir

        io._op_status(
            config.Job.model_validate({
                "name": "x",
                "snapshot_dir": snap,
                "source_subvolume": "/mnt/data",
                "repo": "primary",
            })
        )
        parsed = json.loads(capsys.readouterr().out)

        assert parsed["source_generation"] == 300
        assert parsed["last_snapshot_generation"] is None
        assert parsed["changed"] is True  # fresh install -> creation warranted
