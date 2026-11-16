# ATLAS X — autonomous, tax- and cost-aware capital allocator (UK investor)

**Why it is built this way:** [docs/RESEARCH_AND_DECISION.md](docs/RESEARCH_AND_DECISION.md). The
evidence says the money that individual investors reliably lose goes to tax, costs, idle cash and
over-trading, not a lack of clever trades. ATLAS X captures diversified market returns and stops those
leaks, on its own, within a mandate you approve once.

ATLAS X researches, chooses, sizes, executes and monitors a long-only portfolio of London-listed
UCITS ETFs. Every trade goes through a risk engine that has the final say. The platform runs as a
background worker and comes with an authenticated investor dashboard.

**This installation is locked to paper trading.** It is connected to IBKR paper account DU-REDACTED
through IB Gateway on this PC. `PAPER_ONLY = True` in code, so a live or unverified account engages
the kill switch and every order is rejected. See [docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md).

## Why this design (research summary)

* **What to trade.** The universe is diversified London-listed UCITS ETFs: global, US, UK and EM
  equity, gilts, index-linked gilts, corporate bonds, gold, UK property, and an ultrashort sterling
  bond fund as the cash proxy. They can be bought by UK retail investors, are cheap, liquid, exempt
  from stamp duty and can be held in an ISA/SIPP. Forex, crypto and single-stock intraday trading were
  ruled out: retail costs and adverse selection there wipe out the documented edges, and leverage
  makes losses poorly bounded.
* **Which broker.** Interactive Brokers UK. It is FCA-regulated, has a full trading API, covers LSE
  ETFs, offers a free paper account, and lets you create API users with trading-only permissions.
  Trading 212's API is limited, and Alpaca does not serve UK retail on LSE.
* **How strategies are chosen.** The research lab (`atlas/research/lab.py`) tests passive benchmarks,
  risk parity, time-series trend with volatility targeting, dual momentum and short-term reversal. All
  of them are tested under one protocol that was fixed in advance:
  * returns are net of costs and executed the next day;
  * parameters are chosen in-sample only;
  * the out-of-sample period is never used for selection;
  * a **Deflated Sharpe Ratio** is calculated across every configuration tried;
  * parameter robustness is checked;
  * each strategy must be executable within the risk limits;
  * active strategies must beat passive ones.

  If nothing passes, the platform holds cash.
* **Honest result so far (13 Nov 2026, real data from Jun 2014).** Once multiple testing is penalised,
  the differences between the strategies that passed are small. The lab picks the candidate with the
  highest DSR. Short-term reversal is rejected (it does not survive costs). Nothing here is a
  profitability claim. A backtest is not evidence of future returns, so live use first needs a forward
  paper record.

## Run it

ATLAS X runs from Windows scheduled tasks; no Claude session is needed. From `atlas-x\`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1            # start dashboard + worker now
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1 -Disable    # stop and keep stopped
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1    # (re)register the tasks
python -m atlas.cli ibkr-check --cancel-test                          # verify the IBKR paper link
python -m pytest -q                                                   # 74 tests (never touch a real broker)
```

- **Dashboard:** http://127.0.0.1:8600 (localhost only). Your first visit creates the investor login.
- **Worker:** cycles every 15 minutes and trades only during LSE hours.
- **Logs:** `data\logs\` (rotating). Daily reports go to `data\reports\`.
- **After a reboot:** log in to IB Gateway (IB API, Paper Trading). Everything else starts by itself.

## Streamlit dashboard (cloud demo, paper only)

`streamlit_app.py` is a second, self-contained dashboard that is built for Streamlit Community Cloud.

* **Paper only, enforced.** Before `atlas` is imported, it forces paper mode and the built-in simulated
  broker, and it points the IBKR port at an unreachable address. It refuses to start if
  `PAPER_ONLY` is ever off. It has no live-trading control.
* **Isolated data.** It keeps its own database, `data/streamlit/atlas-streamlit.db`. It never opens the
  worker's `data/atlas.db`, so it is safe to run on the PC that runs the IBKR paper worker.
* **Public read, password-protected write.** Anyone with the link can view it. Running a cycle,
  refreshing data, re-running the lab, the kill switch, pausing, sell-off and the mandate all need
  `ATLAS_ADMIN_PASSWORD`. That password is set in Streamlit secrets and has a sign-in rate limit.
  Without it the app is read-only.
* **Same engine.** It reuses the API read-models, research lab, risk engine and execution unchanged, so
  every simulated order still passes the risk engine.
* **Self-running.** On first visit it downloads prices from Yahoo and runs the research lab, which
  takes 1–3 minutes. After that, a page visit runs a paper cycle if the last one is older than
  `ATLAS_AUTO_CYCLE_MINUTES` (default 15). Trades only happen while the LSE is open.
* **Storage is temporary on Community Cloud.** A reboot or redeploy starts a fresh paper account.

Run it locally:

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

To deploy on [share.streamlit.io](https://share.streamlit.io):

1. Create the app from this repository, on the `main` branch, with `streamlit_app.py` as the main file
   and Python 3.12.
2. In **Advanced settings → Secrets**, paste your values using the keys in
   [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example).
3. Never commit `.streamlit/secrets.toml`; `.gitignore` excludes it.

Development dependencies are in `requirements-dev.txt` (`pip install -r requirements-dev.txt`, then
`python -m pytest -q`).

## Architecture

| Module | Role |
|---|---|
| `data.py` | Yahoo Finance daily history → SQLite. Bad-print cleaning, validation and staleness checks |
| `research/` | strategy families, backtester with drift and costs, PSR/DSR statistics, lab gate and selection |
| `portfolio.py` | turns target weights into whole-share orders, with a no-trade band so the book doesn't churn |
| `risk.py` | **final authority**: kill switch, live-authorisation check, whitelist, per-ETF caps, no leverage or shorting, order-size, turnover, daily-loss and drawdown breakers, stale-data block. Only it can create an `ApprovedOrder` |
| `execution.py` | sends only approved orders, records the status the broker confirms (never assumes a fill), re-queries open orders after a restart, reconciles positions against the broker |
| `brokers/paper.py` | persistent paper account, filled at real closing prices ± spread plus commission |
| `brokers/ibkr.py` | IB Gateway adapter: paper/live detection (falls back to live), pence→£ conversion, GBP-only cash. Tested against a fake gateway; not yet run against a real one |
| `engine.py` / `worker.py` | the decision cycle, plus the crash-safe background loop with backoff and daily reports |
| `api.py` + `web/index.html` | authenticated API (scrypt hashing, HMAC session cookies, login rate limit, password re-entry for sensitive actions) and the dashboard |

## IBKR paper connection

The setup as built is in [docs/IBKR_PAPER_SETUP.md](docs/IBKR_PAPER_SETUP.md), and the full evidence
in [docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md).

## Live trading

**Readiness assessment:** [docs/LIVE_READINESS_REPORT.md](docs/LIVE_READINESS_REPORT.md).
`python -m atlas.cli live-status` shows every gate. Live trading is **disabled in code** for this
installation. The authorisation endpoint returns 403 and
the dashboard form is locked. Enabling live money would need a deliberate, reviewed code change. It
should also need a forward paper record, a trade-only IBKR API user with no withdrawal rights, and an
explicit capital cap. It is not a runtime setting.

ATLAS X is software, not regulated investment advice. All trading decisions and their consequences
remain the account holder's.
