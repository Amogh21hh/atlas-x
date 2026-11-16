"""ATLAS X — Streamlit dashboard (PAPER TRADING ONLY).

Run locally:   streamlit run streamlit_app.py
Deployed on:   Streamlit Community Cloud (entry point = this file).

Safety design
-------------
* The process is forced into paper mode with the built-in simulated broker BEFORE `atlas` is imported.
  No IBKR connection is possible (the gateway port is pointed at an unreachable port), and the app stops
  if the code-level `PAPER_ONLY` lock is ever off.
* It uses its OWN database (data/streamlit/atlas-streamlit.db). It never opens the local worker's
  database (data/atlas.db), so running it on the PC that runs the IBKR paper worker changes nothing there.
* Anyone with the link can READ the dashboard. Every action that changes state needs the admin password,
  which lives only in Streamlit secrets (ATLAS_ADMIN_PASSWORD), never in the repository.
* Every order still goes through the unchanged risk engine (final authority). Live trading is disabled
  in code and has no control here.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
APP_DB = ROOT / "data" / "streamlit" / "atlas-streamlit.db"
SEED_PRICES = ROOT / "seed" / "prices.csv.gz"  # public daily closes; fallback when live download fails
ADMIN_SESSION_S = 30 * 60


def _secret(name: str, default: str = "") -> str:
    """Read one value from Streamlit secrets (or the environment). Never logged or displayed."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets.toml at all
        pass
    return os.environ.get(name, default)


# ------------------------------------------------------------------ hard paper-only environment
# These are set (not defaulted) so nothing in .env or the host environment can switch them.
os.environ["ATLAS_MODE"] = "paper"
os.environ["ATLAS_BROKER"] = "paper"
os.environ["IBKR_HOST"] = "127.0.0.1"
os.environ["IBKR_PORT"] = "1"           # unreachable: an accidental IBKR connection fails at once
APP_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["ATLAS_DB"] = str(APP_DB)    # never the worker's data/atlas.db
for _k in ("ATLAS_NOTIFY_WEBHOOK", "ATLAS_PAPER_CASH"):
    _v = _secret(_k)
    if _v:
        os.environ[_k] = _v
sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="ATLAS X — paper trading", page_icon="📈", layout="wide")

from atlas import config  # noqa: E402

if config.PAPER_ONLY is not True or config.MODE != "paper" or config.settings.broker != "paper":
    st.error("Refusing to start: this dashboard runs only with the paper-only lock on, in paper mode.")
    st.stop()

from atlas import api, data  # noqa: E402
from atlas.brokers.paper import PaperBroker  # noqa: E402
from atlas.config import UNIVERSE, settings  # noqa: E402
from atlas.db import now  # noqa: E402
from atlas.engine import cycle, lse_open  # noqa: E402
from atlas.execution import emergency_stop  # noqa: E402
from atlas.mandate import TAX_BANDS, WRAPPERS, Mandate, approve, set_paused  # noqa: E402
from atlas.mandate import get as get_mandate  # noqa: E402
from atlas.research.lab import run_lab  # noqa: E402
from atlas.risk import RiskEngine  # noqa: E402

db = api.db
if Path(str(settings.db_path)).resolve() != APP_DB.resolve():
    st.error("Refusing to start: the dashboard database is not the isolated Streamlit database.")
    st.stop()
if db.get("broker") not in (None, "paper"):
    st.error("Refusing to start: this database is configured for a non-paper broker.")
    st.stop()

try:
    AUTO_CYCLE_MIN = max(0, int(_secret("ATLAS_AUTO_CYCLE_MINUTES", "15") or 0))
except ValueError:
    AUTO_CYCLE_MIN = 15
ADMIN_PW = _secret("ATLAS_ADMIN_PASSWORD")
ADMIN_ENABLED = len(ADMIN_PW) >= 12


def paper_broker() -> PaperBroker:
    b = PaperBroker(db, lambda: data.latest_prices_gbp(db)[0])
    assert b.is_live is False and b.name == "paper"
    return b


# ------------------------------------------------------------------ background jobs (one at a time)
@st.cache_resource
def _runner() -> dict:
    return {"lock": threading.Lock(), "job": None, "started": None, "error": None, "result": None,
            "login_fail": []}


