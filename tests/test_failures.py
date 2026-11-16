"""Failure simulation: every scenario must end in a SAFE state (no unintended or duplicate orders,
trading paused or halted, and an event recorded)."""
from atlas import data
from atlas.brokers.ibkr import IBKRBroker
from atlas.engine import cycle
from atlas.execution import emergency_stop, unresolved_orders
from atlas.risk import RiskEngine
from tests.fakes import PENCE, chaos_for

SCALES = {s: 0.01 for s in PENCE}


def _prep(db):
    from atlas.research.lab import run_lab
    run_lab(db)
    db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")


def _cycle(db, fake, **kw):
    return cycle(db, IBKRBroker(ib=fake, scales=SCALES), force_trade=True, ignore_hours=True, **kw)


def _events(db, kind):
    return [r["message"] for r in db.q("SELECT message FROM events WHERE kind=? ORDER BY id", (kind,))]


def test_gateway_down_no_trading_and_alerts_once(db, monkeypatch):
    import atlas.brokers.ibkr as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    _prep(db)
    fake = chaos_for(db)
    fake.refuse_connect = True
    r1 = _cycle(db, fake)
    r2 = _cycle(db, fake)
    assert r1["health"]["ok"] is False and "execution" not in r1 and "execution" not in r2
    lv = [r["level"] for r in db.q("SELECT level FROM events WHERE kind='broker_down' ORDER BY id")]
    assert lv == ["critical", "info"]                     # alert on transition, not every cycle
    fake.refuse_connect = False
    _cycle(db, fake)
    assert _events(db, "broker_up")


def test_connection_lost_after_order_sent_no_duplicate(db):
    _prep(db)
    fake = chaos_for(db)
    fake.drop_after_accept = True
    rep = _cycle(db, fake)
    assert [x["status"] for x in rep["execution"]] == ["submit_unknown"]   # stopped after the first
    assert len(fake._trades) == 1
    fake.drop_after_accept = False
    rep2 = _cycle(db, fake)                                # recovery finds it by atlas ref
    first = db.q("SELECT status, filled_qty FROM orders ORDER BY id LIMIT 1")[0]
    assert first["status"] == "filled"
    refs = [t.order.orderRef for t in fake._trades]
    assert len(refs) == len(set(refs))                    # no reference was ever sent twice
    assert rep2["reconcile"]["ok"]


def test_order_never_fills_pauses_new_orders(db):
    _prep(db)
    fake = chaos_for(db)
    fake.fill_mode = "none"
    import atlas.execution as ex
    orig = ex.execute
    ex_calls = []

    def fast(*a, **k):
        k["timeout_s"] = 0
        ex_calls.append(1)
        return orig(*a, **k)
    import atlas.engine as eng
    eng.execute = fast
    try:
        _cycle(db, fake)
        n = len(fake._trades)
        rep = _cycle(db, fake)
    finally:
        eng.execute = orig
    assert "waiting_on_unresolved_orders" in rep["steps"] and len(fake._trades) == n
    assert unresolved_orders(db, "ibkr")


def test_partial_fill_recorded_exactly_and_reconciles(db):
    _prep(db)
    fake = chaos_for(db)
    fake.fill_mode = "partial"
    import atlas.engine as eng
    import atlas.execution as ex
    orig = ex.execute
    eng.execute = lambda *a, **k: orig(*a, **{**k, "timeout_s": 0})
    try:
        rep = _cycle(db, fake)
    finally:
        eng.execute = orig
    rows = db.q("SELECT qty, filled_qty, status FROM orders")
    assert rows and all(r["status"] == "partially_filled" and 0 < r["filled_qty"] < r["qty"] for r in rows)
    from atlas.execution import reconcile
    assert reconcile(db, IBKRBroker(ib=fake, scales=SCALES))["ok"]


def test_external_trade_detected_and_trading_suspended(db):
    _prep(db)
    fake = chaos_for(db)
    _cycle(db, fake)
    b = IBKRBroker(ib=fake, scales=SCALES)
    fake.external_trade(b.contract("VUKE.L"), 25, 47.0)    # someone trades the account by hand
    n = len(fake._trades)
    rep = _cycle(db, fake)
    assert rep["reconcile"]["ok"] is False
    assert "reconciliation" in (RiskEngine(db).state()["suspended"] or "")
    buys = [t for t in fake._trades[n:] if t.order.action == "BUY" and t.contract.symbol != "ERNS"]
    assert not buys                                       # no risk-increasing orders while suspended


