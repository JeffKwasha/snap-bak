"""Version resolution, including the copied install the setup script produces."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import snap_bak

PACKAGE_SRC = Path(snap_bak.__file__).resolve().parent


def test_a_copied_install_reports_the_version_marker(tmp_path):
    """A deploy has no dist-info, so the VERSION file next to the package is the
    only source. -S drops site-packages, which hides this repo's editable
    install and forces the marker branch."""
    shutil.copytree(
        PACKAGE_SRC, tmp_path / "snap_bak", ignore=shutil.ignore_patterns("__pycache__")
    )
    (tmp_path / "VERSION").write_text("9.9.9\n")
    out = subprocess.run(
        [sys.executable, "-S", "-c", "import snap_bak; print(snap_bak.__version__)"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        cwd=tmp_path,
        check=True,
    )
    assert out.stdout.strip() == "9.9.9"
