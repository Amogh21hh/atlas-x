"""Gilt engine: maths checked against an independent published table; sleeve policy; end-to-end on a fake gateway."""
import json
import statistics
from datetime import date
from pathlib import Path

import pytest

from atlas.gilts import (BOND_SLEEVE, CASH_SLEEVE, Gilt, after_tax_yield, commission_gbp, expected_coupons,
                         gross_yield, parse_maturity, select)
from atlas.mandate import Mandate

FIX = json.loads((Path(__file__).parent / "fixtures" / "gilts_2026-11-13.json").read_text())
TRADE = date(2026, 11, 13)


def _gilts():
    return [Gilt(x["name"], x["coupon"], parse_maturity(x["maturity"]), x["clean"], ticker=x["ticker"]) for x in FIX["gilts"]]


def test_yields_match_independent_source():
    d_gross, d_basic, d_higher = [], [], []
    for x, g in zip(FIX["gilts"], _gilts()):
        d_gross.append(abs(gross_yield(g, TRADE) * 100 - x["gry"]["zero"]))
        d_basic.append(abs(after_tax_yield(g, TRADE, "basic", flat_rate=0.20) * 100 - x["gry"]["basic"]))
        d_higher.append(abs(after_tax_yield(g, TRADE, "higher", flat_rate=0.40) * 100 - x["gry"]["higher"]))
    for d in (d_gross, d_basic, d_higher):
        assert statistics.median(d) < 0.01 and max(d) < 0.1     # percentage points


def test_tax_makes_low_coupon_gilts_win_for_taxable_but_not_isa():
    isa = select(_gilts(), TRADE, None, CASH_SLEEVE, 10_000)
    hi = select(_gilts(), TRADE, "higher", CASH_SLEEVE, 10_000)
    avg = lambda ps: sum(p["gilt"].coupon for p in ps) / len(ps)
    assert avg(hi) < 1.5 < avg(isa)
    for p in hi + isa:
        assert 0.25 <= p["years"] <= 3.0
    assert len({p["gilt"].maturity.year for p in hi}) == len(hi)            # a ladder, one per year
    assert abs(sum(p["weight"] for p in hi) - 1) < 1e-9


def test_bond_ladder_spans_range_with_mid_duration():
    picks = select(_gilts(), TRADE, "higher", BOND_SLEEVE, 40_000)
    yrs = [p["years"] for p in picks]
    assert min(yrs) >= 2 and max(yrs) <= 16 and len(picks) >= 4
    dur = sum(p["duration"] * p["weight"] for p in picks)
    assert 5 < dur < 11


def test_commission_and_coupons():
    assert commission_gbp(500) == 1.0 and commission_gbp(10_000) == 10.0 and commission_gbp(20_000) == 12.5
    g = Gilt("x", 4.0, date(2028, 6, 7))
    assert expected_coupons({"GILT:x": 10_000}, {"GILT:x": g}, date(2026, 11, 1), date(2027, 6, 30)) == pytest.approx(400)


def test_split_targets_never_forces_igl_sale():
    from atlas.sleeve import split_targets
    m = Mandate(gilt_sleeve="cash+bonds")
    t = {"VWRL.L": 0.6, "IGLT.L": 0.4, "ERNS.L": 0.0}
    tgt, cash_w, bond_w = split_targets(t, m, {"IGLT.L": 2_000.0}, 10_000)   # already holds 20% in IGLT
    assert bond_w == pytest.approx(0.2) and tgt["IGLT.L"] == pytest.approx(0.2 / 0.8)
    tgt2, _, bond_w2 = split_targets(t, Mandate(gilt_sleeve="cash"), {}, 10_000)
    assert bond_w2 == 0 and tgt2 == pytest.approx(t)


# ---------------- end to end on a fake IBKR gateway with gilts ----------------
def _setup(db, sleeve="cash+bonds", wrapper="taxable"):
    from atlas.mandate import approve
    from atlas.research.lab import run_lab
    run_lab(db)
    db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")
    db.set("selection_override", None)
    approve(db, Mandate(capital_limit_gbp=50_000, wrapper=wrapper, tax_band="higher", gilt_sleeve=sleeve), "t")


def _force_6040(db, monkeypatch):
    import atlas.engine as eng
    monkeypatch.setattr(eng, "current_selection", lambda db: {"name": "static_60_40", "params": {}})


def test_sleeve_inactive_until_verified(db, monkeypatch):
    from atlas.brokers.ibkr import IBKRBroker
    from atlas.engine import cycle
    from tests.fakes import PENCE, chaos_for
    _setup(db)
    _force_6040(db, monkeypatch)
    fake = chaos_for(db)
    cycle(db, IBKRBroker(ib=fake, scales={s: 0.01 for s in PENCE}), force_trade=True, ignore_hours=True)
    assert not any(getattr(t.contract, "secType", "") == "BOND" for t in fake._trades)


def test_full_cycle_buys_gilts_reconciles_and_redeems(db, monkeypatch):
    from atlas.brokers.ibkr import IBKRBroker
    from atlas.engine import cycle
    from tests.fakes import PENCE, chaos_for
    _setup(db)
    _force_6040(db, monkeypatch)
    db.set("gilt_verified", {"at": "test"})
    fake = chaos_for(db)
    scales = {s: 0.01 for s in PENCE}
    rep = cycle(db, IBKRBroker(ib=fake, scales=scales), force_trade=True, ignore_hours=True)
    bonds = [t for t in fake._trades if getattr(t.contract, "secType", "") == "BOND"]
    assert "gilt_sleeve" in rep["steps"] and bonds
    assert all(t.order.orderType == "LMT" for t in bonds)                    # always limit orders
    assert not any(t.contract.conId == 7005 for t in bonds)                  # index-linked excluded
    assert not any(t.order.action == "SELL" for t in fake._trades)
    assert rep["reconcile"]["ok"]
    rep2 = cycle(db, IBKRBroker(ib=fake, scales=scales), ignore_hours=True)  # next cycle: no deposit, no churn
    assert rep2["reconcile"]["ok"] and not db.q("SELECT 1 FROM cash_flows")
    # --- one gilt matures: IBKR removes the position and pays nominal + final coupon ---
    held = {cid: v for cid, v in fake.pos.items() if cid in (7001, 7002, 7003, 7004)}
    cid, (c, nominal, _) = next(iter(held.items()))
    row = fake._bond_row(cid)
    key = next(k for k, m in db.get("gilt_meta").items() if m["isin"] == row[1])
    meta = db.get("gilt_meta")
    meta[key]["maturity"] = "2026-01-01"
    db.set("gilt_meta", meta)
    del fake.pos[cid]
    fake.cash += nominal * (1 + row[3] / 200)
    fake.GILTS = [r for r in fake.GILTS if r[0] != cid]
    rep3 = cycle(db, IBKRBroker(ib=fake, scales=scales), ignore_hours=True)
    assert rep3["reconcile"]["ok"]                                            # redemption recorded in ledger
    assert not db.q("SELECT 1 FROM cash_flows")                               # and not mistaken for a deposit
    assert db.q("SELECT 1 FROM events WHERE kind='gilt_redemption'")


def test_risk_rejects_unselected_gilt(db):
    from atlas import data
    from atlas.risk import ProposedOrder, RiskEngine
    _, dates = data.latest_prices_gbp(db)
    dates["GILT:GB00XXXX"] = date.today().isoformat()
    rep = RiskEngine(db).review([ProposedOrder("GILT:GB00XXXX", "buy", 1000, 0.95)], 1e5, 1e5, {}, dates, False)
    assert "whitelist" in rep.rejected[0][1]
