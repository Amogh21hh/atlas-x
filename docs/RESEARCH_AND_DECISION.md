# What should an autonomous capital system actually do? — research and decision

_14 November 2026 · ATLAS X_

## The question, challenged

The brief asked for a system that "generates sustainable financial returns from capital" for a UK
individual (£1k–£100k), using free tools and a regulated institution, with minimal involvement.

The premise needs one correction up front. **No legitimate system can reliably create returns beyond
what markets pay for bearing risk.** For a retail investor, the money that is reliably *lost* (to tax,
costs, idle cash and behaviour) is larger and more controllable than any trading edge the evidence
supports. So the strongest product is not a cleverer trader. It is an allocator that captures market
returns and **stops the predictable leaks**, autonomously and safely.

## Evidence

### A. Competing return sources (independent research agent, sources in the agent report)

| Source | Verdict | Why |
|---|---|---|
| Diversified index ETFs (equity + bond premia) | **STRONG** | 120+ years of global data. Costs under 0.3% p.a. Unlimited capacity. Automatable at IBKR UK |
| Cash / short gilts | **STRONG** (preservation) | Bank Rate 4.00% (5 Nov 2026); CPI 3.5%, so the real return is about zero |
| Tax wrappers (ISA / SIPP) | **STRONG** | A guaranteed, legislated advantage. IBKR UK offers a Stocks & Shares ISA (cash-only, no margin) |
| Factor ETFs (value, momentum…) | WEAK | Returns about 58% lower after publication (McLean & Pontiff 2016); decade-long drawdowns |
| Trend following (UCITS ETF) | WEAK (5–15% diversifier) | Century of evidence, but weak since 2009 net of fees |
| Covered calls / put writing | WEAK–AVOID | No better risk-adjusted return than equities (Israelov & Nielsen 2015); crash exposure |
| Crypto yield / basis / DeFi | AVOID | Retail crypto derivatives are banned in the UK; carry predicts crashes (BIS WP 1087); 2022 failures |
| P2P lending | AVOID | Platform failures; Funding Circle closed retail in 2022 |
| Day / algorithmic trading | AVOID | 97% of persistent day traders lose money (Chague et al.); FCA: 76% of CFD clients lose |
| Matched betting / prediction markets | AVOID | Accounts get restricted; it breaches terms; it's labour, not return on capital |

This matches ATLAS X's own research lab: after costs and multiple-testing correction, simple 60/40
diversification ranked at or near the top of everything tested. Short-term reversal failed.

### B. UK tax facts, 2026/27 (verified on GOV.UK where possible)

| Item | Figure |
|---|---|
| ISA allowance | £20,000 |
| CGT allowance | £3,000 |
| CGT rates | 18% / 24% |
| Dividend allowance | £500 |
| Dividend rates (from April 2026) | 10.75% / 35.75% / 39.35% |
| Savings interest rates | 20/40/45%, rising to **24/44/49% from April 2027** (announced, not yet enacted) |

Interest-type distributions from bond ETFs (IGLT, ERNS…) are taxed as savings income. **Gilts are
CGT-exempt.**

**Worked example.** 0⅛% Treasury 2028, at the 13 Nov 2026 price (third-party data): a higher-rate
taxpayer nets about **4.13%** after tax, against about **2.4%** from a money-market fund or savings
account at the same gross yield.

### C. ATLAS X's own experiments (real LSE ETF data, Oct 2013 → Nov 2026)

Reproduce: `python research/exp_leaks.py`

**60/40 VWRL/IGLT, £100k invested at the start, higher-rate taxpayer:**

| Setting | Value after all tax | Notes |
|---|---|---|
| Inside an ISA | **£266,936** | |
| Taxable account | **£225,009** | £15.2k tax on income + £21.2k CGT when sold |
| Tax wrapper advantage | **≈ £42,000 (≈ 1.3% a year)** | |
| Trading costs | £450–1,200 over 13 years | |

**£10k start + £500/month (£88.5k contributed), ISA:**

| Rebalancing method | Final value | Trades | Fees | Gains realised | Avg / max equity weight |
|---|---|---|---|---|---|
| Monthly sell-and-buy | £148,940 | 316 | £1,038 | £8,902 | 59.6% / 63.0% |
| Annual | £150,396 | 208 | £685 | £2,966 | 60.1% / 65.5% |
| **Contribution-led** (new money to underweight; sell only beyond a band) | **£150,741** | **189** | **£622** | **£0** | 60.4% / 65.5% |

