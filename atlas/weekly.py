"""Weekly profit and loss, plus an honest check of a weekly return goal.

Weeks run Monday to Sunday (UK). For each week:
  start  = account value at the end of the previous week (or the first snapshot)
  end    = last account value in the week
  flows  = deposits minus withdrawals detected that week (cashflow.detect)
  pnl    = end - start - flows          (money made or lost, NOT counting your own deposits)
  fees   = broker commissions on orders created that week (already inside pnl)
  return = pnl / (start + deposits)     (simple, so a first deposit week is measured fairly)
Cumulative return chains the weekly returns (time-weighted), so deposits never look like profit.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .db import DB

RESULTS = Path(__file__).resolve().parent.parent / "research" / "results" / "exp_weekly_target.json"


def _week(ts: str) -> str:
    d = datetime.fromisoformat(ts[:10])
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")  # Monday


def weekly(db: DB, broker: str) -> dict:
    snaps = db.q("SELECT ts, equity FROM equity_snapshots WHERE broker=? ORDER BY ts", (broker,))
    flows: dict[str, float] = {}
    for r in db.q("SELECT ts, amount FROM cash_flows WHERE broker=?", (broker,)):
        flows[_week(r["ts"])] = flows.get(_week(r["ts"]), 0.0) + float(r["amount"])
    fees: dict[str, float] = {}
    for r in db.q("SELECT created_at, commission FROM orders WHERE broker=? AND filled_qty > 0", (broker,)):
        fees[_week(r["created_at"])] = fees.get(_week(r["created_at"]), 0.0) + float(r["commission"] or 0)

    ends: dict[str, float] = {}
    first: dict[str, float] = {}
    for s in snaps:
        w = _week(s["ts"])
        first.setdefault(w, float(s["equity"]))
        ends[w] = float(s["equity"])

    rows, prev_end, growth, total_pnl = [], None, 1.0, 0.0
    for w in sorted(ends):
        f = flows.get(w, 0.0)
        start = prev_end if prev_end is not None else first[w] - f
        start = max(start, 0.0)
        end = ends[w]
        pnl = end - start - f
        base = start + max(f, 0.0)
        ret = pnl / base if base > 0 else None
        if ret is not None:
            growth *= 1 + ret
        total_pnl += pnl
        rows.append({"week_of": w, "start": round(start, 2), "deposits": round(f, 2), "end": round(end, 2),
                     "pnl": round(pnl, 2), "fees": round(fees.get(w, 0.0), 2),
                     "return": None if ret is None else round(ret, 5)})
        prev_end = end
    n = sum(1 for r in rows if r["return"] is not None)
    return {"weeks": rows, "total_pnl": round(total_pnl, 2), "total_fees": round(sum(fees.values()), 2),
            "cumulative_return": round(growth - 1, 5) if n else None,
            "avg_weekly_return": round(growth ** (1 / n) - 1, 5) if n else None,
            "best_week": max((r["return"] for r in rows if r["return"] is not None), default=None),
            "worst_week": min((r["return"] for r in rows if r["return"] is not None), default=None)}


def goal_check(goal_weekly: float = 0.30, start_gbp: float = 100.0) -> dict:
    """What a weekly goal implies, against the evidence from research/exp_weekly_target.py."""
    implied = {f"{n}_weeks": round(start_gbp * (1 + goal_weekly) ** n, 0) for n in (4, 12, 26, 52)}
    out = {"goal_weekly": goal_weekly, "start_gbp": start_gbp, "if_achieved": implied,
           "atlas_long_run_weekly": None, "evidence": None}
    if RESULTS.exists():
        a = json.loads(RESULTS.read_text())["assets"]
        out["evidence"] = [
            {"name": v["name"], "symbol": k, "pct_weeks_ge_goal": v["pct_weeks_ge_30"],
             "best_week": v["best_week_pct"], "worst_week": v["worst_week_pct"],
             "longest_run": v["longest_run_of_30pct_weeks"], "avg_week": v["avg_week_compounded_pct"]}
            for k, v in a.items() if "error" not in v]
        if "VWRL.L" in a and "IGLT.L" in a:
            out["atlas_long_run_weekly"] = round(
                0.6 * a["VWRL.L"]["avg_week_compounded_pct"] + 0.4 * a["IGLT.L"]["avg_week_compounded_pct"], 2)
    yearly = (1 + goal_weekly) ** 52 - 1
    g, s, y = f"{goal_weekly:.0%}" if goal_weekly >= 0.01 else f"{goal_weekly:.2%}", f"£{start_gbp:,.0f}", \
        f"£{implied['52_weeks']:,.0f}"
    if yearly > 1.0:  # more than doubling every year
        out["verdict"] = (
            f"Not achievable by any legitimate method. {g} a week would turn {s} into about {y} in one "
            "year. Across every asset tested since 2014, including Bitcoin, Solana and Nvidia, none ever "
            "managed more than 3 weeks of +30% in a row, and the same assets also had single weeks of "
            "-35% to -52%. Anything chasing this goal is gambling and is far more likely to lose the "
            "money than grow it.")
    elif yearly > 0.15:
        out["verdict"] = (f"{g} a week is about {yearly:.0%} a year. Only a handful of the best investors "
                          "in history have sustained that, with large losses along the way. Not realistic "
                          "for an automated system.")
    else:
        out["verdict"] = (f"{g} a week is about {yearly:.0%} a year, within the range of long-run "
                          "diversified investing (not guaranteed; some years lose money).")
    return out
