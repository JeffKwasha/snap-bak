# -*- coding: utf-8 -*-
"""Unit tests for the shared snap_bak/snapshot.py domain module.

The module is the single owner of the snapshot name format and the btrfs
command line, and is used by BOTH the orchestrator (cli.py) and the
privileged wrapper (sudo-exec.py). These tests pin the strictness of the name
parse (the crash that started all this) and the memoized generation reads.
"""

import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


from snap_bak import snapshot


# --- Name format / parse ---


class TestNameFormat:
    def test_round_trip(self):
        dt = datetime(2026, 8, 14, 2, 0, tzinfo=timezone.utc)
        assert snapshot.parse_name(snapshot.snapshot_name(dt)) == dt

    def test_utc_zone(self):
        assert snapshot.snapshot_name(datetime(2026, 8, 9, 2, 0)).endswith("Z")

    def test_bare_no_z_rejected(self):
        """The 2026-08-14_02:00 crash: name must carry the trailing Z or it is
        not a snapshot. Orchestrator and wrapper must agree (they do now)."""
        assert snapshot.parse_name("2026-08-14_02:00") is None

    def test_invalid_rejected(self):
        for bad in (
            "home",
            "random",
            "2026-08-14",
            "2026-08-14_02:00:00Z",
            "2026-8-14_02:00Z",
            "../../home",
        ):
            assert snapshot.parse_name(bad) is None

    def test_lexicographic_equals_chronological(self):
        a = snapshot.parse_name("2026-08-09_02:00Z")
        b = snapshot.parse_name("2026-08-10_04:00Z")
        assert a is not None and b is not None
        assert b > a  # later timestamp parses to later datetime


# --- scan / latest / oldest_within ---


class TestScan:
    def test_skips_non_snapshot_dirs(self, tmp_path):
        (tmp_path / "2026-08-10_04:00Z").mkdir()
        (tmp_path / "random").mkdir()
        (tmp_path / "2026-08-14_02:00").mkdir()  # legacy bare name
        (tmp_path / "file.txt").write_text("x")
        snaps = snapshot.Snapshot.scan(tmp_path)
        assert [s.name for s in snaps] == ["2026-08-10_04:00Z"]
        assert all(s.is_snapshot for s in snaps)

    def test_latest_picks_most_recent(self, tmp_path):
        for n in ("2026-08-09_02:00Z", "2026-08-10_03:00Z"):
            (tmp_path / n).mkdir()
        latest = snapshot.Snapshot.latest(tmp_path)
        assert latest is not None and latest.name == "2026-08-10_03:00Z"

    def test_latest_empty_is_none(self, tmp_path):
        assert snapshot.Snapshot.latest(tmp_path) is None

    def test_oldest_within(self, tmp_path):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=30)).strftime("%Y-%m-%d_%H:%MZ")
        old = (now - timedelta(hours=24)).strftime("%Y-%m-%d_%H:%MZ")
        (tmp_path / old).mkdir()
        (tmp_path / recent).mkdir()
        got = snapshot.Snapshot.oldest_within(tmp_path, timedelta(hours=3))
        assert got is not None and got.name == recent


# --- Source (non-snapshot) / generation ---


class TestSourceSubvolume:
    def test_time_is_none(self):
        s = snapshot.Snapshot("/mnt/data")
        assert s.time is None
        assert not s.is_snapshot
        assert s.age is None

    def test_generation_memoized(self, monkeypatch):
        calls = []

        def fake_run(*a, **k):
            calls.append(a[0])
            return types.SimpleNamespace(
                stdout="/mnt/data\n\tGeneration: \t\t\t7\n", stderr="", returncode=0
            )

        monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
        s = snapshot.Snapshot("/mnt/data")
        assert s.generation == 7
        assert s.generation == 7
        assert len(calls) == 1  # read exactly once despite two accesses

    def test_has_changed_different_generations(self, monkeypatch):
        gens = {"/a": 10, "/b": 8}
        monkeypatch.setattr(
            snapshot.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(
                stdout=f"{a[0][-1]}\n\tGeneration: \t\t\t{gens[a[0][-1]]}\n",
                stderr="",
                returncode=0,
            ),
        )
        new, old = snapshot.Snapshot("/a"), snapshot.Snapshot("/b")
        assert new.has_changed(old) is True

    def test_has_changed_fail_open_on_error(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("denied: needs root")

        monkeypatch.setattr(snapshot.subprocess, "run", boom)
        new, old = snapshot.Snapshot("/a"), snapshot.Snapshot("/b")
        assert new.has_changed(old) is False  # unknown != change: never suppress

    def test_same_generation_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            snapshot.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(
                stdout=f"{a[0][-1]}\n\tGeneration: \t\t\t5\n", stderr="", returncode=0
            ),
        )
        a, b = snapshot.Snapshot("/a"), snapshot.Snapshot("/b")
        assert a.has_changed(b) is False


# --- name property stays a basename (paths stay Path until subprocess) ---


class TestPathHandling:
    def test_name_is_basename(self):
        assert (
            snapshot.Snapshot("/mnt/bak/2026-08-10_04:00Z").name == "2026-08-10_04:00Z"
        )

    def test_path_kept_as_pathlib(self):
        s = snapshot.Snapshot("/mnt/bak/x")
        assert isinstance(s.path, Path)
