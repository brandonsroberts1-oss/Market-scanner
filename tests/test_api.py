"""HTTP-level tests against the real FastAPI app via TestClient."""
import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import config
    monkeypatch.setattr(config.settings, "db_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(config.settings, "provider_name", "demo")
    db._initialised = False
    db.init_db(force=True)
    with TestClient(app) as c:
        yield c
    db._initialised = False


def test_status_reports_provider_and_data_quality(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "demo"
    assert body["data_note"]
    assert "core" in body["presets"]


def test_index_and_static_assets_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_quote_history_and_chain(client):
    q = client.get("/api/quote/SPY").json()
    assert q["symbol"] == "SPY" and q["last"] > 0

    h = client.get("/api/history/SPY?days=120").json()
    assert len(h["bars"]) > 60

    exps = client.get("/api/expirations/SPY").json()["expirations"]
    assert exps and all("dte" in e for e in exps)

    chain = client.get(f"/api/chain/SPY?expiration={exps[2]['expiration']}").json()
    assert chain["calls"] and chain["puts"]
    assert chain["underlying_price"] > 0


def test_unknown_symbol_returns_404(client):
    assert client.get("/api/chain/SPY?expiration=1999-01-01").status_code == 404


def test_scan_endpoint(client):
    r = client.get("/api/scan?preset=core&min_dte=0&max_dte=3&min_conviction=20&limit=15")
    assert r.status_code == 200
    body = r.json()
    assert body["narrative"]
    assert len(body["ideas"]) <= 15
    assert all(i["conviction"] >= 20 for i in body["ideas"])


def test_scan_rejects_an_inverted_dte_window(client):
    assert client.get("/api/scan?min_dte=10&max_dte=2").status_code == 400


def test_market_brief(client):
    body = client.get("/api/market/brief").json()
    assert body["indices"]
    assert isinstance(body["catalysts"], list)
    assert body["narrative"]


def test_equities_endpoint(client):
    body = client.get("/api/equities?preset=core&limit=10").json()
    assert isinstance(body["equities"], list)
    assert all(0 <= e["score"] <= 100 for e in body["equities"])


def test_payoff_endpoint_matches_expected_shape(client):
    r = client.post("/api/payoff", json={
        "underlying_price": 765,
        "legs": [
            {"action": "buy", "kind": "call", "strike": 765, "price": 4.06, "quantity": 1},
            {"action": "sell", "kind": "call", "strike": 775, "price": 1.20, "quantity": 1},
        ],
    })
    curve = r.json()["curve"]
    assert curve[0]["pnl"] == pytest.approx(-286.0, abs=1.0)
    assert curve[-1]["pnl"] == pytest.approx(714.0, abs=1.0)


# ---------------- paper trading over HTTP ----------------
def test_paper_session_lifecycle(client):
    created = client.post("/api/paper/sessions",
                          json={"name": "API test", "starting_cash": 60_000}).json()
    sid = created["id"]
    assert created["starting_cash"] == 60_000

    assert any(s["id"] == sid for s in client.get("/api/paper/sessions").json())

    order = client.post(f"/api/paper/sessions/{sid}/orders", json={
        "legs": [{"symbol": "AAPL", "side": "buy", "quantity": 10, "asset_type": "equity"}],
        "strategy": "equity_long",
    })
    assert order.status_code == 200
    assert order.json()["status"] == "filled"

    portfolio = client.get(f"/api/paper/sessions/{sid}?snapshot=true").json()
    assert len(portfolio["positions"]) == 1
    assert portfolio["total_equity"] == pytest.approx(
        portfolio["cash"] + portfolio["positions_value"], abs=0.01)

    assert len(client.get(f"/api/paper/sessions/{sid}/orders").json()) == 1
    assert len(client.get(f"/api/paper/sessions/{sid}/curve").json()) >= 1

    closed = client.post(f"/api/paper/sessions/{sid}/close-position",
                         json={"symbol": "AAPL"})
    assert closed.status_code == 200
    assert client.get(f"/api/paper/sessions/{sid}/performance").json()["trades"] == 1

    renamed = client.patch(f"/api/paper/sessions/{sid}", json={"name": "Renamed"}).json()
    assert renamed["name"] == "Renamed"

    assert client.post(f"/api/paper/sessions/{sid}/close").json()["status"] == "closed"
    assert client.delete(f"/api/paper/sessions/{sid}").status_code == 200
    assert client.get(f"/api/paper/sessions/{sid}").status_code == 404


def test_missing_session_is_404(client):
    assert client.get("/api/paper/sessions/98765").status_code == 404


def test_invalid_session_cash_is_rejected(client):
    assert client.post("/api/paper/sessions",
                       json={"name": "bad", "starting_cash": -100}).status_code == 422


def test_unaffordable_order_is_400_with_a_readable_reason(client):
    sid = client.post("/api/paper/sessions",
                      json={"name": "tiny", "starting_cash": 200}).json()["id"]
    r = client.post(f"/api/paper/sessions/{sid}/orders", json={
        "legs": [{"symbol": "AAPL", "side": "buy", "quantity": 500, "asset_type": "equity"}],
    })
    assert r.status_code == 400
    assert "Insufficient" in r.json()["detail"]


def test_order_with_no_legs_is_rejected(client):
    sid = client.post("/api/paper/sessions",
                      json={"name": "empty", "starting_cash": 10_000}).json()["id"]
    assert client.post(f"/api/paper/sessions/{sid}/orders",
                       json={"legs": []}).status_code == 422


def test_scanner_idea_can_be_traded_end_to_end(client):
    """The path a user actually takes: scan, then send an idea to paper."""
    ideas = client.get("/api/scan?preset=core&min_dte=1&max_dte=3&min_conviction=20&limit=30").json()["ideas"]
    spread = next(i for i in ideas if len(i["legs"]) == 2)
    sid = client.post("/api/paper/sessions",
                      json={"name": "from scan", "starting_cash": 50_000}).json()["id"]
    r = client.post(f"/api/paper/sessions/{sid}/orders", json={
        "legs": [{"symbol": l["symbol"], "side": l["action"], "quantity": 2,
                  "asset_type": "option"} for l in spread["legs"]],
        "strategy": spread["strategy"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["legs"]) == 2
    positions = client.get(f"/api/paper/sessions/{sid}").json()["positions"]
    assert len(positions) == 2
    assert {p["quantity"] for p in positions} == {2, -2}

    closed = client.post(f"/api/paper/sessions/{sid}/close-group",
                         json={"group_id": body["group_id"]})
    assert closed.status_code == 200
    assert client.get(f"/api/paper/sessions/{sid}").json()["positions"] == []


# ---------------- persistence ----------------
def test_saved_scans_round_trip(client):
    scan = client.get("/api/scan?preset=core&limit=5&save=true").json()
    saved_id = scan["saved_scan_id"]
    listing = client.get("/api/scan/saved").json()
    assert any(s["id"] == saved_id for s in listing)

    detail = client.get(f"/api/scan/saved/{saved_id}").json()
    assert detail["result"]["narrative"] == scan["narrative"]
    assert len(detail["result"]["ideas"]) == len(scan["ideas"])

    assert client.delete(f"/api/scan/saved/{saved_id}").status_code == 200
    assert client.get(f"/api/scan/saved/{saved_id}").status_code == 404


def test_backtest_endpoint_and_persistence(client):
    r = client.post("/api/backtest", json={
        "symbols": ["SPY"], "lookback_days": 200, "hold_days": 3,
        "min_conviction": 50, "label": "api test",
    })
    assert r.status_code == 200
    body = r.json()
    assert "Model-based" in body["method"]
    assert "stats" in body

    saved = client.get(f"/api/backtest/saved/{body['backtest_id']}").json()
    assert saved["label"] == "api test"
    assert client.delete(f"/api/backtest/saved/{body['backtest_id']}").status_code == 200


def test_backtest_rejects_a_bad_mode(client):
    r = client.post("/api/backtest", json={"symbols": ["SPY"], "mode": "nonsense"})
    assert r.status_code == 400


def test_backtest_requires_symbols(client):
    assert client.post("/api/backtest", json={"symbols": []}).status_code == 422