def test_stale_market_data_blocks_buys(db):
    _prep(db)
    db.x("DELETE FROM prices WHERE date >= date('now','-20 day')")
    fake = chaos_for(db)
    rep = _cycle(db, fake)
    assert fake._trades == [] and all("stale" in r["reason"] for r in rep["rejected"])


def test_emergency_stop_cancels_working_orders(db):
    _prep(db)
    fake = chaos_for(db)
    fake.fill_mode = "none"
    import atlas.engine as eng
    import atlas.execution as ex
    orig = ex.execute
    eng.execute = lambda *a, **k: orig(*a, **{**k, "timeout_s": 0})
    try:
        _cycle(db, fake)
    finally:
        eng.execute = orig
    assert fake.openTrades()
    out = emergency_stop(db, IBKRBroker(ib=fake, scales=SCALES), "test")
    assert out["kill_switch"] is True and out["cancelled"] >= 1 and not fake.openTrades()
    n = len(fake._trades)
    _cycle(db, fake)
    assert len(fake._trades) == n                         # halted


def test_emergency_stop_still_halts_when_broker_unreachable(db, monkeypatch):
    import atlas.brokers.ibkr as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    fake = chaos_for(db)
    fake.refuse_connect = True
    out = emergency_stop(db, IBKRBroker(ib=fake), "test")
    assert out["kill_switch"] is True and out["error"]
    assert "FAILED" in _events(db, "emergency_stop")[0]


def test_strategy_exception_places_no_orders(db, monkeypatch):
    _prep(db)
    import atlas.engine as eng
    monkeypatch.setattr(eng, "target_weights", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad model")))
    fake = chaos_for(db)
    try:
        _cycle(db, fake)
    except ValueError:
        pass
    assert fake._trades == []


def test_second_worker_lock_refused(tmp_path, monkeypatch):
    import atlas.worker as w
    from atlas.config import settings
    monkeypatch.setattr(settings, "db_path", tmp_path / "x.db")
    monkeypatch.setattr(w, "_lock_handle", None)
    assert w._single_instance() is True
    first = w._lock_handle
    assert w._single_instance() is False                  # a second handle cannot take the lock
    first.close()


def test_account_switch_paper_to_live_mid_session_halts(db):
    _prep(db)
    fake = chaos_for(db)
    _cycle(db, fake)
    fake.account = "U5555555"                              # gateway re-logged into a LIVE account
    n = len(fake._trades)
    rep = _cycle(db, fake)
    assert rep["blocked"] == "live_or_unverified_account" and db.get("kill_switch") is True
    assert len(fake._trades) == n
    assert data.latest_prices_gbp(db)[0]                  # platform state intact


def test_withdrawal_is_not_a_loss_and_deposit_is_not_profit(db):
    """External cash flows are detected; they shift risk baselines and are excluded from returns."""
    from atlas.cashflow import time_weighted
    from atlas.engine import snapshot
    from atlas.reporting import performance
    _prep(db)
    fake = chaos_for(db, gbp_cash=100_000.0)
    b = IBKRBroker(ib=fake, scales=SCALES)
    prices, _ = data.latest_prices_gbp(db)
    r = RiskEngine(db)
    db.set("active_broker", "ibkr")
    s = snapshot(db, b, prices)
    r.update_equity(s["equity"])
    fake.cash -= 30_000                                   # investor withdraws £30k (-30%)
    s = snapshot(db, b, prices)
    assert s["external_flow"] == -30_000
    r.update_equity(s["equity"])
    assert not r.state()["suspended"]                     # no false drawdown / daily-loss stop
    fake.cash += 50_000                                   # deposit
    s = snapshot(db, b, prices)
    assert s["external_flow"] == 50_000
    tw = time_weighted(db, "ibkr")
    assert abs(tw["twr"]) < 1e-9 and tw["net_external_flows"] == 20_000
    assert abs(performance(db)["twr"]) < 1e-9
