# ATLAS X ↔ IBKR paper trading — setup as built on this PC

**Status (14 Nov 2026): connected.**
- Paper account **DU-REDACTED**, GBP base currency, £1,000,000 simulated cash.
- IB Gateway 10.50 (Stable) installed at `C:\Jts\ibgateway`.
- All 10 ETFs verified.
- Cancellation test passed. The buy/sell round-trip is scheduled for Mon 16 Nov at 09:20 UK.

Full evidence: [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md).

ATLAS X is **locked to paper trading in code** (`PAPER_ONLY = True` in `atlas/config.py`). A live or
unverified account engages the kill switch and every order is rejected. There is no setting that
turns this off.

## Account
An IBKR UK **free trial** account (interactivebrokers.co.uk → Free Trial). It's simulated-only and
needs just an email, username and password. The simulated GBP balance appeared about 15 minutes
after the first login.

To use a full IBKR account's paper twin instead, log in to IB Gateway with that account's **paper**
username. Its ID will also start with `DU`.

## IB Gateway settings (already applied)
Log in with **IB API**, **Paper Trading**.

| Setting | Value |
|---|---|
| Configure → Settings → API → Settings: Socket port | 4002 |
| Read-Only API | **off** |
| Allow connections from localhost only | on |
| Trusted IPs | 127.0.0.1 |
| Configure → Settings → Lock and Exit | **Auto restart** 11:45 PM |
| API → Precautions | unchanged |

On the precautions: they reject direct-routed orders (error 10311), so ATLAS sends orders via SMART
routing with the primary listing required to be LSEETF/GBP.

## ATLAS configuration
`atlas-x/.env` (git-ignored, no credentials):
```
ATLAS_BROKER=ibkr
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
ATLAS_CYCLE_SECONDS=900
```

IBKR client IDs:

| Process | Client ID |
|---|---|
| Worker | 17 |
| Dashboard | 18 |
| `ibkr-check` | 19 |

## Verify any time
```bash
python -m atlas.cli ibkr-check                  # connection, paper check, 10 ETFs (name/ISIN/listing/units), GBP cash
python -m atlas.cli ibkr-check --cancel-test    # + unfillable 1-share limit order, then cancel (works when market closed)
python -m atlas.cli ibkr-check --roundtrip      # + buy & sell 1 share ERNS (LSE hours only; skipped otherwise)
```
```bash
python -m atlas.cli gilt-check                  # gilts: IBKR contracts, size rules, quotes, unfillable 1-lot order + cancel
```
`gilt-check` must pass before the mandate's UK gilts option does anything.

Every test order passes through the risk engine first, and the tool refuses to run against any
non-`DU` account.

## Daily operation
- **Day to day:** IB Gateway restarts itself nightly.
- **Weekly:** IBKR requires a full login about once a week, and after any PC restart. Log in with
  Paper Trading. Until you do, ATLAS logs "broker down" (alerting once) and retries every 15 minutes.
- **Startup:** the worker and dashboard start automatically at Windows logon (scheduled tasks). See
  the README for start/stop commands.
