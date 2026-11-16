"""Operator commands.

    python -m atlas.cli ibkr-check               # connect, verify account type, contracts, price units, cash
    python -m atlas.cli ibkr-check --roundtrip   # + buy then sell 1 share of the cash-proxy ETF (PAPER ONLY)
"""
from __future__ import annotations

import argparse
import sys
import time

from . import data
from .config import CASH_PROXY, ISINS, UNIVERSE
from .db import DB

CHECK_CLIENT_ID = 19  # distinct from worker (17) and API (18)


def _p(ok: bool | None, msg: str) -> None:
    tag = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    print(f"[{tag}] {msg}", flush=True)


def ibkr_check(roundtrip: bool = False, port: int | None = None, cancel_test: bool = False) -> int:
    from .brokers.ibkr import IBKRBroker
    db = DB()
    b = IBKRBroker(port=port, client_id=CHECK_CLIENT_ID, scales=db.get("ibkr_scale", {}) or {})
    failures = 0
    try:
        h = b.health()
        if not h["ok"]:
            _p(False, f"cannot connect to IB Gateway at {b.host}:{b.port}: {h.get('error')}")
            print("      Is IB Gateway running and logged in? Configure > Settings > API: enable socket "
                  "clients, port 4002 (paper), add 127.0.0.1 to trusted IPs, untick 'Read-Only API' "
                  "only if you want orders.")
            return 1
        _p(True, f"connected: account {h['account']} on port {b.port}")
        if h["live"] is not False:
            _p(False, f"account {h.get('account')} is NOT a verified paper (DU...) account. ATLAS X is paper-only: "
                      "nothing further is run against it. Log out and log in with Paper Trading selected.")
            return 1
        _p(True, "paper account verified (DU...)")
        values = {(v.tag, v.currency): v.value for v in b.ib.accountValues(h["account"])}
        _p(None, f"account currencies with cash: "
                 + ", ".join(f"{cur} {float(val):,.2f}" for (tag, cur), val in values.items()
                             if tag == "TotalCashValue" and cur != "BASE"))

        # contracts + price units
        if not db.q("SELECT 1 FROM prices LIMIT 1"):
            data.refresh(db)
        ref, _ = data.latest_prices_gbp(db)
        scales = {}
        for s in UNIVERSE:
            try:
                c = b.contract(s)
            except Exception as e:
                failures += 1
                _p(False, f"{s}: contract not found ({e})")
                continue
            try:
                det = b.contract_details(s)
            except Exception as e:
                det = {}
                _p(None, f"{s}: contract details unavailable ({e})")
            if det:
                exp = ISINS.get(s)
                ok_id = (det.get("isin") == exp) if det.get("isin") else None
                if ok_id is False:
                    failures += 1
                _p(ok_id, f"{s}: {det.get('name')} | ISIN {det.get('isin') or 'n/a'} (expected {exp}) | "
                          f"{det.get('primary') or det.get('exchange')} {det.get('currency')} | class {det.get('trading_class')}")
            try:
                q = b.quote(s)
            except Exception as e:
                q = None
                _p(None, f"{s}: quote request failed ({e})")
            if q and ref.get(s):
                ratio = q / ref[s]
                if 0.8 < ratio < 1.25:
                    scales[s] = 1.0
                elif 80 < ratio < 125:
                    scales[s] = 0.01
                else:
                    failures += 1
                    _p(False, f"{s}: IBKR price {q} vs platform £{ref[s]:.4f} (ratio {ratio:.2f}) — units unclear")
                    continue
                unit = "pence" if scales[s] == 0.01 else "pounds"
                _p(True, f"{s}: conId {c.conId} on {c.exchange or c.primaryExchange}, quoted in {unit} "
                         f"({q} vs £{ref[s]:.4f})")
            else:
                _p(None, f"{s}: conId {c.conId} qualified; no quote available (market closed / no data) — "
                         f"price-unit check skipped")
        if scales:
            merged = {**(db.get("ibkr_scale", {}) or {}), **scales}
            db.set("ibkr_scale", merged)
            b.scales = merged
            _p(True, f"stored price scales for {len(scales)} ETFs")

        acct = b.account()
        if acct.cash > 0:
            _p(True, f"GBP cash £{acct.cash:,.2f}; {len(acct.positions)} open positions")
        else:
            failures += 1
            _p(False, "no GBP cash found. Set the paper account base currency to GBP (or convert some cash "
                      "to GBP in TWS); the risk engine refuses buys without settled GBP cash")

        if roundtrip or cancel_test:
            if failures:
                _p(False, "skipping order tests because earlier checks failed")
            else:
                if cancel_test:
                    failures += _cancel_test(b, ref, db)
                if roundtrip:
                    failures += _roundtrip(b, ref, db)
    finally:
        b.close()
    print("\nRESULT:", "all checks passed" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


def _risk_ok(b, db, sym: str, side: str, ref: dict) -> bool:
    """Test orders go through the real risk engine, like every other order."""
    from .risk import ProposedOrder, RiskEngine
    _, dates = data.latest_prices_gbp(db)
    acct = b.account()
    held = {s: p.qty * ref.get(s, 0.0) for s, p in acct.positions.items()}
    equity = acct.cash + sum(held.values())
    rep = RiskEngine(db).review([ProposedOrder(sym, side, 1, ref[sym])], equity, acct.cash, held, dates, b.is_live)
    if rep.rejected:
        _p(False, f"risk engine rejected test {side} of 1 {sym}: {rep.rejected[0][1]}")
        return False
    _p(True, f"risk engine approved test {side} of 1 {sym}")
    return True


def _wait(b, st, terminal=("filled", "cancelled", "rejected"), secs=90):
    t0 = time.time()
    while st.status not in terminal and time.time() - t0 < secs:
        time.sleep(3)
        st = b.order_status(st.broker_order_id)
    return st


def _cancel_test(b, ref: dict, db) -> int:
    """Place a 1-share BUY limit far below market (cannot fill), confirm the broker holds it, cancel it,
    confirm the broker reports it cancelled. Works when the market is closed."""
    from ib_async import LimitOrder
    sym = CASH_PROXY
    if b.is_live or not _risk_ok(b, db, sym, "buy", ref):
        return 1
    scale = b.scales.get(sym, 1.0)
    limit = round(ref[sym] * 0.5 / scale, 2)        # half the market price: will not fill
    order = LimitOrder("BUY", 1, limit)
    order.tif = "DAY"
    order.orderRef = "atlas-cancel-test"
    trade = b.ib.placeOrder(b.contract(sym), order)
    b.ib.sleep(3)
    oid = str(trade.order.orderId)
    b._trades[oid] = (sym, trade)
    status = trade.orderStatus.status
    if status not in ("PreSubmitted", "Submitted", "PendingSubmit"):
        msg = trade.log[-1].message if trade.log else ""
        _p(False, f"limit order not accepted by broker: {status} {msg}")
        return 1
    _p(True, f"broker accepted 1-share limit BUY {sym} @ {limit} (order {oid}, status {status})")
    b.ib.cancelOrder(trade.order)
    st = _wait(b, b.order_status(oid), terminal=("cancelled", "filled", "rejected"), secs=30)
    if st.status != "cancelled":
        _p(False, f"cancellation not confirmed by broker: {st.status} {st.message}")
        return 1
    _p(True, f"broker confirmed cancellation of order {oid}")
    return 0


def _ledger(db, sym: str, side: str, st) -> None:
    """Record the test fill in the platform's order ledger, so worker reconciliation stays exact."""
    from .db import now
    db.x("INSERT INTO orders(created_at,broker,broker_order_id,symbol,side,qty,ref_price,status,filled_qty,"
         "avg_fill_price,commission,risk_decision,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (now(), "ibkr", st.broker_order_id, sym, side, 1, st.avg_fill_price, st.status, st.filled_qty,
          st.avg_fill_price, st.commission, "approved: ibkr-check round-trip test", now()))


