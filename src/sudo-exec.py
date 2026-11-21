#!/usr/bin/env python3
"""
sudo-exec.py: root-owned privileged helper for snap-bak.

The only command snap-bak may run via sudo (NOPASSWD). It derives every
path from the ROOT-OWNED config file, so the unprivileged service user can
never pass arbitrary paths to btrfs/mount. Accepted operations:

  sudo-exec.py snapshot      <job> <name> -> btrfs subvolume snapshot -r
  sudo-exec.py delete       <job> <name> -> btrfs subvolume delete
  sudo-exec.py mount        <job> <name> -> mkdir + mount -o bind,ro
  sudo-exec.py umount       <job>        -> umount -l
  sudo-exec.py status       <job>        -> pool disk state + subvolume generations (JSON)
  sudo-exec.py update-restic             -> validate + self-update + setcap

  sudo-exec.py restic-backup <job>             -> restic backup (mutating)
  sudo-exec.py restic-backup-dry-run <job>     -> restic backup --dry-run
  sudo-exec.py restic-forget <job>             -> restic forget --tag + retention
  sudo-exec.py restic-prune <job>              -> restic prune
  sudo-exec.py restic-rewrite <job> <id>...    -> restic rewrite --forget

  The restic ops build the command from the root-owned config and drop to the
  service user before exec. Restic never runs as root.

  Mount points live at <mount_base>/<source_subvolume final component>
  (e.g. /mnt/data -> /mnt/bak/data), always derived from the source
  subvolume's last path component — never configurable. Snapshots are bound
  read-only; btrfs has no uid/gid mount options, and restic reads root-owned
  files via cap_dac_read_search on the restic binary (applied by
  'update-restic').

This wrapper is intentionally standalone: it must NOT import the orchestrator
modules (`job`, `cli`) — the security boundary is the wrapper itself; coupling
to the orchestrator would let a bug there widen this path. It is installed
root:snap-bak 0750 by snap-bak-setup.py.

The snapshot DOMAIN — name format, strict parse, btrfs create/delete, and the
generation read — is shared with the orchestrator through the dependency-free
`snap_bak.snapshot` module. Sharing it is what keeps the orchestrator and
wrapper from disagreeing about what a valid snapshot name is.

The sudoers rule `snap-bak ALL=(root) NOPASSWD:/opt/snap-bak/sudo-exec.py`
is valid on both sudo-rs and sudo.ws: a bare command name matches any
arguments (see sudoers(5)), and '*' is deliberately avoided because sudo-rs
mishandles it.

Config is parsed by `snap_bak.config` (`config.Config.load`), so the wrapper
and orchestrator validate the same JSONC file identically and derive the same
mount points; the JSONC handling lives in exactly one place.
"""

import grp
import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from snap_bak import config, restic, snapshot

CONFIG_PATH = "/opt/snap-bak/config.json"

# Kernel-state sources for the `status` op. Module constants so tests can
# point the functions at a fabricated tree.
_MOUNTINFO_PATH = "/proc/self/mountinfo"
_SYSFS_BTRFS = "/sys/fs/btrfs"
_CLASS_BLOCK = "/sys/class/block"

# sysfs "device/state" values meaning the physical disk is not spinning.
# Nothing else (parked, live, active) is proof of spin-down.
SPUN_DOWN_STATES = frozenset({"standby", "slumber", "powered_off", "offline", "sleep"})
SPUN_UP_STATES = frozenset({"running", "active", "live"})

MOUNT = "/usr/bin/mount"
UMOUNT = "/usr/bin/umount"
RESTIC = "/usr/local/bin/restic"
SETCAP = "/usr/sbin/setcap"
CHGRP = "/usr/bin/chgrp"
CHMOD = "/usr/bin/chmod"
SERVICE_USER = "snap-bak"
SERVICE_GROUP = "snap-bak"
_PROC_PATH = "/proc"

RESTIC_OPS = frozenset(
    {
        "restic-backup",
        "restic-backup-dry-run",
        "restic-forget",
        "restic-prune",
        "restic-rewrite",
    }
)
RESTIC_TIMEOUT_ENV = "SNAPBAK_RESTIC_TIMEOUT"
RESTIC_GRACE_SECONDS = 30
RESTIC_TIMEOUT_S = 30


