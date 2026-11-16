"""The investor's operating mandate: the one-time approval of what ATLAS X may do.

Stored in the database of the current mode (paper and live mandates are separate). In PAPER mode a
clearly-labelled provisional mandate (£10,000) applies until the investor approves their own, so paper
results reflect a realistic account size rather than IBKR's £1,000,000 simulated balance.
In LIVE mode there is no default: nothing trades without an approved mandate (and the live gate).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import MODE
from .db import DB, now

WRAPPERS = ("taxable", "isa", "sipp")
TAX_BANDS = ("basic", "higher", "additional")


@dataclass
class Mandate:
    capital_limit_gbp: float = 10_000.0     # the most ATLAS will ever invest
    cash_reserve_gbp: float = 0.0           # kept as cash, never invested
    wrapper: str = "taxable"                # account type, for after-tax reporting
    tax_band: str = "higher"                # for after-tax reporting only
    paused: bool = False                    # soft pause: no new investing; safety logic keeps running
    gilt_sleeve: str = "off"                # "off" | "cash" | "cash+bonds" (hold cash/bond share in UK gilts)
    approved: bool = False
    approved_by: str = ""
    approved_at: str = ""
    provisional: bool = True

    def validate(self) -> list[str]:
        e = []
        if self.capital_limit_gbp <= 0:
            e.append("capital limit must be more than £0")
        if self.cash_reserve_gbp < 0:
            e.append("cash reserve cannot be negative")
        if self.wrapper not in WRAPPERS:
            e.append(f"account type must be one of {WRAPPERS}")
        if self.tax_band not in TAX_BANDS:
            e.append(f"tax band must be one of {TAX_BANDS}")
        if self.gilt_sleeve not in ("off", "cash", "cash+bonds"):
            e.append("gilt option must be off, cash or cash+bonds")
        return e


def get(db: DB) -> Mandate | None:
    raw = db.get("mandate")
    if raw:
        return Mandate(**{k: v for k, v in raw.items() if k in Mandate.__dataclass_fields__})
    if MODE == "paper":
        return Mandate()  # provisional paper default
    return None


def approve(db: DB, m: Mandate, by: str) -> Mandate:
    errs = m.validate()
    if errs:
        raise ValueError("; ".join(errs))
    m.approved, m.provisional, m.approved_by, m.approved_at = True, False, by, now()
    db.set("mandate", asdict(m))
    db.event("warn", "mandate", f"mandate approved by {by}: capital limit £{m.capital_limit_gbp:,.0f}, "
             f"cash reserve £{m.cash_reserve_gbp:,.0f}, account {m.wrapper}")
    return m


def set_paused(db: DB, paused: bool, by: str) -> Mandate:
    m = get(db) or Mandate()
    m.paused = paused
    db.set("mandate", asdict(m))
    db.event("warn", "mandate", f"investing {'PAUSED' if paused else 'resumed'} by {by}")
    return m


def sizing_capital(m: Mandate | None, equity: float) -> float:
    """Capital ATLAS may invest this cycle = min(account equity, capital limit) minus the cash reserve."""
    if m is None:
        return 0.0
    return max(0.0, min(equity, m.capital_limit_gbp) - m.cash_reserve_gbp)
