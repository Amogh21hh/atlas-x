"""IBKR adapter tested against an in-memory fake IB Gateway (real ib_async contract/order/trade types)."""
from types import SimpleNamespace

import pytest
from ib_async import OrderStatus as IBOrderStatus
from ib_async import Trade

from atlas import data
from atlas.brokers.ibkr import IBKRBroker
from atlas.config import CASH_PROXY, UNIVERSE
from atlas.engine import cycle
from atlas.risk import ProposedOrder, RiskEngine

PENCE = {"EMIM.L", "IUKP.L", "SGLN.L"}


class FakeIB:
    """Minimal stand-in for ib_async.IB: fills market orders instantly at the quoted price."""

    def __init__(self, account="DU1234567", gbp_cash=50_000.0, prices=None):
        self.connected = False
        self.account = account
        self.cash = gbp_cash
        self.prices = prices or {}           # symbol(.L) -> price in IBKR units
        self.pos = {}                        # conId -> [contract, qty, avgCost]
        self.next_id = 100
        self._trades = []
        self.connect_calls = []

    # connection
    def isConnected(self): return self.connected
    def connect(self, host, port, clientId, timeout): self.connected = True; self.connect_calls.append(clientId)
    def disconnect(self): self.connected = False
    def managedAccounts(self): return [self.account]
    def sleep(self, s): pass

    # contracts / data
    def qualifyContracts(self, c):
        sym = c.symbol + ".L"
        # mirror the real gateway: SMART-routed with the LSE ETF line as primary listing
        if sym not in UNIVERSE or c.exchange != "SMART" or c.primaryExchange != "LSEETF":
            return []
        c.conId = 1000 + list(UNIVERSE).index(sym)
        return [c]

    def reqMarketDataType(self, t): pass

    def reqTickers(self, c):
        p = self.prices[c.symbol + ".L"]
        return [SimpleNamespace(marketPrice=lambda: p, last=p, close=p)]

    # account
    def accountValues(self, acct):
        return [SimpleNamespace(tag="TotalCashValue", currency="GBP", value=str(self.cash)),
                SimpleNamespace(tag="TotalCashValue", currency="USD", value="999999")]

    def positions(self, acct):
        return [SimpleNamespace(contract=c, position=q, avgCost=a) for c, q, a in self.pos.values()]

    # orders
    def placeOrder(self, contract, order):
        order.orderId = self.next_id; order.permId = 9000 + self.next_id; self.next_id += 1
        px = self.prices[contract.symbol + ".L"]
        qty = order.totalQuantity
        sign = 1 if order.action == "BUY" else -1
        unit = 0.01 if contract.symbol + ".L" in PENCE else 1.0
        self.cash -= sign * qty * px * unit + 3.0
        c, q, a = self.pos.get(contract.conId, [contract, 0.0, 0.0])
        newq = q + sign * qty
        self.pos[contract.conId] = [c, newq, (a * q + px * qty) / newq if sign > 0 and newq else a]
        st = IBOrderStatus(orderId=order.orderId, status="Filled", filled=qty, remaining=0, avgFillPrice=px)
        fill = SimpleNamespace(commissionReport=SimpleNamespace(commission=3.0))
        t = Trade(contract=contract, order=order, orderStatus=st, fills=[fill], log=[])
        self._trades.append(t)
        return t

    def trades(self): return self._trades
    def reqCompletedOrders(self, apiOnly): return []
    def cancelOrder(self, o): pass


def _fake_for(db, **kw):
    ref, _ = data.latest_prices_gbp(db)
    prices = {s: (p * 100 if s in PENCE else p) for s, p in ref.items()}  # IBKR quotes pence lines in pence
    return FakeIB(prices=prices, **kw)


def test_paper_detection_contracts_and_scale(db):
    fake = _fake_for(db)
    b = IBKRBroker(ib=fake, client_id=19)
    h = b.health()
    assert h["ok"] and h["live"] is False and b.is_live is False
    assert b.contract("VWRL.L").conId > 0
    # scale detection logic (as used by ibkr-check)
    ref, _ = data.latest_prices_gbp(db)
    for s in UNIVERSE:
        ratio = b.quote(s) / ref[s]
        assert (80 < ratio < 125) == (s in PENCE)


def test_live_account_detected_and_blocked_by_risk(db):
    b = IBKRBroker(ib=_fake_for(db, account="U7654321"))
    assert b.is_live is True
    prices, dates = data.latest_prices_gbp(db)
    rep = RiskEngine(db).review([ProposedOrder("VWRL.L", "buy", 1, prices["VWRL.L"])], 1e5, 1e5, {}, dates, b.is_live)
    assert not rep.approved and "paper-only" in rep.rejected[0][1]


def test_unknown_account_type_fails_safe_to_live():
    class Broken(FakeIB):
        def connect(self, *a, **k): raise ConnectionRefusedError("gateway down")
    b = IBKRBroker(ib=Broken())
    assert b.is_live is True
    assert b.health()["ok"] is False


