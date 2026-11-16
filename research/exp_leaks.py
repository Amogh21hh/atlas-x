"""Where does a 60/40 UK investor lose return? Rebalancing method, idle cash, tax wrapper.
Real LSE ETF data (Yahoo, via ATLAS DB). Price return from close, income from adjclose-close gap."""
import sys, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from atlas.db import DB
from atlas.data import load_panel
from atlas.config import UNIVERSE

db = DB()
adj = load_panel(db, "adjclose", ["VWRL.L", "IGLT.L", "ERNS.L"]).dropna()
cls = load_panel(db, "close", ["VWRL.L", "IGLT.L", "ERNS.L"]).loc[adj.index]
for s in cls: cls[s] = cls[s] * UNIVERSE[s]["scale"]
tr = adj.pct_change().fillna(0)            # total return
pr = cls.pct_change().fillna(0)            # price return
inc = (tr - pr).clip(lower=0)              # income yield per day (distributions)
idx = adj.index
W = {"VWRL.L": 0.6, "IGLT.L": 0.4}
COMM_MIN, COMM_BPS, SPREAD_BPS = 3.0, 5.0, 5.0

# tax parameters (2026/27 assumptions, verified separately): higher-rate taxpayer
CGT_ALLOW, CGT_RATE = 3000.0, 0.24
DIV_ALLOW, DIV_RATE = 500.0, 0.3375       # equity fund distributions
INT_RATE = 0.40                           # bond-fund (>60% bonds) distributions taxed as interest
PSA = 500.0                               # higher-rate personal savings allowance

def simulate(method, start=10_000.0, monthly=500.0, taxable=False):
    hold = {s: 0.0 for s in W}; cost = {s: 0.0 for s in W}   # GBP value, cost basis
    cash = start; trades = 0; fees = 0.0; realised = 0.0
    tax_paid = 0.0; contrib = start
    year_gain = 0.0; year_div = 0.0; year_int = 0.0
    months = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).max().values
    month_set = set(pd.DatetimeIndex(months))
    vals = []
    eqw = []
    last_year = idx[0].year
    def trade(s, amt):
        nonlocal cash, trades, fees, realised, year_gain
        if abs(amt) < 1: return
        fee = max(COMM_MIN, abs(amt) * COMM_BPS / 1e4) + abs(amt) * SPREAD_BPS / 1e4
        if amt > 0:
            hold[s] += amt; cost[s] += amt
        else:
            frac = -amt / hold[s]
            g = -amt - cost[s] * frac
            realised += g; year_gain += g
            cost[s] *= (1 - frac); hold[s] += amt
        cash -= amt + fee; fees += fee; trades += 1
    def rebalance(full):
        tot = cash + sum(hold.values())
        for s, w in W.items():
            d = w * tot * 0.995 - hold[s]
            if full or d > 0: trade(s, d)
    first = True
    for i, d in enumerate(idx):
        if i:
            for s in W:
                hold[s] *= (1 + pr[s].iloc[i])
                income = hold[s] * inc[s].iloc[i]
                cash += income
                if s == "IGLT.L": year_int += income
                else: year_div += income
            cash *= (1 + tr["ERNS.L"].iloc[i]) if method.endswith("+cashETF") else 1.0
        if d.year != last_year:   # settle last tax year (approx calendar year)
            if taxable:
                t = max(0, year_gain - CGT_ALLOW) * CGT_RATE + max(0, year_div - DIV_ALLOW) * DIV_RATE \
                    + max(0, year_int - PSA) * INT_RATE
                cash -= t; tax_paid += t
            year_gain = year_div = year_int = 0.0; last_year = d.year
        if d in month_set:
            if not first:
                cash += monthly; contrib += monthly
            tot = cash + sum(hold.values())
            drift = max(abs(hold[s] / tot - w) for s, w in W.items()) if tot else 1
            m = method.replace("+cashETF", "")
            if first: rebalance(True); first = False
            elif m == "monthly": rebalance(True)
            elif m == "band5": rebalance(drift > 0.05) if drift > 0.05 else rebalance(False)
            elif m == "annual": rebalance(d.month == 12) if d.month == 12 else rebalance(False)
            elif m == "cashflow": rebalance(drift > 0.10)       # new money to underweight; sell only if >10pp off
        vals.append(cash + sum(hold.values()))
        eqw.append(hold["VWRL.L"] / max(1e-9, cash + sum(hold.values())))
    unreal = sum(hold.values()) - sum(cost.values())
    exit_tax = max(0, unreal - CGT_ALLOW) * CGT_RATE if taxable else 0.0
    v = pd.Series(vals, index=idx)
    dd = (v / v.cummax() - 1).min()
    return {"method": method, "taxable": taxable, "final": round(v.iloc[-1]), "contrib": round(contrib),
            "gain": round(v.iloc[-1] - contrib), "trades": trades, "fees": round(fees),
            "realised_gains": round(realised), "tax": round(tax_paid), "exit_cgt": round(exit_tax),
            "after_exit": round(v.iloc[-1] - exit_tax), "max_dd_value": round(dd * 100, 1),
            "avg_equity_w": round(float(np.mean(eqw)) * 100, 1), "max_equity_w": round(float(np.max(eqw)) * 100, 1),
            "vol": round(float(pd.Series(vals).pct_change().std() * np.sqrt(252)) * 100, 2)}

rows = []
for taxable in (False, True):
    for m in ("monthly", "annual", "cashflow"):
        rows.append(simulate(m, taxable=taxable))
lump = [simulate(m, start=100_000, monthly=0, taxable=tx) for tx in (False, True) for m in ("monthly", "cashflow")]
# idle-cash test: hold 10% cash reserve idle vs in ERNS (100k lump, no contributions)
def cash_drag(reserve=0.10):
    r_idle = 0.0; e = tr["ERNS.L"]
    yrs = len(idx) / 252
    ern_cagr = (1 + e).prod() ** (1 / yrs) - 1
    return round(ern_cagr * 100, 2)
out = {"lump_100k": lump, "period": f"{idx[0].date()} to {idx[-1].date()}", "rows": rows, "ERNS_cagr_pct": cash_drag(),
       "VWRL_income_yield_pct": round(inc['VWRL.L'].sum() / (len(idx)/252) * 100, 2),
       "IGLT_income_yield_pct": round(inc['IGLT.L'].sum() / (len(idx)/252) * 100, 2)}
print(json.dumps(out, indent=1))
