"""
archiver.py — CERT-In 180-Day Job Archive Scheduler
=====================================================
Rule 6 (CERT-In compliance) states that all logs and job artefacts must be
retained for a minimum of 180 days and stored inside India.

This script runs an APScheduler background job that:
  1. Scans JOBS_DIR for job folders older than ARCHIVE_AFTER_DAYS.
  2. Compresses each eligible folder into a timestamped .zip file.
  3. Moves the archive to COLD_STORAGE_PATH (should map to a MeitY-empanelled
     encrypted archive volume — e.g. a separate encrypted EBS volume or
     object-store bucket in an NIC/ESDS data centre).
  4. Removes the original hot-storage folder ONLY after the archive is
     successfully written and verified.
  5. Logs every archival action to syslog for SIEM ingestion.

Environment variables
---------------------
SECURE_MOUNT_PATH   Path to the MeitY-approved hot-storage encrypted volume.
COLD_STORAGE_PATH   Path to the MeitY-approved cold-storage archive volume.
ARCHIVE_AFTER_DAYS  Age (days) after which a job is eligible for archiving.
                    Default: 30  (jobs older than 30 days move to cold storage).
RETENTION_DAYS      Total retention period in days.  Default: 180.
SIEM_HOST           Syslog receiver host for the CERT-In SIEM.  Default: 127.0.0.1
SIEM_PORT           Syslog receiver UDP port.  Default: 514

Usage
-----
  # Run as a standalone daemon (production — use systemd or supervisord):
  python archiver.py

  # Alternatively, import and start within the Flask process:
  from archiver import start_archiver
  start_archiver()

Dependencies
------------
  pip install apscheduler
"""

import os
import sys
import zipfile
import shutil
import logging
import tempfile
from datetime import datetime, timezone, timedelta
from logging.handlers import SysLogHandler
from pathlib import Path

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
# Configuration (all from environment — never hardcoded)
# ══════════════════════════════════════════════════════════════════════════════

SECURE_MOUNT_PATH = os.environ.get("SECURE_MOUNT_PATH")
if not SECURE_MOUNT_PATH:
    import warnings
    warnings.warn(
        "[SECURITY] SECURE_MOUNT_PATH is not set. "
        "Falling back to OS temp — NOT acceptable in production.",
        RuntimeWarning,
        stacklevel=1,
    )
    SECURE_MOUNT_PATH = tempfile.gettempdir()

JOBS_DIR = os.path.join(SECURE_MOUNT_PATH, "lithosync_jobs")

# Cold-storage path — separate encrypted volume for 180-day retention
COLD_STORAGE_PATH = os.environ.get(
    "COLD_STORAGE_PATH",
    os.path.join(SECURE_MOUNT_PATH, "lithosync_cold_archive"),
)
os.makedirs(COLD_STORAGE_PATH, exist_ok=True)

ARCHIVE_AFTER_DAYS = int(os.environ.get("ARCHIVE_AFTER_DAYS", "30"))
RETENTION_DAYS     = int(os.environ.get("RETENTION_DAYS",     "180"))

SIEM_HOST = os.environ.get("SIEM_HOST", "127.0.0.1")
SIEM_PORT = int(os.environ.get("SIEM_PORT", "514"))

# ══════════════════════════════════════════════════════════════════════════════
# Logging — route archival events to syslog for SIEM ingestion (Rule 6)
# ══════════════════════════════════════════════════════════════════════════════

_syslog_handler = SysLogHandler(address=(SIEM_HOST, SIEM_PORT))
_syslog_handler.setFormatter(
    logging.Formatter("lithosync.archiver %(levelname)s %(asctime)s — %(message)s")
)

archiver_log = logging.getLogger("lithosync.archiver")
archiver_log.setLevel(logging.INFO)
archiver_log.addHandler(_syslog_handler)
archiver_log.addHandler(logging.StreamHandler(sys.stdout))


# ══════════════════════════════════════════════════════════════════════════════
# Archive helpers
# ══════════════════════════════════════════════════════════════════════════════

def _zip_folder(source_dir: Path, dest_zip: Path) -> None:
    """Zip all files in source_dir into dest_zip (preserving relative paths)."""
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in source_dir.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, arcname=file_path.relative_to(source_dir.parent))


