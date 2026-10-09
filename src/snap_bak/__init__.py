"""snap-bak: btrfs snapshots and restic backups.

The version is read from installed package metadata rather than hard-coded, so
`snap-bak --version` cannot drift from `pyproject.toml` — an agent that reports
a version is reporting the build it is actually talking to.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("snap-bak")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "unknown"