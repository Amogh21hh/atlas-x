"""Live-readiness controls. The live path is exercised by monkeypatching the code lock OFF inside the
test process only; the shipped build keeps PAPER_ONLY = True (asserted below)."""
import pytest

import atlas.config as config
from atlas import data
from atlas.brokers.ibkr import IBKRBroker
from atlas.config import UNIVERSE
from atlas.live import LiveAuthError, LiveLimits, gate, load_authorisation, make_authorisation
from atlas.portfolio import small_capital_plan
from atlas.risk import ProposedOrder, RiskEngine
from tests.fakes import chaos_for

ACCT = "U7654321"


def limits(**kw):
    base = dict(account_id=ACCT, max_capital_gbp=10_000, max_order_gbp=4_000, max_daily_loss_gbp=300,
                max_drawdown_pct=0.10)
    base.update(kw)
    return LiveLimits(**base)


@pytest.fixture()
def live_env(monkeypatch):
    """Unlock the live path for this test only."""
    import atlas.brokers.ibkr  # noqa: F401
    import atlas.engine as engine
    import atlas.live as live
    import atlas.risk as risk
    monkeypatch.setattr(config, "PAPER_ONLY", False)
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(live, "PAPER_ONLY", False)
    monkeypatch.setattr(live, "MODE", "live")
    monkeypatch.setattr(risk, "PAPER_ONLY", False)
    monkeypatch.setattr(engine, "MODE", "live")


def authorise(db, **kw):
    db.set("live_authorisation_v2", make_authorisation(limits(**kw), "tester", days=7))


# ---------------- the shipped build is locked ----------------
def test_shipped_build_is_paper_locked():
    import importlib
    src = importlib.util.find_spec("atlas.config").origin
    text = open(src, encoding="utf-8").read()
    assert "\nPAPER_ONLY = True\n" in text
    assert config.MODE == "paper"


def test_gate_refuses_while_locked(db):
    authorise(db)
    with pytest.raises(LiveAuthError, match="PAPER_ONLY"):
        gate(db, {"ok": True, "live": True, "account": ACCT})


def test_cli_authorise_refuses_while_locked(capsys):
    from atlas.cli import live_authorise
    assert live_authorise() == 1
    assert "disabled" in capsys.readouterr().out


def test_live_mode_uses_a_separate_database(monkeypatch):
    monkeypatch.delenv("ATLAS_DB", raising=False)
    monkeypatch.setattr(config, "MODE", "live")
    assert config._default_db().endswith("atlas-live.db")
    monkeypatch.setattr(config, "MODE", "paper")
    assert config._default_db().endswith("atlas.db") and "live" not in config._default_db()


# ---------------- authorisation record ----------------
def test_limits_can_only_be_stricter():
    errs = limits(max_order_gbp=20_000, max_daily_loss_gbp=900, max_drawdown_pct=0.5,
                  etf_caps={"VWRL.L": 0.9, "XXX": 0.1}, account_id="DU123").validate()
    joined = " | ".join(errs)
    for frag in ("non-DU", "max_order_gbp", "max_daily_loss_gbp", "max_drawdown_pct", "stricter only", "not in the ETF"):
        assert frag in joined


