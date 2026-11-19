import io
import json
from datetime import datetime, timedelta, timezone

from ..api import campaigns as campaigns_api
from ..services.ist import IST
from ..services.redial import DEFAULT_REDIAL_POLICY
from ..services.settings_store import set_setting
from ..services import sync
from ..services.sync import CONSOLE_STOP, STOP_SETTLE, close_stale_queued, cursor_key
from .conftest import auth_headers
from .factories import make_agent, make_batch, make_call, make_campaign, make_lead, make_use_case, make_user, _insert

FROZEN = datetime(2026, 11, 19, 11, 0, tzinfo=IST).astimezone(timezone.utc)  # Thursday, inside 10:00-19:00 IST


def _freeze(monkeypatch, at=FROZEN):
    """The routes read the clock through the module's `datetime`."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is None else at.astimezone(tz)

    monkeypatch.setattr(campaigns_api, "datetime", Frozen)


def _synced_after(conn, agent, at):
    """The agent's executions synced fully at `at` (what sync_executions stamps)."""
    set_setting(conn, cursor_key(agent["id"]), at.isoformat())
    conn.commit()


def _csv_phones(bolna_stub):
    return sorted(line.split(",")[0] for name, args in bolna_stub.calls if name == "create_batch"
                  for line in args[2].splitlines()[1:])

CSV = b"phone,owner_name,society_name\n9810043058,Asha,Palm\n9810043059,Ravi,Oak\n9810043058,Asha,Palm\nabc,X,Y\n"


def _hdr(conn, **kw):
    u = make_user(conn, **{"role": "MANAGER", "vertical": "SUPPLY", **kw})
    conn.commit()
    return u, auth_headers(u)


def _payload(agent, **over):
    p = {"name": "Spring push", "agent_id": agent["id"], "from_number": "+918000000001",
         "column_map": {"phone": "phone", "owner_name": "owner_name", "society_name": "society_name"},
         "redial_policy": dict(DEFAULT_REDIAL_POLICY), "start_mode": "draft"}
    p.update(over)
    return p


def _create(client, h, payload, data=CSV, dept="supply"):
    return client.post(f"/api/departments/{dept}/campaigns", headers=h,
                       data={"file": (io.BytesIO(data), "leads.csv"), "payload": json.dumps(payload)},
                       content_type="multipart/form-data")


def _rows(conn, sql, *p):
    with conn.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchall()


