"""FastAPI backend for the investor dashboard.   Run:  python -m uvicorn atlas.api:app --port 8600"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import auth, data
from .config import MODE, PAPER_ONLY, ROOT, UNIVERSE, settings
from .execution import emergency_stop, unresolved_orders
from .db import DB, now
from .engine import cycle, make_broker
from .reporting import active_broker, attribution, daily_report, performance
from .research.lab import GATE, current_selection, run_lab
from .risk import RiskEngine

app = FastAPI(title="ATLAS X", docs_url="/api/docs")
db = DB()
LIVE_PHRASE = "I AUTHORISE LIVE TRADING WITH REAL MONEY"
MIN_PAPER_DAYS = 20
API_CLIENT_ID = 18  # IBKR client id for manual API cycles (worker uses 17)
_attempts: dict[str, list[float]] = defaultdict(list)


class Creds(BaseModel):
    email: str
    password: str


class PasswordOnly(BaseModel):
    password: str


class Toggle(BaseModel):
    on: bool
    password: str | None = None


class LiveAuth(BaseModel):
    password: str
    phrase: str
    max_capital_gbp: float


class CycleReq(BaseModel):
    force: bool = False
    ignore_hours: bool = False


def user(request: Request) -> str:
    u = auth.check(request.cookies.get("atlas_session"))
    if not u:
        raise HTTPException(401, "not authenticated")
    return u


def _reauth(email: str, pw: str) -> None:
    if not auth.verify(db, email, pw):
        db.event("warn", "security", f"failed re-authentication for {email}")
        raise HTTPException(403, "password incorrect")


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "time": now(), "heartbeat": db.get("worker_heartbeat"), "users": bool(db.q("SELECT 1 FROM users"))}


@app.post("/api/setup")
def setup(c: Creds, response: Response):
    if db.q("SELECT 1 FROM users"):
        raise HTTPException(409, "investor account already exists")
    try:
        auth.create_user(db, c.email, c.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.event("info", "security", f"investor account created ({c.email})")
    response.set_cookie("atlas_session", auth.issue(c.email), httponly=True, samesite="strict")
    return {"ok": True}


@app.post("/api/login")
def login(c: Creds, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    _attempts[ip] = [t for t in _attempts[ip] if t > time.time() - 900]
    if len(_attempts[ip]) >= 8:
        raise HTTPException(429, "too many attempts; wait 15 minutes")
    if not auth.verify(db, c.email, c.password):
        _attempts[ip].append(time.time())
        raise HTTPException(401, "invalid credentials")
    response.set_cookie("atlas_session", auth.issue(c.email.lower()), httponly=True, samesite="strict")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("atlas_session")
    return {"ok": True}


@app.get("/api/overview")
def overview(u: str = Depends(user)):
    snap = db.q("SELECT * FROM equity_snapshots WHERE broker=? ORDER BY ts DESC LIMIT 1", (active_broker(db),))
    s = dict(snap[0]) if snap else None
    positions = json.loads(s["positions"]) if s else {}
    tgt = db.q("SELECT * FROM targets ORDER BY id DESC LIMIT 1")
    prices, dates = data.latest_prices_gbp(db)
    risk = RiskEngine(db).state()
    if db.get("broker", settings.broker) == "ibkr":
        # Don't open a gateway connection on every dashboard poll; the worker records health each cycle.
        bh = db.get("broker_health") or {"ok": False, "broker": "ibkr", "live": None,
                                         "error": "no health report yet; is the worker running?"}
    else:
        try:
            bh = make_broker(db).health()
        except Exception as e:
            bh = {"ok": False, "error": str(e)}
    return {
        "user": u, "broker": bh, "snapshot": s and {k: s[k] for k in ("ts", "broker", "equity", "cash")},
        "positions": positions, "attribution": attribution(positions), "performance": performance(db),
        "target": tgt and {"strategy": tgt[0]["strategy"], "weights": json.loads(tgt[0]["weights"]), "asof": tgt[0]["asof"]},
        "selection": current_selection(db), "risk": risk, "limits": settings.limits.__dict__,
        "heartbeat": db.get("worker_heartbeat"), "last_cycle": db.get("last_cycle"),
        "data": {"refreshed_at": db.get("data_refreshed_at"), "latest": dates,
                 "stale": {k: data.staleness_days(v) for k, v in dates.items()}},
        "universe": UNIVERSE, "live_phrase": LIVE_PHRASE, "paper_only": PAPER_ONLY,
        "broker_name": db.get("broker", settings.broker),
        "last_reconcile": db.get("last_reconcile"), "last_reconcile_ok": db.get("last_reconcile_ok"),
        "pending_orders": [dict(r) for r in unresolved_orders(db, db.get("broker", settings.broker))],
        "mode": MODE,
    }


@app.get("/api/equity")
def equity(u: str = Depends(user)):
    return [dict(r) for r in db.q("SELECT ts, equity, cash FROM equity_snapshots WHERE broker=? ORDER BY ts",
                                  (active_broker(db),))]


@app.get("/api/research")
def research(u: str = Depends(user)):
    last = db.q("SELECT MAX(run_at) m FROM strategy_runs")[0]["m"]
    rows = db.q("SELECT * FROM strategy_runs WHERE run_at=? ORDER BY id", (last,)) if last else []
    return {"run_at": last, "gate": GATE, "selection": current_selection(db),
            "strategies": [{"name": r["name"], "params": json.loads(r["params"]), "metrics": json.loads(r["metrics"]),
                            "passed": bool(r["passed"]), "reasons": json.loads(r["reasons"]),
                            "equity": json.loads(r["equity"] or "[]")} for r in rows]}


@app.get("/api/orders")
def orders(u: str = Depends(user)):
    return [dict(r) for r in db.q("SELECT * FROM orders ORDER BY id DESC LIMIT 200")]  # all brokers, labelled


@app.get("/api/events")
def events(u: str = Depends(user)):
    return [dict(r) for r in db.q("SELECT id, ts, level, kind, message FROM events ORDER BY id DESC LIMIT 200")]


@app.post("/api/cycle")
def run_cycle(req: CycleReq, u: str = Depends(user)):
    broker = make_broker(db, client_id=API_CLIENT_ID)
    try:
        if req.ignore_hours and broker.is_live:
            raise HTTPException(400, "ignore_hours is only permitted on paper accounts")
        db.event("info", "manual", f"manual cycle by {u} (force={req.force})")
        rep = cycle(db, broker, force_trade=req.force, ignore_hours=req.ignore_hours)
        return json.loads(json.dumps(rep, default=str))
    finally:
        broker.close()


@app.post("/api/research/run")
def research_run(u: str = Depends(user)):
    out = run_lab(db)
    return {"selection": out["selection"]}


@app.post("/api/data/refresh")
def data_refresh(u: str = Depends(user)):
    return data.refresh(db)


@app.post("/api/kill")
def kill(t: Toggle, u: str = Depends(user)):
    # Engaging is always one click. RELEASING re-enables trading, so it needs the password.
    if not t.on:
        _reauth(u, t.password or "")
    RiskEngine(db).set_kill_switch(t.on, u)
    return {"kill_switch": t.on}


@app.post("/api/emergency-stop")
def emergency(u: str = Depends(user)):
    """Kill switch on, then cancel every working order at the broker. No password needed to STOP."""
    broker = None
    try:
        broker = make_broker(db, client_id=API_CLIENT_ID)
    except Exception:
        broker = None
    try:
        return emergency_stop(db, broker, u)
    finally:
        if broker is not None:
            broker.close()


@app.post("/api/resume")
def resume(p: PasswordOnly, u: str = Depends(user)):
    _reauth(u, p.password)
    RiskEngine(db).resume(u)
    return {"ok": True}


@app.post("/api/liquidate")
def liquidate(p: PasswordOnly, u: str = Depends(user)):
    _reauth(u, p.password)
    db.set("liquidate_requested", True)
    db.event("critical", "liquidation", f"emergency liquidation requested by {u}; executes next cycle")
    return {"ok": True, "note": "executes on the next cycle (reduce-only orders)"}


@app.post("/api/live/authorise")
def live_authorise(a: LiveAuth, u: str = Depends(user)):
    if PAPER_ONLY:
        db.event("warn", "security", f"live-trading authorisation attempted by {u}; blocked by paper-only lock")
        raise HTTPException(403, "ATLAS X is locked to paper trading; live authorisation is disabled in code")
    _reauth(u, a.password)
    if a.phrase.strip() != LIVE_PHRASE:
        raise HTTPException(400, "confirmation phrase does not match")
    perf = performance(db)
    if perf.get("tracked_days", 0) < MIN_PAPER_DAYS:
        raise HTTPException(412, f"need >= {MIN_PAPER_DAYS} days of paper track record "
                                 f"(have {perf.get('tracked_days', 0)})")
    if a.max_capital_gbp <= 0:
        raise HTTPException(400, "max capital must be positive")
    db.set("live_authorisation", {"by": u, "at": now(), "max_capital_gbp": a.max_capital_gbp,
                                   "permissions": "trade-only; no withdrawals/transfers"})
    db.event("critical", "live_authorised", f"LIVE trading authorised by {u}, cap £{a.max_capital_gbp:,.0f}")
    return {"ok": True}


@app.post("/api/live/revoke")
def live_revoke(u: str = Depends(user)):
    db.set("live_authorisation", None)
    db.event("critical", "live_revoked", f"live trading authorisation revoked by {u}")
    return {"ok": True}


@app.get("/api/report")
def report(u: str = Depends(user)):
    return {"markdown": daily_report(db)}


# ---------------------------------------------------------------- plain-language home / mandate / tax
class MandateIn(BaseModel):
    password: str
    capital_limit_gbp: float
    cash_reserve_gbp: float = 0.0
    wrapper: str = "taxable"
    tax_band: str = "higher"
    gilt_sleeve: str = "off"


class PauseIn(BaseModel):
    paused: bool


def _decisions_in_words(limit: int = 12) -> list[dict]:
    """Recent decisions, translated from the event log into plain sentences."""
    words = {"order": "Trade", "risk_reject": "Blocked by safety rules", "strategy_change": "Changed approach",
             "cash_flow": "Money in/out", "mandate": "Your mandate", "kill_switch": "Safety stop",
             "emergency_stop": "Emergency stop", "risk_suspend": "Investing suspended", "liquidation": "Sell-off",
             "broker_down": "Broker connection lost", "broker_up": "Broker connection restored",
             "live_account_blocked": "Blocked a non-paper account", "backup": "Backup"}
    rows = db.q(f"SELECT ts, kind, message FROM events WHERE kind IN ({','.join('?' * len(words))}) "
                "ORDER BY id DESC LIMIT ?", (*words, limit))
    return [{"when": r["ts"][:16].replace("T", " "), "what": words[r["kind"]], "detail": r["message"]} for r in rows]


def _name(key: str) -> str:
    if key in UNIVERSE:
        return UNIVERSE[key]["name"]
    meta = (db.get("gilt_meta", {}) or {}).get(key)
    return f"UK gilt: {meta['name']} (matures {meta['maturity']})" if meta else key


@app.get("/api/home")
def home(u: str = Depends(user)):
    from .attention import items, status_line
    from .evaluation import evaluate
    from .mandate import get as get_mandate
    from .mandate import sizing_capital
    from .reporting import active_broker
    from .tax import year_summary
    b = active_broker(db)
    snap = db.q("SELECT * FROM equity_snapshots WHERE broker=? ORDER BY ts DESC LIMIT 1", (b,))
    s = dict(snap[0]) if snap else {"equity": 0.0, "cash": 0.0, "positions": "{}"}
    pos = json.loads(s["positions"])
    invested = sum(p["value"] for p in pos.values())
    m = get_mandate(db)
    tgt = db.q("SELECT weights FROM targets ORDER BY id DESC LIMIT 1")
    target = json.loads(tgt[0]["weights"]) if tgt else None
    its = items(db)
    return {
        "status": status_line(its), "attention": its, "mode": MODE,
        "what_it_does": ("ATLAS X invests the capital you allow into a small set of low-cost, diversified "
                         "London-listed funds (currently about 60% global shares, 40% UK government bonds, chosen "
                         "by its research lab on evidence after costs). New money and dividends go to whichever "
                         "fund is below target, so it rarely needs to sell; it keeps tax and trading costs low, "
                         "and strict safety rules can stop it at any time. It does not try to beat the market by "
                         "frequent trading, because the evidence says that loses money for individual investors."),
        "money": {"account_value": s["equity"], "cash_at_broker": s["cash"], "invested": invested,
                  "capital_limit": m.capital_limit_gbp if m else None,
                  "cash_reserve": m.cash_reserve_gbp if m else None,
                  "atlas_may_invest": sizing_capital(m, s["equity"]),
                  "holdings": [{"fund": _name(k), "symbol": k, "value": v["value"],
                                "share": v["value"] / invested if invested else 0} for k, v in pos.items()]},
        "mandate": m.__dict__ if m else None,
        "gilts": {"verified": db.get("gilt_verified"), "last_plan": db.get("gilt_sleeve_report"),
                  "explain": ("UK government bonds (gilts) are free of capital gains tax; only their small "
                              "coupons are taxed. For a taxable account, low-coupon gilts held to maturity can "
                              "keep 1-2% a year more than a savings account or bond fund at the same headline "
                              "rate. In an ISA there is no tax either way.")},
        "results": evaluate(db, b, target),
        "tax": year_summary(db, b, m.wrapper if m else "taxable", m.tax_band if m else "higher") if m else None,
        "decisions": _decisions_in_words(),
        "risks": [
            "Investments can fall. In a severe crash (like 2008) a 60/40 mix could lose roughly a quarter to a third of its value.",
            "Safety limits stop new investing after losses; they do not automatically sell what is held.",
            "This is a paper (simulated) account: results show the system works, not that it will make money.",
            "Needs this PC on, online and logged in to IB Gateway. If anything stops, ATLAS stops trading rather than guessing.",
        ] if MODE == "paper" else [
            "Investments can fall. In a severe crash a 60/40 mix could lose roughly a quarter to a third of its value.",
            "Safety limits stop new investing after losses; they do not automatically sell what is held.",
            "Needs this PC on, online and logged in to IB Gateway. If anything stops, ATLAS stops trading rather than guessing.",
        ],
    }


@app.get("/api/weekly")
def weekly_pnl(goal: float = 0.30, start: float = 100.0, u: str = Depends(user)):
    from .reporting import active_broker
    from .weekly import goal_check, weekly
    goal = min(max(goal, 0.0), 10.0)
    start = min(max(start, 1.0), 1e9)
    return {"weekly": weekly(db, active_broker(db)), "goal": goal_check(goal, start), "mode": MODE}


@app.post("/api/mandate")
def set_mandate(mi: MandateIn, u: str = Depends(user)):
    from .mandate import Mandate, approve
    from .mandate import get as get_mandate
    _reauth(u, mi.password)
    cur = get_mandate(db) or Mandate()
    m = Mandate(capital_limit_gbp=mi.capital_limit_gbp, cash_reserve_gbp=mi.cash_reserve_gbp, wrapper=mi.wrapper,
                tax_band=mi.tax_band, paused=cur.paused, gilt_sleeve=mi.gilt_sleeve)
    try:
        return approve(db, m, u).__dict__
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/pause")
def pause(p: PauseIn, u: str = Depends(user)):
    from .mandate import set_paused
    return set_paused(db, p.paused, u).__dict__
