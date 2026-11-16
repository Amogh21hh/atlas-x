"""External cash-flow detection (deposits / withdrawals) and time-weighted performance.

Between two snapshots of the same broker, cash should change only by what our own fills explain
(sale proceeds minus purchases minus commissions) plus small income (dividends, interest). Any larger
unexplained change is an external deposit or withdrawal. It is recorded, announced, and the risk
baselines (peak, start-of-day equity) are shifted by it, so:
  * a withdrawal is not mistaken for a trading loss (no false drawdown stop), and
  * a deposit is not mistaken for profit (no inflated returns).
"""
from __future__ import annotations

from .db import DB


def threshold(prev_equity: float) -> float:
    return max(100.0, 0.005 * max(prev_equity, 0.0))


def detect(db: DB, broker: str, ts: str, cash_now: float) -> float:
    prev = db.q("SELECT ts, cash, equity, positions FROM equity_snapshots WHERE broker=? ORDER BY ts DESC LIMIT 1",
                (broker,))
    accounted = db.get(f"flow_accounted_{broker}", {}) or {}
    explained = 0.0
    for r in db.q("SELECT id, side, filled_qty, avg_fill_price, commission FROM orders "
                  "WHERE broker=? AND filled_qty > 0", (broker,)):
        done = float(accounted.get(str(r["id"]), 0.0))
        new_qty = float(r["filled_qty"]) - done
        if new_qty <= 1e-12:
            continue
        px = float(r["avg_fill_price"] or 0.0)
        comm = float(r["commission"] or 0.0) if done == 0 else 0.0
        explained += (new_qty * px - comm) if r["side"] == "sell" else -(new_qty * px + comm)
        accounted[str(r["id"])] = float(r["filled_qty"])
    db.set(f"flow_accounted_{broker}", accounted)
    if not prev:
        return 0.0
    try:  # gilt coupons paid since the last snapshot are income, not deposits
        import json
        from datetime import date as _d

        from .sleeve import expected_coupon_cash
        explained += expected_coupon_cash(db, json.loads(prev[0]["positions"] or "{}"),
                                          _d.fromisoformat(prev[0]["ts"][:10]), _d.fromisoformat(ts[:10]))
    except Exception:
        pass
    flow = cash_now - (float(prev[0]["cash"]) + explained)
    if abs(flow) < threshold(float(prev[0]["equity"])):
        return 0.0          # small differences = dividends, interest, fees: part of performance
    kind = "deposit" if flow > 0 else "withdrawal"
    db.x("INSERT INTO cash_flows(ts,broker,amount,note) VALUES(?,?,?,?)", (ts, broker, flow, kind))
    for k in ("peak_equity", "day_start_equity"):
        v = db.get(k)
        if v is not None:
            db.set(k, max(0.0, float(v) + flow))
    db.event("warn", "cash_flow", f"{kind} of £{abs(flow):,.2f} detected on {broker}; performance and loss "
             "limits adjusted so it is not counted as profit or loss")
    return flow


def time_weighted(db: DB, broker: str) -> dict:
    """Chain-linked return excluding external flows (the standard way to measure a manager)."""
    snaps = db.q("SELECT ts, equity FROM equity_snapshots WHERE broker=? ORDER BY ts", (broker,))
    rows = db.q("SELECT ts, amount FROM cash_flows WHERE broker=?", (broker,))
    flows: dict[str, float] = {}
    for r in rows:
        flows[r["ts"]] = flows.get(r["ts"], 0.0) + float(r["amount"])
    growth, prev, started = 1.0, None, None
    peak_g, mdd = 1.0, 0.0
    for s in snaps:
        e = float(s["equity"])
        if prev is not None and prev > 0:
            growth *= (e - flows.get(s["ts"], 0.0)) / prev
            peak_g = max(peak_g, growth)
            mdd = min(mdd, growth / peak_g - 1)
        if e > 0 and started is None:
            started = s["ts"]
        prev = e if e > 0 else prev
    net_flows = sum(flows.values())
    return {"twr": growth - 1 if started else None, "max_dd": mdd, "net_external_flows": net_flows,
            "since": started}
