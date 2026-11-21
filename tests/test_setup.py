"""The installer's decisions: rendered files, argument handling, git gates."""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[1] / "snap-bak-setup.py"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def load_setup():
    spec = importlib.util.spec_from_file_location("snap_bak_setup", SETUP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_setup()


def test_launcher_runs_the_venv_without_the_cwd_on_the_path():
    text = setup.render_launcher()
    assert 'exec "/opt/snap-bak/venv/bin/python" -P -m snap_bak.cli "$@"' in text
    assert 'SNAPBAK_CONFIG_FILE="/opt/snap-bak/config.json"' in text


def test_service_runs_the_venv_as_the_service_user():
    text = setup.render_service()
    assert "ExecStart=/opt/snap-bak/venv/bin/python -P -m snap_bak.cli" in text
    assert "User=snap-bak" in text
    assert "Environment=SNAPBAK_CONFIG_FILE=/opt/snap-bak/config.json" in text
    assert "Type=oneshot" in text


def test_timer_is_hourly_and_persistent():
    text = setup.render_timer()
    assert "OnCalendar=hourly" in text
    assert "Persistent=true" in text


def test_sudoers_allows_only_the_wrapper():
    text = setup.render_sudoers()
    rules = [line for line in text.splitlines() if not line.startswith("#") and line.strip()]
    assert rules == ["snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/sudo-exec.py"]
    assert "*" not in text


def test_wrapper_shebang_names_the_installed_venv(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/sudo-exec.py").write_text("#!/usr/bin/env python3\nprint('x')\n")
    lines = setup.render_wrapper(tmp_path).splitlines()
    assert lines[0] == "#!/opt/snap-bak/venv/bin/python"
    assert lines[1] == "print('x')"


def test_ref_is_accepted_in_both_forms():
    assert setup.parse_args(["--ref", "v1"]).ref == "v1"
    assert setup.parse_args(["--ref=abc123"]).ref == "abc123"
    assert setup.parse_args([]).ref is None


def test_the_old_backup_flag_is_gone():
    with pytest.raises(SystemExit):
        setup.parse_args(["--backup"])
    with pytest.raises(SystemExit):
        setup.parse_args(["--ref"])


def test_uv_is_preferred_and_pip_is_the_fallback():
    assert setup.choose_tool("/usr/bin/uv", True) == "uv"
    assert setup.choose_tool(None, True) == "pip"
    with pytest.raises(setup.SetupError, match="Neither uv nor pip"):
        setup.choose_tool(None, False)


def test_update_line_is_time_version_commit():
    assert setup.update_line("c0ffee", "0.6.0", "2026-11-18T00:00:00Z") == (
        "2026-11-18T00:00:00Z 0.6.0 c0ffee"
    )


def test_package_version_matches_lockfile():
    root = Path(__file__).resolve().parents[1]
    spec = re.search(
        r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.MULTILINE
    )
    lock = re.search(
        r'name = "snap-bak"\nversion = "([^"]+)"', (root / "uv.lock").read_text()
    )
    assert spec is not None and lock is not None
    assert spec.group(1) == lock.group(1)


def make_repo(path):
    (path / "src/snap_bak").mkdir(parents=True)
    (path / "src/sudo-exec.py").write_text("#!/usr/bin/env python3\n")
    (path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0.0.0'\n")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=path,
        check=True,
    )


@needs_git
def test_a_dirty_checkout_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_repo(repo)
    setup.check_clean(repo)
    (repo / "scratch.txt").write_text("uncommitted")
    with pytest.raises(setup.SetupError, match="Uncommitted changes"):
        setup.check_clean(repo)


@needs_git
def test_a_non_checkout_is_refused(tmp_path):
    (tmp_path / "src/snap_bak").mkdir(parents=True)
    (tmp_path / "src/sudo-exec.py").write_text("#!/usr/bin/env python3\n")
    with pytest.raises(setup.SetupError, match="not a git checkout"):
        setup.resolve_source(tmp_path, None, tmp_path)


@needs_git
def test_a_ref_is_exported_and_resolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_repo(repo)
    stage = tmp_path / "stage"
    stage.mkdir()
    source, commit = setup.resolve_source(repo, "HEAD", stage)
    assert (source / "src/sudo-exec.py").is_file()
    assert commit == setup.git(repo, "rev-parse", "HEAD").stdout.strip()


@needs_git
def test_an_unknown_ref_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_repo(repo)
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(setup.SetupError, match="Unknown ref"):
        setup.resolve_source(repo, "nope", stage)


def test_plan_reports_what_changed(tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    installed = tmp_path / "installed"
    installed.mkdir()
    (staged / "same").write_text("x")
    (installed / "same").write_text("x")
    (staged / "new").write_text("y")
    (staged / "changed").write_text("new")
    (installed / "changed").write_text("old")
    managed = {
        "same": setup.Managed("same", installed / "same", "0644"),
        "new": setup.Managed("new", installed / "new", "0644"),
        "changed": setup.Managed("changed", installed / "changed", "0644"),
    }
    plan = setup.plan_files(
        {"same": staged / "same", "new": staged / "new", "changed": staged / "changed"}, managed
    )
    assert plan.unchanged == ["same"]
    assert plan.added == ["new"]
    assert plan.updated == ["changed"]
    assert plan.changes
    assert setup.summary_line(plan) == (
        "Install report: 1 added, 1 updated, 1 unchanged, venv unchanged"
    )


def test_a_mode_drift_counts_as_a_change(tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "launcher").write_text("x")
    installed = tmp_path / "launcher"
    installed.write_text("x")
    installed.chmod(0o755)
    plan = setup.plan_files(
        {"launcher": staged / "launcher"},
        {"launcher": setup.Managed("launcher", installed, "0644")},
    )
    assert plan.updated == ["launcher"]


def test_build_is_never_compared(tmp_path):
    (tmp_path / "BUILD").write_text("old")
    plan = setup.plan_files(
        {"BUILD": tmp_path / "staged-build"},
        {"BUILD": setup.Managed("BUILD", tmp_path / "BUILD", "0644")},
    )
    assert plan.changes is False


def test_tree_diff_ignores_pycache_and_finds_changes(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "__pycache__").mkdir(parents=True)
        (root / "pkg").mkdir()
        (root / "pkg/mod.py").write_text("same")
    (left / "__pycache__/x.cpython-314.pyc").write_text("a")
    (right / "__pycache__/x.cpython-314.pyc").write_text("b")
    assert setup.tree_diff(left, right) == []
    (right / "pkg/mod.py").write_text("changed")
    (right / "pkg/extra.py").write_text("x")
    (left / "pkg/gone.py").write_text("x")
    assert setup.tree_diff(left, right) == ["pkg/extra.py", "pkg/gone.py", "pkg/mod.py"]


def test_env_fingerprint_follows_the_dependency_inputs(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nversion = '1'\n")
    first = setup.env_fingerprint(tmp_path, sys.executable)
    assert setup.env_fingerprint(tmp_path, sys.executable) == first
    (tmp_path / "uv.lock").write_text("lock")
    assert setup.env_fingerprint(tmp_path, sys.executable) != first


def test_render_records_the_commit_and_the_fingerprint(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    paths = setup.render(repo, tmp_path, "2026-11-18T00:00:00Z", "abc123", "feedface")
    assert paths["REF"].read_text() == "abc123\n"
    assert paths["ENV"].read_text() == "feedface\n"
    assert paths["BUILD"].read_text() == "2026-11-18T00:00:00Z\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_a_destination_the_caller_cannot_read_is_left_alone(tmp_path):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "home.ignore").write_text("default\n")
    hidden = tmp_path / "excludes"
    hidden.mkdir()
    (hidden / "home.ignore").write_text("*important*\n")
    hidden.chmod(0o000)
    try:
        plan = setup.plan_files(
            {"home.ignore": staged / "home.ignore"},
            {"home.ignore": setup.Managed("home.ignore", hidden / "home.ignore", "0640")},
        )
    finally:
        hidden.chmod(0o755)
    assert plan.unchanged == ["home.ignore"]
    assert plan.updated == []
    assert plan.added == []


def test_the_wrapper_is_closed_to_other_users():
    managed = setup.targets({})
    assert managed["wrapper"].mode == "0750"
    assert managed["wrapper"].owner == "root:snap-bak"
