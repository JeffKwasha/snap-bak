#!/usr/bin/env python3
"""snap-bak-setup.py: install or upgrade snap-bak on this box.

Nothing that can be built is built as root. The venv, the package, the
wrapper, the launcher, the unit files, the sudoers rule and the example config
are staged in a temp directory as the invoking user; one sudo elevation then
writes them into the root-owned paths. Re-running is idempotent: it keeps
config.json, refreshes the program and the units, and re-runs the restic
update.

Nothing is written that did not change. Every managed file is compared with the
installed one by content and mode, and the venv by its contents; unchanged
files are skipped and the run reports what it found. When the commit and the
environment fingerprint match what is installed, the venv is not even built.

The install runs a committed tree only. --ref installs an older commit through
this same script, which is what an undo is: git is the history, and no copy of
the replaced install is kept.
"""

import argparse
import grp
import hashlib
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SERVICE_USER = "snap-bak"
SERVICE_GROUP = "snap-bak"
INSTALL_DIR = Path("/opt/snap-bak")
MOUNT_BASE = Path("/bak")
CONFIG_FILE = INSTALL_DIR / "config.json"
VERSION_FILE = INSTALL_DIR / "VERSION"  # legacy marker; removed by this install
CLI_PATH = Path("/usr/local/bin/snap-bak")
WRAPPER_PATH = INSTALL_DIR / "sudo-exec.py"
PKG_PATH = INSTALL_DIR / "snap_bak"  # legacy copy layout; removed by this install
VENV_DIR = INSTALL_DIR / "venv"
VENV_PY = VENV_DIR / "bin/python"
STATE_DIR = INSTALL_DIR / "state"
CACHE_DIR = INSTALL_DIR / "cache"
PASSWORDS_DIR = INSTALL_DIR / "passwords"
EXCLUDES_DIR = INSTALL_DIR / "excludes"
RCLONE_DIR = INSTALL_DIR / ".config/rclone"
UPDATE_LOG = INSTALL_DIR / "updates.log"
UNIT_DIR = Path("/etc/systemd/system")
SERVICE_NAME = "snap-bak"
SUDOERS_FILE = Path("/etc/sudoers.d/snap-bak")
STALE_MODULES = ("cli.py", "config.py", "job.py", "restic.py", "snapshot.py", "snap-bak-io")
IGNORED_PARTS = ("__pycache__",)