def test_preview(client, conn):
    u, h = _hdr(conn)
    agent = make_agent(conn)
    conn.commit()
    r = client.post("/api/departments/supply/campaigns/preview", headers=h,
                    data={"file": (io.BytesIO(CSV), "leads.csv"), "agent_id": agent["id"]},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    b = r.get_json()
    assert b["total"] == 4 and len(b["sample"]) == 4 and b["column_map"]["phone"] == "phone"
    assert b["variables"] == ["owner_name", "society_name"]


def test_create_campaign_from_upload(client, conn):
    u, h = _hdr(conn)
    agent = make_agent(conn)
    uc = make_use_case(conn)
    conn.commit()
    r = _create(client, h, _payload(agent, use_case_id=uc["id"], start_mode="now"))
    assert r.status_code == 201, r.get_json()
    b = r.get_json()
    assert (b["created"], b["invalid"], b["duplicates"]) == (2, 1, 1)
    c = _rows(conn, "SELECT * FROM campaign WHERE id = %s", b["id"])[0]
    assert c["vertical"] == "SUPPLY" and c["status"] == "SCHEDULED" and c["created_by_id"] == u["id"]
    assert len(_rows(conn, "SELECT 1 FROM lead WHERE campaign_id = %s", b["id"])) == 2
    assert len(_rows(conn, "SELECT 1 FROM scheduled_run WHERE campaign_id = %s", b["id"])) == 1
    acts = [a["action"] for a in _rows(conn, "SELECT action FROM audit_log ORDER BY created_at, id")]
    assert "campaign.create" in acts and "campaign.plan" in acts


def test_create_draft_has_no_run(client, conn):
    u, h = _hdr(conn)
    agent = make_agent(conn)
    conn.commit()
    b = _create(client, h, _payload(agent)).get_json()
    assert _rows(conn, "SELECT status FROM campaign WHERE id = %s", b["id"])[0]["status"] == "DRAFT"
    assert not _rows(conn, "SELECT 1 FROM scheduled_run")


def test_create_validation(client, conn):
    u, h = _hdr(conn)
    agent = make_agent(conn)
    other = make_agent(conn, vertical="DEMAND")
    both = make_agent(conn, vertical="BOTH")
    uc_other = make_use_case(conn, department="DEMAND")
    conn.commit()
    assert _create(client, h, _payload(other)).status_code == 400  # agent of other dept
    assert _create(client, h, _payload(both)).status_code == 201
    assert _create(client, h, _payload(agent, use_case_id=uc_other["id"])).status_code == 400
    assert _create(client, h, _payload(agent, name="x")).status_code == 400
    assert _create(client, h, _payload(agent, from_number="123")).status_code == 400
    assert _create(client, h, _payload(agent, start_mode="later")).status_code == 400
    assert _create(client, h, _payload(agent, start_mode="schedule")).status_code == 400
    assert _create(client, h, _payload(agent, redial_policy={"max_attempts": 99})).status_code == 400
    p = _payload(agent)
    del p["redial_policy"]
    r = _create(client, h, p)
    assert r.status_code == 400 and r.get_json() == {"error": "redial_policy is required"}
    assert _create(client, h, _payload(agent), data=b"phone\nabc\n").status_code == 400  # no valid phones
    assert _create(client, h, _payload(agent), dept="demand").status_code == 403  # manager is SUPPLY


def test_create_schedule(client, conn):
    u, h = _hdr(conn)
    agent = make_agent(conn)
    conn.commit()
    r = _create(client, h, _payload(agent, start_mode="schedule", scheduled_at="2030-01-07T05:00:00Z"))
    assert r.status_code == 201
    run = _rows(conn, "SELECT * FROM scheduled_run")[0]
    assert run["requested_start"] == datetime(2030, 1, 7, 5, 0, tzinfo=timezone.utc)


def test_controls_cross_dept_403(client, conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="RUNNING")
    u, h = _hdr(conn, vertical="DEMAND")
    for path in ("pause", "resume", "stop", "plan", "redial"):
        assert client.post(f"/api/campaigns/{camp['id']}/{path}", headers=h, json={}).status_code == 403, path
    assert client.get(f"/api/campaigns/{camp['id']}", headers=h).status_code == 403
    assert client.put(f"/api/campaigns/{camp['id']}/policy", headers=h,
                      json={"redial_policy": DEFAULT_REDIAL_POLICY}).status_code == 403


def test_list_and_detail(client, conn):
    u, h = _hdr(conn)
    uc = make_use_case(conn)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT", use_case_id=uc["id"])
    make_campaign(conn, agent=agent, status="RUNNING")
    make_lead(conn, camp, phone="+919810000001", status="CONNECTED", attempts=1, final_tag="qualified")
    make_lead(conn, camp, phone="+919810000002", status="NOT_CONNECTED", attempts=1)
    make_lead(conn, camp, phone="+919810000003")
    make_batch(conn, camp)
    conn.commit()
    items = client.get("/api/departments/supply/campaigns?status=DRAFT", headers=h).get_json()["items"]
    assert len(items) == 1
    it = items[0]
    assert (it["leads"], it["dialed"], it["connected"], it["good"], it["pending"]) == (3, 2, 1, 1, 1)
    assert it["use_case_name"] and it["agent_name"]
    assert len(client.get("/api/departments/supply/campaigns", headers=h).get_json()["items"]) == 2
    assert len(client.get(f"/api/departments/supply/campaigns?use_case_id={uc['id']}",
                          headers=h).get_json()["items"]) == 1
    d = client.get(f"/api/campaigns/{camp['id']}", headers=h).get_json()
    assert d["campaign"]["id"] == camp["id"] and d["agent"]["id"] == agent["id"] and d["use_case"]["id"] == uc["id"]
    assert d["counters"]["by_status"] == {"CONNECTED": 1, "NOT_CONNECTED": 1, "PENDING": 1}
    assert (d["counters"]["dialed"], d["counters"]["connected"], d["counters"]["good"]) == (2, 1, 1)
    assert len(d["batches"]) == 1 and d["run"] is None
    ls = client.get(f"/api/campaigns/{camp['id']}/leads?status=PENDING", headers=h).get_json()
    assert ls["total"] == 1 and ls["items"][0]["phone"] == "+919810000003"
    assert client.get(f"/api/campaigns/{camp['id']}/leads", headers=h).get_json()["total"] == 3


def test_plan_and_start_now_requires_head(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    make_lead(conn, camp, phone="+919810000001")
    u, h = _hdr(conn)
    head, hh = _hdr(conn, role="HEAD")
    conn.commit()
    assert client.post(f"/api/campaigns/{camp['id']}/start-now", headers=h).status_code == 403
    assert not bolna_stub.calls
    r = client.post(f"/api/campaigns/{camp['id']}/plan", headers=h, json={"scheduled_at": "2030-01-07T05:00:00Z"})
    assert r.status_code == 200 and r.get_json()["campaign_id"] == camp["id"]
    assert client.post(f"/api/campaigns/{camp['id']}/plan", headers=h, json={}).status_code == 409  # already planned
    camp2 = make_campaign(conn, agent=agent, status="DRAFT")
    make_lead(conn, camp2, phone="+919810000002")
    conn.commit()
    r = client.post(f"/api/campaigns/{camp2['id']}/start-now", headers=hh)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["leads"] == 1 and any(c[0] == "create_batch" for c in bolna_stub.calls)
    acts = {a["action"] for a in _rows(conn, "SELECT action FROM audit_log")}
    assert {"campaign.plan", "campaign.start"} <= acts


def test_pause_resume(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="RUNNING")
    lead = make_lead(conn, camp, phone="+919810000001", status="QUEUED")
    with conn.cursor() as cur:  # its batch starts in an hour: nobody has dialled it
        cur.execute("UPDATE lead SET next_dial_at = %s WHERE id = %s", (FROZEN + timedelta(hours=1), lead["id"]))
    done = make_lead(conn, camp, phone="+919810000002", status="CONNECTED", attempts=1)
    live = make_batch(conn, camp, status="scheduled", bolna_batch_id="bb-live")
    finished = make_batch(conn, camp, status="completed", bolna_batch_id="bb-done")
    with conn.cursor() as cur:  # Bolna said so (else it may be an idle-rule completion pause has to stop)
        cur.execute("UPDATE batch SET stats = %s WHERE id = %s", (json.dumps({"status": "completed"}), finished["id"]))
    conn.commit()
    r = client.post(f"/api/campaigns/{camp['id']}/pause", headers=h)
    assert r.status_code == 200
    assert r.get_json() == {"stopped": 1, "errors": []}
    assert [c for c in bolna_stub.calls if c[0] == "stop_batch"] == [("stop_batch", ("bb-live",))]
    assert _rows(conn, "SELECT status FROM batch WHERE id = %s", live["id"])[0]["status"] == "stopped"
    assert _rows(conn, "SELECT status FROM batch WHERE id = %s", finished["id"])[0]["status"] == "completed"
    l = _rows(conn, "SELECT status, next_dial_at FROM lead WHERE id = %s", lead["id"])[0]
    assert l["status"] == "PENDING" and l["next_dial_at"] is None
    c = _rows(conn, "SELECT status, paused_at FROM campaign WHERE id = %s", camp["id"])[0]
    assert (c["status"], c["paused_at"]) == ("PAUSED", FROZEN)

    _synced_after(conn, agent, FROZEN + timedelta(minutes=1))
    _freeze(monkeypatch, FROZEN + timedelta(minutes=2))
    r = client.post(f"/api/campaigns/{camp['id']}/resume", headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["leads"] == 1
    assert len([c for c in bolna_stub.calls if c[0] == "create_batch"]) == 1
    c = _rows(conn, "SELECT status, paused_at FROM campaign WHERE id = %s", camp["id"])[0]
    assert (c["status"], c["paused_at"]) == ("RUNNING", None)
    assert _rows(conn, "SELECT status FROM lead WHERE id = %s", done["id"])[0]["status"] == "CONNECTED"
    acts = {a["action"] for a in _rows(conn, "SELECT action FROM audit_log")}
    assert {"campaign.pause", "campaign.start"} <= acts


def test_quick_resume_never_redials_a_lead_whose_call_may_be_in_progress(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="RUNNING")
    started = FROZEN - timedelta(minutes=10)
    make_batch(conn, camp, status="running", bolna_batch_id="bb-1", scheduled_at=started)
    waiting = make_lead(conn, camp, phone="+919810000001", status="QUEUED")  # a later batch, not started
    untouched = make_lead(conn, camp, phone="+919810000002", status="QUEUED")  # started, Bolna never reached it
    talking = make_lead(conn, camp, phone="+919810000003", status="QUEUED", attempts=1,
                        last_call_at=FROZEN - timedelta(minutes=3))  # its call is in progress
    unsynced = make_lead(conn, camp, phone="+919810000004", status="QUEUED")  # called; the result is not synced yet
    with conn.cursor() as cur:
        cur.execute("UPDATE lead SET next_dial_at = %s WHERE id = %s", (FROZEN + timedelta(hours=1), waiting["id"]))
        cur.execute("UPDATE lead SET next_dial_at = %s WHERE id = ANY(%s)", (started, [untouched["id"], unsynced["id"]]))
    make_call(conn, phone=talking["phone"], started_at=FROZEN - timedelta(minutes=3), status="in-progress",
              outcome="UNKNOWN", campaign=camp, lead=talking)
    conn.commit()

    assert client.post(f"/api/campaigns/{camp['id']}/pause", headers=h).status_code == 200
    status = lambda l: _rows(conn, "SELECT status FROM lead WHERE id = %s", l["id"])[0]["status"]  # noqa: E731
    assert [status(x) for x in (waiting, untouched, talking, unsynced)] == ["PENDING", "QUEUED", "QUEUED", "QUEUED"]

    # resume straight away: only leads whose batch never started are dialled; QUEUED ones are left to the sync
    _freeze(monkeypatch, FROZEN + timedelta(minutes=1))
    r = client.post(f"/api/campaigns/{camp['id']}/resume", headers=h)
    assert r.status_code == 200, r.get_json()
    assert _csv_phones(bolna_stub) == [waiting["phone"]]
    assert [status(x) for x in (waiting, untouched, talking, unsynced)] == ["QUEUED", "QUEUED", "QUEUED", "QUEUED"]
    stamp = _rows(conn, "SELECT stats FROM batch WHERE bolna_batch_id = 'bb-1'")[0]["stats"][CONSOLE_STOP]
    assert stamp == (FROZEN + STOP_SETTLE).isoformat()

    # a sync that fetched from before the stop settled may not have every call yet: nothing closes
    _synced_after(conn, agent, FROZEN + timedelta(minutes=1))
    assert close_stale_queued(conn, camp["id"], FROZEN + timedelta(minutes=1)) == 0
    # (nor while the resume's own batch may still dial; Bolna reports it done)
    with conn.cursor() as cur:
        cur.execute("UPDATE batch SET status = 'completed', stats = %s WHERE campaign_id = %s AND bolna_batch_id <> 'bb-1'",
                    (json.dumps({"status": "completed"}), camp["id"]))
    # the sync after the pause brings the unsynced call in, then hands back only the lead Bolna never reached
    make_call(conn, phone=unsynced["phone"], started_at=started + timedelta(minutes=2), status="completed",
              outcome="NOT_PICKED", campaign=camp, lead=unsynced)
    _synced_after(conn, agent, FROZEN + timedelta(minutes=5))
    close_stale_queued(conn, camp["id"], FROZEN + timedelta(minutes=5))
    conn.commit()
    u = _rows(conn, "SELECT status, last_outcome, attempts FROM lead WHERE id = %s", untouched["id"])[0]
    assert (u["status"], u["last_outcome"], u["attempts"]) == ("NOT_CONNECTED", None, 0)
    assert [status(x) for x in (talking, unsynced)] == ["QUEUED", "QUEUED"]  # the sync rolls these up from their calls


def test_pause_stops_a_batch_the_idle_rule_completed_while_bolna_may_still_dial(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    camp = make_campaign(conn, agent=make_agent(conn), status="RUNNING")
    idle = make_batch(conn, camp, status="completed", bolna_batch_id="bb-idle", scheduled_at=FROZEN - timedelta(hours=3))
    done = make_batch(conn, camp, status="completed", bolna_batch_id="bb-done", scheduled_at=FROZEN - timedelta(hours=5))
    with conn.cursor() as cur:
        cur.execute("UPDATE batch SET stats = %s WHERE id = %s", (json.dumps({"status": "running"}), idle["id"]))
        cur.execute("UPDATE batch SET stats = %s WHERE id = %s", (json.dumps({"status": "completed"}), done["id"]))
    conn.commit()
    r = client.post(f"/api/campaigns/{camp['id']}/pause", headers=h)
    assert r.status_code == 200 and r.get_json() == {"stopped": 1, "errors": []}
    assert [c for c in bolna_stub.calls if c[0] == "stop_batch"] == [("stop_batch", ("bb-idle",))]
    row = _rows(conn, "SELECT status, stats FROM batch WHERE id = %s", idle["id"])[0]
    assert row["status"] == "stopped" and row["stats"]["status"] == "running" and CONSOLE_STOP in row["stats"]


def _pause_leaves_queued(client, conn, h, camp, lead):
    r = client.post(f"/api/campaigns/{camp['id']}/pause", headers=h)
    assert r.status_code == 200
    errors = r.get_json()["errors"]
    assert "leads left QUEUED: a batch may still be dialling" in errors
    assert _rows(conn, "SELECT status FROM lead WHERE id = %s", lead["id"])[0]["status"] == "QUEUED"
    assert _rows(conn, "SELECT status FROM campaign WHERE id = %s", camp["id"])[0]["status"] == "PAUSED"
    return errors


def test_pause_stop_failure_keeps_leads_queued(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    camp = make_campaign(conn, agent=make_agent(conn), status="RUNNING")
    lead = make_lead(conn, camp, phone="+919810000001", status="QUEUED")
    b = make_batch(conn, camp, status="running", bolna_batch_id="bb-x")
    conn.commit()
    bolna_stub.fail_on.add("stop_batch")
    r = client.post(f"/api/campaigns/{camp['id']}/pause", headers=h)
    assert r.status_code == 200 and r.get_json()["stopped"] == 0 and len(r.get_json()["errors"]) == 2
    assert _rows(conn, "SELECT status FROM batch WHERE id = %s", b["id"])[0]["status"] == "running"
    assert _rows(conn, "SELECT status FROM lead WHERE id = %s", lead["id"])[0]["status"] == "QUEUED"
    assert _rows(conn, "SELECT status FROM campaign WHERE id = %s", camp["id"])[0]["status"] == "PAUSED"
    r = client.post(f"/api/campaigns/{camp['id']}/resume", headers=h)
    assert r.status_code == 409 and r.get_json() == {"error": "a batch may still be dialling"}
    assert not [c for c in bolna_stub.calls if c[0] == "create_batch"]


def test_pause_creating_batch_keeps_leads_queued(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    camp = make_campaign(conn, agent=make_agent(conn), status="RUNNING")
    lead = make_lead(conn, camp, phone="+919810000001", status="QUEUED")
    creating = make_batch(conn, camp, status="creating")
    conn.commit()
    errors = _pause_leaves_queued(client, conn, h, camp, lead)
    assert f"batch {creating['id']} was being created and may still dial" in errors
    assert client.post(f"/api/campaigns/{camp['id']}/resume", headers=h).status_code == 409


def test_resume_bolna_failure_returns_to_paused_and_is_retryable(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="PAUSED")
    with conn.cursor() as cur:
        cur.execute("UPDATE campaign SET paused_at = %s WHERE id = %s", (FROZEN - timedelta(hours=1), camp["id"]))
    lead = make_lead(conn, camp, phone="+919810000001")
    _synced_after(conn, agent, FROZEN - timedelta(minutes=5))
    bolna_stub.fail_on = {"create_batch"}
    r = client.post(f"/api/campaigns/{camp['id']}/resume", headers=h)
    assert r.status_code == 502 and r.get_json()["error"].startswith("Bolna")
    c = _rows(conn, "SELECT status, paused_at FROM campaign WHERE id = %s", camp["id"])[0]
    assert (c["status"], c["paused_at"]) == ("PAUSED", FROZEN - timedelta(hours=1))  # the pause still stands
    assert _rows(conn, "SELECT status FROM lead WHERE id = %s", lead["id"])[0]["status"] == "PENDING"
    audit_row = _rows(conn, "SELECT meta FROM audit_log WHERE action = 'campaign.start'")[0]
    assert audit_row["meta"]["error"].startswith("Bolna")
    bolna_stub.fail_on = set()
    r = client.post(f"/api/campaigns/{camp['id']}/resume", headers=h)
    assert r.status_code == 200 and r.get_json()["leads"] == 1
    assert _rows(conn, "SELECT status FROM campaign WHERE id = %s", camp["id"])[0]["status"] == "RUNNING"


def test_resume_only_paused(client, conn, bolna_stub):
    u, h = _hdr(conn)
    camp = make_campaign(conn, agent=make_agent(conn), status="STOPPED")
    conn.commit()
    assert client.post(f"/api/campaigns/{camp['id']}/resume", headers=h).status_code == 409
    assert not bolna_stub.calls


def test_stop(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="SCHEDULED")
    make_batch(conn, camp, status="scheduled", bolna_batch_id="bb-1")
    run = _insert(conn, "scheduled_run", department="SUPPLY", campaign_id=camp["id"],
                  requested_start="2030-01-07T05:00:00Z")
    conn.commit()
    r = client.post(f"/api/campaigns/{camp['id']}/stop", headers=h)
    assert r.status_code == 200
    c = _rows(conn, "SELECT * FROM campaign WHERE id = %s", camp["id"])[0]
    assert c["status"] == "STOPPED" and c["completed_at"] is not None
    assert _rows(conn, "SELECT status FROM scheduled_run WHERE id = %s", run["id"])[0]["status"] == "CANCELLED"
    assert ("stop_batch", ("bb-1",)) in bolna_stub.calls
    acts = {a["action"] for a in _rows(conn, "SELECT action FROM audit_log")}
    assert {"campaign.pause", "campaign.stop"} <= acts


def test_redial(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="RUNNING")
    recent = FROZEN - timedelta(minutes=5)
    make_lead(conn, camp, phone="+919810000001", status="NOT_CONNECTED", attempts=1, last_call_at=recent,
              last_outcome="NOT_PICKED")
    conn.commit()
    r = client.post(f"/api/campaigns/{camp['id']}/redial", headers=h, json={})
    assert r.status_code == 200 and r.get_json()["leads"] == 0 and r.get_json()["skipped"]  # inside the gap
    r = client.post(f"/api/campaigns/{camp['id']}/redial", headers=h, json={"ignore_gap": True})
    assert r.status_code == 200 and r.get_json()["leads"] == 1
    assert client.post(f"/api/campaigns/{camp['id']}/redial", headers=h,
                       json={"ignore_gap": "yes"}).status_code == 400
    assert "campaign.redial" in {a["action"] for a in _rows(conn, "SELECT action FROM audit_log")}


def test_redial_only_a_running_campaign(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    u, h = _hdr(conn)
    agent = make_agent(conn)
    for status in ("PAUSED", "COMPLETED", "SCHEDULED", "DRAFT", "STOPPED"):
        camp = make_campaign(conn, agent=agent, status=status)
        make_lead(conn, camp, phone="+919810000001", status="NOT_CONNECTED", attempts=1,
                  last_call_at=FROZEN - timedelta(days=1), last_outcome="NOT_PICKED")
        conn.commit()
        r = client.post(f"/api/campaigns/{camp['id']}/redial", headers=h, json={"ignore_gap": True})
        assert r.status_code == 409 and r.get_json() == {"error": "only a running campaign can be redialled"}, status
    assert bolna_stub.calls == []


def _scheduled_for(bolna_stub):
    return [args[1] for name, args in bolna_stub.calls if name == "schedule_batch"]


def test_start_now_outside_the_department_window_dials_at_the_next_window_start(client, conn, bolna_stub,
                                                                                 monkeypatch):
    evening = datetime(2026, 11, 19, 20, 0, tzinfo=IST).astimezone(timezone.utc)
    _freeze(monkeypatch, evening)
    head, hh = _hdr(conn, role="HEAD")
    wide = {**DEFAULT_REDIAL_POLICY, "window_start": "09:00", "window_end": "22:00"}  # not the start-now window
    camp = make_campaign(conn, agent=make_agent(conn), status="DRAFT", redial_policy=wide)
    make_lead(conn, camp, phone="+919810000001")
    conn.commit()
    r = client.post(f"/api/campaigns/{camp['id']}/start-now", headers=hh)
    assert r.status_code == 200, r.get_json()
    opens = datetime(2026, 11, 20, 10, 0, tzinfo=IST).astimezone(timezone.utc)
    assert _scheduled_for(bolna_stub) == [opens]
    assert datetime.fromisoformat(r.get_json()["scheduled_at"]) == opens
    assert _rows(conn, "SELECT status FROM campaign WHERE id = %s", camp["id"])[0]["status"] == "SCHEDULED"


def test_resume_outside_the_department_window_dials_at_the_next_window_start(client, conn, bolna_stub, monkeypatch):
    early = datetime(2026, 11, 19, 8, 0, tzinfo=IST).astimezone(timezone.utc)
    _freeze(monkeypatch, early)
    u, h = _hdr(conn)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="PAUSED")
    with conn.cursor() as cur:
        cur.execute("UPDATE campaign SET paused_at = %s WHERE id = %s", (early - timedelta(hours=12), camp["id"]))
    make_lead(conn, camp, phone="+919810000001")
    _synced_after(conn, agent, early - timedelta(minutes=5))
    r = client.post(f"/api/campaigns/{camp['id']}/resume", headers=h)
    assert r.status_code == 200, r.get_json()
    assert _scheduled_for(bolna_stub) == [datetime(2026, 11, 19, 10, 0, tzinfo=IST).astimezone(timezone.utc)]


def test_redial_outside_the_policy_window_dials_at_its_next_start(client, conn, bolna_stub, monkeypatch):
    evening = datetime(2026, 11, 19, 20, 0, tzinfo=IST).astimezone(timezone.utc)
    _freeze(monkeypatch, evening)
    u, h = _hdr(conn)
    with conn.cursor() as cur:  # the department window is open; the campaign's redial window is not
        cur.execute("UPDATE department SET window_start = '09:00', window_end = '22:00' WHERE key = 'SUPPLY'")
    camp = make_campaign(conn, agent=make_agent(conn), status="RUNNING")  # policy window 10:00-19:00
    make_lead(conn, camp, phone="+919810000001", status="NOT_CONNECTED", attempts=1,
              last_call_at=evening - timedelta(days=1), last_outcome="NOT_PICKED")
    conn.commit()
    r = client.post(f"/api/campaigns/{camp['id']}/redial", headers=h, json={})
    assert r.status_code == 200 and r.get_json()["leads"] == 1, r.get_json()
    assert _scheduled_for(bolna_stub) == [datetime(2026, 11, 20, 10, 0, tzinfo=IST).astimezone(timezone.utc)]


def test_policy(client, conn):
    u, h = _hdr(conn)
    camp = make_campaign(conn, agent=make_agent(conn), status="DRAFT")
    conn.commit()
    pol = {**DEFAULT_REDIAL_POLICY, "max_attempts": 5}
    r = client.put(f"/api/campaigns/{camp['id']}/policy", headers=h, json={"redial_policy": pol})
    assert r.status_code == 200 and r.get_json()["redial_policy"]["max_attempts"] == 5
    assert client.put(f"/api/campaigns/{camp['id']}/policy", headers=h,
                      json={"redial_policy": {"max_attempts": 99}}).status_code == 400
    assert client.put(f"/api/campaigns/{camp['id']}/policy", headers=h, json={}).status_code == 400
    assert "campaign.policy" in {a["action"] for a in _rows(conn, "SELECT action FROM audit_log")}


def test_delete_running_campaign_409(client, conn):
    agent = make_agent(conn)
    running = make_campaign(conn, agent=agent, status="RUNNING")
    draft = make_campaign(conn, agent=agent, status="DRAFT")
    mgr, mh = _hdr(conn)
    owner, oh = _hdr(conn, role="ADMIN", vertical="BOTH")
    conn.commit()
    assert client.delete(f"/api/campaigns/{draft['id']}", headers=mh).status_code == 403
    assert client.delete(f"/api/campaigns/{running['id']}", headers=oh).status_code == 409
    assert client.delete(f"/api/campaigns/{draft['id']}", headers=oh).status_code == 204
    assert not _rows(conn, "SELECT 1 FROM campaign WHERE id = %s", draft["id"])
    assert "campaign.delete" in {a["action"] for a in _rows(conn, "SELECT action FROM audit_log")}


def test_delete_while_a_batch_may_still_dial_409(client, conn):
    camp = make_campaign(conn, agent=make_agent(conn), status="STOPPED")
    b = make_batch(conn, camp, status="running", bolna_batch_id="bb-1")  # its stop failed at Bolna
    owner, oh = _hdr(conn, role="ADMIN", vertical="BOTH")
    conn.commit()
    r = client.delete(f"/api/campaigns/{camp['id']}", headers=oh)
    assert r.status_code == 409 and r.get_json() == {"error": "a batch may still be dialling"}
    with conn.cursor() as cur:
        cur.execute("UPDATE batch SET status = 'stopped' WHERE id = %s", (b["id"],))
    conn.commit()
    assert client.delete(f"/api/campaigns/{camp['id']}", headers=oh).status_code == 204


def test_start_now_bolna_failure_is_502_json(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    lead = make_lead(conn, camp, phone="+919810000001")
    head, hh = _hdr(conn, role="HEAD")
    conn.commit()
    bolna_stub.fail_on = {"create_batch"}
    r = client.post(f"/api/campaigns/{camp['id']}/start-now", headers=hh)
    assert r.status_code == 502
    assert r.get_json()["error"].startswith("Bolna")
    assert _rows(conn, "SELECT status FROM lead WHERE id = %s", lead["id"])[0]["status"] == "PENDING"


# A pause (and resume) landing between a sync's executions and rollup steps (re-review N1): the lead Bolna dialled
# after the fetch must stay QUEUED and must not be dialled again in that sync.
def _pause_mid_sync(conn, client, bolna_stub, monkeypatch, *, resume):
    agent = make_agent(conn, bolna_agent_id="ba-1")
    camp = make_campaign(conn, agent=agent, status="RUNNING")
    start = FROZEN - timedelta(minutes=10)
    b = make_batch(conn, camp, status="running", bolna_batch_id="bb-1", scheduled_at=start)
    lead = make_lead(conn, camp, phone="+919810000001", status="QUEUED")
    with conn.cursor() as cur:
        cur.execute("UPDATE campaign SET started_at = %s WHERE id = %s", (FROZEN - timedelta(hours=1), camp["id"]))
        cur.execute("UPDATE batch SET stats = %s WHERE id = %s", (json.dumps({"status": "running"}), b["id"]))
        cur.execute("UPDATE lead SET next_dial_at = %s WHERE id = %s", (start, lead["id"]))
    u, h = _hdr(conn)
    clock = {"t": FROZEN}
    monkeypatch.setattr(sync, "_utcnow", lambda: clock["t"])
    bolna_stub.batches["bb-1"] = {"batch_id": "bb-1", "status": "running"}
    bolna_stub.executions = {"ba-1": [[]]}  # Bolna dials the lead right after this page is fetched
    real_exec, real_rules = sync.sync_executions, sync.retry_rules

    def executions(*a, **k):
        clock["t"] = FROZEN + timedelta(minutes=1)
        return real_exec(*a, **{**k, "now": clock["t"]})

    def rules(*a, **k):  # meanwhile the user pauses (and resumes)
        real_rules(*a, **k)
        _freeze(monkeypatch, FROZEN + timedelta(minutes=2))
        assert client.post(f"/api/campaigns/{camp['id']}/pause", headers=h).status_code == 200
        if resume:
            assert client.post(f"/api/campaigns/{camp['id']}/resume", headers=h).status_code == 200
        clock["t"] = FROZEN + timedelta(minutes=3)  # the rollup step starts

    monkeypatch.setattr(sync, "sync_executions", executions)
    monkeypatch.setattr(sync, "retry_rules", rules)
    report = sync.new_report()
    sync._run_steps(conn, report, False, 40)
    assert report["errors"] == []
    return lead


def test_pause_mid_sync_never_hands_back_a_lead_whose_call_is_unsynced(client, conn, bolna_stub, monkeypatch):
    lead = _pause_mid_sync(conn, client, bolna_stub, monkeypatch, resume=False)
    assert _rows(conn, "SELECT status FROM lead WHERE id = %s", lead["id"])[0]["status"] == "QUEUED"


def test_pause_resume_mid_sync_never_redials(client, conn, bolna_stub, monkeypatch):
    lead = _pause_mid_sync(conn, client, bolna_stub, monkeypatch, resume=True)
    assert _csv_phones(bolna_stub) == []
    assert _rows(conn, "SELECT status FROM lead WHERE id = %s", lead["id"])[0]["status"] == "QUEUED"
