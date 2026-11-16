"""Independent evaluation: the account's actual result versus simple alternatives the investor could
have had with no system at all. Benchmarks are computed from market prices, not from ATLAS's own
accounting, so the system cannot mark its own homework.
"""
from __future__ import annotations

from .cashflow import time_weighted
from .db import DB


def _bench(db: DB, symbol_weights: dict[str, float], since: str) -> float | None:
    """Buy-and-hold total return of a fixed mix from `since` to the latest price (no rebalancing)."""
    total = 0.0
    for sym, w in symbol_weights.items():
        a = db.q("SELECT adjclose FROM prices WHERE symbol=? AND date>=? ORDER BY date LIMIT 1", (sym, since[:10]))
        b = db.q("SELECT adjclose FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,))
        if not a or not b:
            return None
        total += w * (b[0]["adjclose"] / a[0]["adjclose"] - 1)
    return total


def evaluate(db: DB, broker: str, target: dict[str, float] | None = None) -> dict:
    tw = time_weighted(db, broker)
    if not tw["since"]:
        return {"status": "no funded history yet"}
    since = tw["since"]
    target = {k: v for k, v in (target or {"VWRL.L": 0.6, "IGLT.L": 0.4}).items() if v > 0}
    rows = [
        {"name": "ATLAS X (actual, after costs, excluding deposits/withdrawals)", "return": tw["twr"]},
        {"name": "Cash alternative (sterling ultrashort bond fund ERNS)", "return": _bench(db, {"ERNS.L": 1.0}, since)},
        {"name": "Same target mix bought once, never traded", "return": _bench(db, target, since)},
        {"name": "Global shares only (VWRL), bought once", "return": _bench(db, {"VWRL.L": 1.0}, since)},
    ]
    days = db.q("SELECT COUNT(DISTINCT substr(ts,1,10)) n FROM equity_snapshots WHERE broker=? AND equity>0", (broker,))[0]["n"]
    costs = db.q("SELECT COALESCE(SUM(commission),0) c FROM orders WHERE broker=? AND filled_qty>0", (broker,))[0]["c"]
    return {
        "since": since, "tracked_days": days, "rows": rows, "commissions_paid": costs,
        "max_drawdown": tw["max_dd"],
        "meaningful": days >= 250,
        "caveat": ("Less than about a year of results: differences are mostly noise and say nothing reliable "
                   "about future performance." if days < 250 else
                   "Past results do not predict future returns."),
    }