def start_job(name: str, fn) -> bool:
    r = _runner()
    if not r["lock"].acquire(blocking=False):
        return False

    def work():
        try:
            r["result"] = {"job": name, "at": now(), "out": fn()}
            r["error"] = None
        except Exception as e:  # shown on the page and recorded; never crashes the app
            r["error"] = f"{name} failed: {e}"
            db.event("warn", "streamlit_job", r["error"])
        finally:
            r["job"] = None
            r["lock"].release()

    r["job"], r["started"] = name, time.time()
    threading.Thread(target=work, daemon=True, name=f"atlas-{name}").start()
    return True


def load_seed_prices() -> str:
    """Load the bundled snapshot of public daily closing prices (seed/prices.csv.gz).

    Used only when the live download fails on a database that has no prices at all, for example when
    Yahoo rate-limits the cloud host on a fresh deployment. Existing rows are never overwritten. The
    risk engine still blocks trading on stale prices, so a snapshot can never cause trades on old data."""
    df = pd.read_csv(SEED_PRICES)
    df = df[df["symbol"].isin(list(UNIVERSE))]
    db.many("INSERT OR IGNORE INTO prices(symbol,date,close,adjclose,volume) VALUES(?,?,?,?,?)",
            [(r.symbol, r.date, float(r.close), float(r.adjclose), float(r.volume or 0)) for r in df.itertuples()])
    last = str(df["date"].max())
    db.set("prices_source", f"bundled snapshot to {last}")
    db.set("data_refreshed_at", now())  # stops the engine retrying a blocked download inside every cycle
    db.event("warn", "data_refresh", f"live download unavailable; loaded bundled price snapshot to {last}")
    return last


def tolerant_refresh() -> dict:
    """Refresh each ETF separately so one failed download does not block the rest. Symbols that fail
    keep their old prices, and the risk engine blocks trading in them once those prices go stale."""
    db.set("data_refresh_attempt_at", now())
    out = {}
    for s in UNIVERSE:
        try:
            out[s] = data.refresh(db, [s])[s]
        except Exception as e:
            out[s] = {"error": str(e)}
            db.event("warn", "data_refresh", f"{s}: {e}")
    if all("error" in v for v in out.values()):
        if not has_prices() and SEED_PRICES.exists():
            return {"seed": load_seed_prices()}
        if has_prices():
            # Keep the stored prices and stop the engine retrying the download inside this cycle. The
            # risk engine judges freshness by each price's own date, so stale prices still block trading.
            db.set("data_refreshed_at", now())
            db.set("prices_source", f"stored prices (live download failed at {now()[:16]} UTC)")
        raise RuntimeError("no market data could be downloaded")
    db.set("prices_source", "live (Yahoo Finance daily closes)")
    return out


def refresh_due() -> bool:
    """Prices older than 6 hours, and no download attempt in the last hour (avoids hammering a blocked host)."""
    return _older_than("data_refreshed_at", timedelta(hours=6)) and _older_than("data_refresh_attempt_at", timedelta(hours=1))


def _older_than(key: str, delta: timedelta) -> bool:
    v = db.get(key)
    if not v:
        return True
    t = datetime.fromisoformat(v)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t < datetime.now(timezone.utc) - delta


def has_prices() -> bool:
    return bool(db.q("SELECT 1 FROM prices LIMIT 1"))


def bootstrap() -> dict:
    db.set("broker", "paper")
    steps = []
    if not has_prices() or refresh_due():
        res = tolerant_refresh()
        steps.append("seed_prices_loaded" if "seed" in res else "data_refreshed")
    if _older_than("lab_ran_at", timedelta(days=7)):
        run_lab(db)
        steps.append("lab_ran")
    rep = cycle(db, paper_broker())
    return {"steps": steps + rep.get("steps", []), "equity": rep.get("equity")}


def auto_cycle() -> dict:
    if refresh_due():
        try:
            tolerant_refresh()
        except RuntimeError:
            pass  # keep the stored prices; stale ones are blocked by the risk engine
    rep = cycle(db, paper_broker())
    db.set("streamlit_auto_cycle_at", now())
    return {"steps": rep.get("steps", []), "equity": rep.get("equity")}


