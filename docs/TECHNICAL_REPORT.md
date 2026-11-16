# ATLAS X — IBKR paper-trading setup: technical report

_14 November 2026 · prepared during the setup session · every result below was observed on this PC_

## 1. Software installed

| Software | Version | Location | Source / verification |
|---|---|---|---|
| IB Gateway (Stable) | 10.50, build 2026-11-09 | `C:\Jts\ibgateway` | Downloaded from `download2.interactivebrokers.com` (337 MB). Authenticode signature **Valid**, signed by Interactive Brokers Group, Inc. (DigiCert G4). SHA-256 `0F12694F…95FC57`. Silent install, exit code 0. |
| Python | 3.14.4 | `C:\Users\<user>\AppData\Local\Python\pythoncore-3.14-64` | already present |
| ib_async | 2.1.0 | site-packages | already present; works in worker threads |
| numpy / pandas / fastapi / uvicorn | 2.4.6 / 2.3.3 / 0.162.6 / 0.48.0 | site-packages | `pip check`: no broken requirements |

The installer is kept at `E:\atlas-installers\ibgateway\`. No firewall rule was added: the gateway and
ATLAS X talk only over 127.0.0.1, and IB Gateway itself accepts localhost connections only.

## 2. Configuration completed

**IB Gateway** (set in its UI; the paper login was entered by the account holder):
- IB API · Paper Trading
- Socket port **4002**
- **Read-Only API off** (orders allowed — paper account only)
- **Allow connections from localhost only** on
- Trusted IPs **127.0.0.1**
- Lock and Exit: **Auto restart** at 11:45 PM (was Auto logoff)
- Precautionary settings left unchanged: they block direct-routed orders, so ATLAS routes SMART instead (see 6)

**ATLAS X** — `.env` (git-ignored; contains no credentials):
```
ATLAS_BROKER=ibkr
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
ATLAS_CYCLE_SECONDS=900
```

IBKR client IDs are fixed so connections never collide:

| Process | Client ID |
|---|---|
| Worker | 17 |
| Dashboard manual cycle | 18 |
| `ibkr-check` | 19 |
| Ad-hoc diagnostics | 21 / 22 |

**Safety changes in code:**
- **Hard paper lock.** `PAPER_ONLY = True` is set in `atlas/config.py` and cannot be changed through
  environment variables.
- **Live or unverified accounts are blocked.** If the account is live, or its type can't be confirmed
  (fail-closed), the kill switch engages. The risk engine, the adapter and `ibkr-check` each refuse
  such orders independently.
- **Live authorisation disabled.** The endpoint returns 403 and the dashboard form is locked.
- **Account type re-checked every cycle.**
- **Duplicate-order protection:**
  - every order carries an `atlas-<id>` reference at the broker;
  - after a restart, orders are found again by that reference;
  - new orders are paused while any earlier order is unresolved;
  - an operating-system lock allows only one worker at a time.
- **Reconnection:** 3 attempts with exponential backoff. Alerts fire only when the connection changes
  state (down or restored), not on every failed check.
- **Broker switch:** changing broker resets the risk engine's equity baselines, so no fake drawdown.
- **Separate performance figures:** performance and the equity chart cover only the active broker's
  broker-confirmed snapshots. Backtest results are labelled "BACKTEST" on the dashboard.

## 3. IBKR connection result
**Connected.** `python -m atlas.cli ibkr-check` reached IB Gateway on 127.0.0.1:4002.
- **Market data:** the US market-data and historical-data farms are on, and the EU farm was also seen
  connected during the session.
- **Disconnect/reconnect:** tested against the real gateway. After a forced disconnect the adapter
  reconnected and re-verified the account as paper.

## 4. Paper-account verification
- Account **DU-REDACTED**: the `DU` prefix means paper, and the gateway title reads "Simulated Trading".
  Type INDIVIDUAL.
- Base currency **GBP**. Cash **£1,000,000.00** (the free-trial simulated balance: it read £0.00 at
  00:28 and £1,000,000 by 00:39 UTC, roughly 15 minutes after the first login).

## 5. ETF contract verification (all 10 pass)

| ETF | IBKR description | ISIN | Listing | Quote unit |
|---|---|---|---|---|
| VWRL | VANG FTSE AW USDD | IE00B3RBWM25 | LSEETF GBP | pounds |
| VUSA | VANG S&P500 USDD | IE00B3XXRP09 | LSEETF GBP | pounds |
| VUKE | VANG FTSE100 GBPD | IE00B810Q511 | LSEETF GBP | pounds |
| EMIM | ISHARES CORE EM IMI ACC | IE00BKM4GZ66 | LSEETF GBP | **pence** |
| IUKP | ISHARES UK PROPERTY | IE00B1TXLS18 | LSEETF GBP | **pence** |
| IGLT | ISHARES CORE UK GILTS | IE00B1FZSB30 | LSEETF GBP | pounds |
| INXG | ISHARES GBP INDEX-LNK GILTS | IE00B1FZSD53 | LSEETF GBP | pounds |
| SLXX | ISHARES CORE GBP CORP | IE00B00FV011 | LSEETF GBP | pounds |
| SGLN | ISHARES PHYSICAL GOLD ETC | IE00B4ND3602 | LSEETF GBP | **pence** |
| ERNS | ISHARES GBP ULTRASHORT BOND | IE00BCRY6441 | LSEETF GBP | pounds |

**Discrepancy found and resolved.** The first run reported ISIN mismatches for SLXX and ERNS. IBKR
gave `IE00B00FV011` and `IE00BCRY6441`; the platform's reference list had `IE00B00FV128` and
`IE00BCRY6557`. justETF independently confirmed IBKR's values, with matching fund names and prices, so
the reference list was wrong and has been corrected. No instrument was substituted.

**Price units.** IBKR prices were compared with the platform's GBP prices (13 Nov close). Every ratio
was within 0.3% of 1×, or of 100× for the pence lines. A per-ETF scale is stored, and fills and costs
are converted to pounds.

## 6. Market data
- Delayed/frozen quotes (market-data type 4) worked for all 10 ETFs with no paid subscription.
- Sizing uses the platform's own end-of-day prices; IBKR quotes are used only to check price units.
- **No real-time LSE data subscription.** That's acceptable for daily rebalancing and needs no action.

## 7. Order tests (real paper gateway)

**Cancellation test: PASS** (`ibkr-check --cancel-test`, 14 Nov)
- The risk engine approved a 1-share ERNS buy.
- IBKR accepted a limit order at £50.58 (half the market price, so it could not fill) as order 66,
  status PreSubmitted.
- ATLAS cancelled it, and IBKR confirmed the cancellation (error 202 "Order Canceled").

**Integration fault found and fixed in this run.** The first attempt was refused with **error
10311/201**: IB Gateway's precautionary settings block direct-routed orders. The adapter now uses
SMART routing with the primary listing required to be LSEETF/GBP. The instrument is identical, and
the safety setting was not weakened.

**Risk gate on the real account: PASS.** Before funding (equity £0), the risk engine rejected the test
buy and nothing reached the gateway.

**Buy/sell round-trip: not yet run.** The LSE was closed (Saturday), and the tool refuses to queue
market orders. A one-off scheduled task, `ATLAS-X-RoundtripCheck`, runs it on **Mon 16 Nov 2026 at
09:20 UK** and writes the result to `data\logs\ibkr-roundtrip-2026-11-16.log`. It buys and sells 1
share of ERNS on the paper account, after the paper and risk checks, and records both fills in the
ATLAS ledger.

**Real fills: none yet.** The account has had no executions so far. The worker's first real rebalance
will be Mon 16 Nov after 08:05 UK: 60% VWRL / 40% IGLT of about £990k, by the selected strategy.

## 8. Automated tests
**28 passed** (baseline was 18). The 10 new tests cover:
- the paper lock engaging the kill switch on a live account, with no orders reaching the gateway;
- the adapter refusing orders on a non-DU account;
- rejection when the account type is unverified;
- reconnection after a transient failure;
- restart recovery by order reference with no duplicate order;
- new orders pausing while an earlier order is unresolved;
- the daily loss limit;
- worker survival when a cycle fails;
- performance figures never mixing brokers;
- tests being isolated from the real broker.

**A fault found by the suite is now fixed.** Tests were reading the real `.env` and contacted the paper
gateway. The account showed 0 orders and 0 executions afterwards. `tests/conftest.py` now forces
`ATLAS_BROKER=paper` and an unreachable IBKR port.

## 9. Dashboard
- `http://127.0.0.1:8600` is served by a scheduled task and bound to localhost only.
- Checked on a throwaway copy of the live database, with a temporary test login (your login is not
  created). It shows:
  - PAPER TRADING MODE;
  - IBKR CONNECTED, account DU-REDACTED, type PAPER;
  - GBP cash and equity of £1,000,000.00;
  - open positions, pending orders and the last reconciliation time;
  - risk engine ACTIVE and kill switch status.
