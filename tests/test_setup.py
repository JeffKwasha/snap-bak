"""The installer's decisions: rendered files, argument handling, git gates."""

import importlib.util
import shutil
import subprocess
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


def test_update_line_is_time_version_commit():
    assert setup.update_line("c0ffee", "0.6.0", "2026-11-18T00:00:00Z") == (
        "2026-11-18T00:00:00Z 0.6.0 c0ffee"
    )


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
