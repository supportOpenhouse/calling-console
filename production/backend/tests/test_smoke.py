"""End-to-end: dataset -> ingest -> workflow run -> scheduler dispatch -> Bolna results -> sync -> DI push, plus the
backfill and seed scripts. Time is frozen so the flow does not depend on the clock the suite runs at."""
import io
from datetime import datetime, timedelta, timezone

import pytest

from .. import config
from ..scripts import backfill, seed
from ..services import di_push, redial, scheduler, sync, workflows
from ..services.ist import IST
from ..services.settings_store import get_setting
from .conftest import auth_headers
from .factories import _insert, bolna_execution, make_agent, make_dataset, make_use_case, make_user
from .test_workflows import make_workflow

FROZEN = datetime(2026, 11, 19, 11, 0, tzinfo=IST).astimezone(timezone.utc)  # Thursday, inside the window
QUALIFIED, ANGRY, NEW_LEAD = "+919810043058", "+919810043059", "+919810043060"
CSV = b"phone,name,city\n9810043058,Asha,Gurgaon\n9810043059,Ravi,Delhi\n"


def _freeze(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN if tz is None else FROZEN.astimezone(tz)

    for mod in (sync, workflows, scheduler, redial, di_push):
        monkeypatch.setattr(mod, "datetime", Frozen)


def _calls(stub, name):
    return [c for c in stub.calls if c[0] == name]


def test_end_to_end(client, conn, bolna_stub, monkeypatch):
    _freeze(monkeypatch)
    monkeypatch.setattr(config, "INGEST_TOKEN", "ingest-secret")
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    sent = []

    class Res:
        status_code, text = 200, "ok"

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json))
        return Res()

    monkeypatch.setattr(di_push.requests, "post", post)

    agent, uc = make_agent(conn, bolna_agent_id="bolna-smoke"), make_use_case(conn)
    manager = make_user(conn, role="MANAGER", vertical="SUPPLY")
    new_lead_ds = make_dataset(conn, use_case_id=uc["id"])
    make_workflow(conn, agent=agent, use_case=uc, dataset=new_lead_ds,
                  trigger={"type": "ON_NEW_LEAD", "within_minutes": 2})
    conn.commit()
    h = auth_headers(manager)

    # 1. upload a 2-row dataset
    r = client.post("/api/departments/supply/datasets", headers=h, content_type="multipart/form-data", data={
        "name": "Sellers", "use_case_id": uc["id"], "source": "UPLOAD", "file": (io.BytesIO(CSV), "s.csv")})
    assert r.status_code == 201, r.get_json()
    ds = r.get_json()["dataset"]
    assert ds["row_count"] == 2

    # 2. a new lead is ingested to the ON_NEW_LEAD workflow's dataset -> a batch is created
    r = client.post(f"/api/ingest/datasets/{new_lead_ds['id']}/leads", json={"phone": NEW_LEAD, "name": "Meera"},
                    headers={"Authorization": "Bearer ingest-secret"})
    assert r.status_code == 200 and r.get_json()["errors"] == [], r.get_json()
    assert len(_calls(bolna_stub, "create_batch")) == 1

    # 3. a MANUAL workflow on the uploaded dataset, run -> PLANNED on the calendar, then dispatched
    body = {"use_case_id": uc["id"], "name": "Follow up", "agent_id": agent["id"], "dataset_id": ds["id"],
            "from_number": "+918000000001", "trigger": {"type": "MANUAL"}, "rules": [],
            "redial_policy": dict(redial.DEFAULT_REDIAL_POLICY)}
    r = client.post("/api/departments/supply/workflows", headers=h, json=body)
    assert r.status_code == 201, r.get_json()
    r = client.post(f"/api/workflows/{r.get_json()['id']}/run", headers=h, json={})
    assert r.status_code == 200 and r.get_json()["leads"] == 2, r.get_json()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM scheduled_run")
        run = cur.fetchone()
    assert run["status"] == "PLANNED"
    assert scheduler.dispatch_due_runs(conn, now=run["planned_start"]) == [run["id"]]
    assert len(_calls(bolna_stub, "create_batch")) == 2
    with conn.cursor() as cur:
        cur.execute("SELECT bolna_batch_id FROM batch b JOIN campaign c ON c.id = b.campaign_id "
                    "WHERE c.id = %s", (run["campaign_id"],))
        batch_id = cur.fetchone()["bolna_batch_id"]
    assert batch_id

    # 4. Bolna reports both calls an hour before "now"
    started = (FROZEN - timedelta(hours=1)).isoformat()

    def ex(phone, tag):
        return bolna_execution(agent_id="bolna-smoke", batch_id=batch_id, created_at=started,
                               telephony_data={"to_number": phone, "from_number": "+918000000001",
                                               "hangup_provider_code": 4000},
                               extracted_data={"Seller": {"call_tag": {"objective": tag}}})

    bolna_stub.executions = {"bolna-smoke": [[ex(QUALIFIED, "qualified"), ex(ANGRY, "angry")]]}

    # 5. one sync
    report = sync.run_sync()
    assert report["errors"] == [], report["errors"]
    assert report["calls_upserted"] == 2 and report["di_pushed"] == 2
    with conn.cursor() as cur:
        cur.execute("SELECT phone, tag FROM handoff")
        assert [(x["phone"], x["tag"]) for x in cur.fetchall()] == [(QUALIFIED, "qualified")]
        cur.execute("SELECT phone FROM dnc_phone")
        assert [x["phone"] for x in cur.fetchall()] == [ANGRY]
    assert len(sent) == 2 and {p["phone"] for _, p in sent} == {QUALIFIED, ANGRY}
    assert get_setting(conn, "lastSyncReport")["errors"] == []