def test_authorisation_tamper_and_expiry_detected(db):
    authorise(db)
    assert load_authorisation(db).max_capital_gbp == 10_000
    rec = db.get("live_authorisation_v2")
    rec["limits"]["max_capital_gbp"] = 1_000_000          # someone edits the DB
    db.set("live_authorisation_v2", rec)
    with pytest.raises(LiveAuthError, match="signature"):
        load_authorisation(db)
    authorise(db)
    rec = db.get("live_authorisation_v2")
    rec["limits"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    from atlas.live import _sign
    rec["sig"] = _sign(rec["limits"])
    db.set("live_authorisation_v2", rec)
    with pytest.raises(LiveAuthError, match="expired"):
        load_authorisation(db)
    with pytest.raises(LiveAuthError):
        make_authorisation(limits(), "x", days=31)


# ---------------- independent live-account verification ----------------
def test_gate_account_checks(db, live_env):
    authorise(db)
    assert gate(db, {"ok": True, "live": True, "account": ACCT}).account_id == ACCT
    for h, why in [({"ok": True, "live": False, "account": "DU999"}, "paper"),
                   ({"ok": True, "live": None, "account": ""}, "unverified"),
                   ({"ok": True, "live": True, "account": "U0000001"}, "not the authorised"),
                   ({"ok": False, "error": "down"}, "not healthy")]:
        with pytest.raises(LiveAuthError, match=why):
            gate(db, h)


def test_gate_refuses_account_with_foreign_holdings(db, live_env):
    authorise(db)
    fake = chaos_for(db, account=ACCT)
    b = IBKRBroker(ib=fake)
    from ib_async import Stock
    alien = Stock("TSLA", "SMART", "USD")
    alien.conId = 76792991
    fake.pos[alien.conId] = [alien, 10, 200.0]
    with pytest.raises(LiveAuthError, match="outside the ATLAS universe"):
        gate(db, b.health(), b)


def test_live_cycle_blocks_wrong_account_and_engages_kill_switch(db, live_env):
    from atlas.engine import cycle
    from atlas.research.lab import run_lab
    run_lab(db)
    db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")
    authorise(db)
    fake = chaos_for(db, account="U0000001")
    rep = cycle(db, IBKRBroker(ib=fake), force_trade=True, ignore_hours=True)
    assert "live_verification_failed" in rep["blocked"]
    assert db.get("kill_switch") is True and fake._trades == []


def test_adapter_refuses_live_account_in_paper_mode_and_paper_account_in_live_mode(db, monkeypatch, live_env):
    st = IBKRBroker(ib=chaos_for(db, account="DU1")).submit_market("ERNS.L", "buy", 1, 100.0)
    assert st.status == "rejected" and "live mode refuses" in st.message
    monkeypatch.setattr(config, "MODE", "paper")
    st = IBKRBroker(ib=chaos_for(db, account=ACCT)).submit_market("ERNS.L", "buy", 1, 100.0)
    assert st.status == "rejected" and "paper-only" in st.message


# ---------------- live limits in the risk engine ----------------
def _review(db, lim, orders, equity=100_000, cash=100_000, held=None):
    _, dates = data.latest_prices_gbp(db)
    return RiskEngine(db, live=lim).review(orders, equity, cash, held or {}, dates, True)


def test_live_capital_cap_order_cap_and_capped_concentration(db, live_env):
    lim = limits()
    prices, _ = data.latest_prices_gbp(db)
    p = prices["VWRL.L"]
    # 1) £ max order (4k) — even though 65% of a £100k account would allow far more
    rep = _review(db, lim, [ProposedOrder("VWRL.L", "buy", int(5_000 / p), p)])
    assert "max order" in rep.rejected[0][1]
    # 2) concentration is measured against the £10k CAP, not the £100k account: VUKE cap 35% => £3.5k
    pv = prices["VUKE.L"]
    rep = _review(db, lim, [ProposedOrder("VUKE.L", "buy", int(3_900 / pv), pv)])
    assert "cap" in rep.rejected[0][1]
    # 3) total invested may never exceed the capital cap, whatever the planner asks for
    held = {"VWRL.L": 6_400.0, "IGLT.L": 3_000.0}
    pi = prices["IGLT.L"]
    rep = _review(db, lim, [ProposedOrder("IGLT.L", "buy", int(1_000 / pi), pi)], held=held)
    assert "capital cap" in rep.rejected[0][1]
    # 4) cash-proxy buys are also capped (no "risk-reducing" loophole for size)
    pe = prices["ERNS.L"]
    rep = _review(db, lim, [ProposedOrder("ERNS.L", "buy", int(4_500 / pe), pe)])
    assert "max order" in rep.rejected[0][1]


def test_live_requires_fresh_prices(db, live_env):
    prices, _ = data.latest_prices_gbp(db)
    rep = RiskEngine(db, live=limits()).review([ProposedOrder("VWRL.L", "buy", 1, prices["VWRL.L"])], 1e5, 1e5, {},
                                               {"VWRL.L": "2026-01-02"}, True)
    assert "stale" in rep.rejected[0][1]


def test_live_daily_loss_and_capped_drawdown_in_pounds(db, live_env):
    r = RiskEngine(db, live=limits(max_daily_loss_gbp=300))
    r.update_equity(100_000)
    r.update_equity(99_650)                      # -£350 on a £100k account = only -0.35%
    assert "live daily loss" in (r.state()["suspended"] or "")
    db.set("suspended", None)
    db.set("risk_day", None)
    db.set("peak_equity", None)
    r2 = RiskEngine(db, live=limits(max_daily_loss_gbp=400, max_drawdown_pct=0.05))
    r2.update_equity(10_000)
    db.set("day_start_equity", 9_500)           # new day baseline
    r2.update_equity(9_490)                      # -£510 from peak >= 5% of £10k cap
    assert "live drawdown" in (r2.state()["suspended"] or "")


def test_live_orders_are_collared_limit_orders(db, live_env):
    from atlas.execution import execute
    authorise(db)
    lim = load_authorisation(db)
    fake = chaos_for(db, account=ACCT)
    fake.price_move = 0.03                       # market gapped 3% above the approved price
    b = IBKRBroker(ib=fake)
    prices, dates = data.latest_prices_gbp(db)
    p = prices["IGLT.L"]
    rr = RiskEngine(db, live=lim).review([ProposedOrder("IGLT.L", "buy", int(2_000 / p), p)], 50_000, 50_000, {},
                                         dates, True)
    assert rr.approved, rr.rejected
    res = execute(db, b, RiskEngine(db, live=lim), rr.approved, timeout_s=0, collar=lim.price_collar_pct)
    t = fake._trades[0]
    assert t.order.orderType == "LMT" and t.order.lmtPrice <= p * 1.005 + 1e-9
    assert res[0]["status"] == "submitted"         # did NOT chase the gap


# ---------------- small-capital mode ----------------
def test_small_capital_mode():
    t = {s: 0.0 for s in UNIVERSE}
    t.update({"VWRL.L": 0.6, "IGLT.L": 0.4})
    tiny, band, mo = small_capital_plan(t, 2_000)
    assert tiny["IGLT.L"] == 0 and tiny["VWRL.L"] <= UNIVERSE["VWRL.L"]["max_w"] and band == 0.05 and mo == 250
    assert sum(tiny.values()) <= 1.0
    big, band2, _ = small_capital_plan(t, 50_000)
    assert big is t and band2 is None
    for cap in (1_500, 3_000, 8_000, 20_000):
        tgt, _, _ = small_capital_plan(t, cap)
        assert all(w * cap >= 1_000 - 1e-6 for w in tgt.values() if w > 0)
