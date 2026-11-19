import pytest


def test_health_reports_db_connected(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json() == {"status": "ok", "db": "connected"}


def test_api_error_is_json(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and "error" in r.get_json()


def test_create_app_requires_jwt_secret(monkeypatch):
    from backend import config, app as appmod
    monkeypatch.setattr(config, "JWT_SECRET", "")
    with pytest.raises(RuntimeError):
        appmod.create_app()


def test_cors_allows_frontend_origin(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
