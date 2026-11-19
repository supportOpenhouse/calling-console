from ..tests.conftest import auth_headers
from .factories import _insert, make_agent, make_use_case, make_user


def _one(conn, sql, *p):
    with conn.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchone()


def _hdr(conn, **kw):
    u = make_user(conn, **kw)
    conn.commit()
    return u, auth_headers(u)


def test_head_cannot_demote_owner_via_api(client, conn):
    owner = make_user(conn, role="ADMIN", vertical="BOTH", email="boss@openhouse.in")
    head, h = _hdr(conn, role="HEAD", vertical="SUPPLY")
    body = {"role": "VIEWER", "vertical": "SUPPLY", "use_case_ids": [], "title": None, "active": True}
    assert client.post("/api/users", headers=h, json={**body, "email": "boss@openhouse.in"}).status_code == 403
    assert client.put(f"/api/users/{owner['id']}", headers=h, json=body).status_code == 403
    assert _one(conn, 'SELECT role FROM "user" WHERE id=%s', owner["id"])["role"] == "ADMIN"


def test_head_claims_new_viewer(client, conn):
    viewer = make_user(conn, role="VIEWER", vertical="BOTH", email="new@openhouse.in")
    uc = make_use_case(conn, department="SUPPLY")
    head, h = _hdr(conn, role="HEAD", vertical="SUPPLY")
    r = client.put(f"/api/users/{viewer['id']}", headers=h, json={
        "role": "MANAGER", "vertical": "SUPPLY", "use_case_ids": [uc["id"]], "title": "Lead", "active": True})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["role"] == "MANAGER" and r.get_json()["vertical"] == "SUPPLY"
    assert _one(conn, 'SELECT count(*) n FROM audit_log WHERE action = %s', "user.update")["n"] == 1


def test_head_precreates_by_email(client, conn):
    head, h = _hdr(conn, role="HEAD", vertical="SUPPLY")
    body = {"role": "VIEWER", "vertical": "SUPPLY", "use_case_ids": [], "title": None, "active": True}
    assert client.post("/api/users", headers=h, json={**body, "email": " Ann@Openhouse.in "}).status_code == 200
    assert _one(conn, 'SELECT role FROM "user" WHERE email=%s', "ann@openhouse.in")["role"] == "VIEWER"
    assert client.post("/api/users", headers=h, json={**body, "email": "a@gmail.com"}).status_code == 400
    assert client.post("/api/users", headers=h, json={**body, "email": "b@openhouse.in", "role": "X"}).status_code == 400
    other = make_use_case(conn, department="DEMAND", key="d")
    conn.commit()
    assert client.post("/api/users", headers=h, json={**body, "email": "c@openhouse.in",
                                                      "use_case_ids": [other["id"]]}).status_code == 400


def test_user_lists(client, conn):
    make_user(conn, role="VIEWER", vertical="DEMAND")
    make_user(conn, role="VIEWER", vertical="BOTH")
    owner, ho = _hdr(conn, role="ADMIN", vertical="BOTH")
    mgr, hm = _hdr(conn, role="MANAGER", vertical="SUPPLY")
    assert len(client.get("/api/users", headers=ho).get_json()["items"]) == 4
    assert client.get("/api/users", headers=hm).status_code == 403
    assert client.get("/api/departments/supply/team", headers=hm).status_code == 403
    head, hh = _hdr(conn, role="HEAD", vertical="SUPPLY")
    items = client.get("/api/departments/supply/team", headers=hh).get_json()["items"]
    assert sorted(i["vertical"] for i in items) == ["BOTH", "BOTH", "SUPPLY", "SUPPLY"]