def needs_bootstrap() -> bool:
    return not has_prices() or not db.get("lab_ran_at") or not db.get("last_cycle")


# ------------------------------------------------------------------ admin access
def is_admin() -> bool:
    return ADMIN_ENABLED and st.session_state.get("admin_until", 0) > time.time()


def try_login(pw: str) -> str | None:
    r = _runner()
    cutoff = time.time() - 900
    r["login_fail"] = [t for t in r["login_fail"] if t > cutoff]
    mine = [t for t in st.session_state.get("login_fail", []) if t > cutoff]
    if len(mine) >= 5 or len(r["login_fail"]) >= 30:
        return "Too many attempts. Wait 15 minutes."
    if hmac.compare_digest(pw.encode(), ADMIN_PW.encode()):
        st.session_state["admin_until"] = time.time() + ADMIN_SESSION_S
        st.session_state["login_fail"] = []
        db.event("info", "security", "streamlit admin signed in")
        return None
    st.session_state["login_fail"] = mine + [time.time()]
    r["login_fail"].append(time.time())
    db.event("warn", "security", "failed streamlit admin sign-in")
    return "Incorrect password."


# ------------------------------------------------------------------ formatting helpers
def gbp(v) -> str:
    return "—" if v is None else f"£{v:,.2f}"


def gbp0(v) -> str:
    return "—" if v is None else f"£{v:,.0f}"


def pct(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}%}"


CLOUD_IRRELEVANT = ("background worker", "Interactive Brokers", "ATLAS_NOTIFY_WEBHOOK", "automatic backup",
                    "investor login")


def attention_items(home: dict) -> list[dict]:
    out = [i for i in home["attention"] if not any(x in i["what"] + i["todo"] for x in CLOUD_IRRELEVANT)]
    for i in out:  # the Streamlit controls replace the local dashboard's password prompts
        i["todo"] = i["todo"].replace("on the Controls panel with your password", "in the Controls tab")
        i["todo"] = i["todo"].replace("with your password", "in the Controls tab")
        i["todo"] = i["todo"].replace("Open 'Your mandate'", "Open Controls → Mandate")
    return out


# ================================================================== page
st.markdown(
    "<div style='padding:.6rem 1rem;border-radius:.5rem;background:#7a1f1f;color:#fff;font-weight:600'>"
    "PAPER TRADING ONLY — simulated money, no real orders. Live trading is disabled in code."
    "</div>", unsafe_allow_html=True)
st.title("ATLAS X")
st.caption("Tax- and cost-aware capital allocator for a UK investor · London-listed UCITS ETFs · "
           "risk engine has the final say on every order")

runner = _runner()

# First visit on a fresh deployment: download prices, run the research lab, take a first snapshot.
if needs_bootstrap():
    if runner["job"] is None and not runner["error"]:
        start_job("setup", bootstrap)

    @st.fragment(run_every=4)
    def _setup_progress():
        r = _runner()
        if r["job"]:
            secs = int(time.time() - (r["started"] or time.time()))
            st.info(f"Setting up the paper account: downloading 10 ETF price histories and running the "
                    f"research lab ({r['job']}, {secs}s so far; usually 1–3 minutes).")
        elif r["error"]:
            st.error(r["error"])
            if st.button("Try setup again"):
                r["error"] = None
                st.rerun()
        else:
            st.rerun()

    _setup_progress()
    st.stop()

if AUTO_CYCLE_MIN and runner["job"] is None and _older_than("streamlit_auto_cycle_at", timedelta(minutes=AUTO_CYCLE_MIN)):
    start_job("auto-cycle", auto_cycle)

home = api.home(u="viewer")
ov = api.overview(u="viewer")
money = home["money"]
risk_state = ov["risk"]
items = attention_items(home)

