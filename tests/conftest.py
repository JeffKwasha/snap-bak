# -*- coding: utf-8 -*-
"""Test-wide isolation.

Redirects XDG_STATE_HOME to a throwaway directory BEFORE scripts.snap_bak is
imported. snap_bak.py calls STATE_DIR.mkdir(...) at module import time, so
without this every test run would create the real ~/.local/state/snap-bak in
the running user's home. The directory is removed on interpreter exit.
"""
import atexit
import os
import shutil
import tempfile

_STATE_TMP = tempfile.mkdtemp(prefix="snap-bak-test-state-")
os.environ["XDG_STATE_HOME"] = _STATE_TMP
atexit.register(lambda: shutil.rmtree(_STATE_TMP, ignore_errors=True))