def test_orders_fills_in_pounds_and_cash_is_gbp_only(db):
    fake = _fake_for(db)
    b = IBKRBroker(ib=fake, scales={"EMIM.L": 0.01})
    assert b.account().cash == 50_000.0               # USD line ignored
    ref, _ = data.latest_prices_gbp(db)
    st = b.submit_market("EMIM.L", "buy", 10, ref["EMIM.L"])
    assert st.status == "filled" and st.filled_qty == 10
    assert st.avg_fill_price == pytest.approx(ref["EMIM.L"])   # pence converted to pounds
    pos = b.account().positions["EMIM.L"]
    assert pos.qty == 10 and pos.avg_cost == pytest.approx(ref["EMIM.L"])
    assert b.submit_market("EMIM.L", "buy", 1.5, ref["EMIM.L"]).status == "rejected"  # whole shares only
    # status lookup after a "restart" (fresh adapter, same gateway)
    b2 = IBKRBroker(ib=fake, scales={"EMIM.L": 0.01})
    assert b2.order_status(st.broker_order_id).status == "filled"
    assert b2.order_status("424242").status == "unknown"


def test_full_cycle_on_fake_ibkr_paper(db):
    from atlas.research.lab import run_lab
    run_lab(db)
    db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")
    db.set("peak_equity", 10_000_000.0)          # stale peak from the internal paper account
    db.set("active_broker", "paper")
    fake = _fake_for(db, gbp_cash=100_000.0)
    scales = {s: 0.01 for s in PENCE}
    rep = cycle(db, IBKRBroker(ib=fake, scales=scales), force_trade=True, ignore_hours=True)
    assert rep["health"]["ok"] and rep["health"]["live"] is False
    assert not RiskEngine(db).state()["suspended"]      # baseline reset on broker switch
    assert rep["reconcile"]["ok"], rep["reconcile"]
    assert rep["execution"] and all(x["status"] == "filled" for x in rep["execution"])
    held = IBKRBroker(ib=fake, scales=scales).account().positions
    assert set(held) - {CASH_PROXY} <= set(UNIVERSE)
    assert 0 < fake.cash < 100_000                      # no leverage: GBP cash never negative


# ---------------- paper-only lock, recovery, reconnection ----------------
def test_live_account_engages_kill_switch_and_never_trades(db):
    from atlas.research.lab import run_lab
    run_lab(db)
    db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")
    fake = _fake_for(db, account="U7654321", gbp_cash=100_000.0)
    rep = cycle(db, IBKRBroker(ib=fake), force_trade=True, ignore_hours=True)
    assert rep.get("blocked") == "live_or_unverified_account"
    assert RiskEngine(db).state()["kill_switch"] is True
    assert fake._trades == []                                   # nothing reached the gateway


def test_adapter_refuses_orders_on_non_paper_account(db):
    fake = _fake_for(db, account="U7654321")
    st = IBKRBroker(ib=fake).submit_market("ERNS.L", "buy", 1, 100.0)
    assert st.status == "rejected" and "paper-only" in st.message and fake._trades == []


def test_unverified_account_type_is_rejected_by_risk(db):
    prices, dates = data.latest_prices_gbp(db)
    rep = RiskEngine(db).review([ProposedOrder("VWRL.L", "buy", 1, prices["VWRL.L"])], 1e5, 1e5, {}, dates, None)
    assert not rep.approved and "paper-only" in rep.rejected[0][1]


def test_reconnects_after_transient_gateway_failure(db, monkeypatch):
    import atlas.brokers.ibkr as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    class Flaky(FakeIB):
        fails = 2
        def connect(self, host, port, clientId, timeout):
            if Flaky.fails:
                Flaky.fails -= 1
                raise ConnectionRefusedError("gateway restarting")
            super().connect(host, port, clientId, timeout)

    b = IBKRBroker(ib=Flaky(prices={}))
    assert b.health()["ok"] is True                              # succeeded on the 3rd attempt


def test_restart_recovery_by_ref_and_no_duplicate_orders(db):
    """Crash after the broker accepted an order but before its id was saved: the order is found by
    its ATLAS ref, the ledger is completed, and no second order is sent."""
    from atlas.db import now
    from atlas.execution import refresh_open_orders, unresolved_orders
    fake = _fake_for(db)
    b = IBKRBroker(ib=fake)
    ref, _ = data.latest_prices_gbp(db)
    oid = db.x("INSERT INTO orders(created_at,broker,symbol,side,qty,ref_price,status,updated_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (now(), "ibkr", "VWRL.L", "buy", 3, ref["VWRL.L"], "pending_submit", now()))
    b.submit_market("VWRL.L", "buy", 3, ref["VWRL.L"], ref=f"atlas-{oid}")   # reached broker, then "crash"
    b2 = IBKRBroker(ib=fake)                                                   # restart
    refresh_open_orders(db, b2)
    row = db.q("SELECT status, filled_qty, broker_order_id FROM orders WHERE id=?", (oid,))[0]
    assert row["status"] == "filled" and row["filled_qty"] == 3 and row["broker_order_id"]
    assert unresolved_orders(db, "ibkr") == []
    assert len(fake._trades) == 1


def test_unresolved_order_pauses_new_orders(db):
    from atlas.db import now
    from atlas.research.lab import run_lab
    run_lab(db)
    db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")
    fake = _fake_for(db, gbp_cash=100_000.0)
    db.x("INSERT INTO orders(created_at,broker,symbol,side,qty,ref_price,status,updated_at) "
         "VALUES(?,?,?,?,?,?,?,?)", (now(), "ibkr", "VWRL.L", "buy", 3, 100.0, "pending_submit", now()))
    rep = cycle(db, IBKRBroker(ib=fake, scales={s: 0.01 for s in PENCE}), force_trade=True, ignore_hours=True)
    assert "waiting_on_unresolved_orders" in rep["steps"]
    assert fake._trades == []
