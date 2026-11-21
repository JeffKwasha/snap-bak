#!/usr/bin/env python3
"""Stand-in for restic at the exec boundary.

The wrapper builds an argv and an environment, then execs restic. This script
records what crossed that boundary under RESTIC_CACHE_DIR. It never touches a
repository and never needs root. The test that installs it reads the record
and asserts the command and environment snap-bak actually built.
"""

import json
import os
import sys
from pathlib import Path


def main() -> None:
    cache = os.environ.get("RESTIC_CACHE_DIR")
    if not cache:
        raise SystemExit("fake restic: RESTIC_CACHE_DIR is unset")
    record = {
        "argv": sys.argv,
        "env": {
            key: os.environ.get(key)
            for key in ("HOME", "RCLONE_CONFIG", "RESTIC_CACHE_DIR", "PATH")
        },
        "uid": os.getuid(),
        "gid": os.getgid(),
    }
    out = Path(cache) / f"fake-restic-{os.getpid()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2))
    print(json.dumps({"message_type": "summary", "snapshot_id": "fake"}))


if __name__ == "__main__":
    main()