# ---- sidebar: status + admin sign-in
with st.sidebar:
    st.subheader("Status")
    urgent = any(i["level"] == "urgent" for i in items)
    review = any(i["level"] in ("warn", "action") for i in items)
    if urgent:
        st.error("Needs attention")
    elif review:
        st.warning("Running — a few things to review")
    else:
        st.success("Running normally")
    st.write("**Broker:** simulated paper broker")
    st.write(f"**LSE open now:** {'yes' if lse_open() else 'no'}")
    lc = db.get("last_cycle") or {}
    st.write(f"**Last cycle:** {lc.get('at', '—')[:16].replace('T', ' ')} UTC")
    if runner["job"]:
        st.info(f"Running: {runner['job']}…")
    if runner["error"]:
        st.warning(runner["error"])
    st.divider()
    st.subheader("Admin")
    if not ADMIN_ENABLED:
        st.caption("Controls are read-only: no admin password (12+ characters) is configured in Streamlit secrets.")
    elif is_admin():
        st.success("Signed in as admin")
        if st.button("Sign out"):
            st.session_state["admin_until"] = 0
            st.rerun()
    else:
        with st.form("login", clear_on_submit=True):
            pw = st.text_input("Admin password", type="password")
            if st.form_submit_button("Sign in"):
                err = try_login(pw)
                if err:
                    st.error(err)
                else:
                    st.rerun()

tabs = st.tabs(["Overview", "Performance", "Research lab", "Orders & events", "Controls", "About & safety"])

# ---- Overview
with tabs[0]:
    if items:
        st.subheader("Needs your attention")
        for i in items:
            box = {"urgent": st.error, "warn": st.warning, "action": st.warning}.get(i["level"], st.info)
            box(f"**{i['what']}**  \n{i['todo']}")
    st.subheader("Your money (paper)")
    c = st.columns(4)
    c[0].metric("Account value", gbp0(money["account_value"]))
    c[1].metric("Invested", gbp0(money["invested"]))
    c[2].metric("Cash at broker", gbp0(money["cash_at_broker"]))
    c[3].metric("ATLAS may invest", gbp0(money["atlas_may_invest"]))
    m = home["mandate"] or {}
    st.caption(f"Mandate: capital limit {gbp(m.get('capital_limit_gbp'))}, cash reserve {gbp(m.get('cash_reserve_gbp'))}, "
               f"account {m.get('wrapper', '—')}{' · provisional (not yet approved)' if m.get('provisional') else ''}"
               f"{' · PAUSED' if m.get('paused') else ''}")

    st.subheader("Holdings vs target")
    target = (ov.get("target") or {}).get("weights", {})
    pos = ov["positions"]
    inv = sum(p["value"] for p in pos.values()) or 0.0
    rows = []
    for s in sorted(set(pos) | {k for k, v in target.items() if v > 0}):
        p = pos.get(s, {})
        rows.append({"Fund": UNIVERSE.get(s, {}).get("name", s), "Symbol": s, "Qty": p.get("qty", 0.0),
                     "Price £": p.get("price"), "Value £": p.get("value", 0.0),
                     "Weight": (p.get("value", 0.0) / inv) if inv else 0.0, "Target": target.get(s, 0.0),
                     "Unrealised P&L £": (p["value"] - p["qty"] * p["avg_cost"]) if p else 0.0})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"Weight": st.column_config.NumberColumn(format="percent"),
                                    "Target": st.column_config.NumberColumn(format="percent"),
                                    "Price £": st.column_config.NumberColumn(format="%.4f"),
                                    "Value £": st.column_config.NumberColumn(format="%.2f"),
                                    "Unrealised P&L £": st.column_config.NumberColumn(format="%.2f")})
    else:
        st.write("No holdings or target yet.")
    sel = ov["selection"]
    st.write(f"**Strategy:** `{sel['name']}` — {sel.get('rationale', '')}")

    st.subheader("What it decided recently")
    if home["decisions"]:
        st.dataframe(pd.DataFrame(home["decisions"]), hide_index=True, width="stretch")
    else:
        st.write("No decisions yet. Trades happen only while the London Stock Exchange is open.")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Risk engine (final authority)")
        st.write(f"Kill switch: **{'ENGAGED' if risk_state['kill_switch'] else 'off'}**")
        st.write(f"Suspended: **{risk_state['suspended'] or 'no'}**")
        st.write(f"Peak equity: {gbp(risk_state['peak_equity'])} · day start: {gbp(risk_state['day_start_equity'])}")
        st.write(f"Turnover today: {gbp(risk_state['day_turnover'])}")
        L = ov["limits"]
        st.caption(f"Limits: no leverage, no shorting · daily loss stop {L['daily_loss_limit']:.0%} · "
                   f"drawdown stop {L['max_drawdown']:.0%} · max order {L['max_order_fraction']:.0%} of equity · "
                   f"stale prices (> {L['max_data_staleness_days']} business days) block trading · whitelist only")
    with c2:
        st.subheader("Market data")
        st.write(f"Refreshed: {(ov['data']['refreshed_at'] or '—')[:16].replace('T', ' ')} UTC")
        src = db.get("prices_source")
        if src:
            if src.startswith("live"):
                st.caption(f"Price source: {src}.")
            else:
                st.warning(f"Price source: {src}. Live download is unavailable right now; trading is blocked "
                           "once prices go stale.")
        st.dataframe(pd.DataFrame([{"Symbol": k, "Latest close": v, "Business days old": ov["data"]["stale"].get(k)}
                                   for k, v in ov["data"]["latest"].items()]), hide_index=True, width="stretch")

