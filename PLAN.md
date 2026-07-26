# snap-bak Improvement Plan

Date: 2026-07-25
Status: Phase 1 in progress

## Overview

Two-phase plan:
- **Phase 1**: Pydantic config models + password_file per-repo + mount base /bak + error codes
- **Phase 2**: Delayed job handling (separate plan)

---

## Phase 1: Pydantic Config + password_file per-repo

### Decisions

| Topic | Decision |
|-------|----------|
| Config library | Pydantic v2 models |
| `password_file` | Per-repo, not top-level |
| `snapshots_to_keep` | `Field(ge=1)` — must be positive |
| `snapshot_dir` | `field_validator` — hard error if path doesn't exist |
| `backup_interval` | `Field(ge=1)` |
| `backup_retries` | `Field(ge=1)` — no infinite (-1) |
| `timeout` | Default 86400 (24h), respects backup window |
| `excludes` | List of restic `--exclude` patterns |
| Mount base | `/bak` default, error if missing |
| Extra config keys | Custom `__init__` that warns |
| Error codes | 0=success, 1=general, 2=config, 3=mount, 4=permission |

### config/config.json

```json
{
  "repos": [
    {
      "name": "primary",
      "path": "rclone:CONFIG_ENTRY:DIRECTORY",
      "password_file": "/home/user/.restic_password",
      "size_limit": 1099511627776,
      "latency_ms": 50,
      "throughput_mbps": 10
    }
  ],
  "mount_base": "/bak",
  "stuck_threshold": 3600,
  "jobs": [
    {
      "name": "data",
      "snapshot_dir": "/mnt/data/.snapshots",
      "repo": "primary",
      "retention": {"daily": 6, "monthly": 4, "yearly": 2},
      "snapshots_to_keep": 5,
      "backup_interval": 3600,
      "backup_window_start": "22:00",
      "backup_window_end": "06:00",
      "ignore_file": "/etc/snap-bak/data.ignore",
      "excludes": ["*.tmp", "*.cache", ".DS_Store"],
      "backup_retries": 3,
      "timeout": 86400
    }
  ]
}
```

### Pydantic Models

```python
from pydantic import BaseModel, Field, field_validator, ConfigDict
from pathlib import Path
import warnings

class Repo(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str
    path: str  # NOT Path — restic paths like 'rclone:CONFIG_ENTRY:DIRECTORY'
    password_file: str = ""
    size_limit: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=100, ge=0)
    throughput_mbps: int = Field(default=1, ge=0)

class Job(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str
    repo: str  # references Repo.name
    snapshot_dir: str
    retention: dict = Field(default_factory=dict)
    snapshots_to_keep: int = Field(default=5, ge=1)
    backup_interval: int = Field(default=3600, ge=1)
    backup_window_start: str | None = None
    backup_window_end: str | None = None
    ignore_file: str = ""
    excludes: list = Field(default_factory=list)
    backup_retries: int = Field(default=3, ge=1)
    timeout: int = Field(default=86400, ge=1)
    # Runtime fields (not from config)
    repos: list[Repo] = Field(default_factory=list, exclude=True)
    mount_base: str = "/bak"
    dry_run: bool = False
    config_path: str | None = None

    @field_validator('snapshot_dir')
    @classmethod
    def snapshot_dir_must_exist(cls, v):
        if not Path(v).exists():
            raise ValueError(f'snapshot_dir does not exist: {v}')
        return v

class SnapBakConfig(BaseModel):
    model_config = ConfigDict(extra='ignore')
    repos: list[Repo]
    jobs: list[Job]
    mount_base: str = "/bak"
    stuck_threshold: int = Field(default=3600, ge=1)

    def __init__(self, **data):
        known_keys = {'repos', 'jobs', 'mount_base', 'stuck_threshold'}
        unknown = set(data.keys()) - known_keys
        if unknown:
            warnings.warn(f"Unknown config keys: {unknown}")
        super().__init__(**data)
```

### Code Changes

#### 1. Replace dataclasses with pydantic models

