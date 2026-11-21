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
        monkeypatch.setattr(io.grp, "getgrnam", lambda name: types.SimpleNamespace(gr_gid=0))
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
        monkeypatch.setattr(io.grp, "getgrnam", lambda name: types.SimpleNamespace(gr_gid=0))
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

    def test_update_restricts_restic_before_setcap(self, tmp_path, monkeypatch):
        fake = tmp_path / "restic"
        fake.write_text("")
        fake.chmod(0o755)
        fake_stat = fake.stat()
        monkeypatch.setattr(io, "RESTIC", str(fake))
        monkeypatch.setattr(io.grp, "getgrnam", lambda name: types.SimpleNamespace(gr_gid=0))
        monkeypatch.setattr(io, "CHGRP", "/usr/bin/chgrp")
        monkeypatch.setattr(io, "CHMOD", "/usr/bin/chmod")
        monkeypatch.setattr(io, "SETCAP", "/usr/bin/setcap")

        def fake_stat_result(p):
            return types.SimpleNamespace(st_uid=0, st_mode=fake_stat.st_mode, st_gid=0)

        monkeypatch.setattr(io.os, "stat", fake_stat_result)
        calls = []
        monkeypatch.setattr(
            io.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(io.os.path, "islink", lambda p: False)
        io._update_restic()
        commands = [c[0] for c in calls]
        assert "/usr/bin/chmod" in commands
        chmod = next(c for c in calls if c[0] == "/usr/bin/chmod")
        assert chmod[1] == "0750"
        # The capability is handed out only after execution is restricted.
        assert commands.index("/usr/bin/setcap") > commands.index("/usr/bin/chmod")


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


class TestUmount:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(io, "CONFIG_PATH", str(_write_config(tmp_path)))
        monkeypatch.setattr(io, "UMOUNT", "/usr/bin/umount")
        proc = tmp_path / "proc"
        proc.mkdir()
        monkeypatch.setattr(io, "_PROC_PATH", str(proc))
        calls = []
        monkeypatch.setattr(
            io.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(io.sys, "argv", ["sudo-exec.py", "umount", "home"])
        return proc, calls

    def test_refuses_while_restic_runs(self, tmp_path, monkeypatch):
        proc, calls = self._setup(tmp_path, monkeypatch)
        (proc / "1").mkdir()
        (proc / "1/cmdline").write_bytes(
            b"/usr/local/bin/restic\0--password-file\0/opt/snap-bak/passwords/p\0"
        )
        with pytest.raises(SystemExit) as exc:
            io.main()
        assert exc.value.code == 1
        assert calls == []

    def test_unmounts_when_no_restic_runs(self, tmp_path, monkeypatch):
        proc, calls = self._setup(tmp_path, monkeypatch)
        (proc / "1").mkdir()
        (proc / "1/cmdline").write_bytes(b"python3\0-m\0snap_bak.cli\0")
        io.main()
        assert calls == [["/usr/bin/umount", "-l", "/bak/home"]]


class TestResticOps:
    def _resolved(self, tmp_path, jobs=None):
        cfg = config.Config.load(_write_config(tmp_path, jobs=jobs))
        rj = cfg.get("home")
        assert rj is not None
        return rj

    def _capture(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            io, "_run_restic", lambda cmd, **kw: calls.append((cmd, kw))
        )
        return calls

    def test_backup_derives_repo_mount_tag_and_json(self, tmp_path, monkeypatch):
        rj = self._resolved(tmp_path)
        calls = self._capture(monkeypatch)
        io._op_restic_backup(rj, dry_run=False)
        cmd = calls[0][0]
        assert cmd[0] == io.RESTIC
        assert cmd[1:3] == ["-r", "rclone:test"]
        assert cmd[3] == "backup"
        assert str(rj.mount_point) in cmd
        assert "--tag" in cmd and rj.tag in cmd
        assert "--json" in cmd
        assert "--dry-run" not in cmd

    def test_backup_dry_run_adds_dry_run(self, tmp_path, monkeypatch):
        rj = self._resolved(tmp_path)
        calls = self._capture(monkeypatch)
        io._op_restic_backup(rj, dry_run=True)
        assert "--dry-run" in calls[0][0]

    def test_forget_uses_tag_and_retention(self, tmp_path, monkeypatch):
        jobs = [
            {
                "name": "home",
                "snapshot_dir": str(tmp_path / "snaps"),
                "source_subvolume": "home",
                "repo": "primary",
                "backup_retention": {"daily": 3, "monthly": 2},
            }
        ]
        rj = self._resolved(tmp_path, jobs=jobs)
        calls = self._capture(monkeypatch)
        io._op_restic_forget(rj)
        cmd = calls[0][0]
        assert cmd[3] == "forget"
        assert "--tag" in cmd and rj.tag in cmd
        assert "--keep-daily" in cmd and "3" in cmd
        assert "--keep-monthly" in cmd and "2" in cmd

    def test_forget_refuses_missing_retention(self, tmp_path, monkeypatch):
        rj = self._resolved(tmp_path)
        with pytest.raises(SystemExit):
            io._op_restic_forget(rj)

    def test_prune_is_allowlisted(self, tmp_path, monkeypatch):
        rj = self._resolved(tmp_path)
        calls = self._capture(monkeypatch)
        io._op_restic_prune(rj)
        assert calls[0][0][3] == "prune"

    def test_rewrite_uses_only_tagged_ids(self, tmp_path, monkeypatch):
        rj = self._resolved(tmp_path)
        monkeypatch.setattr(
            io, "_tagged_snapshot_ids", lambda _rj: {"abc12345", "def45678"}
        )
        calls = self._capture(monkeypatch)
        io._op_restic_rewrite(rj, ["abc12345"])
        cmd = calls[0][0]
        assert cmd[3] == "rewrite"
        assert "abc12345" in cmd
        assert "--forget" in cmd

    def test_rewrite_refuses_an_untagged_id(self, tmp_path, monkeypatch):
        rj = self._resolved(tmp_path)
        monkeypatch.setattr(io, "_tagged_snapshot_ids", lambda _rj: {"abc123"})
        with pytest.raises(SystemExit):
            io._op_restic_rewrite(rj, ["deadbeef"])

    def test_rewrite_refuses_a_malformed_id(self, tmp_path, monkeypatch):
        rj = self._resolved(tmp_path)
        with pytest.raises(SystemExit):
            io._op_restic_rewrite(rj, ["../../etc/passwd"])

    def test_extra_arguments_are_refused(self, tmp_path):
        rj = self._resolved(tmp_path)
        with pytest.raises(SystemExit):
            io._op_restic("restic-prune", rj, ["extra"])

    def test_environment_is_explicit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(io, "CONFIG_PATH", str(tmp_path / "config.json"))
        env = io._restic_env()
        assert env["HOME"] == str(tmp_path)
        assert env["RCLONE_CONFIG"] == str(tmp_path / ".config/rclone/rclone.conf")
        assert env["RESTIC_CACHE_DIR"] == str(tmp_path / "cache")
        assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"

    def test_restic_runs_as_the_service_user_in_its_own_session(self, monkeypatch):
        seen = {}

        class FakeProc:
            pid = 1

            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kwargs):
            seen.update(kwargs)
            return FakeProc()

        monkeypatch.setattr(io.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(io, "_service_ids", lambda: (1000, 1001))
        monkeypatch.setattr(io, "_restic_env", lambda: {"HOME": "/x"})
        monkeypatch.setattr(io, "_restic_timeout", lambda: None)
        io._run_restic(["restic"])
        assert seen["user"] == 1000
        assert seen["group"] == 1001
        assert seen["start_new_session"] is True
        assert seen["env"] == {"HOME": "/x"}

    def test_timeout_signals_the_process_group(self, monkeypatch):
        calls = []
        waits = []

        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                waits.append(timeout)
                if len(waits) == 1:
                    raise subprocess.TimeoutExpired("restic", 5)
                return 0

        monkeypatch.setattr(io.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(io, "_service_ids", lambda: (1000, 1000))
        monkeypatch.setattr(io, "_restic_env", dict)
        monkeypatch.setattr(io, "_restic_timeout", lambda: 5)
        monkeypatch.setattr(
            io.os, "killpg", lambda pid, sig: calls.append((pid, sig))
        )
        io._run_restic(["restic", "cat", "config"])
        assert calls == [(4242, io.signal.SIGTERM)]