EXAMPLE_CONFIG = """{
  // Top-level settings
  //   mount_base      : where read-only snapshots get mounted before backup (string)
  //   stuck_threshold : seconds after which a 'running' lock is considered stuck
  //                     (int seconds or e.g. '1h')
  //   restic_update   : when to update restic via the root wrapper. 'after' =
  //                     update restic only after a run in which a backup actually
  //                     completed (never on skipped/dry-run runs — updating a
  //                     setcap-carrying binary must not happen for no reason);
  //                     'never' = only when you run the wrapper by hand. 'before'
  //                     is not allowed: it would update before knowing whether
  //                     any backup will happen.
  "mount_base": "/bak",
  "stuck_threshold": 3600,
  "restic_update": "never",

  "repos": [
    {
      // Repos are restic repositories where backups are stored.
      //   name           : unique repo id referenced by a job's 'repo'  (string)
      //   path           : restic location; local path or 'rclone:REMOTE:DIR'  (string)
      //   password_file  : file containing the restic repo password  (string)
      //   size_limit     : stop when repo hits this many bytes; accepts int bytes
      //                     or e.g. '200GB'; 0 = unlimited  (int|string)
      //   latency_ms     : expected repo latency, scheduling hint  (int ms)
      //   throughput_mbps: expected repo throughput, scheduling hint  (int)
      //   restic_options : extra restic CLI flags for every command against this repo.
      //                    Snap-bak's own flags (--json, --tag, --keep-*, --prune,
      //                    --exclude*, -r, --password-file) are ignored if set here.
      "name": "primary-cloud",
      "path": "rclone:PROVIDER:DIR",
      "password_file": "/opt/snap-bak/passwords/provider",
      "size_limit": 0,
      "latency_ms": 100,
      "throughput_mbps": 10,
      "restic_options": ["--compression=better"]
    }
  ],

  "jobs": [
    {
      // name             : unique job id  (string). Must stay filesystem-safe
      //                     ([A-Za-z0-9_-], no '/' or '..') because it is used
      //                     for <state>/<name>_status.json and the restic tag.
      //                     It does NOT control the mount point: that is always
      //                     <mount_base>/<last component of source_subvolume>.
      // snapshot_dir     : existing dir where btrfs snapshots go; must exist  (string)
      // source_subvolume : btrfs subvolume to snapshot  (string)
      // repo             : repo id from the repos list  (string)
      // priority         : scheduling order, higher = earlier  (int >= 1)
      // snapshot_interval: interval between snapshots, min 900s
      //                     (int seconds or e.g. '2h')
      // skip_snapshot_if_idle: power saving (default true). When true, snap-bak
      //                     does NOT create a snapshot if any disk in the btrfs
      //                     pool is physically spun down (won't wake the drive),
      //                     and never snapshots a subvolume unchanged since the
      //                     last snapshot. Backups are unaffected. Set false to
      //                     always snapshot on schedule.
      // snapshot_retention: how long to keep LOCAL snapshots;
      //                     fn = exponential|datetimes; see SPEC.md for config
      // backup_retention : restic forget/prune keep-policy;
      //                     keys: last, hourly, daily, weekly, monthly, yearly
      // backup_interval  : interval between restic uploads  (int seconds or e.g. '1w')
      // backup_window_start/end: 'HH:MM' LOCAL time window backups run in; omit any
      // ignore_file      : restic --exclude-file (gitignore-style, one pattern/line)
      // excludes         : list of restic --exclude patterns (gitignore-style)
      // tag              : optional restic tag override (default: job name)
      // restic_options   : extra restic CLI flags for this job (see repo comment)
      "name": "Home-Protection",
      "snapshot_dir": "/home/.snapshots",
      "source_subvolume": "home",
      "repo": "primary-cloud",
      "priority": 150,
      "snapshot_interval": 3600,
      "skip_snapshot_if_idle": true,
      "snapshot_retention": {"fn": "exponential", "config": {"n": 5}},
      "backup_retention": {"daily": 7, "weekly": 4},
      "backup_interval": 604800,
      "backup_window_start": "02:00",
      "backup_window_end": "06:00",
      "ignore_file": "/opt/snap-bak/excludes/home.ignore",
      "excludes": ["*.tmp", "*.cache"]
    }
  ]
}"""

DEFAULT_EXCLUDES = """# gitignore-style patterns, one per line (restic --exclude-file)
*.tmp
*.cache"""

NEXT_STEPS = """NEXT STEPS:
 1. Edit {config} to match your actual environment.
 2. Create your restic password file in {install}/passwords/, owned by you and
    readable by the service group:
       sudo chown <you>:snap-bak <file>.pw && sudo chmod 640 <file>.pw
    (or owner-only chmod 600). The gate rejects other groups/other access.
 3. Copy your rclone config to {rclone}/rclone.conf (chmod 600).
 4. Ensure snapshot_dir paths exist (btrfs snapshots are created as root via the wrapper).
 5. Ensure the restic repo path is accessible by {user} (read/write).
 6. If an older setup enabled the oneshot service itself, disable it now:
       sudo systemctl disable --now {service}.service
    Then enable the timer (do NOT enable the bare service name; systemd treats
    an unsuffixed name as .service, not .timer):
       sudo systemctl enable --now {service}.timer
 7. Run the tool as the service user (it owns state, cache and the wrapper):
       sudo -u {user} snap-bak --list-jobs"""


class SetupError(RuntimeError):
    pass


def note(msg):
    print(f"[*] {msg}", flush=True)


def warn(msg):
    print(f"[!] {msg}", file=sys.stderr, flush=True)


def run(cmd, *, check=True, capture=False, input_text=None):
    result = subprocess.run(
        [str(c) for c in cmd],
        text=True,
        check=False,
        capture_output=capture,
        input=input_text,
    )
    if check and result.returncode != 0:
        detail = f": {result.stderr.strip()}" if capture and result.stderr else ""
        raise SetupError(f"command failed (rc={result.returncode}): {' '.join(map(str, cmd))}{detail}")
    return result


def git(repo, *args, capture=True):
    return run(["git", "-C", repo, *args], capture=capture)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="snap-bak-setup.py",
        description="Install or upgrade snap-bak. Elevates once, when writing to root-owned paths.",
    )
    parser.add_argument(
        "--ref",
        metavar="SHA|TAG",
        help="install that commit instead of HEAD",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list every file and say whether it changed",
    )
    return parser.parse_args(argv)


