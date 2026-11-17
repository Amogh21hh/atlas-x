import numpy as np
import pandas as pd
import pytest

from atlas import data
from atlas.brokers.paper import PaperBroker
from atlas.config import CASH_PROXY, UNIVERSE, RiskLimits
from atlas.engine import cycle
from atlas.execution import execute, reconcile
from atlas.portfolio import plan_orders
from atlas.research import stats
from atlas.research.backtest import run
from atlas.research.lab import evaluate, select
from atlas.research.strategies import FAMILIES, trend_vol_target
from atlas.risk import ApprovedOrder, ProposedOrder, RiskEngine


def _ctx(db):
    prices, dates = data.latest_prices_gbp(db)
    return prices, dates


# ---------------- risk engine: the final authority ----------------
def test_approved_order_cannot_be_forged():
    with pytest.raises(PermissionError):
        ApprovedOrder(ProposedOrder("VWRL.L", "buy", 1, 100), [])


def test_kill_switch_blocks_everything(db):
    prices, dates = _ctx(db)
    r = RiskEngine(db)
    r.set_kill_switch(True, "test")
    rep = r.review([ProposedOrder("VWRL.L", "buy", 10, prices["VWRL.L"]),
                    ProposedOrder("VWRL.L", "sell", 1, prices["VWRL.L"])], 100000, 100000, {}, dates, False)
    assert not rep.approved and len(rep.rejected) == 2


def test_position_cap_leverage_short_whitelist(db):
    prices, dates = _ctx(db)
    r = RiskEngine(db)
    p = prices["VUKE.L"]
    big = ProposedOrder("VUKE.L", "buy", int(0.36 * 100000 / p) + 1, p)        # > VUKE 35% cap
    lev = ProposedOrder("VUSA.L", "buy", int(0.30 * 100000 / prices["VUSA.L"]), prices["VUSA.L"])
    short = ProposedOrder("IGLT.L", "sell", 5, prices["IGLT.L"])                 # not held
    alien = ProposedOrder("TSLA", "buy", 1, 200.0)
    rep = r.review([lev, short, alien], 100000, 10000, {}, dates, False)  # only £10k cash
    rep_cap = r.review([big], 100000, 100000, {}, dates, False)            # enough cash, too big
    reasons = " | ".join(w for _, w in rep.rejected + rep_cap.rejected)
    assert not rep.approved and not rep_cap.approved
    assert "cap" in reasons and "leverage" in reasons and "short" in reasons and "whitelist" in reasons


def test_live_blocked_even_with_stored_authorisation(db):
    """Paper-only lock: a live account is rejected even if a live authorisation record exists."""
    prices, dates = _ctx(db)
    db.set("live_authorisation", {"by": "x", "at": "2026-01-01", "max_capital_gbp": 1000})
    rep = RiskEngine(db).review([ProposedOrder("VWRL.L", "buy", 1, prices["VWRL.L"] * 1)], 1e5, 1e5, {}, dates, True)
    assert not rep.approved and "paper-only" in rep.rejected[0][1]


def test_stale_data_rejected(db):
    prices, _ = _ctx(db)
    rep = RiskEngine(db).review([ProposedOrder("VWRL.L", "buy", 10, prices["VWRL.L"])], 1e5, 1e5, {},
                                {"VWRL.L": "2020-01-01"}, False)
    assert not rep.approved and "stale" in rep.rejected[0][1]


def test_drawdown_breaker_allows_only_reductions(db):
    prices, dates = _ctx(db)
    r = RiskEngine(db, RiskLimits(max_drawdown=0.10))
    r.update_equity(100000)
    r.update_equity(85000)
    assert r.state()["suspended"]
    p = prices["VWRL.L"]
    rep = r.review([ProposedOrder("VWRL.L", "buy", 10, p), ProposedOrder("VWRL.L", "sell", 10, p)],
                   85000, 50000, {"VWRL.L": 20 * p}, dates, False)
    assert [a.order.side for a in rep.approved] == ["sell"]


