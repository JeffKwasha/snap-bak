# -*- coding: utf-8 -*-
"""Test-wide isolation.

Redirects SNAPBAK_STATE_DIR to a throwaway directory BEFORE scripts.snap_bak is
imported. snap_bak.py calls STATE_DIR.mkdir(...) at module import time, so
without this every test run would try to create the real /opt/snap-bak/state on
the running machine. The directory is removed on interpreter exit.
"""
import atexit
import os
import shutil
import tempfile

_STATE_TMP = tempfile.mkdtemp(prefix="snap-bak-test-state-")
os.environ["SNAPBAK_STATE_DIR"] = _STATE_TMP
atexit.register(lambda: shutil.rmtree(_STATE_TMP, ignore_errors=True))
