from datetime import datetime, timedelta, timezone

from ..tests.conftest import auth_headers
from ..services.taxonomy import DEFAULT_TAXONOMY
from .factories import _insert, make_agent, make_call, make_campaign, make_use_case, make_user


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def test_bad_window_rejected(client, conn):
    head = make_user(conn, role="HEAD", vertical="SUPPLY")
    conn.commit()
    body = {"name": "Supply", "window_start": "19:00", "window_end": "10:00", "line_share": 5, "cooling_days": 7}
    assert client.put("/api/departments/supply", json=body, headers=auth_headers(head)).status_code == 400
    body["window_start"], body["window_end"] = "9am", "19:00"
    assert client.put("/api/departments/supply", json=body, headers=auth_headers(head)).status_code == 400
    body.update(window_start="09:00", line_share=11)
    assert client.put("/api/departments/supply", json=body, headers=auth_headers(head)).status_code == 400
    body.update(line_share=3, cooling_days=91)
    assert client.put("/api/departments/supply", json=body, headers=auth_headers(head)).status_code == 400
    body.update(cooling_days=14)
    r = client.put("/api/departments/supply", json=body, headers=auth_headers(head))
    assert r.status_code == 200
    assert (r.get_json()["window_start"], r.get_json()["line_share"], r.get_json()["cooling_days"]) == ("09:00", 3, 14)


def test_head_of_other_dept_forbidden(client, conn):
    head = make_user(conn, role="HEAD", vertical="DEMAND")
    conn.commit()
    body = {"name": "Supply", "window_start": "10:00", "window_end": "19:00", "line_share": 5, "cooling_days": 7}
    assert client.put("/api/departments/supply", json=body, headers=auth_headers(head)).status_code == 403
    assert client.get("/api/departments/supply", headers=auth_headers(head)).status_code == 403
    assert client.get("/api/departments/nope", headers=auth_headers(head)).status_code == 404


def test_dashboard_counts(client, conn):
    user = make_user(conn, role="MANAGER", vertical="SUPPLY")
    a_s, a_d = make_agent(conn, vertical="SUPPLY"), make_agent(conn, vertical="DEMAND")
    uc = make_use_case(conn)
    cs = make_campaign(conn, agent=a_s, vertical="SUPPLY", use_case_id=uc["id"])
    cd = make_campaign(conn, agent=a_d, vertical="DEMAND")
    make_call(conn, phone="+919000000001", outcome="CONNECTED", started_at=_ago(hours=2), campaign=cs, tag="qualified")
    make_call(conn, phone="+919000000002", outcome="CONNECTED", started_at=_ago(hours=3), campaign=cs, tag="booked")
    make_call(conn, phone="+919000000003", outcome="NOT_PICKED", started_at=_ago(hours=4), campaign=cs)
    make_call(conn, phone="+919000000004", outcome="CONNECTED", started_at=_ago(hours=2), campaign=cd)
    make_call(conn, phone="+919000000005", outcome="CONNECTED", started_at=_ago(days=3), campaign=cs)
    conn.commit()
    r = client.get("/api/departments/supply/dashboard?days=1", headers=auth_headers(user))
    assert r.status_code == 200
    j = r.get_json()
    assert j["days"] == 1
    assert (j["stats"]["dialed"], j["stats"]["connected"], j["stats"]["not_picked"], j["stats"]["unique_phones"]) == (3, 2, 1, 3)
    assert {"blocked", "voicemail", "failed", "minutes", "cost"} <= set(j["stats"])
    assert j["last_sync"] is None
    f = j["funnel"][0]
    assert (f["use_case_id"], f["dialed"], f["connected"], f["good"]) == (uc["id"], 3, 2, 2)
    assert {"n": 2} .items() <= next(t for t in j["outcomes"] if t["key"] == "CONNECTED").items()
    assert j["agents"] == [{"id": a_s["id"], "name": a_s["name"], "dialed": 3, "connected": 2}]
    assert {t["tag"]: t["n"] for t in j["tags"]}["qualified"] == 1
    assert client.get("/api/departments/supply/dashboard?days=7", headers=auth_headers(user)).get_json()["stats"]["dialed"] == 4
    assert client.get("/api/departments/supply/dashboard?days=5", headers=auth_headers(user)).status_code == 400