def _angry_exec(phone, tag, n):
    return bolna_execution(id=f"bf-{n}", agent_id="ba-bf", created_at=(FROZEN - timedelta(days=30 + n)).isoformat(),
                           telephony_data={"to_number": phone, "from_number": "+918000000001"},
                           extracted_data={"S": {"call_tag": {"objective": tag}}})


def test_backfill_marks_dnc_and_is_rerunnable(conn, bolna_stub, capsys):
    bolna_stub.agents = [{"agent_id": "ba-bf", "agent_name": "Backfill Agent"}]
    # three pages, every execution older than the 48h first lookback: only an uncapped full page-through sees them
    bolna_stub.executions = {"ba-bf": [[_angry_exec("+919810000001", "Angry", 1)],
                                       [_angry_exec("+919810000002", "wrong_person", 2)],
                                       [_angry_exec("+919810000003", "qualified", 3)]]}
    assert backfill.main() == 0
    out = capsys.readouterr().out
    assert "3 calls" in out and "2 DNC" in out
    with conn.cursor() as cur:
        cur.execute("SELECT phone, source, reason FROM dnc_phone ORDER BY phone")
        # the upsert path already blocks a DNC-tagged call that links to no campaign (source call_tag)
        assert [(r["phone"], r["source"], r["reason"]) for r in cur.fetchall()] == [
            ("+919810000001", "call_tag", "Angry"), ("+919810000002", "call_tag", "wrong_person")]
    conn.rollback()
    assert backfill.main() == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM dnc_phone")
        assert cur.fetchone()["n"] == 2
        cur.execute("SELECT count(*) AS n FROM call")
        assert cur.fetchone()["n"] == 3


def test_backfill_exits_1_when_sync_is_running(conn, bolna_stub, capsys):
    with sync.sync_lock() as got:
        assert got
        assert backfill.main() == 1
    assert "already running" in capsys.readouterr().out


def test_seed_refuses_without_flag(monkeypatch, conn, capsys):
    monkeypatch.delenv("ALLOW_SEED", raising=False)
    assert seed.main() == 2
    assert "ALLOW_SEED" in capsys.readouterr().err
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM "user"')
        assert cur.fetchone()["n"] == 0


def test_seed_creates_the_fixtures_and_is_rerunnable(monkeypatch, conn, bolna_stub):
    monkeypatch.setenv("ALLOW_SEED", "1")
    assert seed.main() == 0
    assert seed.main() == 0
    assert bolna_stub.calls == []

    def one(sql):
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    users = {r["email"]: (r["role"], r["vertical"]) for r in one('SELECT email, role, vertical FROM "user"')}
    assert users == {
        "owner@openhouse.in": ("ADMIN", "BOTH"), "supply.head@openhouse.in": ("HEAD", "SUPPLY"),
        "demand.head@openhouse.in": ("HEAD", "DEMAND"), "supply.manager@openhouse.in": ("MANAGER", "SUPPLY"),
        "demand.manager@openhouse.in": ("MANAGER", "DEMAND"), "viewer@openhouse.in": ("VIEWER", "BOTH")}
    assert [r["n"] for r in one("SELECT count(*) AS n FROM agent")] == [2]
    assert [r["n"] for r in one("SELECT count(*) AS n FROM use_case")] == [2]
    assert {r["department"]: r["n"] for r in one(
        "SELECT d.department, count(*) AS n FROM dataset d JOIN dataset_lead l ON l.dataset_id = d.id "
        "GROUP BY d.department")} == {"DEMAND": 20, "SUPPLY": 20}
    assert {r["department"] for r in one("SELECT department FROM workflow WHERE trigger->>'type' = 'MANUAL'")} == {
        "SUPPLY", "DEMAND"}
    assert len(one("SELECT 1 FROM workflow")) == 2