# ---- Performance
with tabs[1]:
    eq = api.equity(u="viewer")
    st.subheader("Account equity (paper broker)")
    if eq:
        df = pd.DataFrame(eq)
        df["ts"] = pd.to_datetime(df["ts"], format="ISO8601")
        st.line_chart(df.set_index("ts")[["equity", "cash"]])
    else:
        st.write("No snapshots yet.")
    res = home["results"]
    st.subheader("Results so far vs simple alternatives")
    if res.get("rows"):
        st.dataframe(pd.DataFrame([{"Alternative": r["name"], "Return": r["return"]} for r in res["rows"]]),
                     hide_index=True, width="stretch",
                     column_config={"Return": st.column_config.NumberColumn(format="percent")})
        st.caption(f"Since {res['since'][:10]} · {res['tracked_days']} tracked day(s) · commissions {gbp(res['commissions_paid'])} "
                   f"· max drawdown {pct(res['max_drawdown'])}. {res['caveat']}")
    else:
        st.write(res.get("status", "No funded history yet."))

    st.subheader("Week by week")
    wk = api.weekly_pnl(goal=0.30, start=100.0, u="viewer")["weekly"]
    if wk["weeks"]:
        st.dataframe(pd.DataFrame(wk["weeks"]), hide_index=True, width="stretch",
                     column_config={"return": st.column_config.NumberColumn(format="percent")})
        st.caption(f"Total P&L {gbp(wk['total_pnl'])} · fees {gbp(wk['total_fees'])} · cumulative return "
                   f"{pct(wk['cumulative_return'])}")
    else:
        st.write("No weekly history yet.")

    st.subheader("Is my goal realistic?")
    g1, g2 = st.columns(2)
    goal = g1.number_input("Weekly return goal (%)", 0.0, 1000.0, 30.0, 0.5) / 100
    start_amt = g2.number_input("Starting amount (£)", 1.0, 1e9, 100.0, 50.0)
    gc = api.weekly_pnl(goal=goal, start=start_amt, u="viewer")["goal"]
    st.info(gc["verdict"])
    st.caption("If achieved: " + ", ".join(f"{k.replace('_', ' ')} → £{v:,.0f}" for k, v in gc["if_achieved"].items()))
    if gc.get("evidence"):
        with st.expander("Evidence from real weekly returns since 2014"):
            st.dataframe(pd.DataFrame(gc["evidence"]), hide_index=True, width="stretch")

    st.subheader("Tax this year (estimate)")
    tax = home["tax"]
    if tax:
        t = tax["estimated_tax"]
        st.write(f"Tax year {tax['tax_year']} · account {tax['wrapper']} · band {tax['tax_band']}"
                 f"{' · sheltered' if tax['sheltered'] else ''}")
        tc = st.columns(4)
        tc[0].metric("Realised gain", gbp0(tax["net_realised_gain"]))
        tc[1].metric("Est. dividends", gbp0(tax["estimated_dividends"]))
        tc[2].metric("Est. interest", gbp0(tax["estimated_interest"]))
        tc[3].metric("Est. tax", gbp0(t["total"]))
        st.caption(tax["note"])

