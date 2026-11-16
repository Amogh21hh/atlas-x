"""Performance attribution and automated daily reports."""
from __future__ import annotations

import json
from datetime import date

from .config import DATA_DIR, UNIVERSE
from .db import DB


def attribution(positions: dict) -> dict:
    by_pos, by_cls = {}, {}
    for s, p in positions.items():
        pnl = p["value"] - p["qty"] * p["avg_cost"]
        by_pos[s] = {"unrealised_pnl": pnl, "value": p["value"]}
        c = UNIVERSE.get(s, {}).get("cls", "other")
        by_cls[c] = by_cls.get(c, 0.0) + pnl
    realised_costs = 0.0
    return {"by_position": by_pos, "by_class": by_cls, "costs": realised_costs}


def active_broker(db: DB) -> str:
    from .config import settings
    return db.get("active_broker") or db.get("broker", settings.broker)


def performance(db: DB, broker: str | None = None) -> dict:
    """Broker-confirmed performance of ONE account (never mixes the internal simulator with IBKR).
    Starts at the first funded snapshot. Not adjusted for later deposits/withdrawals."""
    broker = broker or active_broker(db)
    rows = db.q("SELECT ts, equity FROM equity_snapshots WHERE broker=? AND equity > 0 ORDER BY ts", (broker,))
    if not rows:
        return {"broker": broker, "funded": False}
    eq = [r["equity"] for r in rows]
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    comm = db.q("SELECT COALESCE(SUM(commission),0) c, COUNT(*) n FROM orders WHERE status='filled' AND broker=?",
                (broker,))[0]
    from .cashflow import time_weighted
    tw = time_weighted(db, broker)
    return {"broker": broker, "funded": True, "twr": tw["twr"], "twr_max_dd": tw["max_dd"],
            "net_external_flows": tw["net_external_flows"], "start_equity": eq[0], "equity": eq[-1], "return": eq[-1] / eq[0] - 1, "max_dd": mdd,
            "since": rows[0]["ts"], "commissions_paid": comm["c"], "fills": comm["n"],
            "tracked_days": len({r["ts"][:10] for r in rows})}


def daily_report(db: DB) -> str:
    snap = db.q("SELECT * FROM equity_snapshots WHERE broker=? ORDER BY ts DESC LIMIT 1", (active_broker(db),))
    perf = performance(db)
    ev = db.q("SELECT ts, level, kind, message FROM events WHERE ts >= ? ORDER BY id", (date.today().isoformat(),))
    lines = [f"# ATLAS X daily report — {date.today()}", ""]
    if snap:
        s = snap[0]
        lines += [f"Broker: {s['broker']}  Equity: £{s['equity']:,.2f}  Cash: £{s['cash']:,.2f}", ""]
        for sym, p in json.loads(s["positions"]).items():
            lines.append(f"- {sym}: {p['qty']:g} @ £{p['price']:.4f} = £{p['value']:,.2f}")
    if perf:
        lines += ["", f"Since {perf['since'][:10]}: return {perf['return']:.2%}, max DD {perf['max_dd']:.2%}, "
                      f"commissions £{perf['commissions_paid']:.2f} over {perf['fills']} fills"]
    lines += ["", "## Events today"] + [f"- {e['ts'][11:19]} [{e['level']}] {e['kind']}: {e['message']}" for e in ev]
    out = DATA_DIR / "reports"
    out.mkdir(exist_ok=True)
    text = "\n".join(lines) + "\n"
    (out / f"daily-{date.today()}.md").write_text(text, encoding="utf-8")
    return text
