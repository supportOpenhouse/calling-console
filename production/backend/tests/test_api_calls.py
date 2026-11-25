import csv
import io
from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json

from ..tests.conftest import auth_headers
from .factories import _insert, make_agent, make_call, make_campaign, make_lead, make_use_case, make_user

T = datetime(2026, 11, 10, 6, 30, 5, tzinfo=timezone.utc)


def _one(conn, sql, *p):
    with conn.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchone()


def _setup(conn):
    sa, da = make_agent(conn, vertical="SUPPLY"), make_agent(conn, vertical="DEMAND")
    sc, dc = make_campaign(conn, agent=sa, vertical="SUPPLY"), make_campaign(conn, agent=da, vertical="DEMAND")
    return sa, sc, da, dc


def _hdr(conn, **kw):
    u = make_user(conn, **{"role": "MANAGER", "vertical": "SUPPLY", **kw})
    conn.commit()
    return u, auth_headers(u)


def test_calls_list_scoped_to_dept(client, conn):
    sa, sc, da, dc = _setup(conn)
    lead = make_lead(conn, sc, phone="+919810000001")
    _insert(conn, "lead", campaign_id=sc["id"], phone="+919810000009", name="Meena", variables=Json({}))
    c1 = make_call(conn, phone="+919810000001", started_at=T, campaign=sc, lead=lead)
    make_call(conn, phone="+919810000002", started_at=T, campaign=dc)
    prev = make_call(conn, phone="+919810000003", started_at=T - timedelta(minutes=1), agent=sa)  # backfilled, no campaign
    make_call(conn, phone="+919810000004", started_at=T, agent=da)  # another department's previous call
    with conn.cursor() as cur:
        cur.execute("UPDATE call SET transcript = %s WHERE id = %s", ("मुझे कल बात करनी है", c1["id"]))
    u, h = _hdr(conn)
    r = client.get("/api/departments/supply/calls", headers=h).get_json()
    assert r["total"] == 2  # its campaign's call, and its agent's call made outside the console
    assert [(i["id"], i["source"]) for i in r["items"]] == [(c1["id"], "console"), (prev["id"], "previous")]
    assert client.get(f"/api/departments/supply/calls?campaign_id={sc['id']}", headers=h).get_json()["total"] == 1
    assert client.get(f"/api/departments/supply/calls?agent_id={sa['id']}", headers=h).get_json()["total"] == 2
    assert client.get("/api/departments/demand/calls", headers=h).status_code == 403
    assert client.get("/api/departments/supply/calls?q=कल", headers=h).get_json()["total"] == 1
    assert client.get("/api/departments/supply/calls?q=zzz", headers=h).get_json()["total"] == 0
    assert client.get("/api/departments/supply/calls?q=0001", headers=h).get_json()["total"] == 1
    assert client.get("/api/departments/supply/calls?outcome=UNKNOWN", headers=h).get_json()["total"] == 0
    assert client.get("/api/departments/supply/calls?outcome=BAD", headers=h).status_code == 400
    assert client.get("/api/departments/supply/calls?from=2026-11-11", headers=h).get_json()["total"] == 0
    assert client.get("/api/departments/supply/calls?from=2026-11-10&to=2026-11-10", headers=h).get_json()["total"] == 2


def test_calls_list_use_case_restriction(client, conn):
    sa, _, _, _ = _setup(conn)
    uc1, uc2 = make_use_case(conn, key="a"), make_use_case(conn, key="b")
    k1, k2 = (make_campaign(conn, agent=sa, use_case_id=uc["id"]) for uc in (uc1, uc2))
    make_call(conn, phone="+919810000001", started_at=T, campaign=k1)
    make_call(conn, phone="+919810000002", started_at=T, campaign=k2)
    u, h = _hdr(conn, use_case_ids=[uc1["id"]])
    assert client.get("/api/departments/supply/calls", headers=h).get_json()["total"] == 1


def test_call_detail_cross_dept_403(client, conn):
    sa, sc, da, dc = _setup(conn)
    lead = make_lead(conn, sc, phone="+919810000001")
    c1 = make_call(conn, phone="+919810000001", started_at=T, campaign=sc, lead=lead)
    c0 = make_call(conn, phone="+919810000001", started_at=datetime(2026, 11, 9, tzinfo=timezone.utc), campaign=sc, lead=lead)
    other = make_call(conn, phone="+919810000002", started_at=T, campaign=dc)
    with conn.cursor() as cur:
        cur.execute("UPDATE call SET extracted = %s WHERE id = %s",
                    (Json({"cat": {"budget": "50L"}}), c1["id"]))
    u, h = _hdr(conn)
    d = client.get(f"/api/calls/{c1['id']}", headers=h).get_json()
    assert d["call"]["id"] == c1["id"] and d["campaign"]["id"] == sc["id"] and d["agent"]["id"] == sa["id"]
    assert d["lead"]["id"] == lead["id"] and d["fields"] == {"budget": "50L"}
    assert [c["id"] for c in d["history"]] == [c1["id"], c0["id"]]
    assert client.get(f"/api/calls/{other['id']}", headers=h).status_code == 403
    assert client.get(f"/api/calls/{other['id']}/recording", headers=h).status_code == 403
    assert client.post(f"/api/calls/{other['id']}/tag", headers=h, json={"tag": "x"}).status_code == 403
    assert client.post(f"/api/calls/{other['id']}/dnc", headers=h).status_code == 403
    assert client.get("/api/calls/nope", headers=h).status_code == 404


