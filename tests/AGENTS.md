# Test infrastructure

Conventions for this directory. Read this before adding a test or a helper.

## Temporary files

**Use `tmp_path` (or `tmp_path_factory`) for anything a test creates.**

`pyproject.toml` sets `tmp_path_retention_policy = "none"`, so pytest removes
every `tmp_path` directory it hands out. A test that builds its own temporary
directory with `tempfile.mkdtemp` is invisible to that: the directory survives
the run and survives the suite, and accumulates quietly across development
iterations. This is not hypothetical — a `state_dir` helper written that way left
36 orphaned directories behind on one machine.

`tempfile.mkdtemp` / `tempfile.mkstemp` are banned here by ruff `TID251`
(`[tool.ruff]` in `pyproject.toml`), with one deliberate exception.

### The one exception: `conftest.py`

`conftest.py` redirects `SNAPBAK_STATE_DIR` at **module import**, before any
fixture can exist, because `config.py` does `STATE_DIR.mkdir()` at import time —
without the redirect, every test run would try to create the real
`/opt/snap-bak/state` on the running machine. There is no fixture available at
that point, so that one call uses `tempfile.mkdtemp` and registers an `atexit`
removal. Ruff is told to ignore `conftest.py` for this rule, and only for this
rule.

If you need state redirection *inside* a test, take the `state_dir` fixture that
`test_apply.py` defines, or use `monkeypatch.setattr(config, "STATE_DIR", ...)`.

## Shared helpers

Helpers that more than one test module needs live in `conftest.py`. Do not
re-define them per module — a module-local copy silently drifts from the shared
one, which is exactly how `test_cli_excludes.py` ended up with its own
`_resolved_job` and `test_excludes_integration.py` with its own `_restic`,
`_backup` and `_stored_files`.

Available from `conftest.py`:

| Helper | Purpose |
|--------|---------|
| `restic(repo, *args)` | run the real restic binary against a throwaway repo |
| `init_repo(repo)` | `restic init`, asserting success |
| `backup(repo, source, *extra)` | back up a tree, returning the new snapshot id |
| `stored_files(repo, id)` | the files restic actually stored (no directories) |
| `build_source(root)` | a small tree with keeps and typical excludes |
| `resolved_job(tmp_path, monkeypatch, …)` | a `ResolvedJob` over a temp tree and local repo |
| `PASSWORD` | the restic password used by all of the above |

`resolved_job` returns `(resolved_job, source_dir, repo_path)`.

## Module-level fixtures stay local

`test_excludes_integration.py` keeps its own `repo` and `tree` fixtures on
purpose: `tree` is deliberately exhaustive about which matching rules it
exercises, which `build_source` is not. Locality is fine for fixtures used by a
single module — duplication is only a problem for *helpers*.

## restic

Anything that needs the real binary is guarded:

```python
pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not available"
)
```

or `@needs_restic` for a single class. Prefer the decorator when only part of a
module needs it.