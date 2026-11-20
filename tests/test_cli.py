"""Tests for snap_bak.cli: exit-code ladder, restic-update gating, help and logs."""

import logging
import types

import pytest

from snap_bak import cli, config
from snap_bak.job import Result, ResultStatus


def _r(status, reason=None, timeout=False):
    return Result("j", ResultStatus(status), reason=reason, timeout=timeout)


class TestExitCode:
    def test_empty_is_success(self):
        assert cli.exit_code([]) == config.EXIT_SUCCESS

    def test_success_is_zero(self):
        assert cli.exit_code([_r("success")]) == config.EXIT_SUCCESS

    def test_failed_wins(self):
        assert cli.exit_code([_r("failed")]) == config.EXIT_PERM_FAILED

    def test_timeout(self):
        assert cli.exit_code([_r("skipped", timeout=True)]) == config.EXIT_PERM_TIMEOUT

    def test_delayed_unreachable(self):
        assert (
            cli.exit_code([_r("delayed", "unreachable")])
            == config.EXIT_TEMP_UNREACHABLE
        )

    def test_delayed_size_limit(self):
        assert (
            cli.exit_code([_r("delayed", "size_limit")]) == config.EXIT_TEMP_SIZE_LIMIT
        )

    def test_retention_failed_alone_is_success(self):
        assert cli.exit_code([_r("retention_failed")]) == config.EXIT_SUCCESS

    def test_failed_beats_timeout(self):
        results = [_r("skipped", timeout=True), _r("failed")]
        assert cli.exit_code(results) == config.EXIT_PERM_FAILED

    def test_unreachable_beats_size_limit(self):
        results = [_r("delayed", "size_limit"), _r("delayed", "unreachable")]
        assert cli.exit_code(results) == config.EXIT_TEMP_UNREACHABLE


class TestResticUpdateDue:
    def test_never(self):
        assert cli._restic_update_due([_r("success")], "never") is False

    def test_after_with_success(self):
        assert cli._restic_update_due([_r("success")], "after") is True

    def test_after_without_success(self):
        assert cli._restic_update_due([_r("skipped")], "after") is False
        assert cli._restic_update_due([_r("failed")], "after") is False

    def test_after_with_mixed(self):
        assert cli._restic_update_due([_r("skipped"), _r("success")], "after") is True


class TestHelp:
    @staticmethod
    def _help(parser, capsys):
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])
        return " ".join(capsys.readouterr().out.split())

    def test_top_level_help_names_verbs_and_flags(self, capsys):  # [[R-CLI-31]] [[R-CLI-34]]
        out = self._help(cli._main_parser(), capsys)
        assert "show excludes" in out
        assert "apply excludes" in out
        for needle in (
            "--reveal",
            "-e",
            "--excludes-file",
            "--price",
            "--format",
            "--json",
            "--limit",
        ):
            assert needle in out
        assert "Common requests" in out

    def test_show_help_opens_with_the_inspect_task(self, capsys):  # [[R-CLI-32]]
        out = self._help(cli.excludes_parser("show"), capsys)
        assert "Inspect what the exclude rules match" in out
        assert out.index("Inspect what") < out.index("options:")

    def test_apply_help_opens_with_the_remove_task(self, capsys):  # [[R-CLI-32]]
        out = self._help(cli.excludes_parser("apply"), capsys)
        assert "Remove files that the configured exclude rules match" in out
        assert out.index("Remove files") < out.index("options:")

    def test_every_exclude_flag_answers_a_question(self, capsys):  # [[R-CLI-33]]
        show = self._help(cli.excludes_parser("show"), capsys)
        for phrase in (
            "This is the default when no selector is given",
            "reads the repo over the network",
            "without editing the config",
            "candidate rule would newly exclude",
            "every excluded file",
        ):
            assert phrase in show
        apply = self._help(cli.excludes_parser("apply"), capsys)
        for phrase in ("Fails fast", "next eligible scheduled run"):
            assert phrase in apply
        for out in (show, apply):
            assert "The noun to act on" in out
            assert "Path to config JSON" in out
            assert "Restrict to one job" in out


class TestRunSummary:
    def test_run_start_and_end_lines(self, monkeypatch, tmp_path, caplog):  # [[R-CLI-35]]
        cfg = config.Config.model_validate(
            {
                "mount_base": str(tmp_path),
                "repos": [{"name": "r", "path": str(tmp_path / "repo")}],
                "jobs": [
                    {
                        "name": "j",
                        "snapshot_dir": str(tmp_path / "snaps"),
                        "source_subvolume": str(tmp_path),
                        "repo": "r",
                    }
                ],
            }
        )
        (tmp_path / "snaps").mkdir()
        monkeypatch.setattr(
            config.Config, "load", classmethod(lambda cls, path: cfg)
        )
        monkeypatch.setattr(
            cli, "_run_locked", lambda cfg, jobs, args: ([], config.EXIT_SUCCESS)
        )
        args = types.SimpleNamespace(
            config=str(tmp_path / "config.json"),
            list_jobs=False,
            job=None,
            dry_run=True,
        )
        with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
            cli._run_orchestrator(args)
        assert exc.value.code == config.EXIT_SUCCESS
        assert "Starting snap-bak" in caplog.text
        assert "RUN end" in caplog.text