def _roundtrip(b, ref: dict, db) -> int:
    from .engine import lse_open
    sym = CASH_PROXY
    if b.is_live:  # belt and braces: never place orders on a live account from this tool
        _p(False, "refusing round-trip on a live account")
        return 1
    if not lse_open():
        _p(None, "round-trip skipped: LSE is closed (Mon-Fri 08:05-16:25 UK). A market order now would sit "
                 "queued until the open, so the tool does not place one.")
        return 0
    if not _risk_ok(b, db, sym, "buy", ref):
        return 1
    before = b.account().positions.get(sym)
    q0 = before.qty if before else 0.0
    _p(None, f"round-trip: BUY 1 {sym} (paper)")
    st = _wait(b, b.submit_market(sym, "buy", 1, ref[sym], ref="atlas-roundtrip-buy"))
    if st.status != "filled":
        _p(False, f"buy not filled within 90s: {st.status} {st.message}")
        if st.status not in ("cancelled", "rejected"):
            b.cancel(st.broker_order_id)
        return 1
    _ledger(db, sym, "buy", st)
    _p(True, f"buy filled 1 @ £{st.avg_fill_price:.4f}, commission {st.commission}")
    after = b.account().positions.get(sym)
    if not after or abs(after.qty - (q0 + 1)) > 1e-9:
        _p(False, f"position not updated at broker (before {q0}, after {after.qty if after else 0})")
        return 1
    _p(True, f"broker position confirmed: {after.qty:g}")
    if not _risk_ok(b, db, sym, "sell", ref):
        return 1
    st = _wait(b, b.submit_market(sym, "sell", 1, ref[sym], ref="atlas-roundtrip-sell"))
    if st.status != "filled":
        _p(False, f"sell not filled: {st.status} {st.message}")
        return 1
    _ledger(db, sym, "sell", st)
    final = b.account().positions.get(sym)
    _p(True, f"sell filled 1 @ £{st.avg_fill_price:.4f}; broker position back to {final.qty if final else 0:g}")
    return 0