| Location | Change |
|----------|--------|
| `Repo` class (line 177) | Replace `@dataclass` with `BaseModel`, add `password_file` |
| `Job` class (line 189) | Replace `@dataclass` with `BaseModel`, add validators |
| Add `SnapBakConfig` | New pydantic model for top-level config |

#### 2. Replace config loading

| Location | Change |
|----------|--------|
| `load_config` (line 781) | Replace with `SnapBakConfig.model_validate_json()` |
| `validate_config` (line 789) | Remove — pydantic handles validation |
| `jobs_from_config` (line 822) | Remove — `config.jobs` already returns Job objects |

#### 3. `self.repo` property returns Repo object

Current (line 209-215):
```python
@property
def repo(self) -> str:
    for r in self.repos:
        if r.name == self.repo_name:
            return r.path
```

Revised:
```python
@property
def repo(self) -> Repo:
    for r in self.repos:
        if r.name == self.repo_name:
            return r
```

Update callers:
- `_build_restic_cmd`: use `repo.path` and `repo.password_file`
- `main()` line 888: use `job.repo.path` for listing

#### 4. `_build_restic_cmd` uses Repo object

```python
def _build_restic_cmd(self, subcommand: str, repo: Repo | None = None) -> list[str]:
    if repo is None:
        repo = self.repo
    cmd = ["restic", "-r", repo.path, subcommand]
    if repo.password_file:
        cmd.extend(["--password-file", repo.password_file])
    if self.ignore_file:
        cmd.extend(["--exclude-file", self.ignore_file])
    for pattern in self.excludes:
        cmd.extend(["--exclude", pattern])
    return cmd
```

#### 5. Update method signatures

```python
def _preflight(self, mount_point: Path, repo: Repo | None = None) -> int:
def _backup(self, mount_point: Path, repo: Repo | None = None):
def _forget_and_prune(self, repo: Repo | None = None):
```

#### 6. Mount base /bak + existence check

- Change `DEFAULT_MOUNT_BASE` to `Path("/bak")`
- Add startup check in `main()`:
```python
mount_base = Path(config.mount_base)
if not mount_base.exists():
    logger.error(f"Mount base {mount_base} does not exist")
    sys.exit(3)
```

#### 7. Move permission checks before global lock

Current flow:
```
load_config → validate_config → jobs_from_config → acquire_global_lock → run()
```

Revised:
```
SnapBakConfig.load() → check mount_base → check password files → acquire_global_lock → run()
```

#### 8. Fix `mount_and_lock` to assert

Current (line 627-628):
```python
if snapshot_path is None:
    snapshot_path = self._get_latest_snapshot()
```

Revised:
```python
assert snapshot_path is not None, "mount_and_lock requires explicit snapshot_path"
```

#### 9. Error status codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (job failed, etc.) |
| 2 | Config validation error |
| 3 | Mount base not found |
| 4 | Permission error |

---

## Phase 2: Delayed Job Handling (separate plan)

### Design principles

1. Jobs have fixed repos (no dynamic selection)
2. If repo can't accept backup (size_limit exceeded, drive missing), job is delayed
3. Delayed snapshots are protected from cleanup
4. Delayed jobs retry on next run when conditions improve
5. Run `restic forget --prune` before size check to free space

### Changes (detailed in separate plan)

- Remove `_select_repo`
- Add size_limit check after forget/prune
- Add "delayed" status
- Add delayed snapshot protection
- Add timeout estimation (Phase 2)

---

## Implementation Order

1. Define pydantic models (Repo, Job, SnapBakConfig)
2. Replace config loading/validation
3. Update `self.repo` property + method signatures
4. Move `password_file` to Repo
5. Change mount base to /bak + existence check
6. Move permission checks before lock
7. Fix `mount_and_lock` assert
8. Add error status codes
9. Update config.json
10. Update tests
11. Update README
12. Run tests

## Verification

```bash
PYTHONPATH=. python -m pytest tests/test_snapbak.py -v
```

All tests must pass.
