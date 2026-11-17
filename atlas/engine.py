"""Orchestrator: one autonomous decision cycle (research -> portfolio -> risk -> execution -> monitoring)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import data
from .brokers.base import Broker
from .cashflow import detect as detect_flow
from .config import MODE, UNIVERSE, settings
from .gilts import GILT_PREFIX
from .db import DB, now
from . import sleeve
from .execution import execute, reconcile, refresh_open_orders, unresolved_orders
from .live import LiveAuthError
from .mandate import get as get_mandate
from .mandate import sizing_capital
from .live import gate as live_gate
from .portfolio import liquidation_orders, plan_orders, small_capital_plan
from .research.lab import current_selection, run_lab
from .research.strategies import target_weights
from .risk import RiskEngine

log = logging.getLogger("atlas.engine")
LONDON = ZoneInfo("Europe/London")


def make_broker(db: DB, name: str | None = None, client_id: int | None = None) -> Broker:
    """Worker uses IBKR client id 17 (default); the API passes a different id so they never collide."""
    name = name or db.get("broker", settings.broker)
    if name == "ibkr":
        from .brokers.ibkr import IBKRBroker
        return IBKRBroker(client_id=client_id, scales=db.get("ibkr_scale", {}) or {})
    from .brokers.paper import PaperBroker
    return PaperBroker(db, lambda: data.latest_prices_gbp(db)[0])


def lse_open(t: datetime | None = None) -> bool:
    t = (t or datetime.now(timezone.utc)).astimezone(LONDON)
    return t.weekday() < 5 and (8, 5) <= (t.hour, t.minute) <= (16, 25)


def _older_than(db: DB, key: str, delta: timedelta) -> bool:
    v = db.get(key)
    return not v or datetime.fromisoformat(v) < datetime.now(timezone.utc) - delta


def snapshot(db: DB, broker: Broker, prices: dict[str, float]) -> dict:
    acct = broker.account()
    pos = {s: {"qty": p.qty, "price": prices.get(s), "value": p.qty * prices.get(s, 0.0), "avg_cost": p.avg_cost}
           for s, p in acct.positions.items() if p.qty}
    equity = acct.cash + sum(v["value"] for v in pos.values())
    ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")  # unique, still sortable
    flow = detect_flow(db, broker.name, ts, acct.cash)  # deposits/withdrawals: not profit, not loss
    db.x("INSERT OR REPLACE INTO equity_snapshots(ts,broker,equity,cash,positions) VALUES(?,?,?,?,?)",
         (ts, broker.name, equity, acct.cash, json.dumps(pos)))
    return {"equity": equity, "cash": acct.cash, "positions": pos, "external_flow": flow}


def cycle(db: DB, broker: Broker | None = None, force_trade: bool = False, ignore_hours: bool = False) -> dict:
    broker = broker or make_broker(db)
    risk = RiskEngine(db)
    report: dict = {"at": now(), "broker": broker.name, "steps": []}

    h = broker.health()
    report["health"] = h
    prev_health = db.get("broker_health") or {}
    db.set("broker_health", {**h, "at": now()})
    if h.get("ok") and prev_health and not prev_health.get("ok"):
        db.event("warn", "broker_up", f"{broker.name} connection restored (account {h.get('account')})")
    prev_broker = db.get("active_broker")
    if prev_broker != broker.name:
        # Equity baselines belong to one account; never compare a new account against the old peak.
        for k in ("peak_equity", "day_start_equity", "risk_day"):
            db.set(k, None)
        db.set("day_turnover", 0.0)
        db.set("active_broker", broker.name)
        if prev_broker:
            db.event("warn", "broker_switch", f"broker changed {prev_broker} -> {broker.name}; risk baselines reset")
    if not h.get("ok"):
        # Alert on the transition only (gateway logouts can last hours; don't spam notifications).
        level = "critical" if prev_health.get("ok", True) else "info"
        db.event(level, "broker_down", f"{broker.name} unreachable: {h.get('error', h)}")
        return report
    # Account type is re-verified at the start of EVERY cycle, independently per mode (fail closed).
    live_limits = None
    if MODE == "live":
        try:
            live_limits = live_gate(db, h, broker)
        except LiveAuthError as e:
            if not db.get("kill_switch"):
                RiskEngine(db).set_kill_switch(True, "live verification failed")
            db.event("critical", "live_verification_failed", f"{e}; kill switch engaged, no trading.")
            report["blocked"] = f"live_verification_failed: {e}"
            return report
        risk = RiskEngine(db, live=live_limits)
    elif h.get("live") is not False:
        # Paper-mode process: ANY live or unverifiable account is refused, whatever PAPER_ONLY says.
        if not db.get("kill_switch"):
            RiskEngine(db).set_kill_switch(True, "paper-only lock")
        db.event("critical", "live_account_blocked",
                 f"{broker.name} account {h.get('account', '?')} is not a verified paper account; "
                 "kill switch engaged, no trading.")
        report["blocked"] = "live_or_unverified_account"
        return report

    if _older_than(db, "data_refreshed_at", timedelta(hours=6)):
        data.refresh(db)
        report["steps"].append("data_refreshed")
    if _older_than(db, "lab_ran_at", timedelta(days=7)):
        run_lab(db)
        report["steps"].append("lab_ran")

    refresh_open_orders(db, broker)
    prices, dates = data.latest_prices_gbp(db)
    gilt_book = None
    if hasattr(broker, "load_gilts"):
        broker_pos = broker.account().positions
        holds_gilts = any(k.startswith(GILT_PREFIX) for k in broker_pos) or any(
            r for r in db.q("SELECT 1 FROM orders WHERE broker=? AND symbol LIKE ? LIMIT 1", (broker.name, GILT_PREFIX + "%")))
        if holds_gilts or db.get("gilt_verified"):
            try:
                gilt_book, gprices, gdates = sleeve.price_book(db, broker)
                prices.update(gprices)
                dates.update(gdates)
            except Exception as e:
                db.event("warn", "gilt_prices", f"gilt prices unavailable this cycle: {e}")
            last = db.get("gilt_last_prices", {}) or {}
            for k in broker_pos:   # value a held gilt at its last known price if no quote now (orders blocked: stale)
                if k.startswith(GILT_PREFIX) and k not in prices and k in last:
                    prices[k], dates[k] = last[k]["price"], last[k]["date"]
            sleeve.record_redemptions(db, broker.name, broker_pos)
    if db.get(f"recon_base_{broker.name}") is None:
        db.set(f"recon_base_{broker.name}", {s: p.qty for s, p in broker.account().positions.items()})
    snap = snapshot(db, broker, prices)
    report["mtm"] = risk.update_equity(snap["equity"])
    report["reconcile"] = rec = reconcile(db, broker)
    if not rec["ok"]:
        risk.suspend("broker/ledger reconciliation mismatch")

    sel = current_selection(db)
    if sel["name"] == "cash":
        target = {s: 0.0 for s in UNIVERSE}
        target["ERNS.L"] = 1.0
    else:
        px = data.load_panel(db).dropna()
        target = {k: float(v) for k, v in target_weights(sel["name"], sel["params"], px).items()}
    db.x("INSERT INTO targets(created_at,strategy,weights,asof) VALUES(?,?,?,?)",
         (now(), sel["name"], json.dumps(target), max(dates.values())))
    report["target"] = {"strategy": sel["name"], "weights": target}

    if db.get("liquidate_requested"):
        orders = liquidation_orders({s: p["qty"] for s, p in snap["positions"].items()}, prices)
        rr = risk.review(orders, snap["equity"], snap["cash"], {s: p["value"] for s, p in snap["positions"].items()},
                         dates, broker.is_live, reduce_only=True)
        for o, why in rr.rejected:
            db.event("warn", "risk_reject", f"liquidation {o.side} {o.qty:g} {o.symbol}: {why}")
        report["execution"] = ex = execute(db, broker, risk, rr.approved)
        if rr.approved:  # record the post-sale book now, not at the next cycle
            snap = snapshot(db, broker, prices)
            risk.update_equity(snap["equity"])
        done = not rr.rejected and all(x["status"] == "filled" for x in ex)
        if done:
            db.set("liquidate_requested", False)
            db.event("critical", "liquidation", f"Emergency liquidation complete: {len(ex)} orders filled")
        else:
            db.event("critical", "liquidation", f"Liquidation incomplete ({len(rr.rejected)} blocked, "
                     f"{sum(x['status'] != 'filled' for x in ex)} unfilled); request stays queued")
    elif not (ignore_hours or lse_open()):
        report["steps"].append("market_closed_no_trading")
    elif pending := unresolved_orders(db, broker.name):
        # Never plan new orders while earlier ones are unresolved: prevents duplicates after restarts.
        report["steps"].append("waiting_on_unresolved_orders")
        db.event("warn", "orders_pending", f"{len(pending)} unresolved order(s); new orders paused: "
                 + ", ".join(f"#{p['id']} {p['side']} {p['qty']:g} {p['symbol']} ({p['status']})" for p in pending))
    elif (mandate := get_mandate(db)) is None or (MODE == "live" and not mandate.approved):
        report["steps"].append("no_approved_mandate")
    elif mandate.paused:
        report["steps"].append("paused_by_investor")
    else:
        holdings = {s: p["qty"] for s, p in snap["positions"].items()}
        # Size against the mandate (capital limit minus cash reserve), never the whole account.
        held_value = sum(p["value"] for p in snap["positions"].values())
        sizing_equity = max(sizing_capital(mandate, snap["equity"]), 0.0)
        if live_limits is not None:  # and never beyond the signed live authorisation
            sizing_equity = min(sizing_equity, live_limits.max_capital_gbp)
        report["sizing_capital"] = sizing_equity
        report["invested_value"] = held_value
        tgt, band, min_order = small_capital_plan(target, sizing_equity)
        if tgt is not target:
            report["steps"].append("small_capital_mode")
        etf_prices = {s: px for s, px in prices.items() if s in UNIVERSE}
        etf_holdings = {s: q for s, q in holdings.items() if s in UNIVERSE}
        if gilt_book and sleeve.active(db, mandate, broker):
            etf_val = {s: p["value"] for s, p in snap["positions"].items() if s in UNIVERSE}
            tgt_etf, cash_w, bond_w = sleeve.split_targets(tgt, mandate, etf_val, sizing_equity)
            orders = plan_orders(tgt_etf, sizing_equity * (1 - cash_w - bond_w), etf_holdings, etf_prices,
                                 band=band, force=force_trade, min_order_gbp=min_order)
            gilt_orders, report["gilt_sleeve"] = sleeve.plan(db, mandate, gilt_book, prices, holdings,
                                                             sizing_equity, cash_w, bond_w)
            risk.allowed_gilts = {o.symbol for o in gilt_orders}
            db.set("gilt_sleeve_report", {**report["gilt_sleeve"], "at": now()})
            orders += gilt_orders
            report["steps"].append("gilt_sleeve")
        else:
            orders = plan_orders(tgt, sizing_equity, etf_holdings, etf_prices, band=band, force=force_trade,
                                 min_order_gbp=min_order)
        rr = risk.review(orders, snap["equity"], snap["cash"], {s: p["value"] for s, p in snap["positions"].items()},
                         dates, broker.is_live)
        report["proposed"] = len(orders)
        report["rejected"] = [{"symbol": o.symbol, "side": o.side, "qty": o.qty, "reason": why} for o, why in rr.rejected]
        for o, why in rr.rejected:
            db.event("info", "risk_reject", f"{o.side} {o.qty:g} {o.symbol}: {why}")
        report["execution"] = execute(db, broker, risk, rr.approved,
                                      collar=live_limits.price_collar_pct if live_limits else None)
        if rr.approved:
            snap = snapshot(db, broker, prices)
            risk.update_equity(snap["equity"])

    report["equity"] = snap["equity"]
    db.set("last_cycle", {"at": report["at"], "equity": snap["equity"], "steps": report["steps"]})
    return report
