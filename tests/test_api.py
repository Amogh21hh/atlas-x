from fastapi.testclient import TestClient

from tests.conftest import seed_prices


def test_api_auth_and_controls():
    from atlas import api
    seed_prices(api.db)
    api.db.set("data_refreshed_at", "2999-01-01T00:00:00+00:00")
    c = TestClient(api.app)
    assert c.get("/api/overview").status_code == 401
    assert c.post("/api/setup", json={"email": "inv@example.com", "password": "short"}).status_code == 400
    assert c.post("/api/setup", json={"email": "inv@example.com", "password": "a-long-password-1"}).status_code == 200
    assert c.post("/api/setup", json={"email": "x@example.com", "password": "a-long-password-2"}).status_code == 409
    c.post("/api/logout")
    c.cookies.clear()
    assert c.post("/api/login", json={"email": "inv@example.com", "password": "wrong-password!"}).status_code == 401
    assert c.post("/api/login", json={"email": "inv@example.com", "password": "a-long-password-1"}).status_code == 200
    assert c.post("/api/research/run").status_code == 200
    r = c.post("/api/cycle", json={"force": True, "ignore_hours": True})
    assert r.status_code == 200 and r.json()["equity"] > 0
    o = c.get("/api/overview").json()
    assert o["snapshot"]["broker"] == "paper" and o["broker"]["live"] is False
    # kill switch then cycle => no orders approved
    c.post("/api/kill", json={"on": True})
    assert c.get("/api/overview").json()["risk"]["kill_switch"] is True
    assert c.post("/api/kill", json={"on": False}).status_code == 403          # release needs the password
    assert c.get("/api/overview").json()["risk"]["kill_switch"] is True
    assert c.post("/api/kill", json={"on": False, "password": "a-long-password-1"}).status_code == 200
    stop = c.post("/api/emergency-stop").json()                                  # paper broker: no open orders
    assert stop["kill_switch"] is True
    assert c.post("/api/kill", json={"on": False, "password": "a-long-password-1"}).status_code == 200
    # sensitive actions need the password
    assert c.post("/api/liquidate", json={"password": "nope-nope-nope"}).status_code == 403
    # paper-only lock: live authorisation is refused outright, even with the right password and phrase
    locked = c.post("/api/live/authorise", json={"password": "a-long-password-1", "phrase": api.LIVE_PHRASE,
                                                 "max_capital_gbp": 1000})
    assert locked.status_code == 403
    assert api.db.get("live_authorisation") is None
    ov = c.get("/api/overview").json()
    assert ov["paper_only"] is True and ov["pending_orders"] == [] and ov["last_reconcile_ok"]
    assert c.get("/api/research").json()["strategies"]
    assert "ATLAS X daily report" in c.get("/api/report").json()["markdown"]


def test_home_mandate_and_pause():
    from atlas import api
    c = TestClient(api.app)
    if c.post("/api/login", json={"email": "inv@example.com", "password": "a-long-password-1"}).status_code != 200:
        c.post("/api/setup", json={"email": "inv@example.com", "password": "a-long-password-1"})
    h = c.get("/api/home").json()
    assert h["status"] and "what_it_does" in h and h["mandate"]["provisional"] is True
    assert any("mandate" in a["what"].lower() for a in h["attention"])
    assert c.post("/api/mandate", json={"password": "wrong-password-1", "capital_limit_gbp": 5000}).status_code == 403
    assert c.post("/api/mandate", json={"password": "a-long-password-1", "capital_limit_gbp": -1}).status_code == 400
    ok = c.post("/api/mandate", json={"password": "a-long-password-1", "capital_limit_gbp": 5000,
                                     "cash_reserve_gbp": 500, "wrapper": "isa", "tax_band": "basic"})
    assert ok.status_code == 200 and ok.json()["approved"] is True
    h = c.get("/api/home").json()
    assert h["money"]["capital_limit"] == 5000 and h["tax"]["sheltered"] is True
    assert c.post("/api/pause", json={"paused": True}).json()["paused"] is True
    r = c.post("/api/cycle", json={"force": True, "ignore_hours": True}).json()
    assert "paused_by_investor" in r["steps"]
    c.post("/api/pause", json={"paused": False})


def test_weekly_endpoint():
    from atlas import api
    c = TestClient(api.app)
    assert c.get("/api/weekly").status_code == 401
    if c.post("/api/login", json={"email": "inv@example.com", "password": "a-long-password-1"}).status_code != 200:
        c.post("/api/setup", json={"email": "inv@example.com", "password": "a-long-password-1"})
    r = c.get("/api/weekly", params={"goal": 0.30, "start": 100}).json()
    assert "weeks" in r["weekly"] and r["goal"]["verdict"].startswith("Not achievable")
    assert c.get("/api/weekly", params={"goal": 999}).json()["goal"]["goal_weekly"] == 10.0  # clamped