def test_dashboard_use_case_restriction(client, conn):
    uc1 = make_use_case(conn, key="one")
    uc2 = make_use_case(conn, key="two")
    user = make_user(conn, role="MANAGER", vertical="SUPPLY", use_case_ids=[uc1["id"]])
    agent = make_agent(conn)
    c1 = make_campaign(conn, agent=agent, use_case_id=uc1["id"])
    c2 = make_campaign(conn, agent=agent, use_case_id=uc2["id"])
    make_call(conn, phone="+919000000001", started_at=_ago(hours=1), campaign=c1)
    make_call(conn, phone="+919000000002", started_at=_ago(hours=1), campaign=c2)
    make_call(conn, phone="+919000000003", started_at=_ago(hours=1), campaign=c2)
    _insert(conn, "handoff", department="SUPPLY", use_case_id=uc2["id"], phone="+919000000002")
    conn.commit()
    h = auth_headers(user)
    j = client.get("/api/departments/supply/dashboard", headers=h).get_json()
    assert j["stats"]["dialed"] == 1
    assert [f["use_case_id"] for f in j["funnel"]] == [uc1["id"]]
    assert j["open_handoffs"] == []
    assert j["agents"][0]["dialed"] == 1
    assert client.get(f"/api/departments/supply/dashboard?use_case_id={uc2['id']}", headers=h).status_code == 403
    assert [i["id"] for i in client.get("/api/departments/supply/use-cases", headers=h).get_json()["items"]] == [uc1["id"]]
    admin = make_user(conn, role="ADMIN", vertical="BOTH")
    conn.commit()
    j = client.get(f"/api/departments/supply/dashboard?use_case_id={uc2['id']}", headers=auth_headers(admin)).get_json()
    assert j["stats"]["dialed"] == 2 and len(j["open_handoffs"]) == 1
    assert j["open_handoffs"][0]["use_case_name"] == uc2["name"]
    other = make_use_case(conn, department="DEMAND", key="d")
    conn.commit()
    assert client.get(f"/api/departments/supply/dashboard?use_case_id={other['id']}", headers=auth_headers(admin)).status_code == 404


def test_use_case_key_slugged_and_scoped(client, conn):
    head = make_user(conn, role="HEAD", vertical="SUPPLY")
    mgr = make_user(conn, role="MANAGER", vertical="SUPPLY")
    agent_demand = make_agent(conn, vertical="DEMAND")
    agent_both = make_agent(conn, vertical="BOTH")
    other = make_use_case(conn, department="DEMAND", key="buyer")
    conn.commit()
    h = auth_headers(head)
    body = {"key": "Seller Qualification!", "name": "Seller Qualification", "priority": 3, "color": "#112233",
            "tag_taxonomy": DEFAULT_TAXONOMY}
    r = client.post("/api/departments/supply/use-cases", json={k: v for k, v in body.items() if k != "tag_taxonomy"}, headers=h)
    assert r.status_code == 400 and r.get_json()["error"] == "tag_taxonomy required"
    r = client.post("/api/departments/supply/use-cases", json={"key": "min", "name": "Min", "tag_taxonomy": DEFAULT_TAXONOMY}, headers=h)
    assert r.status_code == 201 and (r.get_json()["priority"], r.get_json()["active"]) == (5, True)
    r = client.post("/api/departments/supply/use-cases", json=body, headers=h)
    assert r.status_code == 201
    uc = r.get_json()
    assert uc["key"] == "seller_qualification" and uc["department"] == "SUPPLY" and uc["priority"] == 3
    assert uc["color"] == "#112233"
    assert client.post("/api/departments/supply/use-cases", json=body, headers=h).status_code == 409
    assert client.post("/api/departments/supply/use-cases", json={**body, "key": "!!!"}, headers=h).status_code == 400
    assert client.post("/api/departments/supply/use-cases", json=body, headers=auth_headers(mgr)).status_code == 403
    r = client.post("/api/departments/supply/use-cases", json={**body, "key": "x", "default_agent_id": agent_demand["id"]}, headers=h)
    assert r.status_code == 400
    r = client.post("/api/departments/supply/use-cases", json={**body, "key": "y", "default_agent_id": agent_both["id"]}, headers=h)
    assert r.status_code == 201
    r = client.put(f"/api/use-cases/{uc['id']}", json={**body, "name": "Renamed", "active": False}, headers=h)
    assert r.status_code == 200 and r.get_json()["name"] == "Renamed" and r.get_json()["active"] is False
    r = client.put(f"/api/use-cases/{uc['id']}", json={"default_agent_id": agent_demand["id"]}, headers=h)
    assert r.status_code == 400
    assert client.put(f"/api/use-cases/{other['id']}", json={"name": "z"}, headers=h).status_code == 403
    assert client.put("/api/use-cases/nope", json={"name": "z"}, headers=h).status_code == 404
