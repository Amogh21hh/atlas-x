"""E3: Is "30% per week" achievable? Evidence from real weekly returns, plus what fees do to £100.

Universe: the 10 ATLAS LSE ETFs (from the DB) plus the most volatile liquid things a UK retail
investor can legally buy, fetched fresh from Yahoo: Bitcoin, Ether, Solana, Nvidia, Tesla, and the
3x leveraged Nasdaq ETF (TQQQ, shown for evidence only: leverage is outside the mandate).

For each asset: share of weeks with a gain of >=30%, best week, worst week, and the best
consecutive run of >=30% weeks. Then: what 30%/week compounds to, and fee drag on £100 at IBKR.
Writes research/results/exp_weekly_target.json. Run: python research/exp_weekly_target.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd

from atlas.data import fetch_yahoo, load_panel
from atlas.db import DB
from atlas.config import UNIVERSE

TARGET = 0.30
EXTRA = {"BTC-GBP": "Bitcoin", "ETH-GBP": "Ether", "SOL-USD": "Solana", "NVDA": "Nvidia",
         "TSLA": "Tesla", "TQQQ": "3x leveraged Nasdaq ETF"}


def weekly_stats(px: pd.Series) -> dict:
    w = px.dropna().resample("W-FRI").last().pct_change().dropna()
    hit = (w >= TARGET).astype(int)
    run = best = 0
    for h in hit:
        run = run + 1 if h else 0
        best = max(best, run)
    return {"weeks": int(len(w)), "from": str(w.index[0].date()), "to": str(w.index[-1].date()),
            "pct_weeks_ge_30": round(100 * hit.mean(), 2),
            "best_week_pct": round(100 * w.max(), 1), "worst_week_pct": round(100 * w.min(), 1),
            "median_week_pct": round(100 * w.median(), 2),
            "longest_run_of_30pct_weeks": best,
            "avg_week_compounded_pct": round(100 * (np.prod(1 + w) ** (1 / len(w)) - 1), 2)}


def main() -> dict:
    db = DB()
    out = {"target_weekly": TARGET, "assets": {}}
    panel = load_panel(db, "adjclose", list(UNIVERSE))
    for s in panel:
        out["assets"][s] = {"name": UNIVERSE[s].get("name", s), **weekly_stats(panel[s])}
    for sym, name in EXTRA.items():
        try:
            df = fetch_yahoo(sym, start=1388534400)  # from 2014
            out["assets"][sym] = {"name": name, **weekly_stats(df["adjclose"])}
        except Exception as e:  # network/data problems are reported, not hidden
            out["assets"][sym] = {"name": name, "error": str(e)}

    out["what_30pct_a_week_means"] = {
        f"after_{n}_weeks_from_100": round(100 * 1.3 ** n, 0) for n in (4, 12, 26, 52)}

    # Fee drag on £100 at IBKR UK (min £3 per LSE ETF order, ~0.05% spread)
    fee = lambda amt: max(3.0, amt * 5 / 1e4) + amt * 5 / 1e4
    out["fees_on_100"] = {
        "one_fund_one_purchase_pct": round(100 * fee(100) / 100, 2),
        "two_funds_60_40_pct": round(100 * (fee(60) + fee(40)) / 100, 2),
        "weekly_trading_in_and_out_per_year_pct": round(100 * 52 * 2 * fee(100) / 100, 0),
    }
    res = ROOT / "research" / "results"
    res.mkdir(exist_ok=True)
    (res / "exp_weekly_target.json").write_text(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