def _restic_running() -> bool:
    """True if any restic process is alive, whatever its arguments."""
    try:
        entries = Path(_PROC_PATH).iterdir()
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv0 = (entry / "cmdline").read_bytes().decode("utf-8", "ignore").split("\0", 1)[0]
        except OSError:
            continue
        if os.path.basename(argv0) == "restic":
            return True
    return False


def _exit_err(msg: str, code: int = 1) -> NoReturn:
    print(f"sudo-exec: {msg}", file=sys.stderr)
    sys.exit(code)


def _validate_name(name: str) -> str:
    if snapshot.parse_name(name) is None:
        _exit_err(f"invalid snapshot name: {name!r} (expected YYYY-MM-DD_HH:MMZ)")
    return name


def _job_path(snapshot_dir: Path, name: str) -> str:
    """snapshot_dir/name, rejecting any traversal beyond snapshot_dir."""
    base = os.path.realpath(snapshot_dir)
    target = os.path.realpath(os.path.join(snapshot_dir, name))
    if target != os.path.join(base, name) or os.path.commonpath([base, target]) != base:
        _exit_err(f"path escapes snapshot_dir: {name}")
    return target


def _run(cmd: list[str]) -> None:
    print("sudo-exec CMD: " + " ".join(cmd), file=sys.stderr)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)


# --- Status op (power-saving) ---
# The `status` op reports whether the btrfs pool hosting a job's source
# subvolume has any physically spun-down disk, and — only when no disk is
# spun down — the source's current btrfs generation vs. the latest snapshot's.
# Everything is derived at runtime from kernel state (/proc/self/mountinfo,
# /sys/fs/btrfs, /sys/class/block); no device names are hardcoded. When a
# spun-down disk is detected the wrapper performs NO disk I/O at all (no btrfs
# calls, no snapshot-dir listing): an hourly snapshot must never wake the pool.


def _btrfs_mount_device(source_subvolume: str) -> str | None:
    """Device node (e.g. '/dev/dm-3') of the btrfs mount hosting the path.

    The deepest btrfs mount whose mountpoint contains the source wins. The
    mount's device node comes from the mountinfo SOURCE field (field after
    '-'); for btrfs subvolume mounts it carries a '[/@/subvol]' suffix that is
    stripped. mountinfo major:minor is NOT used — btrfs subvolume mounts get a
    private anonymous device (anon_dev) whose major:minor (e.g. '0:63') is the
    mount root inode's st_dev, not the backing block device.
    """
    src = os.path.realpath(source_subvolume)
    best = None  # (mount depth, device node)
    try:
        with open(_MOUNTINFO_PATH) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        try:
            i = parts.index("-")
            fstype = parts[i + 1]
            source = parts[i + 2]
        except (ValueError, IndexError):
            continue
        if fstype != "btrfs":
            continue
        mp = parts[4].rstrip("/") or "/"
        if not (src == mp or (mp != "/" and src.startswith(mp + "/"))):
            continue
        depth = mp.count("/")
        if best is None or depth > best[0]:
            best = (depth, source.split("[", 1)[0])
    return best[1] if best else None


def _pool_device_names(source_subvolume: str) -> list[str]:
    """Block device names of every device in the btrfs pool hosting the path.

    The pool is identified by matching the mounted device node's major:minor
    against /sys/fs/btrfs/<uuid>/devices/*/dev (entries are symlinks to the
    block devices). All pool devices are returned — e.g. a RAID1 across
    dm-3,4,6,9,11 — so a snapshot would wake ANY of them.
    """
    dev_node = _btrfs_mount_device(source_subvolume)
    if not dev_node:
        return []
    try:
        st = os.stat(dev_node)
    except OSError:
        return []
    want = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    try:
        for fsname in os.listdir(_SYSFS_BTRFS):
            devdir = os.path.join(_SYSFS_BTRFS, fsname, "devices")
            if not os.path.isdir(devdir):
                continue
            try:
                names = sorted(os.listdir(devdir))
            except OSError:
                continue
            for name in names:
                try:
                    with open(os.path.join(devdir, name, "dev")) as f:
                        if f.read().strip() == want:
                            # Every device of the pool, not just the ones
                            # listed before the match: a RAID1 snapshot must
                            # wake all of them.
                            return names
                except OSError:
                    continue
    except OSError:
        pass
    return []


