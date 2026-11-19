import psycopg2
import psycopg2.extras

from .. import config
from ..services import sync as sync_svc
from ..tests.conftest import auth_headers
from ..services.redial import DEFAULT_REDIAL_POLICY
from .factories import _insert, make_agent, make_dataset, make_use_case, make_user

ROW = {"phone": "+919810043058", "name": "Asha", "city": "Gurgaon"}


def _hdr(conn, **kw):
    u = make_user(conn, **{"role": "MANAGER", "vertical": "SUPPLY", **kw})
    conn.commit()
    return u, auth_headers(u)


def _body(uc, agent, ds, **over):
    b = {"use_case_id": uc["id"], "name": "Follow up", "agent_id": agent["id"], "dataset_id": ds["id"] if ds else None,
         "from_number": "+918000000001", "trigger": {"type": "MANUAL"}, "rules": [],
         "redial_policy": dict(DEFAULT_REDIAL_POLICY)}
    b.update(over)
    return b


def _setup(conn):
    uc, agent, ds = make_use_case(conn), make_agent(conn), None
    ds = make_dataset(conn, rows=[ROW])
    conn.commit()
    return uc, agent, ds


def _create(client, h, uc, agent, ds, **over):
    return client.post("/api/departments/supply/workflows", headers=h, json=_body(uc, agent, ds, **over))


