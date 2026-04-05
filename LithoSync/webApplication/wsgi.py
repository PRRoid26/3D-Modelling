"""
wsgi.py — Production Entry Point
=================================
This module is the Gunicorn entry point for the Lithosync Flask application.

It wires the CERT-In 180-day archiver scheduler (Rule 6) into the same
process so no separate archiver daemon is required in single-host deployments.

Usage (with Gunicorn — Rule 7: NEVER use Flask dev server in production):
    gunicorn -w 4 -b 127.0.0.1:5050 --timeout 120 wsgi:application

With systemd, create /etc/systemd/system/lithosync.service:
    [Unit]
    Description=Lithosync GIS Flask App
    After=network.target postgresql.service

    [Service]
    User=lithosync
    WorkingDirectory=/opt/lithosync
    Environment="SECURE_MOUNT_PATH=/mnt/ongc-encrypted"
    Environment="COLD_STORAGE_PATH=/mnt/ongc-cold-archive"
    Environment="DATABASE_URL=postgresql+psycopg2://user:pass@db.internal:5432/lithosync_geo?sslmode=require"
    Environment="FLASK_SECRET_KEY=<generated-random-key>"
    Environment="SIEM_HOST=10.0.1.50"
    Environment="SIEM_PORT=514"
    Environment="DEFAULT_SAP_ROLE=VIEWER"
    ExecStart=/opt/lithosync/venv/bin/gunicorn -w 4 -b 127.0.0.1:5050 --timeout 120 wsgi:application
    Restart=on-failure
    RestartSec=5

    [Install]
    WantedBy=multi-user.target
"""

import atexit
import logging

from app import app as application

log = logging.getLogger("lithosync.wsgi")

# ── Start the CERT-In 180-day archiver alongside the app process ──────────────
_scheduler = None
try:
    from archiver import start_archiver, run_archival_pass

    # Run one pass immediately on startup to cover any gap since last run
    run_archival_pass()

    # Then schedule recurring passes every 6 hours
    _scheduler = start_archiver(interval_hours=6)

    # Shut down gracefully when Gunicorn exits
    atexit.register(lambda: _scheduler.shutdown(wait=False) if _scheduler else None)

    log.info("EVENT=ARCHIVER_WIRED_INTO_WSGI interval=6h")

except Exception as exc:
    log.warning(f"EVENT=ARCHIVER_WIRE_FAILURE DETAIL={exc} — archiver not started")
