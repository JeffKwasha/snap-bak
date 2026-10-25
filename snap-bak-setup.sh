#!/bin/bash

# snap-bak-setup.sh
# Automates the creation of the snap-bak user, sudoers permissions,
# and systemd timer/service for the snap-bak manager.
#
# USAGE INSTRUCTIONS:
# 1. Make the script executable: chmod +x snap-bak-setup.sh
# 2. Run with root privileges: sudo ./snap-bak-setup.sh
# 3. After completion, perform the 'NEXT STEPS' listed in the script output.
#
# Idempotent: re-running "fixes" the box to match the current repo state while
# preserving your config.json edits.

set -e

# --- ROOT GUARD ---
if [ "$EUID" -ne 0 ]; then
    echo "[!] This script must be run as root (it writes to /etc, /opt, creates users)." >&2
    echo "    Re-run with: sudo $0" >&2
    exit 1
fi

# The interpreter the service and the wrapper will share. python3 by default;
# PYTHON_BIN overrides it for a virtualenv or a non-system install.
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 2>/dev/null)}"
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    echo "[!] No python3 on PATH and PYTHON_BIN is unset." >&2
    echo "    Install Python 3.14 or newer, or point PYTHON_BIN at one:" >&2
    echo "      sudo PYTHON_BIN=/path/to/python3 ./snap-bak-setup.sh" >&2
    exit 1
fi

# --- CONFIGURATION ---
SERVICE_USER="snap-bak"
SERVICE_GROUP="snap-bak"
SNAPBAK_DIR="/opt/snap-bak"
MOUNT_BASE="/bak"
CONFIG_FILE="$SNAPBAK_DIR/config.json"
WRAPPER_PATH="$SNAPBAK_DIR/sudo-exec.py"
PKG_PATH="$SNAPBAK_DIR/snap_bak"
SERVICE_NAME="snap-bak"
STATE_DIR="$SNAPBAK_DIR/state"
CACHE_DIR="$SNAPBAK_DIR/cache"

# Source files co-located with this setup script (repo layout: src/snap_bak/).
PKG_SRC="$(dirname "$(readlink -f "$0")")/src/snap_bak"
WRAPPER_SRC="$(dirname "$(readlink -f "$0")")/src/sudo-exec.py"

# --- 0. PYTHON DEPENDENCIES (for configured Python) ---
# The service and wrapper share one configured interpreter (PYTHON_BIN), and
# both need pydantic, json5, rich etc. importable. json5 parses the JSONC
# config via config.py, shared by the root wrapper; rich backs the reports.
echo "[*] Checking Python dependencies (pydantic, python-dateutil, json5, rich)..."
if ! "$PYTHON_BIN" -c "import pydantic, dateutil, json5, rich" &>/dev/null; then
    echo "[*] Installing Python dependencies with pip..."
    "$PYTHON_BIN" -m pip install --quiet pydantic python-dateutil json5 rich
fi
if ! "$PYTHON_BIN" -c "import pydantic, dateutil, json5, rich" &>/dev/null; then
    echo "[!] Python dependencies are not importable by $PYTHON_BIN." >&2
    echo "    If pip is externally managed (PEP 668, stock Debian/Ubuntu), do" >&2
    echo "    not fight it: create a virtualenv and re-run with PYTHON_BIN." >&2
    echo "      sudo python3 -m venv /opt/snap-bak/venv" >&2
    echo "      sudo /opt/snap-bak/venv/bin/pip install pydantic python-dateutil json5 rich" >&2
    echo "      sudo PYTHON_BIN=/opt/snap-bak/venv/bin/python ./snap-bak-setup.sh" >&2
    echo "    Otherwise install them manually:" >&2
    echo "      $PYTHON_BIN -m pip install pydantic python-dateutil json5 rich" >&2
    exit 1
fi

# --- 1. CREATE USER, GROUP & DIRECTORIES ---
if ! getent group "$SERVICE_GROUP" >/dev/null; then
    echo "[*] Creating system group: $SERVICE_GROUP"
    groupadd --system "$SERVICE_GROUP"
else
    echo "[!] Group $SERVICE_GROUP already exists. Skipping creation."