# ---------------- research ----------------
def test_backtest_has_no_lookahead():
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(0)
    px = pd.DataFrame({s: 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx))) for s in UNIVERSE}, index=idx)
    # oracle weights that know TODAY's return: if the backtester used them same-day it would print money
    fwd = px.pct_change()
    w = pd.DataFrame(0.0, index=idx, columns=list(UNIVERSE))
    best = fwd.idxmax(axis=1)
    for d, s in best.items():
        if isinstance(s, str):
            w.loc[d, s] = 1.0
    net, _ = run(px, w, cost_bps=0)
    assert stats.sharpe(net) < 3  # same-day oracle would give Sharpe >> 10


def test_strategy_weights_are_valid(db):
    px = data.load_panel(db).dropna()
    for fam, (fn, grid, _) in FAMILIES.items():
        w = fn(px, **grid[0])
        assert (w >= -1e-9).all().all(), fam
        assert (w.sum(axis=1) <= 1 + 1e-6).all(), fam
        assert (w.drop(columns=[CASH_PROXY]) <= 0.5 + 1e-9).all().all() or fam.startswith(("buy_hold", "static")), fam


def test_dsr_penalises_many_trials():
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0.0004, 0.01, 2000))
    few = stats.dsr(r, [0.01, 0.02])
    many = stats.dsr(r, list(rng.normal(0, 0.03, 200)))
    assert many < few


def test_lab_gate_and_selection(db):
    px = data.load_panel(db).dropna()
    res = evaluate(px)
    assert {r["name"] for r in res} == set(FAMILIES)
    for r in res:
        assert r["passed"] == (not r["reasons"])
    sel = select(res)
    assert sel["name"] == "cash" or any(r["passed"] and r["name"] == sel["name"] for r in res)


# ---------------- portfolio / paper broker / execution ----------------
def test_paper_round_trip_and_reconcile(db):
    prices, dates = _ctx(db)
    b = PaperBroker(db, lambda: data.latest_prices_gbp(db)[0])
    risk = RiskEngine(db)
    target = {s: 0.0 for s in UNIVERSE}
    target.update({"VWRL.L": 0.3, "IGLT.L": 0.3, CASH_PROXY: 0.4})
    acct = b.account()
    orders = plan_orders(target, acct.cash, {}, prices)
    rep = risk.review(orders, acct.cash, acct.cash, {}, dates, False)
    assert len(rep.approved) == 3
    res = execute(db, b, risk, rep.approved)
    assert all(x["status"] == "filled" for x in res)
    acct2 = b.account()
    assert acct2.cash < acct.cash and acct2.cash > 0
    assert reconcile(db, b)["ok"]
    with pytest.raises(PermissionError):
        execute(db, b, risk, [ProposedOrder("VWRL.L", "buy", 1, 1.0)])


def test_full_cycle_paper(db):
    from atlas.research.lab import run_lab
    run_lab(db)
    db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")  # don't hit network in tests
    rep = cycle(db, PaperBroker(db, lambda: data.latest_prices_gbp(db)[0]), force_trade=True, ignore_hours=True)
    assert rep["health"]["ok"] and rep["reconcile"]["ok"]
    assert rep["equity"] > 0
    # a second cycle immediately after should not churn (no-trade band)
    rep2 = cycle(db, PaperBroker(db, lambda: data.latest_prices_gbp(db)[0]), ignore_hours=True)
    assert rep2.get("proposed", 0) == 0
    # liquidation blocked by kill switch stays queued; after release it executes
    risk = RiskEngine(db)
    risk.set_kill_switch(True, "t")
    db.set("liquidate_requested", True)
    cycle(db, PaperBroker(db, lambda: data.latest_prices_gbp(db)[0]), ignore_hours=True)
    assert db.get("liquidate_requested") is True
    risk.set_kill_switch(False, "t")
    rep3 = cycle(db, PaperBroker(db, lambda: data.latest_prices_gbp(db)[0]), ignore_hours=True)
    assert db.get("liquidate_requested") is False
    held = PaperBroker(db, lambda: {}).account().positions
    assert all(s == CASH_PROXY for s, p in held.items() if p.qty > 0)
    assert rep3["reconcile"]["ok"]
    # the dashboard's latest snapshot already shows the post-sale book (not the pre-sale one)
    import json
    last = db.q("SELECT equity, positions FROM equity_snapshots WHERE broker='paper' ORDER BY ts DESC LIMIT 1")[0]
    assert all(s == CASH_PROXY for s in json.loads(last["positions"]))
    assert rep3["equity"] == last["equity"]


