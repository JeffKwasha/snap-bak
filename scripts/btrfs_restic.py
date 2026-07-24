#!/usr/bin/env python3
"""
Btrfs Snapshot -> Restic Backup Orchestrator.

Strictly data-driven: jobs are defined entirely in the config file.
Supports CLI overrides, env vars, and preflight checks.

Usage:
  btrfs_restic.py --help
  btrfs_restic.py --list-jobs
  btrfs_restic.py --job <name> --dry-run
  BTRFS_RESTIC_JOB=home btrfs_restic.py
"""
import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- Constants ---
DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "config.json"
DEFAULT_MOUNT_BASE = Path("/bak")
STATUS_FILE = Path.home() / ".hermes" / "backup_status.json"
LOG_FILE = Path.home() / ".cache" / "btrfs_restic.log"
LOCK_FILE = Path("/tmp/btrfs_restic.lock")

# --- Logging ---
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

def acquire_lock():
    """Prevent concurrent runs."""
    LOCK_FILE.touch()
    try:
        fcntl.flock(LOCK_FILE.open('r'), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.error("Another instance is running. Exiting.")
        sys.exit(1)

def release_lock():
    fcntl.flock(LOCK_FILE.open('r'), fcntl.LOCK_UN)
    LOCK_FILE.unlink(missing_ok=True)

def run(cmd, check=True):
    """Run a shell command safely."""
    logger.info(f"CMD: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
        if result.stdout: logger.debug(f"STDOUT: {result.stdout}")
        if result.stderr: logger.debug(f"STDERR: {result.stderr}")
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed (rc={e.returncode}): {e.cmd} | stderr: {e.stderr}")
        raise

def update_hermes_status(job, status, message, dry_run=False):
    """Write status to Hermes drop file for monitoring."""
    if dry_run: return
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({
        "job": job,
        "status": status,
        "timestamp": datetime.now().isoformat(),
        "message": message
    }, indent=2))
    logger.info(f"HERMES: {status} - {message}")

def get_latest_snapshot(snapshot_dir):
    """Find the latest Btrfs snapshot by sorting subvolumes (ISO 8601 support)."""
    subs = list(Path(snapshot_dir).iterdir())
    if not subs:
        raise ValueError(f"No snapshots found in {snapshot_dir}")
    return sorted(subs)[-1]

def mount_snapshot(snapshot_path, mount_point):
    """Mount the snapshot to a specific mount point."""
    mount_point.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(mount_point):
        logger.warning(f"{mount_point} is already mounted. Unmounting first.")
        run(["umount", "-l", str(mount_point)])
    run(["mount", "-o", "ro", str(snapshot_path), str(mount_point)])

def unmount_snapshot(mount_point):
    """Safely unmount the backup source."""
    if mount_point.exists() and os.path.ismount(mount_point):
        run(["umount", "-l", str(mount_point)])

def estimate_backup_size(repo, mount_point, tag, password_file=""):
    """Run a restic dry-run to estimate backup size and check remote."""
    logger.info("Running preflight dry-run to estimate size and check remote...")
    cmd = [
        "restic", "-r", repo,
        "backup", str(mount_point),
        "--no-symlinks",
        "--tag", tag,
        "--json",
        "--dry-run"
    ]
    if password_file:
        cmd.extend(["--password-file", password_file])
        
    try:
        result = run(cmd)
        lines = result.stdout.strip().split('\n')
        summary = json.loads(lines[-1])
        total_size = summary.get("total_size", 0)
        total_files = summary.get("total_files", 0)
        logger.info(f"PREFLIGHT ESTIMATE: {total_files} files, {(total_size / 1e9):.2f} GB")
        return total_size
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Preflight failed (remote connectivity or config issue): {e.stderr}")
    except (json.JSONDecodeError, IndexError) as e:
        logger.warning(f"Could not parse preflight output: {e}")
        return 0

def delete_old_snapshots(snapshot_dir, backup_time, dry_run=False):
    """Delete snapshots older than the provided backup timestamp."""
    logger.info(f"Cleaning snapshots in {snapshot_dir} older than {backup_time.isoformat()}")
    subs = list(Path(snapshot_dir).iterdir())
    for sub in subs:
        try:
            name = sub.name
            if "T" in name:
                snap_time = datetime.fromisoformat(name)
            else:
                snap_time = datetime.strptime(name, "%Y%m%d_%H%M%S")
            
            if snap_time < backup_time:
                logger.info(f"Deleting old snapshot: {sub.name}")
                if not dry_run:
                    run(["btrfs", "subvolume", "delete", str(sub)])
        except ValueError:
            logger.warning(f"Skipping non-standard snapshot name: {sub.name}")

def run_job(job, global_config, dry_run=False):
    """Execute a single backup job."""
    job_name = job.get("name")
    if not job_name:
        raise ValueError("Job missing 'name' field.")
        
    repo = job.get("repo", global_config.get("repo"))
    password_file = job.get("password_file", global_config.get("password_file", ""))
    snapshot_dir = job.get("snapshot_dir")
    mount_base = Path(global_config.get("mount_base", str(DEFAULT_MOUNT_BASE)))
    mount_point = mount_base / job_name
    
    if not repo or not snapshot_dir:
        raise ValueError(f"Job '{job_name}' missing required fields: repo or snapshot_dir.")
        
    logger.info(f"--- Starting Job: {job_name} ---")
    update_hermes_status(job_name, "started", "Backup initiated", dry_run)
    
    try:
        # 1. Find latest snapshot
        latest_snap = get_latest_snapshot(snapshot_dir)
        logger.info(f"Found latest snapshot: {latest_snap}")
        
        # 2. Mount snapshot
        mount_snapshot(latest_snap, mount_point)
        
        # 3. Preflight: Estimate size and verify remote connectivity
        size_bytes = estimate_backup_size(repo, mount_point, job_name, password_file)
        
        if dry_run:
            logger.info(f"DRY-RUN: Preflight passed. Would run backup for {job_name}.")
            return

        # 4. Restic Backup (Streams directly to remote, minimal local disk usage)
        cmd = [
            "restic", "-r", repo,
            "backup", str(mount_point),
            "--no-symlinks",
            "--tag", job_name
        ]
        if password_file:
            cmd.extend(["--password-file", password_file])
            
        # Add retention flags (Intelligent space management)
        for key, val in job.get("retention", {}).items():
            cmd.extend([f"--keep-{key}", str(val)])
            
        run(cmd)
        
        # 5. Prune restic (Reclaims space on remote)
        prune_cmd = ["restic", "-r", repo, "prune"]
        if password_file:
            prune_cmd.extend(["--password-file", password_file])
        run(prune_cmd)
        
        # 6. Record success time
        backup_time = datetime.now()
        update_hermes_status(job_name, "success", f"Backup completed at {backup_time.isoformat()}", dry_run)
        
        # 7. Cleanup old snapshots (Frees local disk space AFTER backup)
        delete_old_snapshots(snapshot_dir, backup_time)
        
    except Exception as e:
        logger.error(f"Job {job_name} failed: {e}", exc_info=True)
        update_hermes_status(job_name, "failed", str(e), dry_run)
    finally:
        # 8. Unmount
        unmount_snapshot(mount_point)
        logger.info(f"--- Finished Job: {job_name} ---")

def main():
    parser = argparse.ArgumentParser(
        description="Btrfs Snapshot -> Restic Backup Orchestrator",
        epilog="Config: %(default)s | Env: BTRFS_RESTIC_CONFIG, BTRFS_RESTIC_JOB",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-c", "--config", default=os.environ.get("BTRFS_RESTIC_CONFIG", str(DEFAULT_CONFIG)), 
                        help="Path to config JSON file.")
    parser.add_argument("-j", "--job", default=os.environ.get("BTRFS_RESTIC_JOB"),
                        help="Run a specific job by name (default: all jobs).")
    parser.add_argument("-n", "--dry-run", action="store_true", 
                        help="Simulate backup. Runs preflight (size/remote check) but skips upload & deletion.")
    parser.add_argument("-l", "--list-jobs", action="store_true",
                        help="List all configured jobs and exit.")
    args = parser.parse_args()
    
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config not found: {config_path}")
        sys.exit(1)
        
    global_config = json.loads(config_path.read_text())
    jobs = global_config.get("jobs", [])
    
    if args.list_jobs:
        print("Configured jobs:")
        for j in jobs:
            print(f"  - {j.get('name', 'UNNAMED')}: {j.get('snapshot_dir', '?')} -> {j.get('repo', '?')}")
        sys.exit(0)
        
    if args.job:
        jobs = [j for j in jobs if j.get("name") == args.job]
        if not jobs:
            logger.error(f"Job '{args.job}' not found in config.")
            sys.exit(1)

    acquire_lock()
    try:
        for job in jobs:
            run_job(job, global_config, dry_run=args.dry_run)
    finally:
        release_lock()

if __name__ == "__main__":
    main()