def test_workflow_validation(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    other = make_dataset(conn, department="DEMAND")
    conn.commit()
    assert _create(client, h, uc, agent, other).status_code == 400
    bad_rule = [{"when": {"outcomes": ["CONNECTED"]}, "actions": [{"type": "crm_push"}]}]
    assert _create(client, h, uc, agent, ds, rules=bad_rule).status_code == 400
    assert _create(client, h, uc, agent, None, trigger={"type": "AFTER_STEP", "delay_hours": 2}).status_code == 400
    assert _create(client, h, uc, agent, ds, trigger={"type": "SCHEDULE"}).status_code == 400
    assert _create(client, h, uc, agent, ds, trigger={"type": "SCHEDULE", "time": "25:00"}).status_code == 400
    assert _create(client, h, uc, agent, ds, trigger={"type": "NOPE"}).status_code == 400
    assert _create(client, h, uc, agent, None).status_code == 400  # dataset null only for AFTER_STEP
    assert _create(client, h, uc, agent, ds, rules=[{"when": {}, "actions": [{"type": "redial"}]}]).status_code == 400
    assert _create(client, h, uc, agent, ds, rules=[{"when": {}, "actions": [{"type": "whatsapp"}]}]).status_code == 400
    assert _create(client, h, uc, agent, ds, redial_policy={"max_attempts": 99}).status_code == 400
    demand_agent = make_agent(conn, vertical="DEMAND")
    conn.commit()
    assert _create(client, h, uc, demand_agent, ds).status_code == 400
    nobody = _body(uc, agent, ds)
    del nobody["redial_policy"]
    r = client.post("/api/departments/supply/workflows", headers=h, json=nobody)
    assert r.status_code == 400 and r.get_json() == {"error": "redial_policy is required"}
    ok_rule = [{"when": {"outcomes": ["CONNECTED"]}, "actions": [{"type": "redial", "after_minutes": 30}]}]
    r = _create(client, h, uc, agent, ds, rules=ok_rule, trigger={"type": "SCHEDULE", "every_minutes": 60})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["department"] == "SUPPLY"


def test_after_step_and_new_lead_rules(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    parent = _create(client, h, uc, agent, ds).get_json()
    step = {"type": "AFTER_STEP", "delay_hours": 4, "when_tags": ["qualified"]}
    r = _create(client, h, uc, agent, None, trigger=step, parent_id=parent["id"])
    assert r.status_code == 201, r.get_json()
    for match in ({}, {"when_tags": [], "when_outcomes": []}):  # would follow up every call of the parent
        r = _create(client, h, uc, agent, None, trigger={"type": "AFTER_STEP", "delay_hours": 4, **match},
                    parent_id=parent["id"])
        assert r.status_code == 400
        assert r.get_json() == {"error": "an AFTER_STEP trigger needs when_tags or when_outcomes"}
    r = _create(client, h, uc, agent, None, trigger={"type": "AFTER_STEP", "delay_hours": 4,
                                                    "when_outcomes": ["NOT_PICKED"]}, parent_id=parent["id"])
    assert r.status_code == 201
    assert _create(client, h, uc, agent, None, trigger={"type": "AFTER_STEP", "delay_hours": 0},
                   parent_id=parent["id"]).status_code == 400
    foreign = _insert(conn, "workflow", department="DEMAND", use_case_id=make_use_case(conn, department="DEMAND")["id"],
                      name="x", agent_id=make_agent(conn, vertical="DEMAND")["id"], from_number="+1",
                      trigger=psycopg2.extras.Json({"type": "MANUAL"}), redial_policy=psycopg2.extras.Json({}))
    conn.commit()
    assert _create(client, h, uc, agent, None, trigger=step, parent_id=foreign["id"]).status_code == 400
    nl = {"type": "ON_NEW_LEAD"}
    first = _create(client, h, uc, agent, ds, trigger=nl)
    assert first.status_code == 201
    assert _create(client, h, uc, agent, ds, trigger=nl).status_code == 400
    inactive = _create(client, h, uc, agent, ds, trigger=nl, active=False)
    assert inactive.status_code == 201
    wid = inactive.get_json()["id"]
    assert client.post(f"/api/workflows/{wid}/toggle", headers=h, json={"active": True}).status_code == 400
    assert client.post(f"/api/workflows/{first.get_json()['id']}/toggle", headers=h,
                       json={"active": False}).get_json()["active"] is False
    assert client.post(f"/api/workflows/{wid}/toggle", headers=h, json={"active": True}).status_code == 200


def test_workflow_cross_dept_403(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    wid = _create(client, h, uc, agent, ds).get_json()["id"]
    _, dh = _hdr(conn, vertical="DEMAND")
    assert client.get(f"/api/workflows/{wid}", headers=dh).status_code == 403
    assert client.put(f"/api/workflows/{wid}", headers=dh, json={"name": "z"}).status_code == 403
    assert client.post(f"/api/workflows/{wid}/run", headers=dh, json={}).status_code == 403
    assert client.get("/api/departments/supply/workflows", headers=dh).status_code == 403
    assert client.post("/api/departments/supply/workflows", headers=dh, json=_body(uc, agent, ds)).status_code == 403
    # a body id from another department is rejected even with manage rights
    both = make_user(conn, role="HEAD", vertical="BOTH")
    d_uc = make_use_case(conn, department="DEMAND")
    conn.commit()
    bh = auth_headers(both)
    assert client.post("/api/departments/supply/workflows", headers=bh,
                       json=_body(d_uc, agent, ds)).status_code == 400
    assert client.put(f"/api/workflows/{wid}", headers=bh, json={"use_case_id": d_uc["id"]}).status_code == 400


def test_crud_detail_toggle_delete(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    wid = _create(client, h, uc, agent, ds).get_json()["id"]
    r = client.put(f"/api/workflows/{wid}", headers=h, json={"name": "Renamed", "priority": 2})
    assert r.status_code == 200 and r.get_json()["name"] == "Renamed" and r.get_json()["priority"] == 2
    items = client.get("/api/departments/supply/workflows", headers=h).get_json()["items"]
    assert items[0]["use_case_name"] and items[0]["agent_name"] and items[0]["dataset_name"]
    assert items[0]["runs"] == 0 and items[0]["live"] == 0
    d = client.get(f"/api/workflows/{wid}", headers=h).get_json()
    assert d["workflow"]["id"] == wid and d["use_case"]["id"] == uc["id"] and d["agent"]["id"] == agent["id"]
    assert d["dataset"]["id"] == ds["id"] and d["children"] == [] and d["runs"] == []
    assert client.post(f"/api/workflows/{wid}/toggle", headers=h, json={"active": False}).get_json()["active"] is False
    assert client.post(f"/api/workflows/{wid}/toggle", headers=h, json={}).status_code == 400
    assert client.delete(f"/api/workflows/{wid}", headers=h).status_code == 403  # manager is not admin
    _, head = _hdr(conn, role="HEAD")
    assert client.delete(f"/api/workflows/{wid}", headers=head).status_code == 204
    assert client.get(f"/api/workflows/{wid}", headers=head).status_code == 404


def test_delete_workflow_with_live_campaigns_or_steps_409(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    _, head = _hdr(conn, role="HEAD")
    wid = _create(client, h, uc, agent, ds).get_json()["id"]
    camp = _insert(conn, "campaign", name="run", agent_id=agent["id"], from_number="+918000000001",
                   redial_policy=psycopg2.extras.Json({}), workflow_id=wid, status="DRAFT")
    conn.commit()
    for status in ("DRAFT", "SCHEDULED", "RUNNING", "PAUSED"):
        with conn.cursor() as cur:
            cur.execute("UPDATE campaign SET status = %s WHERE id = %s", (status, camp["id"]))
        conn.commit()
        r = client.delete(f"/api/workflows/{wid}", headers=head)
        assert r.status_code == 409 and r.get_json() == {"error": "the workflow has live campaigns"}, status
    with conn.cursor() as cur:  # a finished run keeps the workflow id as history
        cur.execute("UPDATE campaign SET status = 'COMPLETED' WHERE id = %s", (camp["id"],))
    conn.commit()
    step = _create(client, h, uc, agent, None, trigger={"type": "AFTER_STEP", "delay_hours": 4, "when_tags": ["x"]},
                   parent_id=wid).get_json()
    r = client.delete(f"/api/workflows/{wid}", headers=head)
    assert r.status_code == 409 and r.get_json() == {"error": "the workflow has follow-up steps"}
    assert client.delete(f"/api/workflows/{step['id']}", headers=head).status_code == 204
    assert client.delete(f"/api/workflows/{wid}", headers=head).status_code == 204


def test_builder_data(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    make_agent(conn, vertical="DEMAND")
    _insert(conn, "phone_number", number="+918000000001")
    _insert(conn, "phone_number", number="+918000000002", active=False)
    parent = _create(client, h, uc, agent, ds).get_json()
    conn.commit()
    d = client.get("/api/departments/supply/builder-data", headers=h).get_json()
    assert [a["id"] for a in d["agents"]] == [agent["id"]]
    assert [n["number"] for n in d["numbers"]] == ["+918000000001"]
    assert d["use_cases"][0]["id"] == uc["id"] and d["datasets"][0]["id"] == ds["id"]
    assert d["facets"][ds["id"]]["cities"] and [p["id"] for p in d["parents"]] == [parent["id"]]
    assert d["default_policy"]["max_attempts"] == 3
    _, v = _hdr(conn, role="VIEWER")
    assert client.get("/api/departments/supply/builder-data", headers=v).status_code == 403


def test_builder_data_and_detail_never_carry_the_source_token(client, conn):
    u, h = _hdr(conn)
    uc, agent = make_use_case(conn), make_agent(conn)
    ds = _insert(conn, "dataset", department="SUPPLY", name="CRM", source="CRM",
                 source_config=psycopg2.extras.Json({"url": "http://x", "token": "secret"}))
    conn.commit()
    d = client.get("/api/departments/supply/builder-data", headers=h).get_json()
    assert d["datasets"][0]["source_config"] == {"url": "http://x"} and d["datasets"][0]["has_token"] is True
    wid = _create(client, h, uc, agent, ds).get_json()["id"]
    detail = client.get(f"/api/workflows/{wid}", headers=h).get_json()
    assert detail["dataset"]["source_config"] == {"url": "http://x"} and detail["dataset"]["has_token"] is True


def test_run_now_plans_on_calendar(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    wid = _create(client, h, uc, agent, ds).get_json()["id"]
    r = client.post(f"/api/workflows/{wid}/run", headers=h, json={})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["leads"] == 1 and body["campaign_id"] and body["planned_start"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM scheduled_run WHERE workflow_id = %s", (wid,))
        run = cur.fetchone()
    assert run["status"] == "PLANNED" and run["planned_start"] is not None
    # nobody left to call -> the empty result
    again = client.post(f"/api/workflows/{wid}/run", headers=h, json={}).get_json()
    assert again == {"campaign_id": None, "leads": 0}
    detail = client.get(f"/api/workflows/{wid}", headers=h).get_json()
    assert detail["runs"][0]["id"] == body["campaign_id"] and detail["runs"][0]["dialed"] == 0
    sched = client.get("/api/departments/supply/schedule?from=2020-01-01T00:00:00Z&to=2100-01-01T00:00:00Z",
                       headers=h).get_json()
    assert [x["id"] for x in sched["runs"]] == [run["id"]] and sched["runs"][0]["department"] == "SUPPLY"
    assert [x["id"] for x in sched["queue"]] == [run["id"]] and sched["window"]["start"]
    assert client.get("/api/departments/supply/schedule", headers=h).status_code == 400
    assert client.post(f"/api/workflows/{wid}/run", headers=h, json={"requested_start": "junk"}).status_code == 400


def test_cancel_replan_and_reprioritise(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    wid = _create(client, h, uc, agent, ds).get_json()["id"]
    client.post(f"/api/workflows/{wid}/run", headers=h, json={})
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM scheduled_run")
        rid = cur.fetchone()["id"]
    assert client.post(f"/api/runs/{rid}/priority", headers=h, json={"priority": 1}).status_code == 403  # manager
    _, head = _hdr(conn, role="HEAD")
    assert client.post(f"/api/runs/{rid}/priority", headers=head, json={"priority": "x"}).status_code == 400
    for bad in (0, 10, -1, 1.5, True, None):
        r = client.post(f"/api/runs/{rid}/priority", headers=head, json={"priority": bad})
        assert r.status_code == 400 and r.get_json() == {"error": "priority must be a whole number from 1 to 9"}, bad
    r = client.post(f"/api/runs/{rid}/priority", headers=head, json={"priority": 1})
    assert r.status_code == 200 and r.get_json()["items"][0]["priority"] == 1
    assert client.post(f"/api/runs/{rid}/priority", headers=head, json={"priority": 1}).status_code == 200
    assert client.post("/api/departments/supply/replan", headers=h).get_json()["items"]
    _, dem = _hdr(conn, vertical="DEMAND")
    assert client.post(f"/api/runs/{rid}/cancel", headers=dem).status_code == 403
    assert client.post(f"/api/runs/{rid}/cancel", headers=h).status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM scheduled_run WHERE id = %s", (rid,))
        assert cur.fetchone()["status"] == "CANCELLED"
    assert client.post("/api/runs/nope/cancel", headers=h).status_code == 404


def test_reprioritise_requires_head(client, conn):
    u, h = _hdr(conn)
    uc, agent, ds = _setup(conn)
    wid = _create(client, h, uc, agent, ds).get_json()["id"]
    client.post(f"/api/workflows/{wid}/run", headers=h, json={})
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM scheduled_run")
        rid = cur.fetchone()["id"]
    assert client.post(f"/api/runs/{rid}/priority", headers=h, json={"priority": 1}).status_code == 403


def test_sync_409_when_locked(client, conn):
    u, h = _hdr(conn)
    other = psycopg2.connect(config.DATABASE_URL)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (sync_svc.LOCK_KEY,))
        r = client.post("/api/sync", headers=h, json={})
        assert r.status_code == 409 and r.get_json() == {"error": "a sync is already running"}
    finally:
        other.close()


def test_sync_runs_and_status(client, conn, bolna_stub):
    u, h = _hdr(conn)
    assert client.get("/api/sync/status", headers=h).get_json() == {"last_report": None, "last_full_sync_date": None}
    r = client.post("/api/sync", headers=h, json={"full": True})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["full"] is True
    st = client.get("/api/sync/status", headers=h).get_json()
    assert st["last_report"]["full"] is True and st["last_full_sync_date"]
    _, v = _hdr(conn, role="VIEWER")
    assert client.post("/api/sync", headers=v, json={}).status_code == 403
    assert client.get("/api/sync/status", headers=v).status_code == 200
    assert client.get("/api/sync/status").status_code == 401
