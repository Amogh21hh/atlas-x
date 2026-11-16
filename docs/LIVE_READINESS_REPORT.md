# ATLAS X — live-trading readiness report

_14 November 2026 · status: **NOT approved for live trading** · live trading remains disabled in code_

This report covers what is engineered, what has been proven, what has not, and what you would have
to decide before any real money is involved. It makes no claim that the strategy will be profitable.

---

## 1. Bottom line

| Area | Status |
|---|---|
| Safety engineering for a future live account | **Built and tested in simulation** (54 automated tests, incl. 11 failure simulations) |
| Live trading | **Disabled.** `PAPER_ONLY = True` in code; `live-authorise` refuses to run; live mode cannot start |
| Real paper fills on IBKR | **Not yet observed.** The LSE was closed when this was built (Saturday). Automatic test scheduled for Mon 16 Nov |
| Live code path against a real live account | **Never exercised**, by design — first contact would be the first real test |
| Evidence that the strategy makes money | **None that is sufficient.** A 12-year backtest and a few weeks of paper results cannot establish it |
| Your personal suitability (capital, risk tolerance, tax) | **Not assessed.** Only you, ideally with a regulated adviser, can decide this |

**Ready:**
- the controls that stop the software doing things you didn't authorise;
- the evidence tooling to check every fill.

**Not ready:**
- proof that real orders behave as expected;
- proof that the strategy suits your money.

---

## 2. Item 1 — the IBKR paper buy-and-fill test

**Status: scheduled, not completed.** No fills could happen on 14 Nov: the LSE was closed (Saturday),
and the tool refuses to queue market orders outside trading hours. Nothing here substitutes a
simulated fill.

What runs automatically, with no Claude session needed:

| When (UK) | What | Evidence file |
|---|---|---|
| Mon 16 Nov, first cycle after 08:05 | Worker rebalances the paper account to the selected 60/40 target | Orders and Events panels; ledger |
| Mon 16 Nov 09:20 | `ibkr-check --roundtrip`: risk-approved BUY then SELL of 1 ERNS share, both recorded in the ledger | `data\logs\ibkr-roundtrip-2026-11-16.log` |
| Mon 16 Nov 16:45 | `verify-fills` (read-only), comparing per order: IBKR executions, prices in £ and commissions against the ATLAS ledger; broker positions against the ledger; open orders at the broker; unresolved orders | `docs\evidence\paper-fills-2026-11-16.md` and `data\logs\verify-fills-2026-11-16.log` |

**Already proven against the real paper gateway (14 Nov):**
- connection and paper-account verification;
- GBP cash of £1,000,000;
- all 10 ETFs: identity, ISIN, listing and price units;
- a risk-approved limit order accepted, then cancelled and confirmed;
- the risk engine rejecting orders at £0 equity;
- disconnect and reconnect;
- reconciliation OK.

A baseline `verify-fills` run is saved in `docs/evidence/paper-fills-2026-11-14.md`: 0 executions,
0 open orders, consistent.

**What to check on Monday** (or ask me to):
- every ledger row matches IBKR on quantity and average price;
- commissions are in GBP and plausible (about £3 minimum per order on LSE ETFs);
- reconciliation is OK;
- 0 unresolved orders;
- 0 open orders after the close.

---

## 3. Independent audit of every risk control

Each control was re-read in the code and tested. "Gap" means a weakness that would matter with real
money.