def find_python():
    candidate = os.environ.get("PYTHON_BIN") or shutil.which("python3")
    if not candidate:
        raise SetupError(
            "No python3 on PATH and PYTHON_BIN is unset. "
            "Install Python 3.14 or newer, or set PYTHON_BIN=/path/to/python3."
        )
    proc = run(
        [candidate, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 14) else 1)"],
        check=False,
    )
    if proc.returncode != 0:
        raise SetupError(f"snap-bak needs Python 3.14 or newer; {candidate} is older.")
    return Path(candidate)


def choose_tool(uv, pip_present):
    """What populates the venv: uv when present, else the interpreter's pip."""
    if uv:
        return "uv"
    if pip_present:
        return "pip"
    raise SetupError(
        "Neither uv nor pip is available. Install uv (https://docs.astral.sh/uv/) "
        "or the interpreter's pip (Debian/Ubuntu: apt install python3-pip)."
    )


def find_tool(python):
    uv = shutil.which("uv")
    pip_present = run([python, "-m", "pip", "--version"], check=False, capture=True).returncode == 0
    return choose_tool(uv, pip_present), uv


def check_clean(repo):
    """Refuse to install anything git cannot name."""
    dirty = git(repo, "status", "--porcelain").stdout.strip()
    if dirty:
        listed = "\n".join(f"    {line}" for line in dirty.splitlines())
        raise SetupError(f"Uncommitted changes in {repo}. Commit them before installing:\n{listed}")


def resolve_source(repo, ref, stage):
    """Return the tree to install and the commit it is."""
    if not (repo / "src/snap_bak").is_dir() or not (repo / "src/sudo-exec.py").is_file():
        raise SetupError(f"{repo} has no src/snap_bak and src/sudo-exec.py. Run this from the repo root.")
    if run(["git", "-C", repo, "rev-parse", "--git-dir"], check=False, capture=True).returncode != 0:
        raise SetupError(f"{repo} is not a git checkout; the install needs one.")
    check_clean(repo)
    if ref is None:
        return repo, git(repo, "rev-parse", "HEAD").stdout.strip()
    resolved = run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        check=False,
        capture=True,
    )
    if resolved.returncode != 0:
        raise SetupError(f"Unknown ref: {ref}")
    commit = resolved.stdout.strip()
    export = stage / "source"
    export.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        stdout=subprocess.PIPE,
    )
    stdout = archive.stdout
    if stdout is None:
        raise SetupError("Could not read the git archive stream.")
    try:
        extract = subprocess.run(["tar", "-x", "-C", str(export)], stdin=stdout, check=False)
    finally:
        stdout.close()
        archive.wait()
    if archive.returncode != 0 or extract.returncode != 0:
        raise SetupError(f"Could not export {ref} ({commit}).")
    note(f"Installing {ref} ({commit}) instead of HEAD.")
    return export, commit


def site_packages_of(python):
    return Path(
        run([python, "-c", "import site; print(site.getsitepackages()[0])"], capture=True).stdout.strip()
    )


def now_utc():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def env_fingerprint(source, python):
    """What decides the venv's contents: the dependency inputs and the interpreter."""
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        path = source / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b"<absent>")
    version = run([python, "-c", "import sys; print(sys.version)"], capture=True).stdout
    digest.update(version.encode())
    return digest.hexdigest()


def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def render_launcher():
    return f"""#!/bin/sh
# Installed by snap-bak-setup.py. Run it as the service user.
SNAPBAK_CONFIG_FILE="{CONFIG_FILE}"
export SNAPBAK_CONFIG_FILE
exec "{VENV_PY}" -P -m snap_bak.cli "$@"
"""


def render_service():
    return f"""[Unit]
Description=Snap-Bak Backup Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={SERVICE_USER}
Group={SERVICE_GROUP}
WorkingDirectory={INSTALL_DIR}
Environment=SNAPBAK_CONFIG_FILE={CONFIG_FILE}
ExecStart={VENV_PY} -P -m snap_bak.cli

[Install]
WantedBy=multi-user.target
"""


def render_timer():
    return """[Unit]
Description=Run Snap-Bak Orchestrator periodically

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
"""