Contribution-led rebalancing adds about £1,800 with **no extra risk** (drawdown the same, −15.6%), and
it **never created a taxable sale**.

With a lump sum and no new money, a wide band lets equities drift to 64% on average (71% at the
peak). The extra return in that case is extra risk, not skill. ATLAS therefore uses a **5-point band**,
not a wider one.

**Idle cash.** ERNS (sterling ultrashort) returned 1.95% a year over the period. Cash left idle at the
broker earns nothing on the first £8k at IBKR, so the platform keeps idle cash under 1%.

### Ranking of the levers (largest first, for a typical investor)

1. **Tax wrapper:** about 1–1.5% a year for a higher-rate taxpayer, more as rates rise.
2. **Asset class mix and staying invested:** the market premium itself.
3. **Cash management:** about 1.5–2% a year on idle cash (after-tax gap: gilts vs savings).
4. **Disciplined, contribution-led rebalancing:** about 0.1–0.3% a year, and avoids taxable sales.
5. **Trading costs:** 0.05–0.2% a year at IBKR.
6. **Active trading strategies:** negative to zero after costs, on the evidence.

## Decision

**ATLAS X is an autonomous, tax- and cost-aware capital allocator. It is not a trading system.** It
keeps the verified IBKR execution, research lab and safety stack, and adds:

| Capability | Status |
|---|---|
| **Mandate**: your one-time approval of capital limit, cash reserve, account type (ISA / taxable / SIPP) and tax band. Paper sizing uses it too, so paper results reflect a realistic account size, not IBKR's £1M simulation | Built, tested |
| **Contribution-led rebalancing** with a 5-point band | Built, tested; the default |
| **Deposit / withdrawal detection** and time-weighted returns. Loss limits are adjusted, so cash movements aren't counted as profit or loss | Built, tested |
| **UK tax engine**: share-pooling (same day, 30-day rule, Section 104), 6-April tax year, CGT allowance tracking, estimated dividend/interest tax by fund type, ISA allowance tracking | Built, tested (estimates; not tax advice) |
| **Independent evaluation**: actual results against cash, the untraded target mix, and global equity alone, all from market prices | Built |
| **Plain-language home screen**: status, attention items with what to do, money, results, tax, decisions, risks, mandate, pause, emergency stop | Built, verified in the browser |
| **Daily verified backups** and restore | Built, tested |

**What I chose not to build, and why:**
- **Trend / factor sleeves:** the evidence is too weak to justify the added complexity, cost and
  behavioural risk. The research lab can still re-test them under the same gate.
- **Gilt sleeve: BUILT, waiting for verification on IBKR.** See the "Gilt sleeve" section below.
- **Automatic bed-and-ISA / CGT-allowance harvesting:** needs a second (ISA) account connected and
  the matching rules confirmed. The tax engine already reports the remaining allowance.
- Crypto, P2P, options, betting and day trading: ruled out on the evidence (table A).

## What this does not change

- The system **cannot promise profit**. It captures market returns minus as little leakage as possible.
  Markets can fall 25–35% in a severe crisis (no 2008-type event in the 2014–2026 data).
- Live trading stays disabled (see `LIVE_READINESS_REPORT.md`). The biggest single lever, opening the
  real account as an **IBKR Stocks & Shares ISA**, is your decision, as are the amount and your tax
  position. A regulated adviser can help.


## Gilt sleeve (built 14 Nov 2026)

`atlas/gilts.py` + `atlas/sleeve.py`. It is opt-in in the mandate: **Off**, **Cash share in short gilts**,
or **Cash and bond share in gilts**.

**What it does.** It holds the strategy's cash share, and optionally its bond (IGLT) share, in directly
held conventional UK gilts. Gilts are ranked by **after-tax, after-commission yield** for your account
type and tax band:
- coupons are taxed at the savings rate of the tax year they're paid in (rising from April 2027);
- the gain to maturity is CGT-free.

**Sleeves**
- Cash sleeve: a 0.25–3 year ladder, one gilt per maturity year.
- Bond sleeve: a 2–16 year ladder, one gilt per maturity bucket, about 8–9 years' duration.