| # | Control | Paper behaviour (unchanged) | Gap found | Live behaviour now |
|---|---|---|---|---|
| 1 | Kill switch | Blocks every order | Didn't cancel orders already at the broker; releasing needed no password | **Emergency stop** = kill switch, then broker-wide cancel of working orders (dashboard, API and CLI). Releasing now needs the password |
| 2 | Paper/live separation | `DU` paper accounts only | — | Four independent layers: the cycle gate, the risk engine, the adapter and the CLI. **Separate live database.** Live mode refuses paper accounts and paper mode refuses live accounts |
| 3 | Capital cap | Applied only by the planner | **The risk engine didn't enforce it**, so a planner bug could exceed it | The risk engine rejects any buy that would take total invested above `max_capital_gbp` |
| 4 | Per-ETF concentration | % of whole account | With a small cap in a large account, caps were far too loose (65% of £100k = £65k) | Caps measured against the **capped capital**; can be made stricter per ETF, never looser |
| 5 | Order size | % of equity only; cash-proxy buys exempt | No £ ceiling; cash-proxy buys could be any size | **£ maximum order** on every buy, cash proxy included |
| 6 | Daily turnover | % of equity | — | £ ceiling (defaults to the capital cap) |
| 7 | Daily loss | −4% of equity | Too loose when the account is much bigger than the cap | **£ daily-loss limit**, ≤ 4% of capped capital |
| 8 | Drawdown | −20% from peak | Same issue | **£ drawdown against capped capital**, ≤ 20% (you choose; default 10%) |
| 9 | Price freshness | ≤ 5 business days | Too stale for sizing real orders | ≤ **1** business day |
| 10 | Price protection | Market orders | A gap or bad print could fill far from the approved price | **Limit orders at most 0.5% through the reference price**, rounded to the tick in your favour; tested: a 3% gap is not chased |
| 11 | Duplicate orders | Order refs, recovery by ref, pause while unresolved, single worker | A submit error was treated as final, so a duplicate was possible if IBKR had actually accepted it | Submit errors are **unresolved**: submitting stops, the order is looked up by ref, and a manual `resolve-order` exists for anything never found |
| 12 | Reconciliation | Mismatch → suspend buying | — | Unchanged; external trades are detected (tested) |
| 13 | Pre-existing holdings | Absorbed into the baseline | ATLAS would trade ETFs you hold yourself | Live verification **refuses an account holding anything outside the ATLAS universe**. A dedicated account or sub-account is required |
| 14 | Leverage / shorting | Prohibited by ATLAS checks | IBKR margin accounts can still lend if cash is miscounted | ATLAS checks unchanged, plus a 1% cash buffer and price collar. **You should still open a Cash (not Margin) account** — see section 7 |
| 15 | Live authorisation | Web form (now 403) | A web form is too easy to trigger | Interactive keyboard-only CLI. The record is signed (HMAC), lasts at most 30 days, covers **one exact account ID**, and its limits are validated against hard ceilings. Tampering or expiry fails closed |
| 16 | Paper → live transfer | — | — | **Impossible automatically.** It requires a code change (`PAPER_ONLY=False`), `ATLAS_MODE=live` set by hand (new empty database), and the interactive authorisation. Even then the kill switch starts ENGAGED |

**Unchanged on purpose:**
- the research method, the 60/40 selection, cost assumptions and the out-of-sample split;
- the paper-mode limits.

**Findings that are documented, not fixed:**
- **Loss limits don't sell anything.** The daily-loss and drawdown breakers stop *new* risk; they do
  not sell holdings. Losses can therefore exceed the limit, for example in a market fall. Selling is
  the separate, explicit Emergency liquidate action.
- **Deposits and withdrawals distort P&L limits.** A withdrawal looks like a loss and suspends
  trading (fails safe). A deposit looks like a gain.
- **UK bank holidays aren't modelled.** Orders could be submitted on a holiday; they're DAY limit
  orders and would expire unfilled.

---

## 4. Live configuration as designed (all locked)

```
Live trading requires ALL of:
  PAPER_ONLY = False                    <- reviewed code change (currently True)
  ATLAS_MODE=live                       <- separate database data/live/atlas-live.db
  live-authorise (interactive)          <- password, account ID twice, limits, exact phrase
     -> signed record, <= 30 days, one account ID, limits <= hard ceilings
  every cycle: atlas.live.gate()        <- connected account == authorised ID, not DU,
                                           no foreign holdings, GBP cash, unexpired, signature OK
  risk engine with LiveLimits           <- capital cap, £ order, £ daily loss, £ drawdown,
                                           capped per-ETF caps, 1-day prices
  adapter                               <- re-checks mode vs account before every order
Kill switch starts ENGAGED after authorisation; you release it with your password.
```

**Small-capital mode** (automatic below £25,000 of capital):
- no position under £1,000, so the £3 minimum commission stays at or under 0.3%;
- 5% rebalance band and £250 minimum order;
- anything that can't be placed sensibly stays as cash.

For example, at £2,000 the 60/40 target becomes about 65% VWRL plus cash. The £800 gilt position
would cost proportionally too much to hold.

`python -m atlas.cli live-status` prints every gate. Today: **PAPER_ONLY locked · mode paper · no
authorisation.**

---

## 5. Failure simulations (all pass; system ends in a safe state)

| Scenario | Result |
|---|---|
| Gateway down at cycle start | No orders. Alert on the transition only; recovery is logged |
| Connection lost after IBKR accepted an order | Marked unknown; no more orders that cycle; found by its ref next cycle; **no duplicate** |
| Order never fills | New orders pause until it is resolved |
| Partial fill | Exact filled quantity recorded; reconciliation OK |
| Someone trades the account by hand | Mismatch detected; buying suspended |
| Market data stale | All buys rejected |
| Price gaps 3% past the approved price (live) | Limit order not filled; nothing chased |
| Emergency stop with working orders | Kill switch on; broker cancel-all; no new orders |
| Emergency stop while the broker is unreachable | Kill switch still on; failure reported so you can cancel manually |
| Strategy code throws | No orders |
| Second worker started | Refused by the OS lock |
| Gateway re-logged into a LIVE account mid-session (paper mode) | Kill switch; no orders |
| Live mode connected to the wrong, a paper, or an unverified account | Kill switch; no orders |
| Authorisation edited in the database, or expired | Signature or expiry check fails; no trading |

Not simulated:
- a power cut or Windows update mid-order (covered in principle by restart recovery, not tested on
  the real gateway);
- an IBKR outage lasting days;
- a corrupted database file (keep backups).

