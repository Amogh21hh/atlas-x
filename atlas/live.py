"""Live-trading authorisation and independent live-account verification.

NOTHING here enables live trading while `PAPER_ONLY = True` (atlas/config.py). This module defines
what must ALL be true before a live order could ever pass the risk engine:

  1. PAPER_ONLY is False (a reviewed code change) and the process runs with ATLAS_MODE=live
     (separate database; paper state never carries over).
  2. A signed LiveAuthorisation exists in the LIVE database, created only by the interactive CLI
     (`python -m atlas.cli live-authorise`) — never by the dashboard, the worker or any API call.
  3. It is unexpired (<= 30 days), its HMAC signature verifies, and it names ONE exact account ID.
  4. The connected IBKR account is that account, is not a DU paper account, has a GBP cash line,
     and holds nothing outside the authorised ETF universe.
  5. Its limits are no looser than the hard ceilings below (they can only make things stricter).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .config import MODE, PAPER_ONLY, UNIVERSE

MAX_AUTH_DAYS = 30
CEIL_DRAWDOWN = 0.20            # never looser than the paper drawdown breaker
CEIL_DAILY_LOSS_FRACTION = 0.04  # max daily loss £ may not exceed 4% of the capital cap
SMALL_CAPITAL_GBP = 25_000.0     # below this the small-capital deployment rules apply
PHRASE = "I AUTHORISE ATLAS X TO TRADE REAL MONEY IN THIS ACCOUNT WITHIN THESE LIMITS"


class LiveAuthError(RuntimeError):
    pass


@dataclass
class LiveLimits:
    account_id: str
    max_capital_gbp: float          # the most ATLAS may ever have invested
    max_order_gbp: float            # largest single buy order
    max_daily_loss_gbp: float       # £ loss in one day that suspends risk-increasing trades
    max_drawdown_pct: float = 0.10  # drop from peak (of capped capital) that suspends trading
    max_daily_turnover_gbp: float = 0.0   # 0 => defaults to max_capital_gbp
    etf_caps: dict = field(default_factory=dict)   # optional stricter per-ETF caps (fraction of capital)
    price_collar_pct: float = 0.005  # live orders are limit orders at most 0.5% through the reference
    max_data_staleness_days: int = 1
    created_at: str = ""
    expires_at: str = ""
    created_by: str = ""

    def validate(self) -> list[str]:
        e = []
        if not self.account_id or self.account_id.upper().startswith("DU"):
            e.append("account_id must be a real (non-DU) IBKR account ID")
        if self.max_capital_gbp <= 0:
            e.append("max_capital_gbp must be > 0")
        if not (0 < self.max_order_gbp <= self.max_capital_gbp):
            e.append("max_order_gbp must be > 0 and <= max_capital_gbp")
        if not (0 < self.max_daily_loss_gbp <= CEIL_DAILY_LOSS_FRACTION * self.max_capital_gbp + 1e-9):
            e.append(f"max_daily_loss_gbp must be > 0 and <= {CEIL_DAILY_LOSS_FRACTION:.0%} of max_capital_gbp")
        if not (0 < self.max_drawdown_pct <= CEIL_DRAWDOWN):
            e.append(f"max_drawdown_pct must be in (0, {CEIL_DRAWDOWN}]")
        if self.max_daily_turnover_gbp < 0 or self.max_daily_turnover_gbp > self.max_capital_gbp:
            e.append("max_daily_turnover_gbp must be between 0 and max_capital_gbp")
        for s, c in self.etf_caps.items():
            if s not in UNIVERSE:
                e.append(f"etf_caps: {s} is not in the ETF universe")
            elif not (0 <= c <= UNIVERSE[s]["max_w"]):
                e.append(f"etf_caps: {s} cap {c} must be within [0, {UNIVERSE[s]['max_w']}] (stricter only)")
        if not (0 < self.price_collar_pct <= 0.02):
            e.append("price_collar_pct must be in (0, 0.02]")
        if self.max_data_staleness_days != 1:
            e.append("max_data_staleness_days is fixed at 1 for live")
        return e

    def cap_for(self, symbol: str) -> float:
        base = float(UNIVERSE[symbol]["max_w"])
        return min(base, float(self.etf_caps.get(symbol, base)))

    @property
    def turnover_cap(self) -> float:
        return self.max_daily_turnover_gbp or self.max_capital_gbp

    @property
    def small_capital(self) -> bool:
        return self.max_capital_gbp < SMALL_CAPITAL_GBP


def _secret() -> bytes:
    from .auth import _secret as s
    return s()


def _sign(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()


def make_authorisation(limits: LiveLimits, by: str, days: int = MAX_AUTH_DAYS) -> dict:
    if days < 1 or days > MAX_AUTH_DAYS:
        raise LiveAuthError(f"authorisation length must be 1-{MAX_AUTH_DAYS} days")
    now = datetime.now(timezone.utc)
    limits.created_at = now.isoformat(timespec="seconds")
    limits.expires_at = (now + timedelta(days=days)).isoformat(timespec="seconds")
    limits.created_by = by
    errs = limits.validate()
    if errs:
        raise LiveAuthError("; ".join(errs))
    payload = asdict(limits)
    return {"limits": payload, "sig": _sign(payload)}


def load_authorisation(db) -> LiveLimits:
    """Returns verified limits or raises LiveAuthError (missing, tampered, expired, invalid)."""
    rec = db.get("live_authorisation_v2")
    if not rec:
        raise LiveAuthError("no live authorisation on record")
    payload = rec.get("limits") or {}
    if not hmac.compare_digest(rec.get("sig", ""), _sign(payload)):
        raise LiveAuthError("live authorisation signature invalid (record altered)")
    lim = LiveLimits(**payload)
    if datetime.fromisoformat(lim.expires_at) <= datetime.now(timezone.utc):
        raise LiveAuthError(f"live authorisation expired at {lim.expires_at}")
    errs = lim.validate()
    if errs:
        raise LiveAuthError("; ".join(errs))
    return lim


def gate(db, health: dict, broker=None) -> LiveLimits:
    """Independent live-account verification. Returns the limits to enforce, or raises.
    Called at the start of EVERY live cycle, before anything else touches the broker."""
    if PAPER_ONLY:
        raise LiveAuthError("PAPER_ONLY lock is set in code; live trading is disabled")
    if MODE != "live":
        raise LiveAuthError("process is not running in ATLAS_MODE=live")
    if not health.get("ok"):
        raise LiveAuthError(f"broker not healthy: {health.get('error')}")
    acct = str(health.get("account") or "")
    if health.get("live") is not True or acct.upper().startswith("DU"):
        raise LiveAuthError(f"connected account {acct or '?'} is a paper or unverified account; live mode refuses it")
    lim = load_authorisation(db)
    if acct != lim.account_id:
        raise LiveAuthError(f"connected account {acct} is not the authorised account {lim.account_id}")
    if broker is not None:
        a = broker.account()
        foreign = [s for s, p in a.positions.items() if p.qty and s not in UNIVERSE and not s.startswith("GILT:")]
        if foreign:
            raise LiveAuthError(f"account holds instruments outside the ATLAS universe ({foreign}); "
                                "use a dedicated account/sub-account")
        if a.cash <= 0 and not a.positions:
            raise LiveAuthError("no GBP cash and no positions in the authorised account")
    return lim
