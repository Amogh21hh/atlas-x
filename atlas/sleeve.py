"""Gilt sleeve: holds the cash and (optionally) bond share of the target in directly-held UK gilts
chosen by AFTER-TAX yield for the investor's account type and tax band.

Activation requires ALL of: the mandate opts in (gilt_sleeve = "cash" or "cash+bonds"), the IBKR bond
path has been verified on this gateway (`python -m atlas.cli gilt-check` sets `gilt_verified`), the
broker supports gilts, and live quotes are available. Otherwise the original ETF targets are used.

Policy (tax- and cost-aware):
  * buy-and-hold to maturity; the sleeve never sells to "optimise" (no taxable disposals, no churn);
  * an existing IGLT holding is kept, not sold, when the bond sleeve is switched on; new money goes to
    gilts instead (no forced capital-gains event);
  * each gilt purchase is at least £1,000 (keeps IBKR's bond commission <= 0.1%);
  * redemptions at maturity and coupons are recorded so cash reconciles exactly.
"""
from __future__ import annotations

import math
from datetime import date

from .config import CASH_PROXY
from .db import DB, now
from .gilts import BOND_SLEEVE, CASH_SLEEVE, GILT_PREFIX, Gilt, accrued, select, settlement
from .risk import ProposedOrder

BOND_ETF = "IGLT.L"
MIN_GILT_ORDER_GBP = 1_000.0
GILT_COLLAR = 0.005


def active(db: DB, mandate, broker) -> bool:
    return bool(mandate and getattr(mandate, "gilt_sleeve", "off") in ("cash", "cash+bonds")
                and db.get("gilt_verified") and hasattr(broker, "load_gilts"))


def dirty_per_pound(g: Gilt, clean: float, on: date | None = None) -> float:
    return (clean + accrued(g, settlement(on or date.today()))) / 100.0


def price_book(db: DB, broker) -> tuple[dict, dict, dict]:
    """Returns (gilts with clean prices, prices per £1 nominal (dirty), price dates). Stores metadata
    for held gilts so maturities/coupons stay known even if a quote is missing later."""
    gilts = broker.load_gilts()
    quotes = broker.quote_gilts()
    meta = db.get("gilt_meta", {}) or {}
    prices, dates, priced = {}, {}, {}
    today = date.today().isoformat()
    for k, g in gilts.items():
        meta[k] = {"name": g.name, "coupon": g.coupon, "maturity": g.maturity.isoformat(), "isin": g.isin}
        if k in quotes:
            g.clean = quotes[k]
            priced[k] = g
            prices[k] = dirty_per_pound(g, g.clean)
            dates[k] = today
    db.set("gilt_meta", meta)
    last = db.get("gilt_last_prices", {}) or {}
    last.update({k: {"price": v, "date": today} for k, v in prices.items()})
    db.set("gilt_last_prices", last)
    return priced, prices, dates


def split_targets(target: dict, mandate, etf_holdings_value: dict, capital: float) -> tuple[dict, float, float]:
    """Move the cash (and optionally bond) share of the target into the gilt sleeve.
    Returns (ETF target re-scaled to the ETF part, cash-sleeve weight, bond-sleeve weight)."""
    t = dict(target)
    cash_w = t.get(CASH_PROXY, 0.0)
    t[CASH_PROXY] = 0.0
    bond_w = 0.0
    if mandate.gilt_sleeve == "cash+bonds" and t.get(BOND_ETF, 0.0) > 0:
        keep = min(t[BOND_ETF], etf_holdings_value.get(BOND_ETF, 0.0) / capital if capital > 0 else 0.0)
        bond_w = t[BOND_ETF] - keep           # existing IGLT is kept (no forced sale); the rest -> gilts
        t[BOND_ETF] = keep
    etf_share = 1.0 - cash_w - bond_w
    scaled = {k: (v / etf_share if etf_share > 0 else 0.0) for k, v in t.items()}
    return scaled, cash_w, bond_w


