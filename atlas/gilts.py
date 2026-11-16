"""UK gilt engine: cash flows, accrued interest, redemption yield, AFTER-TAX yield, and selection.

Why: gilts are exempt from UK capital gains tax; only coupons are taxed (as savings income). A
low-coupon gilt bought below par earns most of its return as a tax-free pull-to-par, so for a taxable
investor it can beat a savings account or bond fund at the same gross yield by 1-2% a year
(docs/RESEARCH_AND_DECISION.md). Inside an ISA/SIPP there is no tax, so the plain gross yield decides.

Conventions: semi-annual coupons on the maturity day/month and six months earlier; accrued interest
actual/actual (ICMA) per half-year; yields quoted with semi-annual compounding (the UK convention).
Simplifications (documented, conservative): the 7-business-day ex-dividend period and the Accrued
Income Scheme adjustment are ignored; index-linked gilts are excluded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from .tax import rates, tax_year

GILT_PREFIX = "GILT:"


@dataclass
class Gilt:
    name: str
    coupon: float              # % per year
    maturity: date
    clean: float | None = None  # per £100 nominal
    isin: str = ""
    ticker: str = ""
    con_id: int | None = None
    index_linked: bool = False

    @property
    def key(self) -> str:
        return GILT_PREFIX + (self.isin or self.ticker)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def coupon_dates(g: Gilt, after: date) -> list[date]:
    """Coupon dates strictly after `after`, up to and including maturity."""
    out, d, k = [], g.maturity, 0
    while d > after:
        out.append(d)
        k += 1
        d = _add_months(g.maturity, -6 * k)
    return sorted(out)


def previous_coupon(g: Gilt, on: date) -> date:
    k, d = 0, g.maturity
    while d > on:
        k += 1
        d = _add_months(g.maturity, -6 * k)
    return d


def accrued(g: Gilt, settle: date) -> float:
    prev = previous_coupon(g, settle)
    nxt = _add_months(prev, 6)
    return g.coupon / 2 * (settle - prev).days / (nxt - prev).days


def settlement(trade: date) -> date:
    d = trade + timedelta(days=1)          # gilts settle T+1 (business days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def cash_flows(g: Gilt, settle: date) -> list[tuple[date, float]]:
    flows = [(d, g.coupon / 2) for d in coupon_dates(g, settle)]
    flows[-1] = (flows[-1][0], flows[-1][1] + 100.0)
    return flows


def _yield(price: float, flows: list[tuple[date, float]], settle: date) -> float:
    """Semi-annual-compounded yield solving price = sum CF / (1 + y/2)^(2t)."""
    def pv(y):
        return sum(cf / (1 + y / 2) ** (2 * (d - settle).days / 365.25) for d, cf in flows)
    lo, hi = -0.5, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if pv(mid) > price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def gross_yield(g: Gilt, trade: date) -> float:
    s = settlement(trade)
    return _yield(g.clean + accrued(g, s), cash_flows(g, s), s)


def savings_rate(band: str, on: date) -> float:
    return rates(tax_year(on))["int"][band]


def after_tax_yield(g: Gilt, trade: date, band: str | None, commission_pct: float = 0.0,
                    flat_rate: float | None = None) -> float:
    """Yield after income tax on coupons (gain to maturity is CGT-free) and after buying commission.
    band=None means no tax (ISA/SIPP). `flat_rate` overrides the year-by-year rates (for testing)."""
    s = settlement(trade)
    flows = []
    for d, cf in cash_flows(g, s):
        coupon = g.coupon / 2
        rate = 0.0 if band is None else (flat_rate if flat_rate is not None else savings_rate(band, d))
        flows.append((d, cf - coupon * rate))
    price = (g.clean + accrued(g, s)) + 100.0 * commission_pct
    # accrued interest bought is deducted from the first coupon for tax (it is not taxed twice)
    if band is not None and flows:
        rate0 = flat_rate if flat_rate is not None else savings_rate(band, flows[0][0])
        flows[0] = (flows[0][0], flows[0][1] + accrued(g, s) * rate0)
    return _yield(price, flows, s)


def modified_duration(g: Gilt, trade: date) -> float:
    s = settlement(trade)
    flows = cash_flows(g, s)
    y = gross_yield(g, trade)
    price = g.clean + accrued(g, s)
    mac = sum((d - s).days / 365.25 * cf / (1 + y / 2) ** (2 * (d - s).days / 365.25) for d, cf in flows) / price
    return mac / (1 + y / 2)


# IBKR UK bond commission (official pricing page, via research agent): 0.10% of face value on the
# first £10,000, 0.025% above; we assume the conservative upper end of the published minimums.
def commission_gbp(nominal: float) -> float:
    return max(1.0, 0.001 * min(nominal, 10_000) + 0.00025 * max(0.0, nominal - 10_000))


@dataclass
class SleeveSpec:
    purpose: str                     # "cash" (0.25-3y) or "bonds" (duration-matched ladder)
    min_years: float = 0.25
    max_years: float = 3.0
    holdings: int = 3
    max_coupon: float | None = None  # e.g. 2.0 to restrict to low-coupon gilts for taxable accounts
    exclude_within_days: int = 30
    meta: dict = field(default_factory=dict)


CASH_SLEEVE = SleeveSpec("cash", 0.25, 3.0, 3)
BOND_SLEEVE = SleeveSpec("bonds", 2.0, 16.0, 5)


def rank(gilts: list[Gilt], trade: date, band: str | None, spec: SleeveSpec, size_gbp: float) -> list[dict]:
    """Eligible conventional gilts ranked by after-tax, after-commission yield."""
    out = []
    for g in gilts:
        if g.index_linked or g.clean is None or g.clean <= 0:
            continue
        yrs = (g.maturity - trade).days / 365.25
        if yrs < spec.min_years or yrs > spec.max_years or (g.maturity - trade).days < spec.exclude_within_days:
            continue
        if spec.max_coupon is not None and g.coupon > spec.max_coupon:
            continue
        per = max(size_gbp / max(spec.holdings, 1), 1.0)
        comm_pct = commission_gbp(per) / per
        out.append({"gilt": g, "years": yrs, "gross": gross_yield(g, trade),
                    "after_tax": after_tax_yield(g, trade, band, comm_pct),
                    "duration": modified_duration(g, trade)})
    return sorted(out, key=lambda r: r["after_tax"], reverse=True)


def select(gilts: list[Gilt], trade: date, band: str | None, spec: SleeveSpec, size_gbp: float) -> list[dict]:
    """Choose `holdings` gilts. Cash sleeve: best after-tax yields, at most one per maturity year
    (a short ladder spreads reinvestment dates). Bond sleeve: spread evenly across the maturity range
    (a ladder whose average duration is the middle of the range), best after-tax pick per bucket."""
    ranked = rank(gilts, trade, band, spec, size_gbp)
    if not ranked:
        return []
    picks: list[dict] = []
    if spec.purpose == "cash":
        years_used = set()
        for r in ranked:
            y = r["gilt"].maturity.year
            if y not in years_used:
                picks.append(r)
                years_used.add(y)
            if len(picks) == spec.holdings:
                break
    else:
        edges = [spec.min_years + (spec.max_years - spec.min_years) * i / spec.holdings for i in range(spec.holdings + 1)]
        for lo, hi in zip(edges, edges[1:]):
            bucket = [r for r in ranked if lo <= r["years"] < hi]
            if bucket:
                picks.append(bucket[0])
    w = 1.0 / len(picks)
    for p in picks:
        p["weight"] = w
    return picks


def expected_coupons(holdings_nominal: dict[str, float], gilts: dict[str, Gilt], start: date, end: date) -> float:
    """Coupon cash expected between two dates (so cash-flow detection does not mistake it for a deposit)."""
    total = 0.0
    for key, nominal in holdings_nominal.items():
        g = gilts.get(key)
        if g is None or nominal <= 0:
            continue
        for d in coupon_dates(g, start):
            if d <= end:
                total += nominal * g.coupon / 200.0
    return total


def parse_maturity(s: str) -> date:
    from datetime import datetime
    return datetime.strptime(s, "%d-%b-%Y").date()


def years_to(d: date, trade: date) -> float:
    return max(0.0, (d - trade).days / 365.25)


__all__ = ["Gilt", "SleeveSpec", "CASH_SLEEVE", "BOND_SLEEVE", "rank", "select", "after_tax_yield", "gross_yield",
           "accrued", "commission_gbp", "expected_coupons", "modified_duration", "GILT_PREFIX", "math"]
