"""Tests for snap_bak.cli: exit-code ladder and restic-update gating."""

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
