"""'Does anything need my attention?' — every item says what is wrong and exactly what to do, in plain words."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import MODE, settings
from .db import DB
from .execution import unresolved_orders
from .mandate import get as get_mandate


def _age_minutes(ts: str | None) -> float | None:
    if not ts:
        return None
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def items(db: DB) -> list[dict]:
    out = []

    def add(level, what, todo):
        out.append({"level": level, "what": what, "todo": todo})

    broker = db.get("broker", settings.broker)
    hb = _age_minutes(db.get("worker_heartbeat"))
    if hb is None or hb > 3 * settings.cycle_seconds / 60 + 5:
        add("urgent", "The background worker is not running, so nothing is being managed.",
            "Run scripts\\start.ps1 (or restart the PC and log in).")
    h = db.get("broker_health") or {}
    if broker == "ibkr" and not h.get("ok"):
        add("urgent", "ATLAS cannot reach Interactive Brokers.",
            "Open IB Gateway and log in (IB API, Paper Trading). IBKR requires this about once a week and after a restart.")
    if db.get("kill_switch"):
        add("urgent", "Trading is halted (kill switch / emergency stop is on).",
            "Find out why in Events. When safe, release it on the Controls panel with your password.")
    if db.get("suspended"):
        add("urgent", f"New investing is suspended: {db.get('suspended')}.",
            "Review Events and Holdings, then press 'Resume after suspension' with your password.")
    pend = unresolved_orders(db, broker)
    if pend:
        add("warn", f"{len(pend)} order(s) have an unconfirmed outcome; new orders are paused until resolved.",
            "Usually clears on its own next cycle. If it persists, check the order in IB Gateway/TWS.")
    rec = db.get("last_reconcile") or {}
    if rec and not rec.get("ok"):
        add("urgent", "Holdings at the broker do not match ATLAS's records.",
            "Did you trade this account yourself? ATLAS needs a dedicated account. See Events for the difference.")
    m = get_mandate(db)
    if m is None or not m.approved:
        add("action", "Your operating mandate has not been approved yet" + (" (a provisional £10,000 paper mandate is in use)." if MODE == "paper" else "."),
            "Open 'Your mandate', check the capital limit and account type, and approve it.")
    elif m.paused:
        add("info", "Investing is paused by you.", "Press Resume when you want ATLAS to invest again.")
    if not settings.notify_webhook:
        add("info", "Alerts only go to log files, so you would not hear about a problem on your phone.",
            "Set ATLAS_NOTIFY_WEBHOOK in .env (for example a free ntfy.sh topic).")
    fresh = db.get("data_refreshed_at")
    if fresh and _age_minutes(fresh) > 60 * 30:
        add("warn", "Market prices have not been refreshed for over a day.", "Check the internet connection; see Events.")
    if db.get("backup_day") is None:
        add("info", "No automatic backup has run yet.", "It runs with the first worker cycle of each day; nothing to do.")
    if not db.q("SELECT 1 FROM users"):
        add("action", "No investor login exists yet.", "Create it on first visit to the dashboard.")
    return out


def status_line(its: list[dict]) -> str:
    if any(i["level"] == "urgent" for i in its):
        return "Needs your attention"
    if any(i["level"] in ("warn", "action") for i in its):
        return "Running — a few things to review"
    return "Running normally"
