"""Snapshot domain object — the single owner of the btrfs snapshot model.

Shared by the unprivileged orchestrator (snap_bak.py) and the root-owned
wrapper (sudo-exec.py, formerly snap-bak-io). Installed as
/opt/snap-bak/snapshot.py (root:root 0644, see snap-bak-setup.sh); both callers
import it with a single fallback:

    try:
        from scripts import snapshot
    except ImportError:          # installed standalone under /opt/snap-bak/
        import snapshot

Everything about a snapshot lives here and nowhere else:

  * the naming format (YYYY-MM-DD_HH:MMZ UTC) — parse AND format
  * how a snapshot directory is scanned and ordered
  * the btrfs command line: snapshot/create, delete, generation

Both modules import the SAME strict parse, so the orchestrator can never hand
the wrapper a name the wrapper will reject (the old bare "YYYY-MM-DD_HH:MM"
parse crashed a run when a legacy name reached a privileged delete), and the
wrapper's btrfs command strings can never drift from the model.

Dependency-free (standard library only) and therefore safe for the root
wrapper to import: it must NOT import scripts.snap_bak. Privileged actions
(create/delete/generation) are plain methods — callers decide who may run
them; the orchestrator deliberately shells out to the wrapper via sudo.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Canonical, timezone-honest snapshot name (UTC). The trailing Z marks the
# zone so names never need translating. Exactly ONE format, shared by both
# modules; the strict regex is what the orchestrator previously duplicated
# leniently (removesuffix("Z") accepted legacy bare names).
SNAPSHOT_FMT = "%Y-%m-%d_%H:%MZ"
SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}:\d{2}Z$")

BTRFS = "/usr/bin/btrfs"
_SUBVOLUME_SHOW_TIMEOUT = 30


def snapshot_name(dt: datetime) -> str:
    """Format a datetime as a snapshot name: YYYY-MM-DD_HH:MMZ (UTC)."""
    return dt.strftime(SNAPSHOT_FMT)


def parse_name(name: str) -> datetime | None:
    """Strictly parse a snapshot name -> aware UTC datetime, or None.

    Rejects everything not exactly YYYY-MM-DD_HH:MMZ — including the legacy
    bare "YYYY-MM-DD_HH:MM" form — so a legacy directory is never treated as
    a valid snapshot and can never be handed to a privileged delete.
    """
    if SNAPSHOT_RE.fullmatch(name) is None:
        return None
    try:
        return datetime.strptime(name, SNAPSHOT_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _run_btrfs(cmd: list[str]) -> None:
    """Run a btrfs mutating command; mirror its exit code on failure.

    Mirrors the wrapper's historic `_run` behaviour (command logged to stderr,
    exit code propagated) so a failed create/delete aborts sudo-exec.py with
    the subprocess's return code.
    """
    print(f"sudo-exec CMD: {' '.join(cmd)}", file=sys.stderr)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)


def _subvolume_generation(path: Path) -> int | None:
    """Btrfs 'Generation' (last-change generation) of a subvolume, or None.

    Equal source and snapshot generations mean no change since the snapshot;
    this is the same mechanism btrbk's 'onchange' snapshots use. Requires root
    (ioctl denied for other users), i.e. the wrapper. Fail-open: any error
    returns None so the status op never gates on garbage.
    """
    try:
        result = subprocess.run(
            [BTRFS, "subvolume", "show", str(path)],
            capture_output=True,
            text=True,
            timeout=_SUBVOLUME_SHOW_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(
            f"sudo-exec: btrfs subvolume show failed for {path}: {e}", file=sys.stderr
        )
        return None
    if result.returncode != 0:
        print(
            f"sudo-exec: btrfs subvolume show failed for {path} "
            f"(rc={result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    m = re.search(r"^\s*Generation:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    if not m:
        print(
            f"sudo-exec: no Generation field in btrfs subvolume show output for {path}",
            file=sys.stderr,
        )
        return None
    return int(m.group(1))


class Snapshot:
    """A btrfs subvolume: a snapshot (time parsed from its name) or a source
    subvolume (time=None, e.g. the path being snapshotted).

    Construction never touches btrfs: scanning is filesystem-only, and the
    privileged generation read is deferred and memoized, so a directory scan
    can never wake a spun-down pool or require root.
    """

    __slots__ = ("path", "time", "_generation", "_generation_read")

    def __init__(self, path: Path | str, time: datetime | None = None):
        self.path = Path(path)
        self.time = time
        self._generation: int | None = None
        self._generation_read = False

    @property
    def name(self) -> str:
        """Basename of the subvolume (a string only on display/subprocess)."""
        return self.path.name

    @property
    def is_snapshot(self) -> bool:
        """True when the path parses as a canonical snapshot name."""
        return self.time is not None

    @property
    def age(self) -> timedelta | None:
        """Age of the snapshot (now - time); None for a source subvolume."""
        if self.time is None:
            return None
        return datetime.now(timezone.utc) - self.time

    @property
    def generation(self) -> int | None:
        """Btrfs 'Generation' of this subvolume, memoized, or None.

        Reads it at most once per Snapshot object, so repeated reads and
        has_changed() comparisons never re-invoke btrfs. Requires root (the
        wrapper); the orchestrator must not call it. Fail-open on error.
        """
        if not self._generation_read:
            self._generation = _subvolume_generation(self.path)
            self._generation_read = True
        return self._generation

    def has_changed(self, older: Snapshot) -> bool:
        """True when this subvolume changed since `older`'s generation.

        Both generations are memoized, so the comparison never adds btrfs
        calls when they have already been read. Fail-open: unknown (None)
        counts as NOT changed so a probe error never suppresses a snapshot.
        """
        new, old = self.generation, older.generation
        if new is None or old is None:
            return False
        return new != old

    def create(self, source: Path | str) -> None:
        """btrfs subvolume snapshot -r <source> <self.path> (requires root)."""
        _run_btrfs([BTRFS, "subvolume", "snapshot", "-r", str(source), str(self.path)])

    def delete(self) -> None:
        """btrfs subvolume delete <self.path> (requires root)."""
        _run_btrfs([BTRFS, "subvolume", "delete", str(self.path)])

    @classmethod
    def scan(cls, snapshot_dir: Path | str) -> list[Snapshot]:
        """All snapshots in a directory matching the canonical format.

        Pure filesystem scan: lists subdirectories, parses names strictly, and
        ignores anything that does not match exactly (legacy no-Z names
        included). Never touches btrfs, so it is safe for the unprivileged
        orchestrator and for the power-saving (do-not-wake-the-pool) path.
        Returned oldest -> newest (names sort lexicographically == by time).
        """
        d = Path(snapshot_dir)
        if not d.is_dir():
            return []
        found: list[Snapshot] = []
        for p in sorted(d.iterdir()):
            if not p.is_dir():
                continue
            t = parse_name(p.name)
            if t is not None:
                found.append(cls(p, t))
        return found

    @classmethod
    def latest(cls, snapshot_dir: Path | str) -> Snapshot | None:
        """Most recent snapshot in a directory, or None."""
        snaps = cls.scan(snapshot_dir)
        return snaps[-1] if snaps else None

    @classmethod
    def oldest_within(
        cls, snapshot_dir: Path | str, interval: timedelta
    ) -> Snapshot | None:
        """Oldest snapshot newer than (now - interval), or None.

        Prefer it over `latest` when backing up: it ages the snapshot cadence
        rather than always hammering the most recent subvolume.
        """
        cutoff = datetime.now(timezone.utc) - interval
        for s in cls.scan(snapshot_dir):
            if s.time is not None and s.time >= cutoff:
                return s
        return None