# ---- Research lab
with tabs[2]:
    rs = api.research(u="viewer")
    st.subheader("Evidence gate")
    gate = rs["gate"]
    st.write(f"A strategy must pass **all** of: OOS Sharpe ≥ {gate['oos_sharpe_min']}, deflated Sharpe ≥ {gate['dsr_min']}, "
             f"OOS max drawdown ≥ {gate['oos_max_dd']:.0%}, ≥ {gate['robust_min']:.0%} of its parameter grid "
             "profitable in-sample, executable within the risk limits, and active strategies must beat passive. "
             "If nothing passes, ATLAS holds cash.")
    st.write(f"**Selected:** `{rs['selection']['name']}` — {rs['selection'].get('rationale', '')}")
    st.caption(f"Last run: {(rs['run_at'] or '—')[:16].replace('T', ' ')} UTC")
    if rs["strategies"]:
        st.dataframe(pd.DataFrame([{
            "Strategy": s["name"], "Passed": s["passed"], "Benchmark": s["metrics"].get("benchmark"),
            "OOS Sharpe": s["metrics"]["oos"].get("sharpe"), "OOS max DD": s["metrics"]["oos"].get("max_dd"),
            "OOS CAGR": s["metrics"]["oos"].get("cagr"), "DSR": s["metrics"].get("dsr"),
            "Robustness": s["metrics"].get("robustness"), "Params": json.dumps(s["params"]),
            "Why rejected": "; ".join(s["reasons"])} for s in rs["strategies"]]),
            hide_index=True, width="stretch",
            column_config={"OOS max DD": st.column_config.NumberColumn(format="percent"),
                           "OOS CAGR": st.column_config.NumberColumn(format="percent"),
                           "Robustness": st.column_config.NumberColumn(format="percent"),
                           "OOS Sharpe": st.column_config.NumberColumn(format="%.2f"),
                           "DSR": st.column_config.NumberColumn(format="%.2f")})
        curves = {}
        for s in rs["strategies"]:
            if s["equity"]:
                curves[s["name"]] = pd.Series({pd.Timestamp(d): v for d, v in s["equity"]})
        if curves:
            st.subheader("Backtest growth of £1 (net of costs)")
            st.line_chart(pd.DataFrame(curves))
            st.caption("A backtest is not evidence of future returns.")

# ---- Orders & events
with tabs[3]:
    st.subheader("Orders")
    o = api.orders(u="viewer")
    if o:
        st.dataframe(pd.DataFrame(o), hide_index=True, width="stretch")
    else:
        st.write("No orders yet.")
    st.subheader("Events")
    e = api.events(u="viewer")
    if e:
        st.dataframe(pd.DataFrame(e), hide_index=True, width="stretch")
    else:
        st.write("No events yet.")

