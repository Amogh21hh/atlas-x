"""Paper broker: persistent simulated account priced from REAL latest market closes.

Fills are simulated at the latest real close +/- spread, with commission (max(min, bps)).
It is labelled PAPER everywhere and can never be mistaken for a live account (is_live=False).
"""
from __future__ import annotations

import uuid

from ..config import settings
from ..db import DB
from .base import Account, Broker, OrderStatus, Position


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, db: DB, price_fn):
        self.db = db
        self.price_fn = price_fn  # () -> dict[symbol, GBP price]
        if not db.q("SELECT 1 FROM paper_account WHERE id=1"):
            db.x("INSERT INTO paper_account(id,cash) VALUES(1,?)", (settings.paper_starting_cash,))
        self._orders: dict[str, OrderStatus] = {}

    def health(self) -> dict:
        return {"ok": True, "broker": "paper", "live": False}

    def account(self) -> Account:
        cash = self.db.q("SELECT cash FROM paper_account WHERE id=1")[0]["cash"]
        pos = {r["symbol"]: Position(r["symbol"], r["qty"], r["avg_cost"])
               for r in self.db.q("SELECT * FROM paper_positions WHERE qty != 0")}
        return Account(cash=cash, positions=pos)

    def submit_market(self, symbol: str, side: str, qty: float, ref_price: float,
                      ref: str | None = None, limit_price: float | None = None) -> OrderStatus:
        oid = "P-" + uuid.uuid4().hex[:12]
        px = self.price_fn().get(symbol)
        if px is None or qty <= 0:
            st = OrderStatus(oid, "rejected", message="no price or bad qty")
            self._orders[oid] = st
            return st
        slip = settings.spread_bps / 1e4
        fill = px * (1 + slip) if side == "buy" else px * (1 - slip)
        if limit_price is not None and ((side == "buy" and fill > limit_price) or (side == "sell" and fill < limit_price)):
            st = OrderStatus(oid, "cancelled", message=f"limit {limit_price:.4f} not reached (fill would be {fill:.4f})")
            self._orders[oid] = st
            return st
        notional = fill * qty
        comm = max(settings.commission_min_gbp, notional * settings.commission_bps / 1e4)
        acct = self.account()
        if side == "buy" and notional + comm > acct.cash + 1e-6:
            st = OrderStatus(oid, "rejected", message="insufficient cash")
            self._orders[oid] = st
            return st
        held = acct.positions.get(symbol)
        if side == "sell" and (held is None or held.qty < qty - 1e-9):
            st = OrderStatus(oid, "rejected", message="insufficient position (no shorting)")
            self._orders[oid] = st
            return st
        cash_delta = -(notional + comm) if side == "buy" else (notional - comm)
        self.db.x("UPDATE paper_account SET cash = cash + ? WHERE id=1", (cash_delta,))
        if side == "buy":
            old_q = held.qty if held else 0.0
            old_c = held.avg_cost if held else 0.0
            new_q = old_q + qty
            new_c = (old_q * old_c + notional) / new_q
        else:
            new_q = held.qty - qty
            new_c = held.avg_cost
        self.db.x("INSERT INTO paper_positions(symbol,qty,avg_cost) VALUES(?,?,?) "
                  "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_cost=excluded.avg_cost",
                  (symbol, new_q, new_c))
        st = OrderStatus(oid, "filled", qty, fill, comm)
        self._orders[oid] = st
        return st

    def order_status(self, broker_order_id: str) -> OrderStatus:
        return self._orders.get(broker_order_id, OrderStatus(broker_order_id, "unknown"))