def verify_fills(port: int | None = None, out_dir: str = "docs/evidence") -> int:
    """Compare IBKR executions + commissions with the ATLAS order ledger and broker positions, and
    write a dated evidence file. Read-only: places no orders."""
    import pathlib
    from datetime import date

    from .brokers.ibkr import IBKRBroker
    from .execution import reconcile, unresolved_orders
    db = DB()
    b = IBKRBroker(port=port, client_id=CHECK_CLIENT_ID, scales=db.get("ibkr_scale", {}) or {})
    lines, bad = [f"# IBKR paper execution evidence — {date.today()}", ""], 0
    try:
        h = b.health()
        if not h["ok"] or h["live"] is not False:
            _p(False, f"refusing: gateway not OK or not a verified paper account ({h})")
            return 1
        lines.append(f"Account **{h['account']}** (paper verified). Generated {time.strftime('%Y-%m-%d %H:%M')} UK.")
        ib = b.ib
        for s in UNIVERSE:
            b.contract(s)
        rev = {c.conId: s for s, c in b._contracts.items()}
        fills = ib.reqExecutions()
        ib.sleep(2)
        lines += ["", "## Executions reported by IBKR", "",
                  "| time | ETF | side | qty | price £ | commission | orderId | ATLAS ref |", "|---|---|---|---|---|---|---|---|"]
        by_order: dict[str, dict] = {}
        for f in fills:
            sym = rev.get(f.contract.conId, f.contract.symbol + ".L")
            sc = b.scales.get(sym, 1.0)
            comm = f.commissionReport.commission if f.commissionReport and f.commissionReport.commission else 0.0
            e = f.execution
            lines.append(f"| {e.time} | {sym} | {e.side} | {e.shares:g} | {e.price * sc:.4f} | {comm:.2f} "
                         f"{f.commissionReport.currency if f.commissionReport else ''} | {e.orderId} | {e.orderRef or ''} |")
            d = by_order.setdefault(str(e.orderId), {"qty": 0.0, "notional": 0.0, "comm": 0.0, "sym": sym})
            d["qty"] += e.shares
            d["notional"] += e.shares * e.price * sc
            d["comm"] += comm
        if not fills:
            lines.append("| — | no executions reported for this account/session | | | | | | |")
        lines += ["", "## ATLAS ledger vs IBKR executions", "",
                  "| ATLAS # | ETF | side | ledger qty | ledger avg £ | IBKR qty | IBKR avg £ | IBKR commission | match |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in db.q("SELECT * FROM orders WHERE broker='ibkr' AND filled_qty > 0 ORDER BY id"):
            x = by_order.get(str(r["broker_order_id"]))
            if x is None:
                lines.append(f"| {r['id']} | {r['symbol']} | {r['side']} | {r['filled_qty']:g} | {r['avg_fill_price'] or 0:.4f} "
                             f"| — | — | — | not in this gateway session's executions |")
                continue
            avg = x["notional"] / x["qty"] if x["qty"] else 0
            ok = abs(x["qty"] - r["filled_qty"]) < 1e-9 and abs(avg - (r["avg_fill_price"] or 0)) < 0.01
            bad += (not ok)
            lines.append(f"| {r['id']} | {r['symbol']} | {r['side']} | {r['filled_qty']:g} | {r['avg_fill_price'] or 0:.4f} | "
                         f"{x['qty']:g} | {avg:.4f} | {x['comm']:.2f} | {'yes' if ok else '**NO**'} |")
        rec = reconcile(db, b)
        acct = b.account()
        lines += ["", "## Positions: broker vs ledger", "", f"Reconciliation: **{'OK' if rec['ok'] else 'MISMATCH'}** {rec['diffs'] or ''}",
                  "", "| ETF | broker qty | broker avg cost £ |", "|---|---|---|"]
        lines += [f"| {s} | {p.qty:g} | {p.avg_cost:.4f} |" for s, p in acct.positions.items()] or ["| — | none | |"]
        pend = unresolved_orders(db, "ibkr")
        lines += ["", f"GBP cash: £{acct.cash:,.2f}", f"Unresolved ATLAS orders: {len(pend)}",
                  f"Open orders at broker: {len(ib.openTrades())}"]
        bad += (not rec["ok"]) + len(pend)
        lines += ["", f"**Result: {'all consistent' if not bad else f'{bad} problem(s) found'}**"]
    finally:
        b.close()
    d = pathlib.Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"paper-fills-{date.today()}.md"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten: {f}")
    return 1 if bad else 0


def gilt_check(port: int | None = None, cancel_test: bool = True) -> int:
    """Verify the IBKR gilt path on the PAPER gateway; on success mark `gilt_verified` so the gilt sleeve
    may be used. Places at most one unfillable limit order (half the market price), then cancels it."""
    from datetime import date

    from ib_async import LimitOrder

    from .brokers.ibkr import IBKRBroker
    from .db import now
    from .gilts import CASH_SLEEVE, select
    from .mandate import get as get_mandate
    from .risk import ProposedOrder, RiskEngine
    db = DB()
    b = IBKRBroker(port=port, client_id=CHECK_CLIENT_ID)
    fails = 0
    try:
        h = b.health()
        if not h["ok"] or h["live"] is not False:
            _p(False, f"refusing: gateway not OK or not a verified paper account ({h.get('error') or h.get('account')})")
            return 1
        _p(True, f"paper account {h['account']} verified")
        gilts = b.load_gilts()
        if not gilts:
            _p(False, f"IBKR returned no conventional UK gilts for issuer symbol '{b.GILT_ISSUER_SYMBOL}' "
                      "(set IBKR_GILT_SYMBOL if IBKR uses a different symbol)")
            return 1
        _p(True, f"{len(gilts)} conventional gilts found (maturity <= 20y)")
        for g in sorted(gilts.values(), key=lambda g: g.maturity)[:5]:
            r = b.gilt_rules(g.key)
            _p(None, f"  {g.isin} {g.name} {g.coupon}% {g.maturity} | min {r['min_size']:g} step {r['increment']:g} tick {r['min_tick']:g}")
        quotes = b.quote_gilts()
        _p(bool(quotes), f"quotes received for {len(quotes)}/{len(gilts)} gilts"
                         + ("" if quotes else " - bond market data may need a subscription; sleeve stays off"))
        if not quotes:
            return 1
        for k, px in list(quotes.items())[:3]:
            _p(None, f"  {gilts[k].name}: clean {px:.3f}")
        for k in quotes:
            gilts[k].clean = quotes[k]
        m = get_mandate(db)
        band = None if (m and m.wrapper in ("isa", "sipp")) else (m.tax_band if m else "higher")
        picks = select([gilts[k] for k in quotes], date.today(), band, CASH_SLEEVE, 10_000)
        _p(bool(picks), "cash-sleeve selection for your mandate: " + "; ".join(
            f"{p['gilt'].name} {p['after_tax'] * 100:.2f}% after tax" for p in picks))
        if cancel_test and picks:
            g = picks[0]["gilt"]
            rules = b.gilt_rules(g.key)
            from .sleeve import dirty_per_pound
            px = dirty_per_pound(g, g.clean)
            nominal = max(rules["min_size"], rules["increment"])
            risk = RiskEngine(db)
            risk.allowed_gilts = {g.key}
            acct = b.account()
            rep = risk.review([ProposedOrder(g.key, "buy", nominal, px)], acct.cash, acct.cash, {},
                              {g.key: date.today().isoformat()}, False)
            # the min-order rule may reject a 1-lot test; that rejection is itself the correct behaviour
            _p(None, f"risk engine on a {nominal:g}-nominal test order: "
                     + ("approved" if rep.approved else f"rejected ({rep.rejected[0][1]})"))
            lim = round(g.clean * 0.5 / rules["min_tick"]) * rules["min_tick"]
            o = LimitOrder("BUY", nominal, lim)
            o.tif, o.orderRef = "DAY", "atlas-gilt-cancel-test"
            tr = b.ib.placeOrder(b.contract(g.key), o)
            b.ib.sleep(3)
            st = tr.orderStatus.status
            if st not in ("PreSubmitted", "Submitted", "PendingSubmit"):
                fails += 1
                _p(False, f"gilt limit order not accepted: {st} {tr.log[-1].message if tr.log else ''}")
            else:
                _p(True, f"IBKR accepted unfillable gilt limit BUY {nominal:g} {g.name} @ {lim} ({st})")
                b.ib.cancelOrder(tr.order)
                b.ib.sleep(3)
                ok = tr.orderStatus.status in ("Cancelled", "ApiCancelled")
                fails += not ok
                _p(ok, f"cancellation {'confirmed' if ok else 'NOT confirmed: ' + tr.orderStatus.status}")
        if not fails:
            db.set("gilt_verified", {"at": now(), "account": h["account"], "gilts": len(gilts), "quotes": len(quotes)})
            db.event("info", "gilt_verified", f"IBKR gilt path verified on {h['account']} ({len(gilts)} gilts, {len(quotes)} quotes)")
            _p(True, "gilt path VERIFIED - the gilt sleeve may now be enabled in the mandate")
    finally:
        b.close()
    return 1 if fails else 0


def live_status() -> int:
    """Shows every live-readiness gate and whether it passes. Changes nothing."""
    from .config import MODE, PAPER_ONLY
    from .live import LiveAuthError, load_authorisation
    db = DB()
    _p(not PAPER_ONLY, f"code lock PAPER_ONLY = {PAPER_ONLY} (must be False for live; currently {'LOCKED' if PAPER_ONLY else 'unlocked'})")
    _p(MODE == "live", f"ATLAS_MODE = {MODE} (database {db.conn.execute('PRAGMA database_list').fetchone()[2]})")
    try:
        lim = load_authorisation(db)
        _p(True, f"signed authorisation for {lim.account_id}, cap £{lim.max_capital_gbp:,.0f}, expires {lim.expires_at}")
    except LiveAuthError as e:
        _p(False, f"authorisation: {e}")
    _p(None, "connected-account verification runs at the start of every live cycle (atlas.live.gate)")
    return 0


def live_authorise() -> int:
    """Interactive only. Creates a signed, expiring authorisation for ONE live account in the LIVE
    database. Refuses while PAPER_ONLY is set, outside ATLAS_MODE=live, or without a keyboard."""
    import getpass

    from . import auth
    from .config import MODE, PAPER_ONLY
    from .live import PHRASE, LiveAuthError, LiveLimits, make_authorisation
    if PAPER_ONLY:
        _p(False, "PAPER_ONLY = True in atlas/config.py: live authorisation is disabled in this build")
        return 1
    if MODE != "live":
        _p(False, "run with ATLAS_MODE=live (separate live database)")
        return 1
    if not sys.stdin.isatty():
        _p(False, "must be run interactively at a keyboard")
        return 1
    db = DB()
    if not db.q("SELECT 1 FROM users"):
        email = input("Create live-console investor email: ").strip()
        auth.create_user(db, email, getpass.getpass("New live-console password (12+ chars): "))
    email = input("Investor email: ").strip()
    if not auth.verify(db, email, getpass.getpass("Password: ")):
        _p(False, "password incorrect")
        return 1
    acct = input("Live IBKR account ID (e.g. U1234567): ").strip()
    if acct != input("Type the account ID again: ").strip():
        _p(False, "account IDs do not match")
        return 1
    lim = LiveLimits(account_id=acct,
                     max_capital_gbp=float(input("Maximum capital ATLAS may invest (GBP): ")),
                     max_order_gbp=float(input("Maximum single order (GBP): ")),
                     max_daily_loss_gbp=float(input("Maximum daily loss before suspension (GBP): ")),
                     max_drawdown_pct=float(input("Maximum drawdown before suspension (e.g. 0.10): ")))
    days = int(input("Authorisation valid for how many days (1-30): "))
    print("\nYou are authorising:", lim)
    if input(f'Type exactly: "{PHRASE}"\n> ').strip() != PHRASE:
        _p(False, "phrase did not match; nothing saved")
        return 1
    try:
        rec = make_authorisation(lim, email, days)
    except LiveAuthError as e:
        _p(False, str(e))
        return 1
    db.set("live_authorisation_v2", rec)
    db.event("critical", "live_authorised", f"live authorisation created by {email} for {acct}: {rec['limits']}")
    _p(True, "authorisation saved. The kill switch stays ENGAGED: release it from the live dashboard with your password.")
    db.set("kill_switch", True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="atlas.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("ibkr-check")
    c.add_argument("--roundtrip", action="store_true", help="buy+sell 1 share of the cash proxy (LSE hours)")
    c.add_argument("--cancel-test", action="store_true", help="place an unfillable 1-share limit order and cancel it")
    c.add_argument("--port", type=int)
    v = sub.add_parser("verify-fills", help="IBKR executions/commissions vs ATLAS ledger (read-only)")
    v.add_argument("--port", type=int)
    sub.add_parser("live-status", help="show every live-readiness gate (read-only)")
    gc = sub.add_parser("gilt-check", help="verify IBKR gilt contracts, quotes and a cancelled test order (paper)")
    gc.add_argument("--port", type=int)
    gc.add_argument("--no-order", action="store_true")
    sub.add_parser("live-authorise", help="interactive live authorisation (disabled while PAPER_ONLY)")
    sub.add_parser("emergency-stop", help="kill switch + cancel all working orders at the broker")
    r = sub.add_parser("resolve-order", help="mark an order the broker cannot find as resolved, after checking the broker")
    r.add_argument("order_id", type=int)
    r.add_argument("--note", required=True)
    a = ap.parse_args()
    if a.cmd == "ibkr-check":
        sys.exit(ibkr_check(a.roundtrip, a.port, a.cancel_test))
    if a.cmd == "verify-fills":
        sys.exit(verify_fills(a.port))
    if a.cmd == "gilt-check":
        sys.exit(gilt_check(a.port, not a.no_order))
    if a.cmd == "live-status":
        sys.exit(live_status())
    if a.cmd == "live-authorise":
        sys.exit(live_authorise())
    if a.cmd == "emergency-stop":
        from .engine import make_broker
        from .execution import emergency_stop
        db = DB()
        b = None
        try:
            b = make_broker(db, client_id=CHECK_CLIENT_ID)
        except Exception as e:
            print("broker unavailable:", e)
        print(emergency_stop(db, b, "cli"))
        if b:
            b.close()
        sys.exit(0)
    if a.cmd == "resolve-order":
        from .execution import resolve_order_manually
        resolve_order_manually(DB(), a.order_id, "cli", a.note)
        sys.exit(0)


if __name__ == "__main__":
    main()
