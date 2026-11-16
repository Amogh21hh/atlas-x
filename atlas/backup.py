"""Consistent online SQLite backups (sqlite3 backup API — safe while the worker is writing) and restore.

    python -m atlas.backup              # back up now
    python -m atlas.backup --list
    python -m atlas.backup --restore data/backups/atlas-20261114-0100.db   (stop the worker first)

The worker makes one backup per day and keeps the newest 30.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time
from pathlib import Path

from .config import DATA_DIR, settings

BACKUP_DIR = Path(os.environ.get("ATLAS_BACKUP_DIR", str(DATA_DIR / "backups")))
KEEP = 30


def backup(db_path: Path | None = None, dest_dir: Path | None = None, keep: int = KEEP) -> Path:
    src_path = Path(db_path or settings.db_path)
    dest_dir = Path(dest_dir or BACKUP_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"backup integrity check failed: {ok}")
    finally:
        dst.close()
        src.close()
    old = sorted(dest_dir.glob(f"{src_path.stem}-*.db"))
    for f in old[:-keep]:
        f.unlink()
    return dest


def restore(backup_file: Path, db_path: Path | None = None) -> Path:
    """Replace the live database with a backup. The current DB is kept as *.pre-restore."""
    target = Path(db_path or settings.db_path)
    chk = sqlite3.connect(backup_file)
    try:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup file failed integrity check; not restoring")
    finally:
        chk.close()
    if target.exists():
        shutil.copy2(target, target.with_suffix(".pre-restore.db"))  # restore is reversible
    for ext in ("-wal", "-shm"):
        p = Path(str(target) + ext)
        if p.exists():
            p.unlink()
    shutil.copy2(backup_file, target)
    return target


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore")
    a = ap.parse_args()
    if a.list:
        for f in sorted(BACKUP_DIR.glob("*.db")):
            print(f, f"{f.stat().st_size / 1e6:.1f} MB")
    elif a.restore:
        print("restored ->", restore(Path(a.restore)))
    else:
        print("backup ->", backup())


if __name__ == "__main__":
    main()
