# -*- coding: utf-8 -*-
"""Unit tests for snap_bak/restic.py — command construction & output parsing.

The central regression here: backup-only flags (excludes, non-global
restic_options) must NEVER leak onto other restic subcommands like `forget`,
which reject them with 'unknown flag'.
"""

from snap_bak import restic


class TestBuild:
    def test_backup_includes_global_and_backup_options(self):
        cmd = restic.build(
            "backup",
            repo_path="rclone:remote:repo",
            password_file="/pw",
            options=["--compression=better", "--one-file-system"],
            exclude_file="/etc/x.ignore",
            excludes=["*.tmp", "*.cache"],
        )
        assert cmd[:4] == ["restic", "-r", "rclone:remote:repo", "backup"]
        assert "--compression=better" in cmd  # global
        assert "--one-file-system" in cmd  # backup-only
        assert ["--exclude-file", "/etc/x.ignore"] == cmd[
            cmd.index("--exclude-file") : cmd.index("--exclude-file") + 2
        ]
        assert "--exclude" in cmd and "*.tmp" in cmd and "*.cache" in cmd
        assert ["--password-file", "/pw"] == cmd[
            cmd.index("--password-file") : cmd.index("--password-file") + 2
        ]

    def test_forget_gets_global_but_no_backup_only_flags(self):
        cmd = restic.build(
            "forget",
            repo_path="r:",
            password_file="/pw",
            options=["--compression=better", "--one-file-system"],
            exclude_file="/etc/x.ignore",
            excludes=["*.tmp"],
        )
        assert cmd[3] == "forget"
        assert "--compression=better" in cmd  # global still applies
        assert "--one-file-system" not in cmd  # backup-only dropped
        assert "--exclude" not in cmd
        assert "--exclude-file" not in cmd

    def test_probe_gets_global_only(self):
        cmd = restic.build(
            "cat",
            repo_path="r:",
            options=["--compression=better", "--option", "rclone.program=foo"],
            exclude_file="/etc/x.ignore",
        )
        assert cmd[3] == "cat"
        assert "--compression=better" in cmd
        assert "--option" in cmd and "rclone.program=foo" in cmd
        assert "--exclude-file" not in cmd

    def test_reserved_flags_dropped_with_warning(self):
        warns = []
        cmd = restic.build(
            "backup",
            repo_path="r:",
            options=[
                "--tag",
                "x",
                "--json",
                "--exclude",
                "y",
                "--prune",
                "-r",
                "/other",
            ],
            warn=warns.append,
        )
        assert set(warns) == {"--tag", "--json", "--exclude", "--prune", "-r"}
        # --tag/-r from options are dropped; only the base -r (repo) remains.
        assert cmd.count("-r") == 1
        for tok in ("--tag", "x", "--json", "--exclude", "y", "--prune", "/other"):
            assert tok not in cmd

    def test_short_global_option_applies(self):
        cmd = restic.build("forget", repo_path="r:", options=["-o", "key=value"])
        assert "-o" in cmd and "key=value" in cmd

    def test_no_password_file_when_empty(self):
        cmd = restic.build("forget", repo_path="r:")
        assert "--password-file" not in cmd

    def test_repo_option_precedes_job_option(self):
        cmd = restic.build(
            "backup",
            repo_path="r:",
            options=["--compression=better", "--one-file-system"],
        )
        assert cmd.index("--compression=better") < cmd.index("--one-file-system")


class TestParsing:
    def test_last_json_line(self):
        out = '{"message_type":"status"}\nnot json\n{"message_type":"summary","total_size":42}\n'
        obj = restic.last_json_line(out)
        assert obj is not None
        assert obj["total_size"] == 42

    def test_last_json_line_none(self):
        assert restic.last_json_line("no json here") is None

    def test_find_summary(self):
        out = '{"message_type":"status"}\n{"message_type":"summary","snapshot_id":"abc"}\n'
        obj = restic.find_summary(out)
        assert obj is not None
        assert obj["snapshot_id"] == "abc"

    def test_find_summary_missing(self):
        out = '{"message_type":"status"}\n'
        assert restic.find_summary(out) is None

    def test_find_summary_skips_bad_lines(self):
        out = 'garbage\n{"message_type":"summary","snapshot_id":"ok"}\n'
        obj = restic.find_summary(out)
        assert obj is not None
        assert obj["snapshot_id"] == "ok"
