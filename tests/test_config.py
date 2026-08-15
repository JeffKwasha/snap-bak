# -*- coding: utf-8 -*-
"""Tests for snap_bak.config: parsing, models, resolution, retention."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError


from snap_bak import config
from snap_bak.config import (
    Config,
    Job,
    Snapshot,
    Window,
    build_retention_datetimes,
    build_retention_exponential,
    build_snapshot_keeplist,
    parse_duration,
    parse_size,
)


def _config(tmp_path, jobs=None, repos=None, **top):
    data = {
        "repos": repos or [{"name": "primary", "path": "rclone:test"}],
        "jobs": jobs
        or [
            {
                "name": "home",
                "snapshot_dir": str(tmp_path),
                "source_subvolume": str(tmp_path),
                "repo": "primary",
            }
        ],
    }
    data.update(top)
    return data


# --- quantity parsing ---


class TestParseDuration:
    def test_int_passthrough(self):
        assert parse_duration(3600) == 3600

    def test_bare_string_int(self):
        assert parse_duration("3600") == 3600

    def test_units(self):
        assert parse_duration("90m") == 5400
        assert parse_duration("2h") == 7200
        assert parse_duration("1w") == 604800

    def test_decimal(self):
        assert parse_duration("1.5h") == 5400

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError):
            parse_duration("10x")

    def test_bool_raises(self):
        with pytest.raises(ValueError):
            parse_duration(True)


class TestParseSize:
    def test_int_passthrough(self):
        assert parse_size(1024) == 1024

    def test_units(self):
        assert parse_size("200GB") == 200 * 1024**3
        assert parse_size("1.5TiB") == int(1.5 * 1024**4)

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError):
            parse_size("10quux")


# --- Config model ---


class TestConfig:
    def test_loads_and_resolves(self, tmp_path):
        cfg = Config(**_config(tmp_path))
        jobs = cfg.resolve("/tmp/cfg.json")
        assert jobs[0].repo.name == "primary"
        assert jobs[0].repo.path == "rclone:test"
        assert jobs[0].mount_point == Path("/bak") / Path(str(tmp_path)).name
        assert jobs[0].status_file == config.STATE_DIR / "home_status.json"
        assert jobs[0].tag == "home"  # resolved from name
        assert jobs[0].config_path == "/tmp/cfg.json"

    def test_unknown_top_level_key_warns(self, tmp_path):
        with pytest.warns(UserWarning):
            Config(**_config(tmp_path, bogus_key=1))

    def test_empty_repos_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Config(repos=[], jobs=[])

    def test_unknown_repo_reference_rejected(self, tmp_path):
        data = _config(
            tmp_path, jobs=[{**_config(tmp_path)["jobs"][0], "repo": "nope"}]
        )
        with pytest.raises(ValidationError):
            Config(**data)

    def test_invalid_restic_update_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Config(**_config(tmp_path, restic_update="before"))

    def test_stuck_threshold_parsed(self, tmp_path):
        cfg = Config(**_config(tmp_path, stuck_threshold="1h"))
        assert cfg.stuck_threshold == 3600


class TestConfigLoad:
    def test_loads_file(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(_config(tmp_path)))
        cfg = Config.load(cfg_file)
        assert cfg.jobs[0].name == "home"

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            Config.load(tmp_path / "nope.json")
        assert exc.value.code == config.EXIT_CONFIG_ERROR

    def test_invalid_json_exits(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{ not json")
        with pytest.raises(SystemExit) as exc:
            Config.load(cfg_file)
        assert exc.value.code == config.EXIT_CONFIG_ERROR


class TestConfigLookup:
    def test_get_hit(self, tmp_path):
        cfg = Config(**_config(tmp_path))
        rj = cfg.get("home")
        assert rj is not None
        assert rj.job.name == "home"

    def test_get_miss_is_none(self, tmp_path):
        cfg = Config(**_config(tmp_path))
        assert cfg.get("nope") is None

    def test_getitem_hit(self, tmp_path):
        cfg = Config(**_config(tmp_path))
        assert cfg["home"].mount_point == Path("/bak") / Path(str(tmp_path)).name

    def test_getitem_miss_raises_keyerror(self, tmp_path):
        cfg = Config(**_config(tmp_path))
        with pytest.raises(KeyError):
            cfg["nope"]


# --- Job validators ---


class TestJobValidators:
    def test_unsafe_name_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Job(
                name="bad/name",
                snapshot_dir=tmp_path,
                source_subvolume=tmp_path,
                repo="r",
            )

    def test_unsafe_tag_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Job(
                name="ok",
                tag="bad tag",
                snapshot_dir=tmp_path,
                source_subvolume=tmp_path,
                repo="r",
            )

    def test_missing_snapshot_dir_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Job(
                name="ok",
                snapshot_dir=tmp_path / "nope",
                source_subvolume=tmp_path,
                repo="r",
            )

    def test_snapshot_interval_minimum(self, tmp_path):
        with pytest.raises(ValidationError):
            Job(
                name="ok",
                snapshot_dir=tmp_path,
                source_subvolume=tmp_path,
                repo="r",
                snapshot_interval=1,
            )

    def test_intervals_parsed(self, tmp_path):
        job = Job(
            name="ok",
            snapshot_dir=tmp_path,
            source_subvolume=tmp_path,
            repo="r",
            snapshot_interval="2h",
            backup_interval="1w",
        )
        assert job.snapshot_interval == 7200
        assert job.backup_interval == 604800


# --- Window ---


class TestWindow:
    def test_parse_missing_is_none(self):
        assert Window.parse(None, None) is None
        assert Window.parse("02:00", None) is None

    def test_parse_invalid_is_none(self):
        assert Window.parse("not-a-time", "06:00") is None

    def test_same_day_window(self):
        w = Window.parse("02:00", "06:00")
        assert w.contains(datetime(2026, 1, 1, 3, 0))
        assert not w.contains(datetime(2026, 1, 1, 1, 0))
        assert not w.contains(datetime(2026, 1, 1, 6, 0))  # end is exclusive

    def test_overnight_window(self):
        w = Window.parse("22:00", "06:00")
        assert w.contains(datetime(2026, 1, 1, 23, 0))
        assert w.contains(datetime(2026, 1, 1, 1, 0))
        assert not w.contains(datetime(2026, 1, 1, 12, 0))


# --- Retention builders ---


class TestRetention:
    def test_exponential(self):
        now = datetime.now(timezone.utc)
        bounds = build_retention_exponential(3)
        assert len(bounds) == 3
        assert all(b <= now for b in bounds)

    def test_datetimes(self):
        bounds = build_retention_datetimes(hourly=2, daily=1)
        assert len(bounds) == 3

    def test_keeplist_keeps_recent_and_slot_oldest(self):
        now = datetime.now(timezone.utc)
        snaps = [Snapshot(Path(f"/s/{i}"), now - timedelta(hours=i)) for i in range(5)]
        keep = build_snapshot_keeplist(snaps, [now - timedelta(hours=2)])
        assert keep == {snaps[0].path, snaps[2].path}

    def test_keeplist_ignores_source_subvolume(self):
        now = datetime.now(timezone.utc)
        snaps = [
            Snapshot(Path("/s/src"), None),  # source subvolume, time=None
            Snapshot(Path("/s/a"), now - timedelta(hours=1)),
        ]
        keep = build_snapshot_keeplist(snaps, [now - timedelta(hours=2)])
        assert keep == {Path("/s/a")}