def test_recording_returns_signed_or_bolna(client, conn, monkeypatch):
    from ..services import storage
    sa, sc, _, _ = _setup(conn)
    c = make_call(conn, phone="+919810000001", started_at=T, campaign=sc)
    u, h = _hdr(conn)
    url = f"/api/calls/{c['id']}/recording"
    assert client.get(url, headers=h).status_code == 404
    with conn.cursor() as cur:
        cur.execute("UPDATE call SET recording_url = 'http://bolna/x.mp3', recording_key = 'recordings/x.mp3' WHERE id = %s", (c["id"],))
    conn.commit()
    monkeypatch.setattr(storage, "storage_enabled", lambda: False)
    assert client.get(url, headers=h).get_json() == {"url": "http://bolna/x.mp3"}
    monkeypatch.setattr(storage, "storage_enabled", lambda: True)
    monkeypatch.setattr(storage, "recording_url", lambda key: f"https://r2/{key}")
    assert client.get(url, headers=h).get_json() == {"url": "https://r2/recordings/x.mp3"}


def test_export_csv_scoped_and_utf8(client, conn):
    sa, sc, da, dc = _setup(conn)
    lead = make_lead(conn, sc, phone="+919810000001")
    with conn.cursor() as cur:
        cur.execute("UPDATE lead SET name = %s WHERE id = %s", ("-cmd|' /C calc'!A0", lead["id"]))
    c = make_call(conn, phone="+919810000001", started_at=T, campaign=sc, lead=lead, tag="Hot")
    make_call(conn, phone="+919810000002", started_at=T, campaign=dc)
    c2 = make_call(conn, phone="+919810000003", started_at=datetime(2026, 11, 9, tzinfo=timezone.utc), campaign=sc)
    with conn.cursor() as cur:
        cur.execute("UPDATE call SET transcript = '=HYPERLINK(\"http://x\")', extracted = %s WHERE id = %s",
                    (Json({"cat": {"budget": "-12.5"}}), c2["id"]))
        cur.execute("UPDATE call SET transcript = %s, extracted = %s WHERE id = %s",
                    ("assistant: नमस्ते\nuser: हां, \"ठीक\"", Json({"cat": {"budget": "50L"}}), c["id"]))
    u, h = _hdr(conn)
    r = client.get("/api/departments/supply/calls/export.csv", headers=h)
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/csv") and "charset=utf-8" in r.headers["Content-Type"]
    assert r.headers["Content-Disposition"].startswith('attachment; filename="calls_')
    rows = list(csv.reader(io.StringIO(r.data.decode("utf-8"))))
    assert rows[0] == ["called_at", "source", "campaign", "agent", "phone", "name", "status", "outcome", "hangup_code",
                       "duration_sec", "human_turns", "tag", "budget", "recording_url", "transcript"]
    assert len(rows) == 3
    row = dict(zip(rows[0], rows[1]))
    row2 = dict(zip(rows[0], rows[2]))
    assert row["phone"] == "+919810000001" and row2["phone"] == "+919810000003"
    assert row2["transcript"] == "'=HYPERLINK(\"http://x\")" and row2["budget"] == "-12.5"
    assert row["called_at"] == "10 Nov, 12:00:05 PM"
    assert row["transcript"] == "assistant: नमस्ते\nuser: हां, \"ठीक\""
    assert row["name"] == "'-cmd|' /C calc'!A0" and row["budget"] == "50L" and row["tag"] == "Hot"
    assert client.get("/api/departments/demand/calls/export.csv", headers=h).status_code == 403


def test_retag_updates_lead(client, conn):
    sa, sc, _, _ = _setup(conn)
    lead = make_lead(conn, sc, phone="+919810000001")
    c = make_call(conn, phone="+919810000001", started_at=T, campaign=sc, lead=lead)
    u, h = _hdr(conn)
    assert client.post(f"/api/calls/{c['id']}/tag", headers=h, json={}).status_code == 400
    r = client.post(f"/api/calls/{c['id']}/tag", headers=h, json={"tag": "Interested"})
    assert r.status_code == 200 and r.get_json()["tag"] == "Interested"
    assert _one(conn, "select tag from call where id=%s", c["id"])["tag"] == "Interested"
    assert _one(conn, "select final_tag from lead where id=%s", lead["id"])["final_tag"] == "Interested"
    assert _one(conn, "select count(*) n from audit_log where action='call.tag'")["n"] == 1


