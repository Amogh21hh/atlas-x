"""UK tax engine (estimates for information; not tax advice).

* Cost basis: HMRC share-pooling for each ETF. Same-day disposals are matched with same-day
  acquisitions, then the 30-day "bed and breakfast" rule applies, then the Section 104 pool.
* Tax year: 6 April to 5 April.
* Income from distributions is estimated from the funds' own distribution history (adjusted minus
  unadjusted price). Bond funds (> 60% bonds) are taxed as interest; equity funds as dividends.
* Rates: 2026/27 from GOV.UK (Budget 2026 Annex A, checked 14 Nov 2026). Savings rates rise to
  24/44/49% from 6 April 2027 (Budget 2025 + 2026, not yet enacted).
* Inside an ISA or SIPP everything here is £0.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .db import DB

RATES = {
    2026: {"cgt_allow": 3000, "cgt": {"basic": .18, "higher": .24, "additional": .24},
           "div_allow": 500, "div": {"basic": .1075, "higher": .3575, "additional": .3935},
           "psa": {"basic": 1000, "higher": 500, "additional": 0},
           "int": {"basic": .20, "higher": .40, "additional": .45}},
    2027: {"cgt_allow": 3000, "cgt": {"basic": .18, "higher": .24, "additional": .24},
           "div_allow": 500, "div": {"basic": .1075, "higher": .3575, "additional": .3935},
           "psa": {"basic": 1000, "higher": 500, "additional": 0},
           "int": {"basic": .24, "higher": .44, "additional": .49}},
}
INTEREST_FUNDS = {"IGLT.L", "INXG.L", "SLXX.L", "ERNS.L"}   # >60% bonds: distributions taxed as interest
NO_INCOME = {"SGLN.L"}                                        # physical gold ETC
ISA_ALLOWANCE = 20_000


def tax_year(d: date) -> int:
    """Tax year label = calendar year in which it starts (2026 => 6 Apr 2026 - 5 Apr 2027)."""
    return d.year if (d.month, d.day) >= (4, 6) else d.year - 1


def rates(year: int) -> dict:
    return RATES.get(year, RATES[max(RATES)])


def _d(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()


def disposals(db: DB, broker: str) -> list[dict]:
    """Chargeable gains per disposal under UK share-matching rules (same day, 30 days, then S104)."""
    fills = [dict(r) for r in db.q(
        "SELECT id, updated_at, symbol, side, filled_qty q, avg_fill_price p, commission c FROM orders "
        "WHERE broker=? AND filled_qty > 0 ORDER BY updated_at, id", (broker,))]
    for f in fills:
        f["d"] = _d(f["updated_at"])
        f["left"] = f["q"]
    pool = defaultdict(lambda: [0.0, 0.0])  # symbol -> [qty, allowable cost]
    out = []
    for i, f in enumerate(fills):
        if f["side"] == "buy":
            continue
        sym, qty = f["symbol"], f["q"]
        proceeds_total = qty * f["p"] - (f["c"] or 0)
        cost, matched, rule = 0.0, 0.0, []
        # same-day and 30-day acquisitions (later buys of the same ETF)
        for g in fills:
            if g["symbol"] != sym or g["side"] != "buy" or g["left"] <= 0:
                continue
            if f["d"] <= g["d"] <= f["d"] + timedelta(days=30):
                take = min(g["left"], qty - matched)
                if take <= 0:
                    break
                cost += take * g["p"] + (g["c"] or 0) * take / g["q"]
                g["left"] -= take
                matched += take
                rule.append("same-day" if g["d"] == f["d"] else "30-day")
        rest = qty - matched
        if rest > 0:
            # build the S104 pool from earlier unmatched buys
            for g in fills[:i]:  # earlier acquisitions not used by same-day/30-day matching
                if g["symbol"] == sym and g["side"] == "buy" and g["left"] > 0:
                    pool[sym][0] += g["left"]
                    pool[sym][1] += g["left"] * g["p"] + (g["c"] or 0) * g["left"] / g["q"]
                    g["left"] = 0
            pq, pc = pool[sym]
            if pq > 0:
                part = min(rest, pq) / pq
                cost += pc * part
                pool[sym][0] -= min(rest, pq)
                pool[sym][1] -= pc * part
            rule.append("s104")
        out.append({"date": f["d"].isoformat(), "symbol": sym, "qty": qty, "proceeds": proceeds_total,
                    "cost": cost, "gain": proceeds_total - cost, "rules": sorted(set(rule)),
                    "tax_year": tax_year(f["d"])})
    return out


def estimated_income(db: DB, broker: str, year: int) -> dict:
    """Distributions received in a tax year, estimated from fund distribution history x units held."""
    start, end = date(year, 4, 6), date(year + 1, 4, 5)
    snaps = db.q("SELECT ts, positions FROM equity_snapshots WHERE broker=? ORDER BY ts", (broker,))
    import json
    held_by_day: dict[str, dict] = {}
    for s in snaps:
        held_by_day[s["ts"][:10]] = {k: v["qty"] for k, v in json.loads(s["positions"]).items()}
    if not held_by_day:
        return {"dividends": 0.0, "interest": 0.0}
    days = sorted(held_by_day)
    div = intr = 0.0
    from .config import UNIVERSE
    for sym in UNIVERSE:
        if sym in NO_INCOME:
            continue
        rows = db.q("SELECT date, close, adjclose FROM prices WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date",
                    (sym, start.isoformat(), end.isoformat()))
        prev = None
        for r in rows:
            if prev is not None and prev["close"] and r["close"]:
                tr = r["adjclose"] / prev["adjclose"] - 1
                pr = r["close"] / prev["close"] - 1
                inc = max(0.0, tr - pr)
                if inc > 0:
                    held = 0.0
                    for d in days:  # holdings as of that date
                        if d <= r["date"]:
                            held = held_by_day[d].get(sym, 0.0)
                    amt = held * r["close"] * UNIVERSE[sym]["scale"] * inc
                    if sym in INTEREST_FUNDS:
                        intr += amt
                    else:
                        div += amt
            prev = r
    return {"dividends": div, "interest": intr}


def year_summary(db: DB, broker: str, wrapper: str, band: str, year: int | None = None) -> dict:
    year = year or tax_year(date.today())
    r = rates(year)
    gains = [x for x in disposals(db, broker) if x["tax_year"] == year]
    net_gain = sum(x["gain"] for x in gains)
    inc = estimated_income(db, broker, year)
    sheltered = wrapper in ("isa", "sipp")
    cgt = 0.0 if sheltered else max(0.0, net_gain - r["cgt_allow"]) * r["cgt"][band]
    div_tax = 0.0 if sheltered else max(0.0, inc["dividends"] - r["div_allow"]) * r["div"][band]
    int_tax = 0.0 if sheltered else max(0.0, inc["interest"] - r["psa"][band]) * r["int"][band]
    flows = db.q("SELECT COALESCE(SUM(amount),0) a FROM cash_flows WHERE broker=? AND amount > 0 AND ts >= ? AND ts < ?",
                 (broker, f"{year}-04-06", f"{year + 1}-04-06"))[0]["a"]
    return {
        "tax_year": f"{year}/{str(year + 1)[-2:]}", "wrapper": wrapper, "tax_band": band, "sheltered": sheltered,
        "disposals": gains, "net_realised_gain": net_gain,
        "cgt_allowance_left": None if sheltered else max(0.0, r["cgt_allow"] - net_gain),
        "estimated_dividends": inc["dividends"], "estimated_interest": inc["interest"],
        "estimated_tax": {"cgt": cgt, "dividends": div_tax, "interest": int_tax, "total": cgt + div_tax + int_tax},
        "isa_deposits_detected": flows if wrapper == "isa" else None,
        "isa_allowance_left": max(0.0, ISA_ALLOWANCE - flows) if wrapper == "isa" else None,
        "note": "Estimates for information only, not tax advice. Your broker's annual tax statement is authoritative.",
    }