- Live authorisation returned **403** even with the correct password and phrase.

## 10. Windows automation

| Task | Trigger | Runs |
|---|---|---|
| ATLAS-X-IBGateway | at logon | opens IB Gateway (you log in; no stored credentials) |
| ATLAS-X-Dashboard | logon +1 min, repeats every 15 min | `pythonw -m atlas.serve` |
| ATLAS-X-Worker | logon +2 min, repeats every 15 min | `pythonw -m atlas.worker` (cycles every 15 min) |
| ATLAS-X-RoundtripCheck | once, Mon 16 Nov 09:20 | the round-trip test |

All tasks run as the logged-in Windows user with no admin rights (RunLevel Limited). Behaviour checked:
- **Duplicates:** if a task fires while its process is already running, the new instance exits (a
  second worker is refused by the lock — verified).
- **Crash recovery:** a killed worker was relaunched and immediately ran a cycle.
- **Logs:** `data\logs\worker.log` and `dashboard.log` rotate at 5 MB, keeping 5 files.
- **Daily report:** `data\reports\daily-YYYY-MM-DD.md`, written after 17:00.
- **Alerts:** warnings and critical events go to the log, and to a webhook if `ATLAS_NOTIFY_WEBHOOK`
  is set.

## 11. Research integrity
- The methodology, the 60/40 selection, cost assumptions, the in-sample/out-of-sample split
  (out-of-sample from 2021-04-07) and the Deflated Sharpe Ratio are all unchanged.
