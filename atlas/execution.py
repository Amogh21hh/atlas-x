"""Execution: submits ONLY risk-approved orders, records broker-confirmed status, reconciles positions."""
from __future__ import annotations

import time

from .brokers.base import Broker
from .db import DB, now
from .risk import ApprovedOrder, RiskEngine

TERMINAL = {"filled", "cancelled", "rejected"}


def execute(db: DB, broker: Broker, risk: RiskEngine, approved: list[ApprovedOrder],
            timeout_s: float = 60.0, collar: float | None = None) -> list[dict]:
    """`collar`: if set (live), each order is a limit order at most `collar` through its reference
    price, so a gap or bad print can never fill far from the price the risk engine approved."""
    results = []
    for a in approved:
        if not isinstance(a, ApprovedOrder):
            raise PermissionError("execution refused: order not approved by risk engine")
        o = a.order
        oid = db.x("INSERT INTO orders(created_at,broker,symbol,side,qty,ref_price,status,risk_decision,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (now(), broker.name, o.symbol, o.side, o.qty, o.ref_price, "pending_submit",
                    "approved: " + ",".join(a.checks), now()))
        limit = None
        if o.symbol.startswith("GILT:"):      # gilts are always collared limit orders
            c = collar or 0.005
            limit = o.ref_price * (1 + c) if o.side == "buy" else o.ref_price * (1 - c)
        elif collar:
            limit = o.ref_price * (1 + collar) if o.side == "buy" else o.ref_price * (1 - collar)
        try:
            st = broker.submit_market(o.symbol, o.side, o.qty, o.ref_price, ref=f"atlas-{oid}", limit_price=limit)
        except Exception as e:
            # The broker may or may not have accepted it. Treat as UNRESOLVED: new orders pause and the
            # next cycle looks it up by its atlas ref. Never assume it failed, never resubmit.
            db.x("UPDATE orders SET status=?, updated_at=? WHERE id=?", ("submit_unknown", now(), oid))
            db.event("critical", "order_error", f"{o.side} {o.qty} {o.symbol}: submit outcome unknown ({e}); "
                     "new orders paused until it is found at the broker")
            results.append({"id": oid, "status": "submit_unknown", "error": str(e)})
            break  # stop submitting further orders this cycle
        t0 = time.time()
        while st.status not in TERMINAL and time.time() - t0 < timeout_s:
            time.sleep(2)
            st = broker.order_status(st.broker_order_id)
        db.x("UPDATE orders SET broker_order_id=?, status=?, filled_qty=?, avg_fill_price=?, commission=?, updated_at=? "
             "WHERE id=?", (st.broker_order_id, st.status, st.filled_qty, st.avg_fill_price, st.commission, now(), oid))
        if st.filled_qty:
            risk.record_turnover(st.filled_qty * (st.avg_fill_price or o.ref_price))
        level = "info" if st.status == "filled" else "warn"
        db.event(level, "order", f"{o.side} {o.qty:g} {o.symbol} -> {st.status} "
                 f"({st.filled_qty:g} @ {st.avg_fill_price or 0:.4f}) {st.message}")
        results.append({"id": oid, "status": st.status, "filled": st.filled_qty, "price": st.avg_fill_price})
    return results


def refresh_open_orders(db: DB, broker: Broker) -> None:
    """Recovery after restart: re-query any order not in a terminal state."""
    for r in db.q("SELECT * FROM orders WHERE broker=? AND status NOT IN ('filled','cancelled','rejected','submit_error','resolved_manual')",
                  (broker.name,)):
        bid = r["broker_order_id"]
        if not bid:
            # Crashed between submit and recording the broker id: look the order up by our own ref.
            found = broker.find_by_ref(f"atlas-{r['id']}")
            if found is None:
                db.x("UPDATE orders SET status='submit_unknown', updated_at=? WHERE id=?", (now(), r["id"]))
                db.event("warn", "recovery", f"order {r['id']} not found at broker by ref; new orders "
                         "paused until it is resolved (check the broker, then mark it cancelled)")
                continue
            bid = found.broker_order_id
        st = broker.order_status(bid)
        db.x("UPDATE orders SET broker_order_id=?, status=?, filled_qty=?, avg_fill_price=?, updated_at=? WHERE id=?",
             (bid, st.status, st.filled_qty, st.avg_fill_price, now(), r["id"]))


UNRESOLVED = ("pending_submit", "submitted", "partially_filled", "submit_unknown", "unknown")


def emergency_stop(db: DB, broker: Broker | None, by: str) -> dict:
    """Engage the kill switch FIRST (so nothing new can be approved), then cancel every working
    order at the broker. Positions are left as they are (selling is a separate, explicit decision)."""
    RiskEngine(db).set_kill_switch(True, f"emergency stop by {by}")
    out = {"kill_switch": True, "cancelled": None, "error": None}
    if broker is not None:
        try:
            out["cancelled"] = broker.cancel_all()
        except Exception as e:
            out["error"] = str(e)
    db.event("critical", "emergency_stop", f"EMERGENCY STOP by {by}: kill switch on; broker cancel-all: "
             f"{out['cancelled'] if out['error'] is None else 'FAILED - ' + out['error'] + ' (cancel in IB Gateway/TWS)'}")
    return out


def resolve_order_manually(db: DB, order_id: int, by: str, note: str) -> None:
    """For an order the platform cannot find at the broker, after a human has checked the broker."""
    db.x("UPDATE orders SET status='resolved_manual', risk_decision=COALESCE(risk_decision,'')||?, updated_at=? "
         "WHERE id=?", (f" | resolved manually by {by}: {note}", now(), order_id))
    db.event("warn", "order_resolved_manual", f"order #{order_id} resolved manually by {by}: {note}")


def unresolved_orders(db: DB, broker_name: str) -> list:
    ph = ",".join("?" * len(UNRESOLVED))
    return db.q(f"SELECT id, symbol, side, qty, status FROM orders WHERE broker=? AND status IN ({ph})",
                (broker_name, *UNRESOLVED))


def reconcile(db: DB, broker: Broker, tol: float = 1e-6) -> dict:
    """Broker positions vs. the ledger implied by our confirmed fills. Mismatch => warn (the broker is
    the source of truth; the platform never overwrites broker state)."""
    acct = broker.account()
    ledger: dict[str, float] = {}
    base = db.get(f"recon_base_{broker.name}", {}) or {}
    for s, q in base.items():
        ledger[s] = q
    for r in db.q("SELECT symbol, side, filled_qty FROM orders WHERE broker=? AND filled_qty > 0", (broker.name,)):
        ledger[r["symbol"]] = ledger.get(r["symbol"], 0.0) + (r["filled_qty"] if r["side"] == "buy" else -r["filled_qty"])
    diffs = {}
    for s in set(ledger) | set(acct.positions):
        b = acct.positions[s].qty if s in acct.positions else 0.0
        l = ledger.get(s, 0.0)
        if abs(b - l) > tol:
            diffs[s] = {"broker": b, "ledger": l}
    if diffs:
        db.event("warn", "reconciliation", f"position mismatch vs broker: {diffs}")
    else:
        db.set("last_reconcile_ok", {"at": now(), "broker": broker.name})
    db.set("last_reconcile", {"at": now(), "broker": broker.name, "ok": not diffs})
    return {"ok": not diffs, "diffs": diffs}