def _verify_zip(zip_path: Path, source_dir: Path) -> bool:
    """Return True if the zip is readable and contains at least the expected files."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad:
                archiver_log.error(f"ZIP CORRUPT: first bad file = {bad} in {zip_path}")
                return False
            zipped_names = set(zf.namelist())
        source_files = {
            str(p.relative_to(source_dir.parent))
            for p in source_dir.rglob("*")
            if p.is_file()
        }
        if not source_files.issubset(zipped_names):
            missing = source_files - zipped_names
            archiver_log.error(f"ZIP INCOMPLETE: missing files {missing} in {zip_path}")
            return False
        return True
    except Exception as exc:
        archiver_log.error(f"ZIP VERIFY EXCEPTION: {exc} for {zip_path}")
        return False


def _folder_age_days(folder: Path) -> float:
    """Return the age of a folder in days based on its last-modified time."""
    mtime = folder.stat().st_mtime
    age   = datetime.now(tz=timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)
    return age.total_seconds() / 86_400


# ══════════════════════════════════════════════════════════════════════════════
# Main archival job — called by the scheduler
# ══════════════════════════════════════════════════════════════════════════════

def run_archival_pass() -> None:
    """
    Scan JOBS_DIR for job folders that exceed ARCHIVE_AFTER_DAYS.
    Compress each into COLD_STORAGE_PATH and remove from hot storage.
    Purge cold-storage archives older than RETENTION_DAYS.
    """
    archiver_log.info(
        f"EVENT=ARCHIVE_PASS_START "
        f"JOBS_DIR={JOBS_DIR} COLD_STORAGE={COLD_STORAGE_PATH} "
        f"ARCHIVE_AFTER={ARCHIVE_AFTER_DAYS}d RETENTION={RETENTION_DAYS}d"
    )

    jobs_root = Path(JOBS_DIR)
    if not jobs_root.exists():
        archiver_log.warning(f"EVENT=ARCHIVE_SKIP REASON=JOBS_DIR_MISSING path={JOBS_DIR}")
        return

    archived = 0
    skipped  = 0
    errors   = 0

    for job_dir in sorted(jobs_root.iterdir()):
        if not job_dir.is_dir():
            continue

        try:
            age_days = _folder_age_days(job_dir)
        except Exception as exc:
            archiver_log.error(f"EVENT=STAT_FAILURE JOB={job_dir.name} DETAIL={exc}")
            errors += 1
            continue

        if age_days < ARCHIVE_AFTER_DAYS:
            skipped += 1
            continue

        # Build a unique archive name: <job_id>_<ISO-date>.zip
        ts_str   = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        zip_name = f"{job_dir.name}_{ts_str}.zip"
        zip_path = Path(COLD_STORAGE_PATH) / zip_name

        try:
            archiver_log.info(
                f"EVENT=ARCHIVE_START JOB={job_dir.name} "
                f"AGE={age_days:.1f}d DEST={zip_path}"
            )

            _zip_folder(job_dir, zip_path)

            # Verify integrity before touching the original
            if not _verify_zip(zip_path, job_dir):
                # Archive is corrupt — leave the original in place, alert SIEM
                archiver_log.critical(
                    f"EVENT=ARCHIVE_CORRUPT JOB={job_dir.name} "
                    f"ZIP={zip_path} — original NOT removed"
                )
                errors += 1
                # Remove the bad zip to avoid confusion
                zip_path.unlink(missing_ok=True)
                continue

            # Only remove the hot-storage folder after a verified archive
            shutil.rmtree(job_dir)
            archiver_log.info(
                f"EVENT=ARCHIVE_COMPLETE JOB={job_dir.name} "
                f"ZIP={zip_path} SIZE={zip_path.stat().st_size}B"
            )
            archived += 1

        except Exception as exc:
            archiver_log.error(
                f"EVENT=ARCHIVE_FAILURE JOB={job_dir.name} DETAIL={exc}"
            )
            errors += 1
            # Clean up partial zip if it exists
            zip_path.unlink(missing_ok=True)

    # ── Purge cold-storage archives beyond the 180-day retention window ───────
    purged = 0
    cold_root = Path(COLD_STORAGE_PATH)
    for archive in cold_root.glob("*.zip"):
        try:
            age_days = _folder_age_days(archive)
            if age_days > RETENTION_DAYS:
                archive.unlink()
                archiver_log.info(
                    f"EVENT=RETENTION_PURGE ARCHIVE={archive.name} "
                    f"AGE={age_days:.1f}d > RETENTION={RETENTION_DAYS}d"
                )
                purged += 1
        except Exception as exc:
            archiver_log.error(f"EVENT=PURGE_FAILURE ARCHIVE={archive.name} DETAIL={exc}")
            errors += 1

    archiver_log.info(
        f"EVENT=ARCHIVE_PASS_DONE "
        f"ARCHIVED={archived} SKIPPED={skipped} PURGED={purged} ERRORS={errors}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler bootstrap
# ══════════════════════════════════════════════════════════════════════════════

def start_archiver(interval_hours: int = 6) -> object:
    """
    Start the APScheduler background scheduler.

    Runs run_archival_pass() every `interval_hours` hours.
    Returns the scheduler object so the caller can call .shutdown() on exit.

    Raises RuntimeError if APScheduler is not installed.
    """
    if not APSCHEDULER_AVAILABLE:
        raise RuntimeError(
            "APScheduler is not installed. Run: pip install apscheduler"
        )

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_archival_pass,
        trigger=IntervalTrigger(hours=interval_hours),
        id="lithosync_archiver",
        name="CERT-In 180-day archive job",
        replace_existing=True,
        max_instances=1,      # prevent overlap if a pass takes longer than the interval
    )
    scheduler.start()

    archiver_log.info(
        f"EVENT=ARCHIVER_STARTED INTERVAL={interval_hours}h "
        f"ARCHIVE_AFTER={ARCHIVE_AFTER_DAYS}d RETENTION={RETENTION_DAYS}d"
    )
    return scheduler


# ══════════════════════════════════════════════════════════════════════════════
# Stand-alone entry point (run as a daemon or cron target)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import time
    import signal

    archiver_log.info("lithosync archiver daemon starting…")

    if not APSCHEDULER_AVAILABLE:
        # Graceful degradation: run once and exit (suitable for cron)
        archiver_log.warning(
            "APScheduler not available — running a single archival pass and exiting. "
            "Schedule this script via cron for periodic execution."
        )
        run_archival_pass()
        sys.exit(0)

    scheduler = start_archiver(interval_hours=6)

    # Run one pass immediately on startup so cron gaps don't leave jobs un-archived
    run_archival_pass()

    # Keep the daemon alive; shut down cleanly on SIGTERM / SIGINT
    def _shutdown(signum, frame):
        archiver_log.info(f"EVENT=ARCHIVER_SHUTDOWN signal={signum}")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    archiver_log.info("Archiver daemon running. Ctrl-C or SIGTERM to stop.")
    while True:
        time.sleep(60)