- Weekly research can only change target weights. Every resulting order still passes the risk engine.
- No profitability claim is made.

## 12. Remaining blockers / your actions
1. **Change your IBKR password.** It was typed into the Claude chat.
2. **Weekly IBKR re-login.** IBKR requires a full login about once a week (and after any PC restart).
   ATLAS reports the broker as down and keeps retrying until then.
3. **Notifications (optional).** Set `ATLAS_NOTIFY_WEBHOOK` in `.env` (e.g. an ntfy.sh topic) to get
   alerts on your phone.
4. **Dashboard login.** Create it on your first visit to http://127.0.0.1:8600.
5. **Check Monday's results:** the round-trip log and the first rebalance fills (Orders panel / events).
6. **Free-trial account.** Its lifetime and limits are set by IBKR; check your IBKR email for any
   expiry notice.

## 13. Start / stop

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1               # start dashboard + worker now
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1                # stop (tasks relaunch within 15 min)
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1 -Disable       # stop and keep stopped
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1       # (re)register tasks
powershell -ExecutionPolicy Bypass -File scripts\uninstall_tasks.ps1     # remove all ATLAS tasks
python -m atlas.cli ibkr-check [--cancel-test] [--roundtrip]             # verify the broker link
```

Emergency: use **Engage kill switch** on the dashboard (halts all trading at once), or run
`stop.ps1 -Disable`.

## 14. After a computer restart
1. Log in to Windows. IB Gateway opens by itself.
2. In IB Gateway choose **IB API → Paper Trading**, log in, and approve 2FA.
3. Nothing else is needed. The dashboard starts within 1 minute and the worker within 2. On its first
   cycle the worker re-checks the account is paper, re-queries any open orders and reconciles
   positions with IBKR before planning anything.
