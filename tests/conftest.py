"""Test-wide isolation.

Redirects SNAPBAK_STATE_DIR to a throwaway directory BEFORE snap_bak.config is
imported. config.py calls STATE_DIR.mkdir(...) at module import time, so
without this every test run would try to create the real /opt/snap-bak/state on
the running machine. The directory is removed on interpreter exit.
"""

import atexit
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

_STATE_TMP = tempfile.mkdtemp(prefix="snap-bak-test-state-")
os.environ["SNAPBAK_STATE_DIR"] = _STATE_TMP
atexit.register(lambda: shutil.rmtree(_STATE_TMP, ignore_errors=True))

PASSWORD = "test-password"

# The pristine repo `init_repo` copies from; inited on first use.
_RESTIC_TEMPLATE = None


def restic(repo, *args, timeout=120):
    """Run restic against a throwaway repo, with the test password in env."""
    env = dict(os.environ, RESTIC_PASSWORD=PASSWORD)
    return subprocess.run(
        ["restic", "-r", repo, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        # Callers assert on returncode/stdout directly, including failures.
        check=False,
    )


def build_source(root):
    """A small tree with one file that must survive and some that may not.

    keep.txt is never excluded; a.tmp and cache/big.bin are the typical
    exclusions, and cache is a directory so it also covers subtree pruning.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "keep.txt").write_text("keep")
    (root / "a.tmp").write_text("tmp")
    cache = root / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "big.bin").write_bytes(b"z" * 100)


def init_repo(repo):
    """Put an initialised restic repo at `repo`, by copying a template.

    `restic init` costs ~2.7s here and produces an 8 KB directory. Nearly every
    test inits a repo, so that single call is most of the suite's wall time —
    which is what made this suite too slow to run on every edit.

    The template is inited once per session and copied per test: ~1ms instead,
    with each destination a real, independent repo carrying its own key and lock
    file. Nothing is shared between tests, and no test observes the difference.
    """
    global _RESTIC_TEMPLATE
    if _RESTIC_TEMPLATE is None:
        template = pathlib.Path(_STATE_TMP) / "restic-template"
        result = restic(str(template), "init")
        assert result.returncode == 0, result.stderr
        _RESTIC_TEMPLATE = template
    repo = pathlib.Path(repo)
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(_RESTIC_TEMPLATE, repo)
    return str(repo)


def backup(repo, source, *extra, tag="Test"):
    """Back up `source`, returning the new snapshot id."""
    result = restic(repo, "backup", source, "--json", "--tag", tag, *extra)
    assert result.returncode == 0, result.stderr
    for line in reversed(result.stdout.splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("message_type") == "summary":
            return obj["snapshot_id"]
    raise AssertionError(f"no summary in backup output: {result.stdout}")


def stored_files(repo, snapshot_id):
    """The files and symlinks restic stored; `restic ls` also lists directories."""
    from snap_bak import excludes

    result = restic(repo, "ls", "--long", snapshot_id)
    assert result.returncode == 0, result.stderr
    return {
        path
        for path, _size, is_dir in excludes.parse_ls_long(result.stdout)
        if not is_dir
    }


def resolved_job(tmp_path, monkeypatch, *, excludes_list):
    """A ResolvedJob over a throwaway source tree and a local restic repo.

    Sets RESTIC_PASSWORD so the CLI's own restic subprocesses can open the repo,
    and points STATE_DIR at a temp dir so no real state is touched. Returns
    (resolved_job, source_dir, repo_path).
    """
    from snap_bak import config

    src = tmp_path / "src"
    build_source(src)
    snaps = src / ".snapshots"
    snaps.mkdir()
    repo = str(tmp_path / "repo")

    cfg = {
        "repos": [{"name": "local", "path": repo}],
        "mount_base": str(tmp_path / "bak"),
        "jobs": [
            {
                "name": "Test",
                "snapshot_dir": str(snaps),
                "source_subvolume": str(src),
                "repo": "local",
                "backup_retention": {"daily": 6},
                "excludes": excludes_list,
            }
        ],
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(cfg))
    monkeypatch.setenv("RESTIC_PASSWORD", PASSWORD)
    # config.py reads SNAPBAK_STATE_DIR at import; the module-level redirection
    # above already pointed it at a temp dir, so only the monkeypatched tests
    # need this. Kept so each test's state is fully isolated.
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    rj = config.Config.load(config_file).resolve(str(config_file))[0]
    return rj, src, repo


def direct_rewrite_cmd(runner, snapshot_ids):
    """The pre-wrapper rewrite, for integration tests with no root.

    The wrapper routing is unit-tested in tests/test_sudo_exec.py; tests that
    run a real restic rewrite use this so they stay unprivileged.
    """
    from snap_bak import restic as restic_module

    return restic_module.build(
        "rewrite",
        repo_path=runner.repo.path,
        password_file=runner.repo.password_file,
        options=list(runner.repo.restic_options) + list(runner.job.restic_options),
        exclude_file=runner.job.ignore_file or None,
        excludes=runner.job.excludes,
        forget=True,
        extra_args=list(snapshot_ids),
    )


def install_fake_restic(tmp_path, monkeypatch):
    """Put the fake restic on PATH with a writable cache.

    Returns a helper with a `scenario` setter and a `records` reader. The fake
    answers as a working restic would; a scenario only supplies repository
    data, never restic's decisions.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake = bin_dir / "restic"
    fake.write_text((pathlib.Path(__file__).parent / "fake_restic.py").read_text())
    fake.chmod(0o755)
    cache = tmp_path / "fake-cache"
    cache.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(cache))

    class FakeRestic:
        @staticmethod
        def scenario(**data):
            (cache / "fake-restic-scenario.json").write_text(json.dumps(data))

        @staticmethod
        def records():
            return [
                json.loads(path.read_text())
                for path in sorted(cache.glob("fake-restic-*.json"))
            ]

    return FakeRestic


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print the taxonomy counts; commit messages carry them forward."""
    from taxonomy import counts

    c = counts()
    terminalreporter.write_sep("-", "taxonomy")
    terminalreporter.write_line(
        f"{c['tests_cited']}/{c['tests']} tests cite a requirement; "
        f"{c['requirements_cited']}/{c['requirements']} requirements are cited by code or tests"
    )
    if not c["spec_index_agree"]:
        terminalreporter.write_line("SPEC.md and INDEX.md disagree on the requirement IDs")
