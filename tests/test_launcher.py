"""The launcher runs the venv interpreter with -P, so a cwd package cannot shadow it."""

import os
import subprocess
import sys
from pathlib import Path

import snap_bak

REAL = Path(snap_bak.__file__).resolve().parent


def test_dash_p_ignores_a_cwd_package(tmp_path):
    fake = tmp_path / "fake" / "snap_bak"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("raise SystemExit('shadowed')\n")
    env = {**os.environ, "PYTHONPATH": str(REAL.parent)}

    shadowed = subprocess.run(
        [sys.executable, "-c", "import snap_bak"],
        cwd=tmp_path / "fake",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert shadowed.returncode != 0
    assert "shadowed" in shadowed.stderr

    guarded = subprocess.run(
        [sys.executable, "-P", "-c", "import snap_bak; print(snap_bak.__file__)"],
        cwd=tmp_path / "fake",
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert guarded.stdout.strip() == str(REAL / "__init__.py")
