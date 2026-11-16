"""Rotating file logs (5 MB x 5 per process) in data/logs, plus console output."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DATA_DIR

LOG_DIR = Path(os.environ.get("ATLAS_LOG_DIR", str(DATA_DIR / "logs")))
FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup(name: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter(FMT))
        root.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(FMT))
        root.addHandler(sh)
        root.setLevel(logging.INFO)
    return logging.getLogger(f"atlas.{name}")
