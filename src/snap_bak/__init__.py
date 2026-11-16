"""snap-bak: btrfs snapshots and restic backups.

A deploy writes `VERSION` next to the package, and that file is authoritative:
the interpreter's package metadata can belong to another tree, such as a stale
editable install, and would then name a build that is not running. Metadata is
the fallback for a wheel or a source tree.
"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version() -> str:
    marker = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        installed = marker.read_text().strip()
    except OSError:
        installed = ""
    if installed:
        return installed
    try:
        return version("snap-bak")
    except PackageNotFoundError:
        return "unknown"


__version__ = _version()
