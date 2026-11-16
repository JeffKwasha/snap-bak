"""snap-bak: btrfs snapshots and restic backups.

The version comes from installed package metadata, so `snap-bak --version`
reports the distribution that is actually installed and cannot drift from
`pyproject.toml`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("snap-bak")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "unknown"
