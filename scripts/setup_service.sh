#!/bin/bash

# snapbak-setup.sh
# Automates the creation of the snapbak-svc user, sudoers permissions,
# and systemd timer/service for the snap-bak manager.
#
# USAGE INSTRUCTIONS:
# 1. Make the script executable: chmod +x snapbak-setup.sh
# 2. Run with root privileges: sudo ./snapbak-setup.sh
# 3. After completion, perform the 'NEXT STEPS' listed in the script output.

set -e

# --- ROOT GUARD ---
if [ "$EUID" -ne 0 ]; then
    echo "[!] This script must be run as root (it writes to /etc, /opt, creates users)." >&2
    echo "    Re-run with: sudo $0" >&2
    exit 1
fi

# --- CONFIGURATION ---
SERVICE_USER="snapbak-svc"
SERVICE_GROUP="snapbak-svc"
SNAPBAK_DIR="/opt/snap-bak"
MOUNT_BASE="/mnt/snapbak_mounts"
CONFIG_FILE="$SNAPBAK_DIR/config.json"
SCRIPT_PATH="$SNAPBAK_DIR/snapbak.py"
SERVICE_NAME="snap-bak"

# Source of the python orchestrator: co-located with this setup script.
SCRIPT_SRC="$(dirname "$(readlink -f "$0")")/snapbak.py"

# --- 1. CREATE USER, GROUP & DIRECTORIES ---
# Creating the group explicitly avoids failing chown when user-private
# groups are disabled on the system.
if ! getent group "$SERVICE_GROUP" >/dev/null; then
    echo "[*] Creating system group: $SERVICE_GROUP"
    groupadd --system "$SERVICE_GROUP"
else
    echo "[!] Group $SERVICE_GROUP already exists. Skipping creation."
fi
echo "[*] Creating system user: $SERVICE_USER"
if ! id "$SERVICE_USER" &>/dev/null; then
    # Create a system user with a home directory for XDG_STATE_HOME
    useradd --system --create-home --home-dir "$SNAPBAK_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
else
    echo "[!] User $SERVICE_USER already exists. Skipping creation."
fi

# Ensure state directory exists and is owned by the service user
mkdir -p "$SNAPBAK_DIR"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$SNAPBAK_DIR"
chmod 750 "$SNAPBAK_DIR"

# --- 1b. INSTALL MANAGED PROGRAM ---
# Copy the orchestrator into place so the systemd unit has something to run.
if [ ! -f "$SCRIPT_SRC" ]; then
    echo "[!] Cannot find $SCRIPT_SRC. Place snapbak.py next to this script." >&2
    exit 1
fi
install -o root -g root -m 0644 "$SCRIPT_SRC" "$SCRIPT_PATH"

# --- 2. GENERATE EXAMPLE CONFIG ---
echo "[*] Generating example configuration at $CONFIG_FILE"
cat <<EOF > "$CONFIG_FILE"
{
  "repos": [
    {
      "name": "primary-cloud",
      "path": "rclone:PROVIDER:DIR",
      "password_file": "/opt/snap-bak/passwords/provider",
      "size_limit": 0,
      "latency_ms": 100,
      "throughput_mbps": 10
    }
  ],
  "mount_base": "/mnt/snapbak_mounts",
  "stuck_threshold": 3600,
  "jobs": [
    {
      "name": "Home-Protection",
      "snapshot_dir": "/home/.snapshots",
      "source_subvolume": "home",
      "repo": "primary-cloud",
      "priority": 150,
      "snapshot_interval": 3600,
      "backup_retention": {"daily": 7, "weekly": 4},
      "snapshot_retention": {"fn": "exponential", "config": {"n": 5}},
      "backup_interval": 604800,
      "backup_window_start": "02:00",
      "backup_window_end": "06:00",
      "excludes": ["*.tmp", "*.cache"]
    }
  ]
}
EOF
chown root:root "$CONFIG_FILE"
chmod 644 "$CONFIG_FILE"

# Create password directory. Restic runs as the service user and must be able
# to read the password file, so the directory is owned by the service user
# (files inside should be chmod 600).
mkdir -p "$SNAPBAK_DIR/passwords"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$SNAPBAK_DIR/passwords"
chmod 750 "$SNAPBAK_DIR/passwords"

# Create mount base directory and ensure service user owns it
mkdir -p "$MOUNT_BASE"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$MOUNT_BASE"
chmod 755 "$MOUNT_BASE"

# --- 3. CONFIGURE SUDOERS ---
SUDOERS_FILE="/etc/sudoers.d/snapbak-svc"
ISO_SNAPSHOT='[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9]:[0-9][0-9]'
echo "[*] Configuring granular sudo permissions in $SUDOERS_FILE"
cat <<EOF > "$SUDOERS_FILE"
# snap-bak specific permissions
# The service user may only manage Btrfs snapshots whose basename is exactly
# the ISO format used by snapbak.py (YYYY-MM-DD_HH:MM, UTC). The rule is a
# single line; the glob is injected via a shell variable that expands to the
# per-character digit ranges. Mount target is pinned to the mount base dir.
$SERVICE_USER ALL=(root) NOPASSWD:/usr/bin/btrfs subvolume snapshot -r * */${ISO_SNAPSHOT}, /usr/bin/btrfs subvolume delete */${ISO_SNAPSHOT}, /usr/bin/mount -o ro,uid=*,gid=* */${ISO_SNAPSHOT} ${MOUNT_BASE}/*, /usr/bin/umount -l ${MOUNT_BASE}/*
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
Environment=SNAPBAK_CONFIG_FILE=$CONFIG_FILE
Environment=XDG_STATE_HOME=$SNAPBAK_DIR
ExecStart=/usr/bin/python3 $SCRIPT_PATH

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

# --- 6. FINALIZE ---
echo "[*] Reloading systemd daemon..."
systemctl daemon-reload

echo ""
echo "===================================================="
echo " SETUP COMPLETE"
echo "===================================================="
echo " User:        $SERVICE_USER"
echo " Config:      $CONFIG_FILE"
echo " State Dir:   $SNAPBAK_DIR"
echo " Service:     $SERVICE_NAME"
echo "===================================================="
echo " NEXT STEPS:"
echo " 1. Edit $CONFIG_FILE to match your actual environment."
echo " 2. Create your restic password file in $SNAPBAK_DIR/passwords/ and chmod 600 it."
echo " 3. (For reference) $SCRIPT_PATH was installed root-owned (mode 0644), readable by $SERVICE_USER."
echo " 4. Ensure snapshot_dir paths are writable by $SERVICE_USER (for btrfs snapshot create/delete)."
echo " 5. Ensure restic repo path is accessible by $SERVICE_USER (read/write)."
echo " 6. Enable the timer: sudo systemctl enable --now $SERVICE_NAME"
echo "===================================================="