def test_call_dnc(client, conn):
    sa, sc, _, _ = _setup(conn)
    lead = make_lead(conn, sc, phone="+919810000001")
    c = make_call(conn, phone="+919810000001", started_at=T, campaign=sc, lead=lead)
    u, h = _hdr(conn)
    r = client.post(f"/api/calls/{c['id']}/dnc", headers=h)
    assert r.status_code == 200 and r.get_json() == {"queued_in": []}
    assert _one(conn, "select source from dnc_phone where phone='+919810000001'")["source"] == "manual"
    assert _one(conn, "select status from lead where id=%s", lead["id"])["status"] == "DNC"


def test_call_dnc_names_the_live_batch_still_holding_the_phone(client, conn):
    sa, sc, _, _ = _setup(conn)
    lead = make_lead(conn, sc, phone="+919810000001", status="QUEUED")  # queued again for the next round
    c = make_call(conn, phone="+919810000001", started_at=T, campaign=sc, lead=lead)
    u, h = _hdr(conn)
    r = client.post(f"/api/calls/{c['id']}/dnc", headers=h)
    assert r.status_code == 200
    assert r.get_json() == {"queued_in": [{"campaign_id": sc["id"], "campaign_name": sc["name"], "scheduled_at": None}]}


def test_handoffs_list_and_patch(client, conn):
    sa, sc, da, dc = _setup(conn)
    uc = make_use_case(conn)
    c = make_call(conn, phone="+919810000001", started_at=T, campaign=sc)
    ho = _insert(conn, "handoff", department="SUPPLY", use_case_id=uc["id"], campaign_id=sc["id"], call_id=c["id"],
                 phone="+919810000001")
    other = _insert(conn, "handoff", department="DEMAND", phone="+919810000002")
    u, h = _hdr(conn)
    peer = make_user(conn, vertical="SUPPLY")
    outsider = make_user(conn, vertical="DEMAND")
    gone = make_user(conn, vertical="SUPPLY", active=False)
    conn.commit()
    items = client.get("/api/departments/supply/handoffs", headers=h).get_json()["items"]
    assert [(i["id"], i["call_id"], i["use_case_name"]) for i in items] == [(ho["id"], c["id"], uc["name"])]
    assert client.get("/api/departments/supply/handoffs?status=NOPE", headers=h).status_code == 400
    assert client.get("/api/departments/supply/handoffs?status=CLOSED_WON", headers=h).get_json()["items"] == []
    url = f"/api/handoffs/{ho['id']}"
    assert client.patch(url, headers=h, json={"status": "WHATEVER"}).status_code == 400
    for bad in (outsider, gone):
        assert client.patch(url, headers=h, json={"assigned_to_id": bad["id"]}).status_code == 400
    assert client.patch(url, headers=h, json={"assigned_to_id": "nope"}).status_code == 400
    r = client.patch(url, headers=h, json={"status": "CONTACTED", "assigned_to_id": peer["id"], "rm_notes": "called"})
    assert r.status_code == 200
    b = r.get_json()
    assert (b["status"], b["assigned_to_id"], b["rm_notes"]) == ("CONTACTED", peer["id"], "called")
    assert client.patch(url, headers=h, json={"assigned_to_id": None}).get_json()["assigned_to_id"] is None
    assert client.patch(f"/api/handoffs/{other['id']}", headers=h, json={"status": "CONTACTED"}).status_code == 403


def test_pulse_newest_call_of_the_department_and_stale_pull(client, conn, monkeypatch):
    from .. import config
    from ..services.settings_store import set_setting
    sa, sc, da, dc = _setup(conn)
    _, h = _hdr(conn)  # Supply manager
    r = client.get("/api/sync/pulse?dept=supply", headers=h)
    assert r.status_code == 200 and r.json["latest"] is None
    c = make_call(conn, phone="+919810000001", started_at=T, campaign=sc)
    d = make_call(conn, phone="+919810000002", started_at=T, campaign=dc)
    with conn.cursor() as cur:  # Demand's call is newer but not Supply's
        cur.execute("UPDATE call SET created_at = now() + interval '1 hour' WHERE id = %s", (d["id"],))
    conn.commit()
    latest = _one(conn, "SELECT extract(epoch FROM created_at)::float AS t FROM call WHERE id = %s", c["id"])["t"]
    assert client.get("/api/sync/pulse?dept=supply", headers=h).json["latest"] == latest
    assert client.get("/api/sync/pulse?dept=demand", headers=h).status_code == 403
    monkeypatch.setattr(config, "SYNC_ENABLED", "1")
    assert client.get("/api/sync/pulse?dept=supply", headers=h).json["stale"] is True  # never pulled
    set_setting(conn, "lastPullAt", datetime.now(timezone.utc).isoformat())
    conn.commit()
    assert client.get("/api/sync/pulse?dept=supply", headers=h).json["stale"] is False
