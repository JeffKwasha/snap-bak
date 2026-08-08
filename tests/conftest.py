# -*- coding: utf-8 -*-
"""Test-wide isolation.

Redirects XDG_STATE_HOME to a throwaway directory BEFORE scripts.snapbak is
imported. snapbak.py calls STATE_DIR.mkdir(...) at module import time, so
without this every test run would create the real ~/.local/state/snapbak in
the running user's home. The directory is removed on interpreter exit.
"""
import atexit
import os
import shutil
import tempfile

_STATE_TMP = tempfile.mkdtemp(prefix="snapbak-test-state-")
os.environ["XDG_STATE_HOME"] = _STATE_TMP
atexit.register(lambda: shutil.rmtree(_STATE_TMP, ignore_errors=True))