# ---- Controls
with tabs[4]:
    if not ADMIN_ENABLED:
        st.info("Read-only deployment. To enable controls, set ATLAS_ADMIN_PASSWORD (12+ characters) in the app's "
                "Streamlit secrets.")
    elif not is_admin():
        st.info("Sign in as admin in the sidebar to use the controls.")
    else:
        busy = runner["job"] is not None
        if runner["result"]:
            with st.expander(f"Last job result: {runner['result']['job']} at {runner['result']['at'][:19]}"):
                st.json(json.loads(json.dumps(runner["result"]["out"], default=str)))
        st.subheader("Run")
        cc = st.columns(3)
        ign = cc[0].checkbox("Ignore market hours (paper only)")
        force = cc[0].checkbox("Force rebalance (ignore no-trade band)")
        if cc[0].button("Run a paper cycle now", disabled=busy):
            db.event("info", "manual", f"manual cycle from Streamlit (force={force}, ignore_hours={ign})")
            start_job("manual cycle", lambda: cycle(db, paper_broker(), force_trade=force, ignore_hours=ign))
            st.rerun()
        if cc[1].button("Refresh market data", disabled=busy):
            start_job("data refresh", tolerant_refresh)
            st.rerun()
        if cc[2].button("Re-run research lab", disabled=busy):
            start_job("research lab", lambda: run_lab(db)["selection"])
            st.rerun()
        if busy:
            st.caption(f"Busy: {runner['job']}. Reload the page to see the result.")

        st.subheader("Safety")
        r = RiskEngine(db)
        sc = st.columns(3)
        if sc[0].button("EMERGENCY STOP (kill switch + cancel orders)", type="primary"):
            emergency_stop(db, paper_broker(), "streamlit-admin")
            st.rerun()
        if risk_state["kill_switch"]:
            ok = sc[1].checkbox("I have checked why trading was halted")
            if sc[1].button("Release kill switch", disabled=not ok):
                r.set_kill_switch(False, "streamlit-admin")
                st.rerun()
        if risk_state["suspended"]:
            ok2 = sc[2].checkbox("I have reviewed the suspension")
            if sc[2].button("Resume after suspension", disabled=not ok2):
                r.resume("streamlit-admin")
                st.rerun()
        mnow = get_mandate(db) or Mandate()
        if st.button("Resume investing" if mnow.paused else "Pause new investing"):
            set_paused(db, not mnow.paused, "streamlit-admin")
            st.rerun()
        with st.expander("Queue an emergency sell-off (reduce-only, executes on the next cycle)"):
            ok3 = st.checkbox("Yes, sell all paper holdings")
            if st.button("Queue sell-off", disabled=not ok3):
                db.set("liquidate_requested", True)
                db.event("critical", "liquidation", "emergency liquidation requested from Streamlit; executes next cycle")
                st.rerun()

        st.subheader("Mandate")
        with st.form("mandate"):
            cap = st.number_input("Capital limit (£) — the most ATLAS will ever invest", 1.0, 1e9,
                                  float(mnow.capital_limit_gbp), 500.0)
            res_ = st.number_input("Cash reserve (£) — kept as cash", 0.0, 1e9, float(mnow.cash_reserve_gbp), 100.0)
            wr = st.selectbox("Account type", WRAPPERS, index=WRAPPERS.index(mnow.wrapper))
            tb = st.selectbox("Tax band (reporting only)", TAX_BANDS, index=TAX_BANDS.index(mnow.tax_band))
            st.caption("The UK gilt sleeve needs the IBKR gateway, so it is not available on the simulated broker.")
            if st.form_submit_button("Approve mandate"):
                try:
                    approve(db, Mandate(capital_limit_gbp=cap, cash_reserve_gbp=res_, wrapper=wr, tax_band=tb,
                                        paused=mnow.paused, gilt_sleeve="off"), "streamlit-admin")
                    st.success("Mandate approved.")
                except ValueError as ex:
                    st.error(str(ex))

    st.subheader("Live trading")
    st.error("Disabled in code (PAPER_ONLY = True). There is no way to enable it from this app; it would need a "
             "reviewed code change, a forward paper record and a trade-only broker user.")

# ---- About & safety
with tabs[5]:
    st.subheader("What ATLAS X does")
    st.write(home["what_it_does"])
    st.subheader("This deployment")
    st.write("- Runs the **built-in simulated paper broker**: fills at the latest real closing price ± spread, plus "
             "commission. It is not connected to any brokerage.\n"
             f"- A paper cycle runs when someone visits and the last one is older than {AUTO_CYCLE_MIN or '—'} minutes; "
             "trades happen only while the London Stock Exchange is open.\n"
             "- Streamlit Community Cloud storage is temporary: a reboot or redeploy starts a fresh paper account.\n"
             "- Prices come from Yahoo Finance's public daily history.")
    st.subheader("Safeguards that stay on")
    st.write("- `PAPER_ONLY = True` in code; this app refuses to start otherwise.\n"
             "- The risk engine is the only component that can approve an order: kill switch, whitelist, per-ETF "
             "caps, no leverage or shorting, order-size, turnover, daily-loss and drawdown breakers, stale-data block.\n"
             "- Viewing is public; every control needs the admin password stored in Streamlit secrets.")
    st.subheader("Risks to know")
    st.write("- Investments can fall. In a severe crash (like 2008) a 60/40 mix could lose roughly a quarter to a third.\n"
             "- Safety limits stop new investing after losses; they do not automatically sell what is held.\n"
             "- This is a simulated account: results show the system works, not that it will make money.")
    st.caption("ATLAS X is software, not regulated investment advice.")