def render_sudoers():
    return (
        "# snap-bak permissions: allow the service user to run only the root-owned\n"
        "# wrapper. The wrapper validates job/snapshot names against /opt/snap-bak/\n"
        "# config.json (root-owned) and executes btrfs/mount/umount with config-derived\n"
        "# paths. A bare command name (no args listed) matches any arguments.\n"
        f"{SERVICE_USER} ALL=(root) NOPASSWD:{WRAPPER_PATH}\n"
    )


def render_wrapper(source_root):
    first, _, rest = (source_root / "src/sudo-exec.py").read_text().partition("\n")
    if not first.startswith("#!"):
        raise SetupError("src/sudo-exec.py has no shebang to rewrite.")
    return f"#!{VENV_PY}\n{rest}"


def update_line(commit, version, when):
    return f"{when} {version} {commit}"


@dataclass(frozen=True)
class Managed:
    name: str
    dest: Path
    mode: str
    owner: str = "root:root"


def targets(paths):
    managed = {
        "wrapper": Managed("wrapper", WRAPPER_PATH, "0750", f"root:{SERVICE_GROUP}"),
        "launcher": Managed("launcher", CLI_PATH, "0755"),
        "service": Managed("service", UNIT_DIR / f"{SERVICE_NAME}.service", "0644"),
        "timer": Managed("timer", UNIT_DIR / f"{SERVICE_NAME}.timer", "0644"),
        "sudoers": Managed("sudoers", SUDOERS_FILE, "0440"),
        "REF": Managed("REF", VENV_DIR / "REF", "0644"),
        "ENV": Managed("ENV", VENV_DIR / "ENV", "0644"),
        "BUILD": Managed("BUILD", VENV_DIR / "BUILD", "0644"),
    }
    if "config" in paths:
        managed["config"] = Managed("config", CONFIG_FILE, "0644")
    if "home.ignore" in paths:
        managed["home.ignore"] = Managed(
            "home.ignore", EXCLUDES_DIR / "home.ignore", "0640", f"root:{SERVICE_GROUP}"
        )
    return managed


def render(source_root, out, when, commit, fingerprint):
    """Write every managed file, without building anything."""
    out.mkdir(parents=True, exist_ok=True)
    texts = {
        "wrapper": render_wrapper(source_root),
        "launcher": render_launcher(),
        "service": render_service(),
        "timer": render_timer(),
        "sudoers": render_sudoers(),
        "REF": commit + "\n",
        "ENV": fingerprint + "\n",
        "BUILD": when + "\n",
    }
    if not CONFIG_FILE.exists():
        texts["config"] = EXAMPLE_CONFIG
    if not (EXCLUDES_DIR / "home.ignore").exists():
        texts["home.ignore"] = DEFAULT_EXCLUDES
    paths = {}
    for name, text in texts.items():
        path = out / name
        path.write_text(text)
        paths[name] = path
    return paths


def probe(path):
    """stat, or None when absent. False means the caller may not look."""
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None
    except PermissionError:
        return False


def matches(dest, staged, mode):
    stat = probe(dest)
    if not stat:
        return False
    if f"{stat.st_mode & 0o777:04o}" != mode:
        return False
    if stat.st_size != staged.stat().st_size:
        return False
    return digest(dest) == digest(staged)


@dataclass
class Plan:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    venv_changed: bool = False
    venv_detail: list[str] = field(default_factory=list)

    @property
    def changes(self):
        return bool(self.added or self.updated or self.venv_changed)


def plan_files(paths, managed):
    plan = Plan()
    for name, item in managed.items():
        # BUILD records the last install that changed something; it is written
        # with the rest, never compared on its own.
        if name == "BUILD":
            continue
        dest = item.dest
        state = probe(dest)
        if state is None:
            plan.added.append(name)
        elif state is False or not os.access(dest, os.R_OK):
            # Root-only content (the sudoers rule, or a file inside a service
            # directory). The commit is unchanged here, so what this install
            # would write is what is already there — and it must not be
            # overwritten on a guess.
            plan.unchanged.append(name)
        elif matches(dest, paths[name], item.mode):
            plan.unchanged.append(name)
        else:
            plan.updated.append(name)
    return plan