def test_manager_cannot_edit_agents_or_numbers(client, conn):
    a = make_agent(conn)
    n = _insert(conn, "phone_number", number="+918000000001")
    m, h = _hdr(conn, role="MANAGER", vertical="SUPPLY")
    assert client.post("/api/agents/refresh", headers=h).status_code == 403
    assert client.put(f"/api/agents/{a['id']}", headers=h, json={"vertical": "SUPPLY"}).status_code == 403
    assert client.put(f"/api/agents/{a['id']}/prompt", headers=h, json={"system_prompt": "x"}).status_code == 403
    assert client.post("/api/numbers/refresh", headers=h).status_code == 403
    assert client.put(f"/api/numbers/{n['id']}", headers=h, json={"label": "x", "active": False}).status_code == 403
    assert len(client.get("/api/numbers", headers=h).get_json()["items"]) == 1


def test_agents_filtered_by_department(client, conn):
    s, d, b = make_agent(conn, vertical="SUPPLY"), make_agent(conn, vertical="DEMAND"), make_agent(conn, vertical="BOTH")
    m, h = _hdr(conn, role="MANAGER", vertical="SUPPLY")
    ids = {i["id"] for i in client.get("/api/agents", headers=h).get_json()["items"]}
    assert ids == {s["id"], b["id"]}
    assert client.get(f"/api/agents/{d['id']}", headers=h).status_code == 403
    assert "config" in client.get(f"/api/agents/{s['id']}", headers=h).get_json()


def test_owner_edits_agent_meta_and_prompt(client, conn, bolna_stub):
    a = make_agent(conn, bolna_agent_id="bx")
    bolna_stub.agents = [{"agent_id": "bx", "agent_name": "A", "agent_prompts": {"task_1": {"system_prompt": "old"}},
                          "tasks": [{}]}]
    o, h = _hdr(conn, role="ADMIN", vertical="BOTH")
    r = client.put(f"/api/agents/{a['id']}", headers=h, json={
        "vertical": "DEMAND", "description": "d", "caller_name": "Riya", "default_from_number": "98100 00001"})
    assert r.status_code == 200 and r.get_json()["vertical"] == "DEMAND"
    assert r.get_json()["default_from_number"] == "+919810000001"
    assert client.put(f"/api/agents/{a['id']}", headers=h, json={"vertical": "NOPE"}).status_code == 400
    r = client.put(f"/api/agents/{a['id']}/prompt", headers=h, json={"system_prompt": "new", "welcome_message": "hi"})
    assert r.status_code == 200, r.get_json()
    names = [c[0] for c in bolna_stub.calls]
    assert names.index("get_agent") < names.index("update_agent") < names.index("list_agents")
    upd = next(c for c in bolna_stub.calls if c[0] == "update_agent")
    assert upd[1][2] == "new" and upd[1][1]["agent_welcome_message"] == "hi"
    meta = _one(conn, "SELECT meta FROM audit_log WHERE action=%s", "agent.prompt")["meta"]
    assert meta["before"] == "old"
    bolna_stub.fail_on = {"update_agent"}
    assert client.put(f"/api/agents/{a['id']}/prompt", headers=h, json={"system_prompt": "z"}).status_code == 502


def test_numbers_refresh_and_update(client, conn, monkeypatch):
    from backend.services import bolna
    monkeypatch.setattr(bolna, "list_phone_numbers", lambda: [
        {"id": "p1", "phone_number": "+918000000009", "telephony_provider": "plivo"}, {"id": "p2"}])
    o, h = _hdr(conn, role="ADMIN", vertical="BOTH")
    assert client.post("/api/numbers/refresh", headers=h).get_json() == {"count": 1}
    n = client.get("/api/numbers", headers=h).get_json()["items"][0]
    assert n["provider"] == "plivo"
    r = client.put(f"/api/numbers/{n['id']}", headers=h, json={"label": "Main", "active": False})
    assert r.get_json()["label"] == "Main" and r.get_json()["active"] is False