---

## 6. Test results
`python -m pytest -q` → **54 passed** (paper suite 28, live controls 15, failure simulations 11).

The tests can't reach a real broker (forced paper broker, unreachable IBKR port) or the real logs
directory.

During this work, earlier test runs wrote some test-only lines into the real
`data/logs/worker.log`: "live_account_blocked U5555555 / U7654321" at 01:01–01:02 UTC on 14 Nov. They
are **not real events**. The real database had no such events, and its kill switch and suspension
were unaffected. The leak is fixed.

---

## 7. Remaining risks (what this engineering cannot remove)

**Investment risk**
- **Losses.** The selected 60/40 mix had a −16.8% worst out-of-sample drawdown (2021–2026), which
  includes 2022, when UK gilts fell sharply. The data starts in 2014, so it contains **no 2008-type
  crisis**: a global equity fall of 40–50% would mean a portfolio loss of roughly 25–35%, more if
  bonds fall at the same time.
- **The loss limits are not stop-losses.** They stop adding risk; they don't sell.
- **Currency.** The ETFs trade in GBP, but most of VWRL's holdings are in other currencies.
- **Thin evidence.** A backtest is one 12-year path, chosen from 41 configurations; the Deflated
  Sharpe Ratio adjusts for this, but only partly. Twenty or thirty paper days prove that the
  **machinery** works. They say almost nothing about **returns**.

**Operational risk**
- This PC must be on, logged in and online. IB Gateway needs a manual login weekly and after reboots.
  Sleep, Windows updates, power cuts and a dead disk all stop trading (they fail safe: no orders).
- Alerts go only to log files until `ATLAS_NOTIFY_WEBHOOK` is set. A critical event could go
  unnoticed.
- One machine, with no redundancy. The signing key sits on the same disk as the database: the
  signature catches database-only edits, not someone with full access to your PC.
- The dashboard is localhost-only and password-protected, without 2FA.

**Broker and market risk**
- The live adapter path has never touched a live account. The first live session is itself a test,
  so it should be small and watched.
- IBKR account type (Cash vs Margin), permissions, market-data entitlements and commission tier for a
  real account may differ from the paper trial.
- LSE auctions, halts, holidays, wide spreads at the open, and ETF price deviating from its
  underlying value.

**Software risk**
- ATLAS X was written by an AI over a few sessions and has **not had an independent human code
  review**. The tests check intended behaviour; they can't prove the absence of bugs.

**Tax and legal** (not assessed — get advice)
- Rebalancing outside an ISA/SIPP can create capital gains tax events, and distributing ETFs pay
  dividends.
- Whether IBKR UK offers an ISA or SIPP wrapper suitable for this, and how automated trading fits
  your circumstances, are questions for you and a qualified adviser.
- ATLAS X is not regulated investment advice, and nothing in this report is a recommendation to
  invest.

---

## 8. What you must personally decide and approve before any real money

These are yours to make; neither the software nor I can make them for you.

1. **Whether to do this at all, and with how much.** Base it on your own finances (emergency fund,
   debts, time horizon, money you could lose without hardship), not on the £1,000,000 paper balance,
   which is IBKR's simulated money. Consider a regulated financial adviser.
2. **Strategy acceptance.** You accept that a 60/40 portfolio can fall 25–35% in a severe crisis, and
   that you might be tempted to stop at the worst time.
3. **Account set-up**, done by you at IBKR:
   - a real IBKR UK account, **Cash** type, GBP base;
   - dedicated to ATLAS (or a dedicated sub-account) with no other holdings;
   - a separate API user with **trading permission only**, no funding, withdrawal or transfer rights;
   - 2FA on.
4. **A forward paper record you've reviewed**, judged on operations, not profit. Suggested bar:
   - 20+ trading days;
   - 0 reconciliation mismatches;
   - 0 unresolved orders at any close;
   - every fill inside its collar;
   - commissions as expected.

   Ideally the record should come from the paper twin of **your real account**, not the free trial.
5. **The limits:**
   - capital cap;
   - maximum order £;
   - daily loss £;
   - drawdown %;
   - authorisation length.

   The software rejects anything looser than the ceilings in section 4.
6. **Independent code review** of `risk.py`, `live.py`, `execution.py` and `brokers/ibkr.py` before
   unlocking. Recommended, given the stakes.
7. **The unlock itself:**
   - change `PAPER_ONLY` to `False` in a reviewed commit;
   - run with `ATLAS_MODE=live` against the live gateway (port 4001);
   - run `live-authorise` at the keyboard;
   - release the kill switch with your password.

   Each of these is a step you take; none can happen automatically.
8. **Monitoring commitment.** Set up phone alerts (`ATLAS_NOTIFY_WEBHOOK`), log in to the gateway
   weekly, check the daily report, and know where the Emergency stop is.
9. **Tax wrapper and reporting**, decided with an adviser if needed.

Until all of these are done, ATLAS X stays a paper-trading system.
