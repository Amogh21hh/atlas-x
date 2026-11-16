"""Persistent background worker. Runs independently of any Claude session or browser.

    python -m atlas.worker            # loop forever (install as a Windows Scheduled Task / systemd unit)
    python -m atlas.worker --once     # single cycle

Crash-safe: every cycle is wrapped; failures are logged, notified, retried with exponential backoff.
State lives in SQLite so a restart resumes where it stopped (open orders are re-queried).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
import traceback

from pathlib import Path

from .config import settings
from .db import DB, now
from .logsetup import setup as setup_logging
from .engine import cycle, make_broker
from .backup import backup as backup_db
from .reporting import daily_report

log = logging.getLogger("atlas.worker")
_lock_handle = None


def _single_instance() -> bool:
    """Exclusive OS file lock: a second worker (e.g. scheduled task + manual run) exits immediately,
    so two processes can never plan and submit orders at the same time."""
    global _lock_handle
    path = Path(settings.db_path).with_suffix(".worker.lock")  # one lock per database
    _lock_handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--ignore-hours", action="store_true", help="trade outside LSE hours (paper testing only)")
    args = ap.parse_args()
    setup_logging("worker")
    if not _single_instance():
        log.warning("another ATLAS X worker is already running; exiting")
        return
    db = DB()
    db.event("info", "worker", "worker started")
    failures = 0
    while True:
        db.set("worker_heartbeat", now())
        broker = None
        try:
            broker = make_broker(db)
            if args.ignore_hours and broker.is_live:
                raise SystemExit("--ignore-hours is not permitted against a live broker")
            rep = cycle(db, broker, ignore_hours=args.ignore_hours)
            if not rep.get("health", {}).get("ok"):
                log.warning("broker unreachable (%s); no trading this cycle", rep.get("health", {}).get("error"))
            elif rep.get("blocked"):
                log.warning("cycle blocked: %s", rep["blocked"])
            else:
                log.info("cycle ok equity=%s steps=%s", rep.get("equity"), rep.get("steps"))
            today = time.strftime("%Y-%m-%d")
            if db.get("backup_day") != today:
                try:
                    db.event("info", "backup", f"daily backup -> {backup_db()}")
                    db.set("backup_day", today)
                except Exception as e:  # a failed backup must never stop trading safety logic
                    db.event("warn", "backup", f"daily backup failed: {e}")
            if db.get("report_day") != today and time.localtime().tm_hour >= 17:
                daily_report(db)
                db.set("report_day", today)
            failures = 0
        except SystemExit:
            raise
        except Exception as e:
            failures += 1
            db.event("critical" if failures >= 3 else "warn", "worker_error",
                     f"cycle failed ({failures}x): {e}", {"tb": traceback.format_exc()})
        finally:
            if broker is not None:
                broker.close()
        if args.once:
            break
        time.sleep(min(settings.cycle_seconds, 60 * 2 ** failures) if failures else settings.cycle_seconds)


if __name__ == "__main__":
    main()