def _leaf_disks(cls_path: str) -> set[str]:
    """Whole-disk names under a /sys/class/block path (generic resolution).

    Stacked devices (dm/md) expose a non-empty slaves/ directory and recurse.
    A whole disk is a directory with a 'device' symlink. Partitions lack that
    link, so the name climbs to the largest existing parent that is a whole
    disk (handles both 'sdd1' and 'nvme0n1p1').
    """
    slaves = os.path.join(cls_path, "slaves")
    if os.path.isdir(slaves) and os.listdir(slaves):
        out: set[str] = set()
        for s in os.listdir(slaves):
            out |= _leaf_disks(os.path.join(slaves, s))
        return out
    name = os.path.basename(cls_path)
    while name:
        cand = os.path.join(_CLASS_BLOCK, name)
        if os.path.islink(os.path.join(cand, "device")):
            return {name}
        name = name[:-1]
    return set()


def _disk_info(disk: str) -> dict:
    """Spin state + rotational flag for a physical disk (sysfs only)."""

    def _read(rel: str) -> str | None:
        try:
            with open(os.path.join(_CLASS_BLOCK, disk, rel)) as f:
                return f.read().strip()
        except OSError:
            return None

    return {
        "device": disk,
        "state": _read(os.path.join("device", "state")),
        "rotational": _read(os.path.join("queue", "rotational")),
    }


def _classify_any_spun_down(disks: list[dict]) -> bool | None:
    """any_spun_down verdict from a list of _disk_info dicts.

    True   a rotational disk positively reports a spun-down state
           -> creating a snapshot would wake it, skip.
    False  every rotational disk reports an up state (or none are
           rotational).
    None   a rotational disk's state is unknown -> fail open.
    """
    rotations = [d for d in disks if d.get("rotational") == "1"]
    if not rotations:
        return False  # nothing can spin down
    if any(d["state"] in SPUN_DOWN_STATES for d in rotations):
        return True
    if all(d["state"] in SPUN_UP_STATES for d in rotations):
        return False
    return None


def _pool_status(source_subvolume: str) -> dict:
    """Disk states of the pool hosting the source + the any-spun-down verdict."""
    pool_devices = _pool_device_names(source_subvolume)
    if not pool_devices:
        return {"any_spun_down": None, "disks": []}
    disks: set[str] = set()
    for dev in pool_devices:
        disks |= _leaf_disks(os.path.join(_CLASS_BLOCK, dev))
    info = [_disk_info(d) for d in sorted(disks)]
    return {"any_spun_down": _classify_any_spun_down(info), "disks": info}


def _op_status(job: config.Job) -> None:
    """Emit the status JSON: disk states + generations (no disk I/O if idle).

    Generation reads go through the shared snapshot module: source and latest
    snapshot each hold a memoized generation, so the two `btrfs subvolume show`
    calls run exactly once per object even though has_changed() re-reads them.
    """
    pool = _pool_status(str(job.source_subvolume))
    out = {
        "any_spun_down": pool["any_spun_down"],
        "disks": pool["disks"],
        "source_generation": None,
        "last_snapshot_generation": None,
        "changed": None,
    }
    # A spun-down disk in the pool: never touch the pool for a snapshot.
    # Report idle with no generation data, i.e. no disk I/O at all.
    if pool["any_spun_down"] is True:
        print(json.dumps(out))
        return
    source = snapshot.Snapshot(job.source_subvolume)
    src_gen = source.generation
    if src_gen is None:
        print(json.dumps(out))
        return
    out["source_generation"] = src_gen
    latest = snapshot.Snapshot.latest(job.snapshot_dir)
    if latest is None:
        out["changed"] = True  # no snapshot yet -> creation warranted
        print(json.dumps(out))
        return
    out["last_snapshot_generation"] = latest.generation
    if latest.generation is not None:
        out["changed"] = source.has_changed(latest)
    print(json.dumps(out))


