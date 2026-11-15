"""snap-bak: btrfs snapshots and restic backups.

The version comes from installed package metadata, so `snap-bak --version`
cannot drift from `pyproject.toml`. The setup script copies the package instead
of installing a wheel, which leaves no metadata, so it also writes `VERSION`
next to the package and that file is the fallback.
"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("snap-bak")
except PackageNotFoundError:
    try:
        __version__ = (
            Path(__file__).resolve().parents[1] / "VERSION"
        ).read_text().strip() or "unknown"
    except OSError:  # running from a source tree, not installed
        __version__ = "unknown"