**Maths verified against an independent source.** Using 70 gilts priced 13 Nov 2026, against a
published table of gross and after-tax yields: median difference **0.001 pp**, largest 0.056 pp
(`tests/test_gilts.py`, fixture `tests/fixtures/gilts_2026-11-13.json`).

**What it would choose today**

| Tax position | Cash sleeve, after tax | Bond ladder, after tax |
|---|---|---|
| ISA / SIPP | 4.77% (4–4.5% coupons) | 5.32% |
| Taxable, basic rate | 4.27% | 4.99% |
| Taxable, higher rate | **4.04%** (0.125–1.25% coupons) | **4.78%** |

For comparison, a higher-rate taxpayer nets about 2.9% after tax from a 5% savings account.

**Policy**
- Buy-and-hold to maturity. The sleeve never sells to "optimise".
- Existing IGLT is **kept, not sold**, so switching creates no taxable gain; only new money goes into
  gilts.
- At least £1,000 per gilt purchase, keeping commission at or under 0.1%.
- Always collared limit orders.
- A per-gilt cap of 30%.
- Index-linked gilts are excluded.
- Coupons and redemptions are recorded, so cash reconciles exactly and maturities aren't mistaken for
  deposits.

**Data**
- Gilt list, coupons, maturities, ISINs and prices all come from **IBKR** (the executing broker).
- The DMO's data is behind a bot-check, which was not bypassed.
- The third-party table was used **once**, as a test fixture only. That site blocks AI crawlers, so it
  is never scraped at runtime.

**Safety gate.** The sleeve stays inactive until `python -m atlas.cli gilt-check` passes on the paper
gateway. That check confirms:
- IBKR lists conventional gilts (issuer symbol `UKT`, configurable);
- their size rules;
- quotes arrive;
- a 1-lot unfillable limit order is accepted and cancelled.

Until it passes, the cash share stays in the ERNS fund and the bond share in IGLT, exactly as before.

**Not yet verified**
- IBKR's issuer symbol, nominal units and size rules for gilts.
- Whether gilt quotes are available without a market-data subscription.
- Real gilt fills.

**Simplifications** (each conservative or small)
- The ex-dividend period is ignored.
- The Accrued Income Scheme applies only above £5,000 nominal, and its effect is small for low-coupon
  gilts.
- Commission is from IBKR's published tiers, taking the upper end of the stated minimums.

## E3. "30% a week from £100" (14 Nov 2026) — rejected on evidence

Experiment: `research/exp_weekly_target.py` → `research/results/exp_weekly_target.json`. Real weekly
returns 2014–2026 for the 10 ATLAS ETFs plus the most volatile liquid assets a UK retail investor
can buy (Bitcoin, Ether, Solana, Nvidia, Tesla, 3x Nasdaq ETF).

| Asset | Weeks with ≥30% gain | Most such weeks in a row | Worst single week |
|---|---|---|---|
| Global shares (VWRL) | 0% | 0 | −10.7% |
| Nvidia | 0.15% | 1 | −20.1% |
| Bitcoin | 0.63% | 2 | −35.1% |
| Solana (best case found) | 6.1% | 3 | −51.7% |
| 3x leveraged Nasdaq | 0% | 0 | −37.5% |

- 30% a week compounds to about 84 million times the stake in a year (£100 → about £84m). No asset, fund or
  strategy in the record does this. The assets that sometimes have +30% weeks also have −35% to
  −52% weeks, so a strategy that targets the goal is a leveraged bet, and its most likely outcome is
  losing the stake. **No strategy was built to chase it** (mandate: no leverage, no gambling).
- **Fees at £100 through IBKR:** the £3 minimum commission is 3% of the stake for one fund,
  6% for a 60/40 pair, and over 300% a year if traded weekly. Trading frequently with £100 at IBKR
  loses money from fees alone. The existing small-capital rule (no position under £1,000) means
  ATLAS would correctly leave £100 uninvested at IBKR.
- **Data fix found by this experiment:** Yahoo's SGLN.L (gold) history before Jul 2011 mixes USD-cent
  and GBp quotes (+67% / −39% "weeks" that never happened). `data.clean` now also removes prices
  more than 25% away from their 21-day median, and SGLN history starts on 2011-07-01. Research windows
  (from 2014) were not affected.
- Product change: a weekly profit-and-loss table and an "Is my goal realistic?" check on the home screen
  (`atlas/weekly.py`, `/api/weekly`).
