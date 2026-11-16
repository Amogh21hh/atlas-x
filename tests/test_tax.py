from datetime import date

from atlas.tax import disposals, tax_year, year_summary


def _fill(db, ts, sym, side, qty, px, comm=0.0):
    db.x("INSERT INTO orders(created_at,broker,symbol,side,qty,ref_price,status,filled_qty,avg_fill_price,commission,updated_at) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ts, "ibkr", sym, side, qty, px, "filled", qty, px, comm, ts))


def test_tax_year_boundary():
    assert tax_year(date(2026, 4, 5)) == 2025 and tax_year(date(2026, 4, 6)) == 2026


def test_section_104_pool(db):
    _fill(db, "2026-05-01T10:00:00", "VWRL.L", "buy", 100, 10.0)
    _fill(db, "2026-06-01T10:00:00", "VWRL.L", "buy", 100, 12.0)
    _fill(db, "2026-08-01T10:00:00", "VWRL.L", "sell", 50, 15.0)
    d = disposals(db, "ibkr")[0]
    assert d["rules"] == ["s104"] and abs(d["gain"] - (50 * 15 - 50 * 11)) < 1e-9


def test_thirty_day_rule_overrides_pool(db):
    _fill(db, "2026-05-01T10:00:00", "VWRL.L", "buy", 100, 10.0)
    _fill(db, "2026-08-01T10:00:00", "VWRL.L", "sell", 100, 15.0)
    _fill(db, "2026-08-20T10:00:00", "VWRL.L", "buy", 100, 14.0)    # re-bought within 30 days
    d = disposals(db, "ibkr")[0]
    assert d["rules"] == ["30-day"] and abs(d["gain"] - 100) < 1e-9


def test_isa_is_sheltered_and_taxable_uses_allowance(db):
    _fill(db, "2026-05-01T10:00:00", "VWRL.L", "buy", 1000, 10.0)
    _fill(db, "2026-09-01T10:00:00", "VWRL.L", "sell", 1000, 15.0)     # £5,000 gain
    taxable = year_summary(db, "ibkr", "taxable", "higher", 2026)
    assert abs(taxable["estimated_tax"]["cgt"] - (5000 - 3000) * 0.24) < 1e-6
    assert taxable["cgt_allowance_left"] == 0
    isa = year_summary(db, "ibkr", "isa", "higher", 2026)
    assert isa["estimated_tax"]["total"] == 0 and isa["isa_allowance_left"] == 20000