def tree_diff(staged, installed):
    """Relative paths whose content or link target differs, ignoring pycache."""

    def files(root):
        found = {}
        if not root.exists():
            return found
        for path in root.rglob("*"):
            if path.is_dir() or any(part in IGNORED_PARTS for part in path.parts):
                continue
            if path.suffix == ".pyc":
                continue
            found[str(path.relative_to(root))] = path
        return found

    left, right = files(staged), files(installed)
    differing = set()
    for rel, path in left.items():
        other = right.get(rel)
        if other is None:
            differing.add(rel)
        elif path.is_symlink() or other.is_symlink():
            if not (path.is_symlink() and other.is_symlink() and os.readlink(path) == os.readlink(other)):
                differing.add(rel)
        elif path.stat().st_size != other.stat().st_size or digest(path) != digest(other):
            differing.add(rel)
    differing.update(right.keys() - left.keys())
    return sorted(differing)


def venv_diff(staged_venv, installed_venv, deps_changed):
    """Whether the installed venv already holds what the staged one would.

    Dependencies are decided by the fingerprint, not by walking them: dist-info
    records a hash of the console script, whose shebang names the staging path,
    so a byte comparison would report a change on every run.
    """
    if deps_changed:
        return True, ["dependency inputs changed"]
    if not (installed_venv / "bin/python").exists():
        return True, ["no interpreter"]
    if run([installed_venv / "bin/python", "-c", "import snap_bak"], check=False, capture=True).returncode != 0:
        return True, ["the installed package cannot be imported"]
    staged = site_packages_of(staged_venv / "bin/python") / "snap_bak"
    installed = site_packages_of(installed_venv / "bin/python") / "snap_bak"
    differing = tree_diff(staged, installed)
    return bool(differing), differing


def summary_line(plan):
    parts = []
    if plan.added:
        parts.append(f"{len(plan.added)} added")
    if plan.updated:
        parts.append(f"{len(plan.updated)} updated")
    parts.append(f"{len(plan.unchanged)} unchanged")
    parts.append("venv replaced" if plan.venv_changed else "venv unchanged")
    return "Install report: " + ", ".join(parts)


def report(plan, verbose=False):
    note(summary_line(plan))
    if not verbose:
        return
    for name in plan.added:
        print(f"    added      {name}")
    for name in plan.updated:
        print(f"    updated    {name}")
    for name in plan.unchanged:
        print(f"    unchanged  {name}")
    for rel in plan.venv_detail:
        print(f"    venv       {rel}")


@dataclass
class Stage:
    root: Path
    source: Path
    commit: str
    version: str
    fingerprint: str
    when: str
    paths: dict
    managed: dict


def build_stage(stage_root, source, commit, python, tool, uv, fingerprint, when):
    venv = stage_root / "venv"
    note(f"Creating a virtualenv in {venv}...")
    if tool == "uv":
        run([uv, "venv", "--python", python, venv])
    else:
        run([python, "-m", "venv", "--without-pip", venv])
    site = site_packages_of(venv / "bin/python")
    site.mkdir(parents=True, exist_ok=True)
    note(f"Installing snap-bak and its dependencies with {tool}...")
    if tool == "uv":
        run([uv, "pip", "install", "--quiet", "--python", venv / "bin/python", source])
        (venv / "bin" / "snap-bak").unlink(missing_ok=True)
    else:
        run(
            [
                python, "-m", "pip", "install", "--quiet", "--upgrade",
                "--root-user-action=ignore", "--target", site, source,
            ]
        )
        shutil.rmtree(site / "bin", ignore_errors=True)
    version = run(
        [venv / "bin/python", "-c", "import snap_bak; print(snap_bak.__version__)"],
        capture=True,
    ).stdout.strip()
    paths = render(source, stage_root / "files", when, commit, fingerprint)
    return Stage(stage_root, source, commit, version, fingerprint, when, paths, targets(paths))


def installed_commit():
    try:
        return (VENV_DIR / "REF").read_text().strip()
    except OSError:
        return ""


def installed_fingerprint():
    try:
        return (VENV_DIR / "ENV").read_text().strip()
    except OSError:
        return ""


def privileged_prefix():
    if os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    if not sudo:
        raise SetupError("This script is not root and sudo is missing; it cannot install into /opt and /etc.")
    return [sudo]


class Privileged:
    def __init__(self):
        self.prefix = privileged_prefix()

    def prepare(self):
        if not self.prefix:
            return
        note("Root is needed for /opt/snap-bak, /etc/systemd/system and /usr/local/bin.")
        run([*self.prefix, "-v"])

    def run(self, *cmd, **kwargs):
        return run([*self.prefix, *cmd], **kwargs)

    def install(self, src, dest, mode, owner="root:root"):
        user, _, group = owner.partition(":")
        self.run("install", "-o", user, "-g", group or user, "-m", mode, src, dest)

    def install_dir(self, path, mode, owner):
        user, _, group = owner.partition(":")
        self.run("install", "-d", "-o", user, "-g", group or user, "-m", mode, path)


