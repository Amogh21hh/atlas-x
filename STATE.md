# ATLAS X — project state (resume from here)

_Last updated: 2026-11-14 01:15 UTC_

## Current operating state
- **Broker:** IBKR **paper** account DU-REDACTED through IB Gateway 10.50 on 127.0.0.1:4002. GBP base
  currency, £1,000,000 simulated cash, no positions yet.
- **Scheduled tasks:** ATLAS-X-Worker (15-minute cycles), ATLAS-X-Dashboard (http://127.0.0.1:8600)
  and ATLAS-X-IBGateway are registered and running. ATLAS-X-RoundtripCheck runs once on Mon 16 Nov at 09:20.
- **Strategy:** static_60_40 (VWRL 60% / IGLT 40%). The first IBKR rebalance happens at the first
  worker cycle after the LSE opens on Mon 16 Nov (08:05 UK or later).
- **Paper-only lock:** `PAPER_ONLY = True` in code. Live authorisation is disabled.
- Full evidence is in `docs/TECHNICAL_REPORT.md`.

## Direction (2026-11-14): tax- and cost-aware allocator
- Research and decision: `docs/RESEARCH_AND_DECISION.md`. Experiments: `research/exp_leaks.py`.
- Built:
  - mandate (provisional £10k paper; approve in the dashboard);
  - contribution-led rebalancing (5pp band);
  - deposit/withdrawal detection and time-weighted returns;
  - UK tax engine (S104, 30-day, tax year, ISA);
  - independent evaluation against cash, the untraded mix and VWRL;
  - plain-language home screen;
  - daily verified backups (`python -m atlas.backup`).
- Gilt sleeve BUILT (`atlas/gilts.py`, `atlas/sleeve.py`; opt-in in the mandate). It is inactive until
  `python -m atlas.cli gilt-check` passes on the paper gateway. NOT YET RUN: the gateway was closed.
- **IB Gateway closed between 01:18 and 01:33 UTC on 14 Nov.** The investor must log in again (IB API,
  Paper) before Mon 08:00 for the scheduled paper fills to happen.

## Live-readiness engineering (2026-11-14) — live still DISABLED
- Report: `docs/LIVE_READINESS_REPORT.md`. Verdict: not approved for live.
- Separate live mode with its own database; signed, expiring, single-account authorisation
  (interactive CLI only, refused while `PAPER_ONLY = True`); per-cycle live-account gate.
- Live limits in the risk engine: capital cap, £ max order, £ daily loss, £ drawdown against capped
  capital, capped per-ETF caps, 1-day price freshness, 0.5% price collar (limit orders).
- Emergency stop (kill switch + broker cancel-all); releasing the kill switch needs the password;
  unknown submissions are unresolved, never resubmitted; `resolve-order` for manual cases.
- Small-capital mode below £25k. `verify-fills` evidence tool. 54 tests, incl. 11 failure simulations.
- Scheduled Mon 16 Nov: 09:20 round-trip, 16:45 verify-fills
  (`docs/evidence/paper-fills-2026-11-16.md`).

## 2026-11-14 (later): weekly P&L + goal check
- `atlas/weekly.py`, `/api/weekly`, home-screen panels "Week by week" and "Is my goal realistic?".
- E3 experiment rejects 30%/week (docs/RESEARCH_AND_DECISION.md E3). £100 at IBKR: fees 3-6% per purchase.
- Data fix: rolling-median bad-print filter; SGLN history from 2011-07-01. Worker + dashboard restarted.

## Done and verified
- Real market data: 10 LSE UCITS ETFs, validated. Research lab of 6 families and 41 configurations,
  with DSR, robustness and feasibility gates. Out-of-sample period from 2021-04-07. All unchanged.
- Risk engine is the final authority; only it can create an `ApprovedOrder`.
- IBKR (verified on the real gateway, 2026-11-14):
  - paper detection;
  - GBP cash;
  - all 10 ETFs checked for name, ISIN, LSEETF listing and GBP;
  - pence/pound units detected and stored;
  - limit order accepted, then cancelled and confirmed;
  - disconnect and reconnect;
  - risk-engine rejection at zero equity;
  - a full worker cycle with reconciliation.
- Fixes made because the real gateway exposed them:
  - SMART routing (direct routing is refused with error 10311);
  - SLXX and ERNS ISINs corrected (confirmed by justETF);
  - performance no longer mixes brokers;
  - tests isolated from the real broker.
- Duplicate-order safety: order references, recovery by reference, pausing new orders while any are
  unresolved, and a single-instance worker lock.
- Windows automation: tasks run as the user without admin rights, relaunch within 15 minutes, use
  rotating logs, and write daily reports.
- Tests: **69 passing** (`python -m pytest -q`).

## Not done / blockers
- **Round-trip buy/sell with real fills** is not yet run because the LSE was closed (Saturday).
  Scheduled for Mon 16 Nov at 09:20. Check `data/logs/ibkr-roundtrip-2026-11-16.log`.
- **Investor actions:**
  - change the IBKR password (it was shared in chat);
  - create the dashboard login;
  - optionally set `ATLAS_NOTIFY_WEBHOOK`;
  - log in to IB Gateway again weekly and after any reboot.
- **No public deployment.** The dashboard stays bound to localhost.

## Next highest-value tasks
1. Mon 16 Nov: review `data/logs/ibkr-roundtrip-2026-11-16.log` and
   `docs/evidence/paper-fills-2026-11-16.md`. Every ledger row should match IBKR, with commissions in
   GBP, reconciliation OK and 0 unresolved orders. Record the outcome in LIVE_READINESS_REPORT section 2.
2. Forward tracker: compare paper results with the backtest's expected distribution.
3. Cross-validate Yahoo prices against IBKR historical bars.
4. Fixed £ minimum commission in the backtester.
5. New hypotheses through the same gate (they add to the DSR trial count).