fi
echo "[*] Creating system user: $SERVICE_USER"
if ! id "$SERVICE_USER" &>/dev/null; then
    # The account exists purely for permissions/separation, so no home
    # directory is created (no /etc/skel copy: no .face, dotfiles, .local).
    # The dir is created root-owned below. Use -g so the pre-created group is
    # assigned as the primary group; without it useradd tries to create a
    # user-private group of the same name and fails (and is not idempotent).
    useradd --system --home-dir "$SNAPBAK_DIR" \
        --shell /usr/sbin/nologin -g "$SERVICE_GROUP" "$SERVICE_USER"
else
    echo "[!] User $SERVICE_USER already exists. Skipping creation."
fi

# /opt/snap-bak is ROOT-owned (0755). The service user must not be able to
# replace the snap_bak/ package or sudo-exec.py: the wrapper is the sudo
# security boundary, so a writable install dir would mean trivial root
# escalation.
mkdir -p "$SNAPBAK_DIR"
chown root:root "$SNAPBAK_DIR"
chmod 0755 "$SNAPBAK_DIR"

# --- 1b. INSTALL MANAGED PROGRAM & WRAPPER ---
if [ ! -d "$PKG_SRC" ]; then
    echo "[!] Cannot find $PKG_SRC. Run this script from the repo root." >&2
    exit 1
fi
if [ ! -f "$WRAPPER_SRC" ]; then
    echo "[!] Cannot find $WRAPPER_SRC. Run this script from the repo root." >&2
    exit 1
fi
# The package is installed as a directory (imported as `snap_bak`); systemd
# runs `python3 -m snap_bak.cli`. Remove any stale flat-installed files from the
# pre-package layout, then install the package and the wrapper.
rm -rf "$PKG_PATH"
for stale in cli.py config.py job.py restic.py snapshot.py; do
    rm -f "$SNAPBAK_DIR/$stale"
done
cp -r "$PKG_SRC" "$PKG_PATH"
# Drop dev bytecode so we never install .pyc built for the wrong interpreter.
find "$PKG_PATH" -type d -name __pycache__ -exec rm -rf {} +
chown -R root:root "$PKG_PATH"
chmod 0755 "$PKG_PATH"
find "$PKG_PATH" -type f -exec chmod 0644 {} \;
install -o root -g root -m 0755 "$WRAPPER_SRC" "$WRAPPER_PATH"
# Deploy the wrapper on the same interpreter that owns the dependencies.
sed -i "1s|^#!.*|#!$PYTHON_BIN|" "$WRAPPER_PATH"
echo "[*] Installed $PKG_PATH (root:root 0644) and $WRAPPER_PATH (root:root 0755)."

# --- 1d. VERIFY/UPDATE RESTIC (setcap + self-update via the wrapper) ---
# The wrapper is the single code path for restic trust + setcap (see its
# 'update-restic' op). Running it here (a) ensures the setcap we rely on is in
# place and (b) never duplicates that logic. Fails hard if restic is missing,
# since the whole point of the wrapper is to make a trusted binary capable of
# reading root-owned files.
if [ ! -f /usr/local/bin/restic ]; then
    echo "[!] /usr/local/bin/restic not found. Install restic, then re-run this script (or: $WRAPPER_PATH update-restic)." >&2
    exit 1
fi
echo "[*] Running $WRAPPER_PATH update-restic (validates restic, self-update, setcap)..."
"$WRAPPER_PATH" update-restic
rc=$?
# rc 0 = updated/current + setcap applied; rc 2 = self-update failed but the
# existing binary is retained, trusted and setcap applied. Both are acceptable.
# Any other rc means the binary is missing/untrusted -> fail the install.
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
    echo "[!] restic validation/update failed (rc=$rc). Fix restic, then re-run this script (or: $WRAPPER_PATH update-restic)." >&2
    exit 1
fi
if [ "$rc" -eq 2 ]; then
    echo "[!] restic self-update failed (network?), but existing restic is retained and setcap applied."
fi