def validate_sudoers(path):
    visudo = shutil.which("visudo") or ("/usr/sbin/visudo" if Path("/usr/sbin/visudo").exists() else None)
    if not visudo:
        warn("visudo not found; installing the sudoers rule without validating it.")
        return
    result = run([visudo, "-c", "-f", path], check=False, capture=True)
    if result.returncode != 0:
        raise SetupError(f"sudoers validation failed: {result.stderr.strip() or result.stdout.strip()}")


def ensure_identity(root):
    try:
        grp.getgrnam(SERVICE_GROUP)
    except KeyError:
        note(f"Creating system group: {SERVICE_GROUP}")
        root.run("groupadd", "--system", SERVICE_GROUP)
    try:
        pwd.getpwnam(SERVICE_USER)
    except KeyError:
        note(f"Creating system user: {SERVICE_USER}")
        root.run(
            "useradd", "--system", "--home-dir", INSTALL_DIR,
            "--shell", "/usr/sbin/nologin", "-g", SERVICE_GROUP, SERVICE_USER,
        )


def ensure_dirs(root):
    root.install_dir(INSTALL_DIR, "0755", "root:root")
    root.install_dir(STATE_DIR, "0750", f"{SERVICE_USER}:{SERVICE_GROUP}")
    root.install_dir(CACHE_DIR, "0750", f"{SERVICE_USER}:{SERVICE_GROUP}")
    root.install_dir(PASSWORDS_DIR, "0750", f"{SERVICE_USER}:{SERVICE_GROUP}")
    root.install_dir(EXCLUDES_DIR, "0750", f"root:{SERVICE_GROUP}")
    root.install_dir(INSTALL_DIR / ".config", "0750", f"{SERVICE_USER}:{SERVICE_GROUP}")
    root.install_dir(RCLONE_DIR, "0750", f"{SERVICE_USER}:{SERVICE_GROUP}")
    root.install_dir(MOUNT_BASE, "0755", f"{SERVICE_USER}:{SERVICE_GROUP}")


def link_cache(root):
    cache_link = INSTALL_DIR / ".cache"
    if cache_link.is_symlink():
        if os.readlink(cache_link) != "cache":
            root.run("ln", "-sfn", "cache", cache_link)
    elif not cache_link.exists():
        root.run("ln", "-s", "cache", cache_link)
    else:
        warn(f"{cache_link} exists and is not a symlink; leaving it untouched.")


def swap_in_venv(root, stage):
    if VENV_DIR.exists():
        root.run("rm", "-rf", VENV_DIR)
    root.run("cp", "-a", stage.root / "venv", VENV_DIR)
    root.run("chown", "-R", "root:root", VENV_DIR)
    # uv creates the venv with the caller's umask; the installed one is root's.
    root.run("chmod", "-R", "go-w", VENV_DIR)


def update_restic(root):
    if not Path("/usr/local/bin/restic").is_file():
        raise SetupError(
            "/usr/local/bin/restic not found. Install restic, then re-run "
            "(or: sudo /opt/snap-bak/sudo-exec.py update-restic)."
        )
    note("Validating restic, self-updating and applying cap_dac_read_search...")
    result = root.run(WRAPPER_PATH, "update-restic", check=False)
    if result.returncode not in (0, 2):
        raise SetupError(
            f"restic validation/update failed (rc={result.returncode}). Fix restic, "
            "then re-run (or: sudo /opt/snap-bak/sudo-exec.py update-restic)."
        )
    if result.returncode == 2:
        warn("restic self-update failed (network?), but the existing restic is retained and setcap applied.")


def install_changed(paths, managed, plan, root, *, touch_build, force=()):
    names = [*plan.added, *plan.updated, *force]
    if touch_build and plan.changes and "BUILD" in managed:
        names.append("BUILD")
    names = list(dict.fromkeys(names))
    if "sudoers" in names:
        validate_sudoers(paths["sudoers"])
    for name in names:
        item = managed[name]
        root.install(paths[name], item.dest, item.mode, item.owner)


