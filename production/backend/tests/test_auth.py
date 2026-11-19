from backend import config
from backend.api.auth import issue_jwt
from backend.tests.conftest import auth_headers
from backend.tests.factories import make_user


def _google(monkeypatch, info):
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["boss@openhouse.in"])
    monkeypatch.setattr("backend.api.auth.id_token.verify_oauth2_token", lambda *a, **k: info)


def _login(client, monkeypatch, email, **extra):
    _google(monkeypatch, {"email": email, "email_verified": True, "name": "N", **extra})
    return client.post("/api/auth/google", json={"id_token": "x"})


def test_google_login_creates_viewer_and_admin(client, conn, monkeypatch):
    r = _login(client, monkeypatch, "a@openhouse.in")
    assert r.status_code == 200
    u = r.get_json()["user"]
    assert (u["role"], u["vertical"], u["active"]) == ("VIEWER", "BOTH", True)
    assert set(u) == {"id", "email", "name", "image", "role", "vertical", "use_case_ids", "title", "active"}
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + r.get_json()["token"]}).status_code == 200
    r = _login(client, monkeypatch, "boss@openhouse.in")
    assert (r.get_json()["user"]["role"], r.get_json()["user"]["vertical"]) == ("ADMIN", "BOTH")
    assert _login(client, monkeypatch, "x@gmail.com").status_code == 403
    assert _login(client, monkeypatch, "a@openhouse.in").get_json()["user"]["role"] == "VIEWER"


def test_google_login_rejects_unverified_and_inactive(client, conn, monkeypatch):
    assert _login(client, monkeypatch, "a@openhouse.in", email_verified=False).status_code == 401
    make_user(conn, email="off@openhouse.in", active=False)
    conn.commit()
    assert _login(client, monkeypatch, "off@openhouse.in").status_code == 403


def test_dev_login_only_without_google_client(client, conn, monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", "1")
    u = make_user(conn, email="dev@example.com")
    make_user(conn, email="off@example.com", active=False)
    conn.commit()
    r = client.post("/api/auth/dev", json={"email": "dev@example.com"})
    assert r.status_code == 200 and r.get_json()["user"]["id"] == u["id"]
    assert client.post("/api/auth/dev", json={"email": "off@example.com"}).status_code == 404
    assert client.post("/api/auth/dev", json={"email": "nobody@example.com"}).status_code == 404
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "cid")
    assert client.post("/api/auth/dev", json={"email": "dev@example.com"}).status_code == 404


def test_deactivated_user_gets_401_with_valid_token(client, conn):
    u = make_user(conn)
    conn.commit()
    h = auth_headers(u)
    assert client.get("/api/auth/me", headers=h).status_code == 200
    conn.cursor().execute('update "user" set active=false where id=%s', (u["id"],))
    conn.commit()
    assert client.get("/api/auth/me", headers=h).status_code == 401


def test_role_change_applies_without_new_token(client, conn):
    u = make_user(conn, role="VIEWER")
    conn.commit()
    h = auth_headers(u)
    assert client.get("/api/auth/me", headers=h).get_json()["user"]["role"] == "VIEWER"
    conn.cursor().execute('update "user" set role=\'MANAGER\' where id=%s', (u["id"],))
    conn.commit()
    assert client.get("/api/auth/me", headers=h).get_json()["user"]["role"] == "MANAGER"


def test_missing_bad_and_unknown_tokens(client, conn):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    ghost = {"id": "nope", "email": "g@example.com"}
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + issue_jwt(ghost)}).status_code == 401


def test_dev_login_404_without_dev_login_flag(client, conn, monkeypatch):
    make_user(conn, email="dev@example.com")
    conn.commit()
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(config, "DEV_LOGIN", "")
    assert client.post("/api/auth/dev", json={"email": "dev@example.com"}).status_code == 404


def test_token_without_exp_or_sub_401(client, conn):
    import jwt
    u = make_user(conn)
    conn.commit()
    for payload in ({"sub": u["id"], "email": u["email"]}, {"email": u["email"], "exp": 4102444800}):
        token = jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401 and r.get_json() == {"error": "invalid token"}, payload
