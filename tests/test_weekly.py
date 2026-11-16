import numpy as np
import pandas as pd
import pytest

from atlas.data import clean
from atlas.db import DB
from atlas.weekly import goal_check, weekly


@pytest.fixture
def bare(tmp_path):
    return DB(tmp_path / "w.db")


def _snap(db, ts, eq):
    db.x("INSERT INTO equity_snapshots(ts,broker,equity,cash,positions) VALUES(?,?,?,?,'{}')", (ts, "paper", eq, eq))


def test_weekly_pnl_excludes_deposits_and_counts_fees(bare):
    # week 1 (Mon 2 Nov): deposit 100, ends at 102 -> +2 profit
    bare.x("INSERT INTO cash_flows(ts,broker,amount,note) VALUES('2026-11-02T09:00:00','paper',100,'deposit')")
    _snap(bare, "2026-11-02T09:00:00", 100.0)
    _snap(bare, "2026-11-06T16:00:00", 102.0)
    bare.x("INSERT INTO orders(created_at,broker,symbol,side,qty,status,filled_qty,commission,updated_at) "
           "VALUES('2026-11-02T09:05:00','paper','VWRL.L','BUY',1,'Filled',1,3.0,'x')")
    # week 2: deposit 50 and value falls to 149 -> -3 loss
    bare.x("INSERT INTO cash_flows(ts,broker,amount,note) VALUES('2026-11-10T09:00:00','paper',50,'deposit')")
    _snap(bare, "2026-11-10T09:00:00", 152.0)
    _snap(bare, "2026-11-13T16:00:00", 149.0)
    w = weekly(bare, "paper")
    a, b = w["weeks"]
    assert a["week_of"] == "2026-11-02" and a["pnl"] == 2.0 and a["fees"] == 3.0
    assert a["return"] == pytest.approx(0.02)
    assert b["start"] == 102.0 and b["deposits"] == 50.0 and b["pnl"] == -3.0
    assert b["return"] == pytest.approx(-3 / 152, abs=1e-5)
    assert w["total_pnl"] == -1.0 and w["total_fees"] == 3.0
    assert w["cumulative_return"] == pytest.approx(1.02 * (1 - 3 / 152) - 1, abs=1e-5)


def test_weekly_empty(bare):
    w = weekly(bare, "paper")
    assert w["weeks"] == [] and w["cumulative_return"] is None


def test_goal_check_flags_30pct_week_as_unrealistic():
    g = goal_check(0.30, 100)
    assert g["if_achieved"]["52_weeks"] > 80_000_000
    assert g["verdict"].startswith("Not achievable")


def test_clean_drops_multiday_unit_glitch_but_keeps_real_regime_move():
    idx = pd.bdate_range("2020-01-01", periods=80)
    px = np.full(80, 100.0)
    px[30:34] = 161.0            # 4-day wrong-currency run (like SGLN.L 2011)
    px[60:] = 85.0               # genuine persistent -15% move (like gilts Sep 2022)
    df = pd.DataFrame({"close": px, "adjclose": px, "volume": 1.0}, index=idx)
    out = clean(df)
    assert not any(d in out.index for d in idx[30:34])
    assert all(d in out.index for d in idx[60:])