def apply(stage, plan, root, log_line):
    ensure_identity(root)
    note("Creating install directories...")
    ensure_dirs(root)
    swapped = plan.venv_changed
    if swapped:
        note("Replacing the virtualenv...")
        swap_in_venv(root, stage)
    # The venv holds REF, ENV and BUILD; replacing it removes them, so they are
    # installed again even when their content did not change.
    install_changed(
        stage.paths,
        stage.managed,
        plan,
        root,
        touch_build=True,
        force=("REF", "ENV", "BUILD") if swapped else (),
    )
    link_cache(root)
    if plan.changes:
        update_restic(root)
        # The unit, the wrapper and the launcher all run the venv now. Remove the
        # copy layout and its marker last, so a failure above left it working.
        root.run("rm", "-rf", PKG_PATH)
        root.run("rm", "-f", VERSION_FILE, *[INSTALL_DIR / name for name in STALE_MODULES])
        note("Reloading systemd daemon...")
        root.run("systemctl", "daemon-reload")
        check = root.run("systemctl", "is-enabled", f"{SERVICE_NAME}.service", check=False)
        if check.returncode == 0:
            warn(f"{SERVICE_NAME}.service is currently enabled. Migrate to the timer:")
            warn(f"    sudo systemctl disable --now {SERVICE_NAME}.service")
            warn(f"    sudo systemctl enable --now {SERVICE_NAME}.timer")
    if log_line:
        root.run("tee", "-a", UPDATE_LOG, input_text=log_line + "\n", capture=True)
        root.run("chown", "root:root", UPDATE_LOG)
        root.run("chmod", "0644", UPDATE_LOG)


def summary(stage):
    print()
    print("=" * 52)
    print(" SETUP COMPLETE")
    print("=" * 52)
    print(f" User:        {SERVICE_USER}")
    print(f" Config:      {CONFIG_FILE}")
    print(f" Venv:        {VENV_DIR}")
    print(f" State Dir:   {STATE_DIR}")
    print(f" Cache Dir:   {CACHE_DIR}")
    print(f" Mount Base:  {MOUNT_BASE}")
    print(f" Service:     {SERVICE_NAME}")
    print(f" Commit:      {stage.commit}")
    print(f" Version:     {stage.version}")
    print("=" * 52)
    print(NEXT_STEPS.format(
        config=CONFIG_FILE,
        install=INSTALL_DIR,
        rclone=RCLONE_DIR,
        user=SERVICE_USER,
        service=SERVICE_NAME,
    ))


def main(argv=None):
    args = parse_args(argv)
    repo = Path(__file__).resolve().parent
    python = find_python()
    tool, uv = find_tool(python)
    work = Path(tempfile.mkdtemp(prefix="snap-bak-setup-"))
    try:
        source, commit = resolve_source(repo, args.ref, work)
        when = now_utc()
        fingerprint = env_fingerprint(source, python)
        previous = installed_commit()
        venv_usable = VENV_PY.exists() and run(
            [VENV_PY, "-c", "import snap_bak"], check=False, capture=True
        ).returncode == 0

        if previous == commit and installed_fingerprint() == fingerprint and venv_usable:
            # Nothing to build: the installed commit and its dependency inputs
            # are the ones this run would install, so the files can be compared
            # as they are.
            paths = render(source, work / "files", when, commit, fingerprint)
            managed = targets(paths)
            plan = plan_files(paths, managed)
            if not plan.changes:
                report(plan, args.verbose)
                note(f"Nothing to do: the box is at {commit[:12]}.")
                return 0
            root = Privileged()
            root.prepare()
            install_changed(paths, managed, plan, root, touch_build=True)
            link_cache(root)
            if {"service", "timer"} & set(plan.added + plan.updated):
                root.run("systemctl", "daemon-reload")
            report(plan, args.verbose)
            note(f"Nothing to rebuild: the box stays at {commit[:12]}.")
            return 0

        stage = build_stage(work, source, commit, python, tool, uv, fingerprint, when)
        plan = plan_files(stage.paths, stage.managed)
        plan.venv_changed, plan.venv_detail = venv_diff(
            stage.root / "venv", VENV_DIR, deps_changed=installed_fingerprint() != fingerprint
        )
        log_line = update_line(commit, stage.version, when) if commit != previous else None
        root = Privileged()
        root.prepare()
        apply(stage, plan, root, log_line)
        report(plan, args.verbose)
        if plan.changes:
            summary(stage)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SetupError as exc:
        warn(str(exc))
        sys.exit(1)