# --- 1c. SERVICE-WRITABLE SUBDIRS ---
# The service user writes lock/status/throughput under state/ and restic's
# cache under cache/. Root owns everything else in /opt/snap-bak.
for d in "$STATE_DIR" "$CACHE_DIR"; do
    mkdir -p "$d"
    chown "$SERVICE_USER":"$SERVICE_GROUP" "$d"
    chmod 0750 "$d"
done

# Expose the cache at restic's implicit default $HOME/.cache (HOME is
# /opt/snap-bak for the service user). We do NOT set RESTIC_CACHE_DIR; restic
# resolves $HOME/.cache itself, which lands on this symlink -> the real
# snap-bak-owned cache dir. Root-owned symlink; the service user writes through
# it. Idempotent: only (re)create if missing or pointing elsewhere.
CACHE_LINK="$SNAPBAK_DIR/.cache"
if [ -L "$CACHE_LINK" ]; then
    if [ "$(readlink "$CACHE_LINK")" != "cache" ]; then
        ln -sfn cache "$CACHE_LINK"
    fi
elif [ ! -e "$CACHE_LINK" ]; then
    ln -s cache "$CACHE_LINK"
else
    echo "[!] $CACHE_LINK exists and is not a symlink; leaving it untouched." >&2
fi

# --- 2. GENERATE EXAMPLE CONFIG ---
# This is JSONC: // and /* */ comments are allowed and stripped by
# config.py (whitespace-preserving, so line numbers stay valid). Every real
# field is shown with a value you must edit. This file loads as-is.
# Idempotent: an existing config is left untouched so your edits survive a
# re-run (use --force ... or rm the file to regenerate).
if [ -e "$CONFIG_FILE" ]; then
    echo "[!] $CONFIG_FILE already exists. Skipping generation (keeps your edits)."
else
    echo "[*] Generating example configuration at $CONFIG_FILE"
    cat <<'SNAPBACK_EOF' > "$CONFIG_FILE"
{
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
}
SNAPBACK_EOF
    chown root:root "$CONFIG_FILE"
    chmod 0644 "$CONFIG_FILE"
fi

# Create password directory. Restic runs as the service user and must be able
# to read the password files, so they are owned by the admin and group-readable
# by the service group (e.g. admin:snap-bak, mode 0640). The directory stays
# owned by snap-bak; the admin creates files here via sudo.
mkdir -p "$SNAPBAK_DIR/passwords"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$SNAPBAK_DIR/passwords"
chmod 0750 "$SNAPBAK_DIR/passwords"

# Create directory holding gitignore-style exclude files (referenced by
# each job's "ignore_file"). Readable by the service user.
mkdir -p "$SNAPBAK_DIR/excludes"
chown root:"$SERVICE_GROUP" "$SNAPBAK_DIR/excludes"
chmod 0750 "$SNAPBAK_DIR/excludes"
if [ ! -f "$SNAPBAK_DIR/excludes/home.ignore" ]; then
    cat > "$SNAPBAK_DIR/excludes/home.ignore" <<'EOF'
# gitignore-style patterns, one per line (restic --exclude-file)
*.tmp
*.cache
EOF
fi
chown root:"$SERVICE_GROUP" "$SNAPBAK_DIR/excludes/home.ignore" 2>/dev/null || true
chmod 0640 "$SNAPBAK_DIR/excludes/home.ignore" 2>/dev/null || true

# rclone config for the service user (restic -> rclone remotes). The operator
# copies the real rclone.conf in; the dir must exist and be writable by the
# service user.
mkdir -p "$SNAPBAK_DIR/.config/rclone"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$SNAPBAK_DIR/.config/rclone"
chmod 0750 "$SNAPBAK_DIR/.config/rclone"

# Create mount base directory and ensure service user owns it
mkdir -p "$MOUNT_BASE"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$MOUNT_BASE"
chmod 0755 "$MOUNT_BASE"

