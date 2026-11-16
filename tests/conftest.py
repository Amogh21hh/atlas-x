import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

_tmp = tempfile.mkdtemp()
os.environ["ATLAS_DB"] = str(Path(_tmp) / "test.db")
os.environ["ATLAS_SECRET_KEY"] = "test-secret"
# Tests must NEVER reach a real broker, whatever the developer's .env says. Set before atlas.config
# is imported (the .env loader only fills variables that are not already set).
os.environ["ATLAS_BROKER"] = "paper"
os.environ["IBKR_PORT"] = "1"  # unreachable: any accidental real IBKR connection fails immediately
os.environ["ATLAS_LOG_DIR"] = str(Path(_tmp) / "logs")  # never write into the real data/logs
os.environ["ATLAS_BACKUP_DIR"] = str(Path(_tmp) / "backups")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.config import UNIVERSE  # noqa: E402
from atlas.db import DB  # noqa: E402


def seed_prices(db: DB, days: int = 900, seed: int = 1):
    rng = np.random.default_rng(seed)
    end = date.today()
    dates = []
    d = end
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()
    rows = []
    for i, s in enumerate(UNIVERSE):
        px = 100 * np.cumprod(1 + rng.normal(0.0003 + 0.0001 * i, 0.01, len(dates)))
        rows += [(s, dt.isoformat(), float(p) / UNIVERSE[s]["scale"] / 100 * 100, float(p), 1000.0)
                 for dt, p in zip(dates, px)]
    db.many("INSERT OR REPLACE INTO prices(symbol,date,close,adjclose,volume) VALUES(?,?,?,?,?)", rows)


@pytest.fixture()
def db(tmp_path):
    d = DB(tmp_path / "t.db")
    seed_prices(d)
    return d