def plan(db: DB, mandate, gilts_priced: dict, prices: dict, holdings_nominal: dict, capital: float,
         cash_w: float, bond_w: float, trade: date | None = None) -> tuple[list[ProposedOrder], dict]:
    """Buy-only orders that top up each sleeve towards its target value."""
    trade = trade or date.today()
    band = None if mandate.wrapper in ("isa", "sipp") else mandate.tax_band
    gl = list(gilts_priced.values())
    held_val = {k: q * prices.get(k, 0.0) for k, q in holdings_nominal.items() if k.startswith(GILT_PREFIX)}
    report, orders = {"band": band or "none (ISA/SIPP)", "sleeves": []}, []
    for spec, w in ((CASH_SLEEVE, cash_w), (BOND_SLEEVE, bond_w)):
        if w <= 0:
            continue
        target_gbp = w * capital
        in_sleeve = 0.0
        meta = db.get("gilt_meta", {}) or {}
        for k, v in held_val.items():
            m = meta.get(k)  # maturity from stored metadata, so an unquoted holding still counts
            yrs = (date.fromisoformat(m["maturity"]) - trade).days / 365.25 if m else 0
            if (spec.purpose == "cash" and yrs <= CASH_SLEEVE.max_years) or (spec.purpose == "bonds" and yrs > CASH_SLEEVE.max_years):
                in_sleeve += v
        picks = select(gl, trade, band, spec, target_gbp)
        deficit = max(0.0, target_gbp - in_sleeve)
        rep = {"purpose": spec.purpose, "target_gbp": target_gbp, "held_gbp": in_sleeve, "to_invest_gbp": deficit,
               "picks": [{"key": p["gilt"].key, "name": p["gilt"].name, "coupon": p["gilt"].coupon,
                          "maturity": p["gilt"].maturity.isoformat(), "after_tax_yield": p["after_tax"],
                          "gross_yield": p["gross"]} for p in picks]}
        report["sleeves"].append(rep)
        if deficit < MIN_GILT_ORDER_GBP or not picks:
            continue
        per = deficit / len(picks)
        if per < MIN_GILT_ORDER_GBP:      # small sleeves: concentrate into the best gilt(s)
            picks = picks[:max(1, int(deficit // MIN_GILT_ORDER_GBP))]
            per = deficit / len(picks)
        for p in picks:
            k = p["gilt"].key
            px = prices[k]
            nominal = math.floor(per / px)
            if nominal * px >= MIN_GILT_ORDER_GBP:
                orders.append(ProposedOrder(k, "buy", float(nominal), px))
    return orders, report


def record_redemptions(db: DB, broker_name: str, broker_positions: dict) -> list[str]:
    """A gilt that has matured disappears from the broker and pays back £1 per £1 nominal plus the final
    coupon. Record that in the ledger so positions and cash reconcile (it is not a deposit)."""
    meta = db.get("gilt_meta", {}) or {}
    done = []
    ledger: dict[str, float] = {}
    for r in db.q("SELECT symbol, side, filled_qty FROM orders WHERE broker=? AND filled_qty > 0 AND symbol LIKE ?",
                  (broker_name, GILT_PREFIX + "%")):
        ledger[r["symbol"]] = ledger.get(r["symbol"], 0.0) + (r["filled_qty"] if r["side"] == "buy" else -r["filled_qty"])
    for k, nominal in ledger.items():
        m = meta.get(k)
        if nominal <= 0 or not m or date.fromisoformat(m["maturity"]) > date.today() or k in broker_positions:
            continue
        cash_per_pound = 1.0 + float(m["coupon"]) / 200.0
        db.x("INSERT INTO orders(created_at,broker,symbol,side,qty,ref_price,status,filled_qty,avg_fill_price,"
             "commission,risk_decision,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (now(), broker_name, k, "sell", nominal, cash_per_pound, "filled", nominal, cash_per_pound, 0.0,
              "redemption at maturity (not a trade)", now()))
        db.event("info", "gilt_redemption", f"{m['name']} matured: £{nominal:,.0f} nominal repaid "
                 f"(£{nominal * cash_per_pound:,.2f} incl. final coupon)")
        done.append(k)
    return done


def expected_coupon_cash(db: DB, prev_positions: dict, start: date, end: date) -> float:
    from .gilts import expected_coupons
    meta = db.get("gilt_meta", {}) or {}
    gl = {k: Gilt(m["name"], float(m["coupon"]), date.fromisoformat(m["maturity"]), isin=m.get("isin", ""))
          for k, m in meta.items()}
    held = {k: float(v["qty"]) for k, v in prev_positions.items() if k.startswith(GILT_PREFIX)}
    return expected_coupons(held, gl, start, end)