def _check_restic_trusted() -> None:
    """Ensure restic is a root-owned binary that no other user can replace.

    setcap is only meaningful on a binary we trust. The wrapper is the root
    boundary, so before self-updating or setting capabilities we verify:
      - the binary path is a regular file (not a symlink into a user dir)
      - its directory is root-owned and not writable by group/other
      - the binary itself is root-owned and not writable by group/other
    """
    real = os.path.realpath(RESTIC)
    for d in (os.path.dirname(real), real):
        try:
            st = os.stat(d)
        except OSError:
            _exit_err(f"restic not found at {RESTIC}", code=3)
        if st.st_uid != 0:
            _exit_err(f"{d} is not owned by root", code=3)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            _exit_err(
                f"{d} is writable by group/other (mode {oct(st.st_mode & 0o777)})",
                code=3,
            )
    if os.path.islink(RESTIC):
        _exit_err(
            f"{RESTIC} must not be a symlink (setcap needs the real file)", code=3
        )


def _restrict_restic() -> None:
    """Only root and the service group may run a capability-carrying restic.

    cap_dac_read_search reads any file, so execution is the whole boundary:
    left world-executable, every local user could read anything. Restrict
    first, then hand out the capability, so a failure here never leaves a
    capable binary that others can run.
    """
    try:
        gid = grp.getgrnam(SERVICE_GROUP).gr_gid
    except KeyError:
        _exit_err(f"group {SERVICE_GROUP} does not exist", code=4)
    try:
        _run([CHGRP, str(gid), RESTIC])
        _run([CHMOD, "0750", RESTIC])
    except SystemExit:
        _exit_err(f"could not restrict {RESTIC} to root:{SERVICE_GROUP} 0750", code=4)


def _setcap() -> None:
    _restrict_restic()
    _run([SETCAP, "cap_dac_read_search=+ep", RESTIC])


def _update_restic() -> None:
    """Self-update restic (as root) then (re)apply cap_dac_read_search.

    restic self-update replaces the binary and thereby drops any setcap xattrs
    it had; re-applying setcap here (idempotent, even when already newest) is
    what makes every update safe. Exit codes: 0 = ok, 2 = self-update failed
    but existing restic retained and setcap applied, 3 = binary missing/
    unusable/untrusted, 4 = setcap failed (backups would fail to read
    root-owned files).
    """
    _check_restic_trusted()
    try:
        _run([RESTIC, "self-update"])
    except SystemExit as e:
        # self-update failed (e.g. no network). The old binary is retained
        # and remains usable; still (re)apply setcap for the retained binary.
        update_failed = e.code
        _check_restic_trusted()
        try:
            _setcap()
        except SystemExit as se:
            _exit_err(
                f"setcap failed (rc={se.code}): restic retained but cannot read "
                f"root-owned snapshot files",
                code=4,
            )
        _exit_err(
            f"restic self-update failed (rc={update_failed}); "
            f"existing restic retained and setcap applied",
            code=2,
        )
    _check_restic_trusted()
    try:
        _setcap()
    except SystemExit as e:
        _exit_err(
            f"setcap failed (rc={e.code}): restic updated but cannot read "
            f"root-owned snapshot files",
            code=4,
        )


# --- restic ops (mutating only) ---
# The service process may ask for these by job name. Every argument that decides
# what is read, where it goes, and how retention is applied comes from the
# root-owned config; the caller supplies only the job name, and for rewrite the
# snapshot ids, which are validated against the job's tagged snapshots first.


def _service_ids() -> tuple[int, int]:
    """uid/gid to drop to before exec, so restic never runs as root."""
    try:
        pw = pwd.getpwnam(SERVICE_USER)
        gr = grp.getgrnam(SERVICE_GROUP)
    except KeyError as e:
        _exit_err(f"service user or group missing: {e}", code=4)
    return pw.pw_uid, gr.gr_gid