def test_daily_loss_limit_trips(db):
    r = RiskEngine(db, RiskLimits(daily_loss_limit=0.04, max_drawdown=0.5))
    r.update_equity(100000)
    r.update_equity(95500)
    assert "daily loss" in (r.state()["suspended"] or "")


def test_worker_survives_broker_failure(db, monkeypatch):
    """A failing cycle is logged as an event and the worker loop exits cleanly in --once mode."""
    import sys
    from atlas import worker
    monkeypatch.setattr(worker, "DB", lambda: db)
    def boom(*a, **k):
        raise ConnectionError("gateway unreachable")
    monkeypatch.setattr(worker, "cycle", boom)
    monkeypatch.setattr(sys, "argv", ["worker", "--once"])
    worker.main()
    ev = db.q("SELECT kind, message FROM events WHERE kind='worker_error'")
    assert ev and "gateway unreachable" in ev[0]["message"]
    assert db.get("worker_heartbeat")


def test_performance_never_mixes_brokers(db):
    from atlas.reporting import performance
    db.x("INSERT INTO equity_snapshots(ts,broker,equity,cash,positions) VALUES('2026-01-01T00:00:00','paper',100000,0,'{}')")
    db.x("INSERT INTO equity_snapshots(ts,broker,equity,cash,positions) VALUES('2026-01-02T00:00:00','ibkr',0,0,'{}')")
    db.set("active_broker", "ibkr")
    assert performance(db) == {"broker": "ibkr", "funded": False}           # no fake -100%
    db.x("INSERT INTO equity_snapshots(ts,broker,equity,cash,positions) VALUES('2026-01-03T00:00:00','ibkr',50000,50000,'{}')")
    db.x("INSERT INTO equity_snapshots(ts,broker,equity,cash,positions) VALUES('2026-01-04T00:00:00','ibkr',51000,1000,'{}')")
    p = performance(db)
    assert p["start_equity"] == 50000 and abs(p["return"] - 0.02) < 1e-9


def test_tests_are_isolated_from_real_broker():
    from atlas.config import settings
    assert settings.broker == "paper"
    import os
    assert os.environ["IBKR_PORT"] == "1"
    from atlas.logsetup import LOG_DIR
    from atlas.config import DATA_DIR
    assert DATA_DIR not in LOG_DIR.parents and LOG_DIR != DATA_DIR / "logs"


def test_contribution_led_rebalancing_never_sells_inside_band():
    from atlas.portfolio import plan_orders
    prices = {"VWRL.L": 100.0, "IGLT.L": 10.0, "ERNS.L": 100.0}
    target = {"VWRL.L": 0.6, "IGLT.L": 0.4, "ERNS.L": 0.0}
    # equities drifted to 63% (inside the 5pp band) and £10k was deposited
    held = {"VWRL.L": 630.0, "IGLT.L": 3700.0}           # £63k + £37k
    orders = plan_orders(target, 110_000, held, prices)
    assert orders and all(o.side == "buy" for o in orders)
    spend = {o.symbol: o.qty * prices[o.symbol] for o in orders}
    assert spend.get("IGLT.L", 0) > spend.get("VWRL.L", 0)  # new money goes to the underweight first
    # beyond the band, a full rebalance (which sells) happens
    held2 = {"VWRL.L": 700.0, "IGLT.L": 3000.0}          # 70/30
    assert any(o.side == "sell" for o in plan_orders(target, 100_000, held2, prices))