# --- 3. CONFIGURE SUDOERS ---
# A single static rule pointing at the root-owned wrapper. A bare command name
# matches any arguments (see sudoers(5)), which is valid on both sudo-rs and
# sudo.ws. A trailing '*' is deliberately NOT used because sudo-rs mishandles
# it. Because the wrapper derives every path from the root-owned config at
# runtime, changing mount_base or snapshot_dir in config NEVER requires
# touching sudoers.
SUDOERS_FILE="/etc/sudoers.d/snap-bak"
echo "[*] Configuring sudo permissions in $SUDOERS_FILE"
cat <<EOF > "$SUDOERS_FILE"
# snap-bak permissions: allow the service user to run only the root-owned
# wrapper. The wrapper validates job/snapshot names against /opt/snap-bak/
# config.json (root-owned) and executes btrfs/mount/umount with config-derived
# paths. A bare command name (no args listed) matches any arguments.
$SERVICE_USER ALL=(root) NOPASSWD:$WRAPPER_PATH
EOF
chmod 0440 "$SUDOERS_FILE"

# Validate the generated sudoers before leaving it in place.
if ! visudo -c -f "$SUDOERS_FILE"; then
    echo "[!] sudoers validation failed; removing invalid $SUDOERS_FILE." >&2
    rm -f "$SUDOERS_FILE"
    exit 1
fi

# --- 4. CREATE SYSTEMD SERVICE ---
echo "[*] Creating systemd service: $SERVICE_NAME.service"
cat <<EOF > "/etc/systemd/system/$SERVICE_NAME.service"
[Unit]
Description=Snap-Bak Backup Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$SNAPBAK_DIR
Environment=SNAPBAK_CONFIG_FILE=$CONFIG_FILE
ExecStart=$PYTHON_BIN -m snap_bak.cli

[Install]
WantedBy=multi-user.target
EOF

# --- 5. CREATE SYSTEMD TIMER ---
TIMER_SCHEDULE="hourly"
echo "[*] Creating systemd timer: $SERVICE_NAME.timer (Schedule: $TIMER_SCHEDULE)"
cat <<EOF > "/etc/systemd/system/$SERVICE_NAME.timer"
[Unit]
Description=Run Snap-Bak Orchestrator periodically

[Timer]
OnCalendar=$TIMER_SCHEDULE
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Ensure generated unit files are readable regardless of root's umask.
chmod 0644 "/etc/systemd/system/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.timer"

# --- 6. FINALIZE ---
echo "[*] Reloading systemd daemon..."
systemctl daemon-reload
if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled "$SERVICE_NAME.service" >/dev/null 2>&1; then
    echo "[!] $SERVICE_NAME.service is currently enabled. Migrate to the timer:"
    echo "    sudo systemctl disable --now $SERVICE_NAME.service"
    echo "    sudo systemctl enable --now $SERVICE_NAME.timer"
fi

echo ""
echo "===================================================="
echo " SETUP COMPLETE"
echo "===================================================="
echo " User:        $SERVICE_USER"
echo " Config:      $CONFIG_FILE"
echo " State Dir:   $STATE_DIR"
echo " Cache Dir:   $CACHE_DIR"
echo " Mount Base:  $MOUNT_BASE"
echo " Service:     $SERVICE_NAME"
echo "===================================================="
echo " NEXT STEPS:"
echo " 1. Edit $CONFIG_FILE to match your actual environment."
echo " 2. Create your restic password file in $SNAPBAK_DIR/passwords/, owned by"
echo "    you and readable by the service group:"
echo "       sudo chown <you>:snap-bak <file>.pw && sudo chmod 640 <file>.pw"
echo "    (or owner-only chmod 600). The gate rejects other groups/other access."
echo " 3. Copy your rclone config to $SNAPBAK_DIR/.config/rclone/rclone.conf (chmod 600)."
echo " 4. Ensure snapshot_dir paths exist (btrfs snapshots are created as root via the wrapper)."
echo " 5. Ensure restic repo path is accessible by $SERVICE_USER (read/write)."
echo " 6. If an older setup enabled the oneshot service itself, disable it now:"
echo "       sudo systemctl disable --now $SERVICE_NAME.service"
echo "    Then enable the timer (do NOT enable the bare service name; systemd"
echo "    treats an unsuffixed name as .service, not .timer):"
echo "       sudo systemctl enable --now $SERVICE_NAME.timer"
echo "===================================================="