def _restic_env() -> dict[str, str]:
    """Explicit environment for restic after sudo-rs resets the caller's.

    rclone discovers its config through HOME, the shared cache is under the
    install, and rclone lives in /usr/local/bin. A root-run restic would read
    /root instead of the service home without these.
    """
    install = Path(CONFIG_PATH).parent
    return {
        "HOME": str(install),
        "RCLONE_CONFIG": str(install / ".config" / "rclone" / "rclone.conf"),
        "RESTIC_CACHE_DIR": str(install / "cache"),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


def _restic_timeout() -> float | None:
    raw = os.environ.get(RESTIC_TIMEOUT_ENV)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        _exit_err(f"{RESTIC_TIMEOUT_ENV} must be seconds, got {raw!r}", code=2)


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _run_restic(cmd: list[str]) -> None:
    """Run restic as the service user in its own process group.

    The wrapper is root, but restic must not be: the capability, not uid 0,
    reads the snapshot. A new session lets a timeout wind down restic and every
    child it spawned, instead of leaving a root-owned grandchild alive with a
    repository lock or a mount.
    """
    print("sudo-exec CMD: " + " ".join(cmd), file=sys.stderr)
    uid, gid = _service_ids()
    proc = subprocess.Popen(
        cmd,
        env=_restic_env(),
        user=uid,
        group=gid,
        extra_groups=[],
        start_new_session=True,
    )
    try:
        rc = proc.wait(timeout=_restic_timeout())
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            rc = proc.wait(timeout=RESTIC_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            rc = proc.wait()
    if rc != 0:
        sys.exit(rc)


def _restic_build(rj, subcommand: str, **kwargs) -> list[str]:
    """One command line from the same builder the service process uses.

    Importing `restic` keeps the option classification in one place; the
    wrapper's docstring forbids only `job` and `cli`, which reach back into the
    service process.
    """
    cmd = restic.build(
        subcommand,
        repo_path=rj.repo.path,
        password_file=rj.repo.password_file,
        options=list(rj.repo.restic_options) + list(rj.job.restic_options),
        warn=lambda opt: print(
            f"sudo-exec: restic_options contains reserved flag {opt!r}; ignoring it",
            file=sys.stderr,
        ),
        **kwargs,
    )
    cmd[0] = RESTIC
    return cmd


def _tagged_snapshot_ids(rj) -> set[str]:
    """IDs of this job's tagged snapshots, for rewrite validation."""
    cmd = _restic_build(rj, "snapshots") + ["--json", "--tag", rj.tag]
    uid, gid = _service_ids()
    try:
        out = subprocess.run(
            cmd,
            env=_restic_env(),
            user=uid,
            group=gid,
            extra_groups=[],
            start_new_session=True,
            capture_output=True,
            text=True,
            timeout=RESTIC_TIMEOUT_S,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        # Keep restic's code: 10/11/12 are the retryable repo states.
        _exit_err(f"could not read tagged snapshots: {e}", code=e.returncode)
    except subprocess.TimeoutExpired as e:
        _exit_err(f"could not read tagged snapshots: {e}")
    try:
        data = json.loads(out.stdout or "[]")
    except ValueError as e:
        _exit_err(f"unparseable snapshots output: {e}")
    if not isinstance(data, list):
        _exit_err("expected a JSON list from restic snapshots")
    return {str(s.get("id", "")) for s in data if s.get("id")}


def _op_restic_backup(rj, *, dry_run: bool) -> None:
    cmd = _restic_build(
        rj,
        "backup",
        exclude_file=rj.job.ignore_file or None,
        excludes=rj.job.excludes,
    )
    cmd.extend([str(rj.mount_point), "--tag", rj.tag, "--json"])
    if dry_run:
        cmd.append("--dry-run")
    _run_restic(cmd)


def _op_restic_forget(rj) -> None:
    if not rj.job.backup_retention:
        _exit_err("restic-forget requires backup_retention in config")
    cmd = _restic_build(rj, "forget")
    cmd.extend(["--tag", rj.tag])
    for key, val in rj.job.backup_retention.items():
        cmd.extend([f"--keep-{key}", str(val)])
    _run_restic(cmd)


def _op_restic_prune(rj) -> None:
    _run_restic(_restic_build(rj, "prune"))


def _op_restic_rewrite(rj, ids: list[str]) -> None:
    if not ids:
        _exit_err("restic-rewrite requires at least one snapshot id")
    for sid in ids:
        if not re.fullmatch(r"[0-9a-f]{8,64}", sid):
            _exit_err(f"invalid snapshot id: {sid!r}")
    known = _tagged_snapshot_ids(rj)
    unknown = [sid for sid in ids if not any(k.startswith(sid) for k in known)]
    if unknown:
        _exit_err(f"snapshot id not tagged {rj.tag}: {', '.join(unknown)}")
    cmd = _restic_build(
        rj,
        "rewrite",
        exclude_file=rj.job.ignore_file or None,
        excludes=rj.job.excludes,
        forget=True,
        extra_args=ids,
    )
    _run_restic(cmd)


def _op_restic(op: str, rj, args: list[str]) -> None:
    if op == "restic-backup":
        if args:
            _exit_err("restic-backup takes: <job>")
        _op_restic_backup(rj, dry_run=False)
    elif op == "restic-backup-dry-run":
        if args:
            _exit_err("restic-backup-dry-run takes: <job>")
        _op_restic_backup(rj, dry_run=True)
    elif op == "restic-forget":
        if args:
            _exit_err("restic-forget takes: <job>")
        _op_restic_forget(rj)
    elif op == "restic-prune":
        if args:
            _exit_err("restic-prune takes: <job>")
        _op_restic_prune(rj)
    elif op == "restic-rewrite":
        _op_restic_rewrite(rj, args)
    else:
        _exit_err(f"unknown operation: {op}")


def main() -> None:
    if len(sys.argv) < 2:
        _exit_err(
            "usage: sudo-exec.py "
            "<snapshot|delete|mount|umount|status|update-restic|"
            "restic-backup|restic-backup-dry-run|restic-forget|restic-prune|"
            "restic-rewrite> <job> [args]"
        )
    op = sys.argv[1]

    if op == "update-restic":
        _update_restic()
        return

    if len(sys.argv) < 3:
        _exit_err(f"{op} requires: <job> [name]")
    job_name = sys.argv[2]
    cfg = config.Config.load(Path(CONFIG_PATH))
    rj = cfg.get(job_name)
    if rj is None:
        _exit_err(f"unknown job: {job_name}")
    job = rj.job

    if op in RESTIC_OPS:
        _op_restic(op, rj, sys.argv[3:])
        return

    if op == "status":
        if len(sys.argv) != 3:
            _exit_err("status has no extra arguments: <job>")
        _op_status(job)
        return

    if op == "snapshot":
        if len(sys.argv) != 4:
            _exit_err("snapshot requires: <job> <name>")
        name = _validate_name(sys.argv[3])
        target = _job_path(job.snapshot_dir, name)
        snapshot.Snapshot(target, snapshot.parse_name(name)).create(
            job.source_subvolume
        )

    elif op == "delete":
        if len(sys.argv) != 4:
            _exit_err("delete requires: <job> <name>")
        name = _validate_name(sys.argv[3])
        target = _job_path(job.snapshot_dir, name)
        snapshot.Snapshot(target, snapshot.parse_name(name)).delete()

    elif op == "mount":
        if len(sys.argv) != 4:
            _exit_err("mount requires: <job> <name>")
        name = _validate_name(sys.argv[3])
        snap_path = _job_path(job.snapshot_dir, name)
        mount_point = rj.mount_point
        os.makedirs(mount_point, exist_ok=True)
        # btrfs has no uid/gid mount options; snapshots are bound read-only so
        # the mount point is stable across backups (restic change-tracking
        # keys on the source path). restic reads root-owned files via
        # cap_dac_read_search (applied by 'update-restic').
        _run([MOUNT, "-o", "bind,ro", snap_path, str(mount_point)])

    elif op == "umount":
        # The orchestrator also checks, but this is the trust boundary: never
        # pull a mounted snapshot out from under an in-flight restic process.
        if _restic_running():
            _exit_err("restic is still running; refusing to unmount")
        _run([UMOUNT, "-l", str(rj.mount_point)])

    else:
        _exit_err(f"unknown operation: {op}")


if __name__ == "__main__":
    main()