def test_test_call_dnc_409(client, conn, bolna_stub):
    a = make_agent(conn, vertical="SUPPLY")
    both = make_agent(conn, vertical="BOTH")
    _insert(conn, "dnc_phone", phone="+919810000001", source="manual")
    m, h = _hdr(conn, role="MANAGER", vertical="SUPPLY")
    body = {"to": "9810000001", "from_number": "+918000000001", "variables": {}}
    assert client.post(f"/api/agents/{a['id']}/test-call", headers=h, json=body).status_code == 409
    assert not [c for c in bolna_stub.calls if c[0] == "place_call"]
    ok = client.post(f"/api/agents/{a['id']}/test-call", headers=h, json={**body, "to": "9810000002"})
    assert ok.status_code == 200 and ok.get_json()["status"] == "queued"
    assert client.post(f"/api/agents/{a['id']}/test-call", headers=h, json={**body, "to": "12"}).status_code == 400
    assert client.post(f"/api/agents/{both['id']}/test-call", headers=h, json=body).status_code == 403
    v, hv = _hdr(conn, role="VIEWER", vertical="SUPPLY")
    assert client.post(f"/api/agents/{a['id']}/test-call", headers=hv, json=body).status_code == 403


def test_integrations_reports_unconfigured(client, conn, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "R2_BLOB_PHONE_CALLS_BUCKET", "")
    monkeypatch.setattr(config, "INGEST_TOKEN", "")
    from backend.services.settings_store import set_setting
    set_setting(conn, "diPushFailures", 3)
    o, h = _hdr(conn, role="ADMIN", vertical="BOTH")
    r = client.get("/api/settings/integrations", headers=h).get_json()
    assert r["r2"] == {"configured": False} and r["ingest"]["configured"] is False
    assert r["ingest"]["path"] == "/api/ingest/datasets/<dataset_id>/leads"
    assert r["bolna"] == {"configured": True} and r["di"]["failures"] == 3
    assert set(r["sync"]) == {"enabled", "last_report", "last_full_sync_date"}
    m, hm = _hdr(conn, role="MANAGER", vertical="SUPPLY")
    assert client.get("/api/settings/integrations", headers=hm).status_code == 403


def test_audit_list(client, conn):
    o, h = _hdr(conn, role="ADMIN", vertical="BOTH")
    from backend.services.audit import audit
    audit(conn, o["id"], "x.y")
    conn.commit()
    r = client.get("/api/settings/audit", headers=h).get_json()
    assert r["total"] == 1 and r["items"][0]["user_email"] == o["email"]


def test_head_cannot_overwrite_an_owner_by_email(client, conn):
    _hdr(conn, role="ADMIN", vertical="BOTH", email="boss@openhouse.in")
    head, h = _hdr(conn, role="HEAD", vertical="SUPPLY")
    body = {"email": "boss@openhouse.in", "role": "VIEWER", "vertical": "SUPPLY", "use_case_ids": [],
            "title": None, "active": True}
    assert client.post("/api/users", headers=h, json=body).status_code == 403
    assert _one(conn, 'SELECT role FROM "user" WHERE email=%s', "boss@openhouse.in")["role"] == "ADMIN"


def test_owner_cannot_deactivate_self(client, conn):
    o, h = _hdr(conn, role="ADMIN", vertical="BOTH", email="me@openhouse.in")
    body = {"role": "ADMIN", "vertical": "BOTH", "use_case_ids": [], "title": None, "active": False}
    assert client.put(f"/api/users/{o['id']}", headers=h, json=body).status_code == 403
    assert client.post("/api/users", headers=h, json={**body, "email": o["email"]}).status_code == 403
    assert _one(conn, 'SELECT active FROM "user" WHERE id=%s', o["id"])["active"] is True


def test_number_put_keeps_absent_keys(client, conn):
    n = _insert(conn, "phone_number", number="+918000000001", label="Main")
    o, h = _hdr(conn, role="ADMIN", vertical="BOTH")
    r = client.put(f"/api/numbers/{n['id']}", headers=h, json={"active": False})
    assert r.status_code == 200 and r.get_json()["label"] == "Main" and r.get_json()["active"] is False
    assert client.put(f"/api/numbers/{n['id']}", headers=h, json={}).status_code == 400
