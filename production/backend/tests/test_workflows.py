import threading
from datetime import datetime, timedelta, timezone

import pytest
from psycopg2.extras import Json

from .. import config, db
from ..services import scheduler, whatsapp, workflows
from ..services.datasets import mark_dnc
from ..services.ist import IST
from ..services.redial import DEFAULT_REDIAL_POLICY
from ..services.workflows import (
    apply_result_rules, create_handoff, fire_triggers, map_variables, rule_matches, run_workflow, schedule_due,
)
from .factories import _insert, _uid, make_agent, make_call, make_campaign, make_dataset, make_lead, make_use_case


def ist(h, m=0, day=0):
    return datetime(2026, 11, 18 + day, h, m, tzinfo=IST).astimezone(timezone.utc)


NOW = ist(11)  # Wednesday 18 Nov 2026, 11:00 IST: inside the 10:00-19:00 window


def P(n: int) -> str:
    return f"+91981000{n:04d}"


def make_workflow(conn, *, agent, use_case, trigger, dataset=None, rules=(), parent=None, variable_map=None,
                  segment=None, step_order=0, last_run_at=None):
    return _insert(conn, "workflow", department="SUPPLY", use_case_id=use_case["id"], name=f"WF {_uid()}",
                   purpose="seller follow-up", agent_id=agent["id"], dataset_id=dataset["id"] if dataset else None,
                   segment=Json(segment or {}), from_number="+918000000001", variable_map=Json(variable_map or {}),
                   trigger=Json(trigger), redial_policy=Json(dict(DEFAULT_REDIAL_POLICY)), rules=Json(list(rules)),
                   parent_id=parent["id"] if parent else None, step_order=step_order, last_run_at=last_run_at)


def _one(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _set(conn, table, row_id, **cols):
    sets = ", ".join(f'"{k}" = %s' for k in cols)
    with conn.cursor() as cur:
        cur.execute(f'UPDATE "{table}" SET {sets} WHERE id = %s RETURNING *', [*cols.values(), row_id])
        return cur.fetchone()


def _dataset_row(conn, dataset, phone):
    return _one(conn, "select * from dataset_lead where dataset_id = %s and phone = %s", dataset["id"], phone)


def _added_at(conn, dataset, phone, when):
    with conn.cursor() as cur:
        cur.execute("update dataset_lead set created_at = %s where dataset_id = %s and phone = %s",
                    (when, dataset["id"], phone))


def _campaigns_of(conn, wf):
    return _all(conn, "select * from campaign where workflow_id = %s order by created_at", wf["id"])


def _leads_of(conn, campaign):
    return _all(conn, "select * from lead where campaign_id = %s order by phone", campaign["id"])


def _stub_calls(stub, name):
    return [args for n, args in stub.calls if n == name]


def _csv_phones(csv_text: str) -> list[str]:
    return [line.split(",")[0] for line in csv_text.splitlines()[1:]]


# ---------------------------------------------------------------- pure helpers

def test_map_variables():
    row = {"name": "Rajendra Kumar", "society": "Gaur City 1st Avenue", "city": "Noida",
           "data": {"Owner Name": "Rajendra Kumar Sharma", "BHK": "3 BHK", "Seller": "Suresh Pal", "society_name": ""}}
    variables = ["owner_name", "bhk", "society_name", "city", "price"]
    # same-name column after normalising ("Owner Name" -> owner_name); first name only; bhk spoken;
    # an empty society_name column falls back to the row's society; no city column -> row city; unknown -> ""
    assert map_variables(row, variables, {}) == {
        "owner_name": "Rajendra", "bhk": "थ्री बी एच के", "society_name": "Gaur City 1st Avenue",
        "city": "Noida", "price": "",
    }
    # variable_map wins over the same-name column
    assert map_variables(row, ["owner_name"], {"owner_name": "Seller"}) == {"owner_name": "Suresh"}
    # nothing anywhere -> the row's own name, first word
    assert map_variables({"name": "  Asha  Rani ", "data": {}}, ["name"], {}) == {"name": "Asha"}


def test_schedule_due():
    every = {"type": "SCHEDULE", "every_minutes": 30}
    assert schedule_due(every, None, NOW) is True
    assert schedule_due(every, NOW - timedelta(minutes=29), NOW) is False
    assert schedule_due(every, NOW - timedelta(minutes=30), NOW) is True

    wed_1030 = {"type": "SCHEDULE", "days": [3], "time": "10:30"}  # 0 = Sunday, as in the old app
    assert schedule_due(wed_1030, None, NOW) is True
    assert schedule_due(wed_1030, None, ist(10)) is False  # before the time
    assert schedule_due({**wed_1030, "days": [1, 2]}, None, NOW) is False  # not a Wednesday
    assert schedule_due({"type": "SCHEDULE", "time": "10:30"}, None, NOW) is True  # no days = every day
    assert schedule_due(wed_1030, ist(10, 31), NOW) is False  # already ran today
    assert schedule_due(wed_1030, ist(10, 31, day=-1), NOW) is True  # ran yesterday
    # "today" is the IST date: 01:00 IST on the 18th is still the 17th in UTC
    early = ist(1)
    assert early.date().day == 17
    assert schedule_due(wed_1030, early, NOW) is False

    with pytest.raises(ValueError, match="time"):
        schedule_due({"type": "SCHEDULE"}, None, NOW)


def test_rule_matches():
    call = {"outcome": "CONNECTED", "tag": "Qualified"}
    assert rule_matches({"when": {"tags": ["qualified"]}, "actions": []}, call) is True
    assert rule_matches({"when": {"outcomes": ["CONNECTED"]}, "actions": []}, call) is True
    assert rule_matches({"when": {"outcomes": ["NOT_PICKED"], "tags": ["angry"]}, "actions": []}, call) is False
    assert rule_matches({"when": {}, "actions": []}, call) is False
    assert rule_matches({"when": {"tags": ["qualified"]}, "actions": []}, {"outcome": "NOT_PICKED", "tag": None}) is False


# ---------------------------------------------------------------- run_workflow

def _run_setup(conn, phones=(1, 2, 3)):
    agent = make_agent(conn)
    uc = make_use_case(conn)
    ds = make_dataset(conn, use_case_id=uc["id"], rows=[
        {"phone": P(n), "name": f"Owner{n} Kumar", "society": "DLF Phase 1", "data": {}} for n in phones])
    wf = make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "MANUAL"})
    return agent, uc, ds, wf


def test_run_workflow_excludes_live_phones(conn):
    agent, uc, ds, wf = _run_setup(conn)
    live = make_campaign(conn, agent=agent, status="RUNNING", workflow_id=wf["id"])
    make_lead(conn, live, phone=P(1))
    done = make_campaign(conn, agent=agent, status="COMPLETED", workflow_id=wf["id"])
    make_lead(conn, done, phone=P(2))  # a finished campaign does not hold the phone
    other_wf = make_campaign(conn, agent=agent, status="RUNNING")
    make_lead(conn, other_wf, phone=P(3))  # neither does a live campaign of another workflow

    r = run_workflow(conn, wf, now=NOW)

    camp = _one(conn, "select * from campaign where id = %s", r["campaign_id"])
    assert [lead["phone"] for lead in _leads_of(conn, camp)] == [P(2), P(3)]
    assert r["leads"] == 2 and r["cooled"] == 0
    assert camp["workflow_id"] == wf["id"] and camp["use_case_id"] == uc["id"] and camp["dataset_id"] == ds["id"]
    assert camp["name"] == f"{wf['name']} · 18 Nov 11:00"
    assert camp["skip_cooling"] is False and camp["status"] == "SCHEDULED"
    run = _one(conn, "select * from scheduled_run where id = %s", r["run_id"])
    assert run["campaign_id"] == camp["id"] and run["status"] == "PLANNED" and run["workflow_id"] == wf["id"]
    assert r["planned_start"] == run["planned_start"] == NOW
    lead = _leads_of(conn, camp)[0]
    assert lead["variables"] == {"owner_name": "Owner2", "society_name": "DLF Phase 1"}
    assert lead["dataset_lead_id"] == _dataset_row(conn, ds, P(2))["id"] and lead["name"] == "Owner2 Kumar"
    assert _one(conn, "select last_run_at from workflow where id = %s", wf["id"])["last_run_at"] == NOW

    # everyone is now in a live campaign of this workflow -> nothing to run
    assert run_workflow(conn, wf, now=NOW + timedelta(minutes=5)) is None


def test_run_workflow_sets_skip_cooling(conn):
    agent, uc, ds, wf = _run_setup(conn, phones=(1,))
    r = run_workflow(conn, wf, ignore_cooling=True, now=NOW)
    assert _one(conn, "select skip_cooling from campaign where id = %s", r["campaign_id"])["skip_cooling"] is True


def test_run_workflow_waits_for_a_concurrent_run(conn):
    """A sync tick and an ingest running the same workflow: the second waits, then skips the first one's phones."""
    agent, uc, ds, wf = _run_setup(conn, phones=(1, 2))
    conn.commit()
    other = db.get_conn()
    result = {}
    worker = threading.Thread(target=lambda: result.update(r=run_workflow(conn, wf, now=NOW)))
    try:
        with other.cursor() as cur:  # the other run: holds the workflow, has inserted P1, not committed yet
            cur.execute("SELECT id FROM workflow WHERE id = %s FOR UPDATE", (wf["id"],))
            cur.execute("INSERT INTO campaign (name, agent_id, from_number, redial_policy, workflow_id) "
                        "VALUES ('other run', %s, '+918000000001', '{}', %s) RETURNING id", (agent["id"], wf["id"]))
            cur.execute("INSERT INTO lead (campaign_id, phone) VALUES (%s, %s)", (cur.fetchone()["id"], P(1)))
        worker.start()
        worker.join(0.5)
        assert worker.is_alive()  # blocked on the workflow row
        other.commit()
        worker.join(5)
    finally:
        other.close()
    assert not worker.is_alive()
    camp = _one(conn, "select * from campaign where id = %s", result["r"]["campaign_id"])
    assert [lead["phone"] for lead in _leads_of(conn, camp)] == [P(2)]


def test_run_workflow_calendar_path_is_one_transaction(conn):
    agent, uc, ds, wf = _run_setup(conn, phones=(1,))
    conn.commit()
    run_workflow(conn, wf, now=NOW)
    conn.rollback()  # the calendar path commits nothing: campaign, leads, run and stamp are the caller's
    assert _campaigns_of(conn, wf) == []
    assert _one(conn, "select count(*) as n from lead")["n"] == 0
    assert _one(conn, "select count(*) as n from scheduled_run")["n"] == 0
    assert _one(conn, "select last_run_at from workflow where id = %s", wf["id"])["last_run_at"] is None


def test_calendar_failure_leaves_no_draft_campaign(conn, monkeypatch):
    agent, uc, ds, wf = _run_setup(conn, phones=(1,))
    _set(conn, "workflow", wf["id"], trigger=Json({"type": "SCHEDULE", "every_minutes": 5}))

    def calendar_down(*a, **k):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(workflows, "plan_run", calendar_down)
    r = fire_triggers(conn, now=NOW)

    assert r == {"fired": [], "errors": [f"{wf['name']}: calendar down"]}
    assert _campaigns_of(conn, wf) == [] and _one(conn, "select count(*) as n from lead")["n"] == 0
    # nothing holds the phone, so the next tick takes it
    monkeypatch.undo()
    assert fire_triggers(conn, now=NOW + timedelta(minutes=5))["fired"][0]["leads"] == 1


def test_immediate_run_failing_before_the_dial_commits_nothing(conn, bolna_stub, monkeypatch):
    agent, uc, ds, wf = _new_lead_setup(conn, NOW)

    def no_department(*a, **k):
        raise RuntimeError("department SUPPLY not found")

    monkeypatch.setattr(workflows, "dept_config", no_department)
    r = fire_triggers(conn, now=NOW, dataset_id=ds["id"])

    assert r["errors"] == [f"{wf['name']}: department SUPPLY not found"]
    assert _campaigns_of(conn, wf) == [] and _stub_calls(bolna_stub, "create_batch") == []


# ---------------------------------------------------------------- ON_NEW_LEAD

def _new_lead_setup(conn, now):
    agent = make_agent(conn)
    uc = make_use_case(conn)
    ds = make_dataset(conn, use_case_id=uc["id"], rows=[
        {"phone": P(1), "name": "Asha Rani", "society": "DLF Phase 1", "data": {"owner_name": "Asha Rani"}},
        {"phone": P(2), "name": "Old Lead"},
        {"phone": P(3), "name": "Blocked"},
    ])
    _added_at(conn, ds, P(1), now - timedelta(minutes=1))
    _added_at(conn, ds, P(2), now - timedelta(hours=25))  # before the first-run lookback
    _added_at(conn, ds, P(3), now - timedelta(minutes=1))
    mark_dnc(conn, P(3), "manual")
    wf = make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "ON_NEW_LEAD", "within_minutes": 2})
    # P1 was spoken to 2 days ago in another campaign: cooling would skip it, a new-lead run must not
    make_call(conn, phone=P(1), outcome="CONNECTED", started_at=now - timedelta(days=2),
              campaign=make_campaign(conn, agent=agent))
    return agent, uc, ds, wf


def test_on_new_lead_dials_immediately_without_calendar(conn, bolna_stub):
    agent, uc, ds, wf = _new_lead_setup(conn, NOW)
    # a due SCHEDULE workflow on the same dataset is not part of a dataset-scoped fire
    sched = make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "SCHEDULE", "every_minutes": 5})

    r = fire_triggers(conn, now=NOW, dataset_id=ds["id"])

    assert r == {"fired": [{"workflow": wf["name"], "type": "ON_NEW_LEAD", "leads": 1}], "errors": []}
    [(_, from_number, csv_text, _)] = _stub_calls(bolna_stub, "create_batch")
    assert _csv_phones(csv_text) == [P(1)] and from_number == "+918000000001"
    assert csv_text.splitlines()[1] == f"{P(1)},Asha,DLF Phase 1"
    [(_, when)] = _stub_calls(bolna_stub, "schedule_batch")
    assert when == NOW + timedelta(seconds=60)
    assert _one(conn, "select count(*) as n from scheduled_run")["n"] == 0
    [camp] = _campaigns_of(conn, wf)
    assert camp["skip_cooling"] is True and camp["status"] == "RUNNING"
    assert camp["name"] == f"{wf['name']} · new leads 1"
    assert [lead["status"] for lead in _leads_of(conn, camp)] == ["QUEUED"]
    assert _campaigns_of(conn, sched) == []

    # the same lead arriving again (ingest twice, or the next sync tick) dials nobody
    r2 = fire_triggers(conn, now=NOW + timedelta(minutes=1), dataset_id=ds["id"])
    r3 = fire_triggers(conn, now=NOW + timedelta(minutes=5))
    assert r2 == {"fired": [], "errors": []}
    assert [f["type"] for f in r3["fired"]] == ["SCHEDULE"]  # only the schedule workflow, on the calendar
    assert len(_stub_calls(bolna_stub, "create_batch")) == 1


def test_on_new_lead_outside_window_waits_on_the_calendar(conn, bolna_stub):
    """Nothing is pre-scheduled at Bolna overnight: the run goes on the calendar for the window start and dispatch
    dials it then (after the DNC guard of that moment)."""
    evening = ist(20)
    agent, uc, ds, wf = _new_lead_setup(conn, evening)

    r = fire_triggers(conn, now=evening, dataset_id=ds["id"])

    assert r["errors"] == [] and r["fired"][0]["leads"] == 1
    assert bolna_stub.calls == []
    [camp] = _campaigns_of(conn, wf)
    assert camp["status"] == "SCHEDULED" and camp["skip_cooling"] is True
    [run] = _all(conn, "select * from scheduled_run")
    assert (run["status"], run["campaign_id"], run["workflow_id"]) == ("PLANNED", camp["id"], wf["id"])
    assert run["requested_start"] == ist(10, day=1) and run["planned_start"] == ist(10, day=1)

    mark_dnc(conn, P(1), "manual")  # blocked overnight
    conn.commit()
    scheduler.dispatch_due_runs(conn, now=ist(10, day=1))
    assert _stub_calls(bolna_stub, "create_batch") == []
    assert [lead["status"] for lead in _leads_of(conn, camp)] == ["DNC"]


def test_on_new_lead_bolna_failure_falls_back_to_calendar(conn, bolna_stub):
    agent, uc, ds, wf = _new_lead_setup(conn, NOW)
    bolna_stub.fail_on = {"create_batch"}

    r = fire_triggers(conn, now=NOW, dataset_id=ds["id"])

    assert r["fired"] == [] and len(r["errors"]) == 1
    assert r["errors"][0].startswith(f"{wf['name']}: Bolna 500")
    [camp] = _campaigns_of(conn, wf)
    assert camp["status"] == "SCHEDULED"
    [run] = _all(conn, "select * from scheduled_run")
    assert (run["status"], run["campaign_id"], run["workflow_id"]) == ("PLANNED", camp["id"], wf["id"])
    assert run["note"].startswith("immediate start failed: Bolna 500") and run["planned_start"] == NOW
    assert [lead["status"] for lead in _leads_of(conn, camp)] == ["PENDING"]

    # the calendar retries it on the next tick (cooling still skipped)
    bolna_stub.fail_on = set()
    scheduler.dispatch_due_runs(conn, now=NOW + timedelta(minutes=5))
    assert [_csv_phones(a[2]) for a in _stub_calls(bolna_stub, "create_batch")] == [[P(1)], [P(1)]]
    assert [lead["status"] for lead in _leads_of(conn, camp)] == ["QUEUED"]


def test_on_new_lead_failure_that_may_still_dial_is_not_rescheduled(conn, bolna_stub):
    agent, uc, ds, wf = _new_lead_setup(conn, NOW)
    bolna_stub.fail_on = {"schedule_batch", "stop_batch"}  # Bolna has the batch and it could not be stopped

    r = fire_triggers(conn, now=NOW, dataset_id=ds["id"])

    assert len(r["errors"]) == 1 and r["errors"][0].startswith(f"{wf['name']}: Bolna 500")
    [camp] = _campaigns_of(conn, wf)
    assert camp["status"] == "RUNNING"  # create_round marked it live; the sync follows the batch
    assert _one(conn, "select count(*) as n from scheduled_run")["n"] == 0
    assert [lead["status"] for lead in _leads_of(conn, camp)] == ["QUEUED"]


def test_immediate_run_with_nobody_dialable_completes(conn, bolna_stub):
    agent, uc, ds, wf = _run_setup(conn, phones=(1,))
    mark_dnc(conn, P(1), "manual")  # e.g. blocked between the new-lead query and the dial

    r = run_workflow(conn, wf, explicit_leads=[_dataset_row(conn, ds, P(1))], ignore_cooling=True, immediate=True,
                     now=NOW)

    camp = _one(conn, "select * from campaign where id = %s", r["campaign_id"])
    assert camp["status"] == "COMPLETED" and camp["completed_at"] == NOW
    assert r["run_id"] is None and r["planned_start"] is None
    assert [lead["status"] for lead in _leads_of(conn, camp)] == ["DNC"]
    assert _stub_calls(bolna_stub, "create_batch") == []


def test_on_new_lead_catches_rows_committed_late(conn, bolna_stub):
    """A row whose insert committed after the last run, with an earlier created_at, is still picked up --
    and the look-back overlap never takes a phone this workflow already had, even from a finished campaign."""
    agent, uc, ds, wf = _new_lead_setup(conn, NOW)
    fire_triggers(conn, now=NOW, dataset_id=ds["id"])
    [first] = _campaigns_of(conn, wf)
    _set(conn, "campaign", first["id"], status="STOPPED")  # P1 never called, and no longer in a live campaign
    _insert(conn, "dataset_lead", dataset_id=ds["id"], phone=P(4), name="Late Row", data=Json({}),
            created_at=NOW - timedelta(seconds=5))

    r = fire_triggers(conn, now=NOW + timedelta(minutes=5), dataset_id=ds["id"])

    assert r == {"fired": [{"workflow": wf["name"], "type": "ON_NEW_LEAD", "leads": 1}], "errors": []}
    assert [_csv_phones(a[2]) for a in _stub_calls(bolna_stub, "create_batch")] == [[P(1)], [P(4)]]


def test_trigger_errors_are_reported_and_do_not_stop_others(conn, bolna_stub):
    agent, uc, ds, wf = _new_lead_setup(conn, NOW)
    broken = make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "SCHEDULE"})  # no time
    cron = make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "CRON"})
    make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "MANUAL"})  # silently skipped

    r = fire_triggers(conn, now=NOW)

    assert sorted(r["errors"]) == sorted([f"{broken['name']}: SCHEDULE trigger needs time or every_minutes",
                                          f"{cron['name']}: unknown trigger type CRON"])
    assert r["fired"] == [{"workflow": wf["name"], "type": "ON_NEW_LEAD", "leads": 1}]


# ---------------------------------------------------------------- AFTER_STEP

def _after_step_setup(conn):
    agent = make_agent(conn)
    uc = make_use_case(conn)
    parent = make_workflow(conn, agent=agent, use_case=uc, trigger={"type": "MANUAL"})
    pc = make_campaign(conn, agent=agent, workflow_id=parent["id"], use_case_id=uc["id"], status="COMPLETED")
    child = make_workflow(conn, agent=agent, use_case=uc, parent=parent, step_order=1,
                          trigger={"type": "AFTER_STEP", "delay_hours": 48, "when_tags": ["qualified"]})

    def called(n, *, tag, hours_ago):
        lead = make_lead(conn, pc, phone=P(n), attempts=1, final_tag=tag,
                         variables={"owner_name": f"Owner{n} Kumar", "society_name": "Gaur City"})
        make_call(conn, phone=P(n), tag=tag, started_at=NOW - timedelta(hours=hours_ago), campaign=pc, lead=lead)

    return child, called


def test_after_step_fires_once_after_delay(conn, bolna_stub):
    child, called = _after_step_setup(conn)
    called(1, tag="qualified", hours_ago=49)
    called(2, tag="qualified", hours_ago=47)  # not yet due
    called(3, tag="not_interested", hours_ago=49)  # never

    r = fire_triggers(conn, now=NOW)

    assert r == {"fired": [{"workflow": child["name"], "type": "AFTER_STEP", "leads": 1}], "errors": []}
    [camp] = _campaigns_of(conn, child)
    [lead] = _leads_of(conn, camp)
    assert lead["phone"] == P(1) and lead["variables"] == {"owner_name": "Owner1", "society_name": "Gaur City"}
    assert camp["skip_cooling"] is True and camp["status"] == "SCHEDULED" and camp["name"] == f"{child['name']} · step 1 · 1"
    assert _one(conn, "select status from scheduled_run where campaign_id = %s", camp["id"])["status"] == "PLANNED"
    assert _stub_calls(bolna_stub, "create_batch") == []  # on the calendar, not dialled at fire time

    assert fire_triggers(conn, now=NOW + timedelta(minutes=5)) == {"fired": [], "errors": []}
    # P2 becomes due later and gets its own run; P1 is never taken again
    later = fire_triggers(conn, now=NOW + timedelta(hours=2))
    assert later["fired"] == [{"workflow": child["name"], "type": "AFTER_STEP", "leads": 1}]
    assert [_leads_of(conn, c)[0]["phone"] for c in _campaigns_of(conn, child)] == [P(1), P(2)]


def test_after_step_without_tags_or_outcomes_is_an_error_not_every_call(conn, bolna_stub):
    child, called = _after_step_setup(conn)
    called(1, tag="qualified", hours_ago=49)
    _set(conn, "workflow", child["id"], trigger=Json({"type": "AFTER_STEP", "delay_hours": 48}))
    conn.commit()

    r = fire_triggers(conn, now=NOW)

    assert r == {"fired": [], "errors": [f"{child['name']}: AFTER_STEP trigger needs when_tags or when_outcomes"]}
    assert _campaigns_of(conn, child) == []


def test_after_step_never_dials_dnc(conn, bolna_stub):
    child, called = _after_step_setup(conn)
    called(1, tag="qualified", hours_ago=49)
    called(2, tag="qualified", hours_ago=49)
    mark_dnc(conn, P(2), "manual")

    fire_triggers(conn, now=NOW)
    scheduler.dispatch_due_runs(conn, now=NOW)

    [camp] = _campaigns_of(conn, child)
    assert {lead["phone"]: lead["status"] for lead in _leads_of(conn, camp)} == {P(1): "QUEUED", P(2): "DNC"}
    [(_, _, csv_text, _)] = _stub_calls(bolna_stub, "create_batch")
    assert _csv_phones(csv_text) == [P(1)]


# ---------------------------------------------------------------- result rules

def _rules_setup(conn, *, tag, outcome="CONNECTED", status="completed", started_at=NOW - timedelta(minutes=30),
                 rules=(), extracted=None, summary=None):
    agent = make_agent(conn)
    uc = make_use_case(conn)
    ds = make_dataset(conn, use_case_id=uc["id"], rows=[{"phone": P(1), "name": "Rajendra Kumar"}])
    wf = make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "MANUAL"}, rules=rules)
    camp = make_campaign(conn, agent=agent, workflow_id=wf["id"], use_case_id=uc["id"])
    lead = make_lead(conn, camp, phone=P(1), variables={"owner_name": "Rajendra", "society_name": "Gaur City"},
                     dataset_lead_id=_dataset_row(conn, ds, P(1))["id"])
    lead = _set(conn, "lead", lead["id"], name="Rajendra Kumar")
    call = make_call(conn, phone=P(1), outcome=outcome, status=status, started_at=started_at, campaign=camp,
                     lead=lead, tag=tag)
    call = _set(conn, "call", call["id"], extracted=Json(extracted) if extracted else None, summary=summary)
    conn.commit()
    return call, camp, lead, uc, ds


def _call(conn, call):
    return _one(conn, "select * from call where id = %s", call["id"])


def _lead(conn, lead):
    return _one(conn, "select * from lead where id = %s", lead["id"])


def test_rules_good_tag_creates_one_handoff(conn):
    call, camp, lead, uc, ds = _rules_setup(conn, tag="qualified", extracted={
        "Seller Details": {"configuration": {"objective": "3 BHK"}, "owner_questions": {"objective": "When is the visit?"}}})

    assert apply_result_rules(conn, call, camp, lead, now=NOW) == ["handoff"]

    [h] = _all(conn, "select * from handoff")
    assert (h["department"], h["use_case_id"], h["campaign_id"], h["call_id"], h["phone"]) == (
        "SUPPLY", uc["id"], camp["id"], call["id"], P(1))
    assert (h["name"], h["tag"], h["team"], h["status"]) == ("Rajendra Kumar", "qualified", uc["name"], "OPEN")
    assert h["summary"] == "When is the visit?" and h["due_at"] == NOW + timedelta(hours=24)
    assert h["details"] == {"owner_name": "Rajendra", "society_name": "Gaur City", "configuration": "3 BHK",
                            "owner_questions": "When is the visit?"}
    assert _call(conn, call)["rules_applied_at"] == NOW
    row = _dataset_row(conn, ds, P(1))  # the dataset mirror
    assert (row["last_tag"], row["last_outcome"], row["total_attempts"]) == ("qualified", "CONNECTED", 1)
    assert row["last_called_at"] == call["started_at"]

    # the second pass -- even with the stale call row -- is stopped by rules_applied_at
    assert apply_result_rules(conn, call, camp, lead, now=NOW + timedelta(minutes=5)) == []
    assert apply_result_rules(conn, _call(conn, call), camp, lead, now=NOW + timedelta(minutes=5)) == []
    assert _one(conn, "select count(*) as n from handoff")["n"] == 1
    assert _dataset_row(conn, ds, P(1))["total_attempts"] == 1
    # one hand-off per phone per campaign
    assert create_handoff(conn, call, camp, uc, lead, {}, now=NOW) is False


def test_rules_dnc_tag_blocks_everywhere(conn):
    call, camp, lead, uc, ds = _rules_setup(conn, tag="angry")

    assert apply_result_rules(conn, call, camp, lead, now=NOW) == ["dnc"]

    dnc = _one(conn, "select * from dnc_phone where phone = %s", P(1))
    assert dnc["source"] == "use_case_tag"
    assert _lead(conn, lead)["status"] == "DNC"
    assert _one(conn, "select count(*) as n from handoff")["n"] == 0


def test_rules_dnc_tag_blocks_without_a_use_case(conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)  # ad hoc: no use case, no workflow
    lead = make_lead(conn, camp, phone=P(1))
    call = make_call(conn, phone=P(1), started_at=NOW - timedelta(minutes=30), campaign=camp, lead=lead, tag="Angry")
    conn.commit()

    assert apply_result_rules(conn, call, camp, lead, now=NOW) == ["dnc"]

    dnc = _one(conn, "select * from dnc_phone where phone = %s", P(1))
    assert (dnc["source"], dnc["reason"]) == ("call_tag", "Angry")
    assert _lead(conn, lead)["status"] == "DNC"


def test_rules_dnc_tag_blocks_even_when_the_taxonomy_omits_it(conn):
    agent = make_agent(conn)
    uc = make_use_case(conn, tag_taxonomy={"good": ["qualified"], "soft": [], "terminal": [], "dnc": [], "retry": []})
    camp = make_campaign(conn, agent=agent, use_case_id=uc["id"])
    lead = make_lead(conn, camp, phone=P(1))
    call = make_call(conn, phone=P(1), started_at=NOW - timedelta(minutes=30), campaign=camp, lead=lead,
                     tag="wrong_person")
    conn.commit()

    assert apply_result_rules(conn, call, camp, lead, now=NOW) == ["dnc"]
    assert _one(conn, "select source from dnc_phone where phone = %s", P(1))["source"] == "call_tag"


def test_rules_dnc_reports_live_batches_still_holding_the_phone(conn):
    call, camp, lead, uc, ds = _rules_setup(conn, tag="angry")
    other = make_campaign(conn, agent=make_agent(conn))
    held = make_lead(conn, other, phone=P(1), status="QUEUED")  # in another campaign's live batch
    conn.commit()

    res = apply_result_rules(conn, call, camp, lead, now=NOW)

    assert res == ["dnc", f"dnc:queued_in:{other['name']} ({other['id']})"]
    assert _lead(conn, held)["status"] == "QUEUED"  # DNC never takes it out of Bolna's batch: pausing does


def test_rules_redial_after(conn):
    rules = [{"when": {"tags": ["callback_requested"]}, "actions": [{"type": "redial", "after_minutes": 60}]}]
    call, camp, lead, uc, ds = _rules_setup(conn, tag="callback_requested", rules=rules)

    res = apply_result_rules(conn, call, camp, lead, now=NOW)

    assert res == ["handoff", "redial"]  # callback_requested is a soft tag, then the explicit rule
    updated = _lead(conn, lead)
    assert updated["status"] == "NOT_CONNECTED" and updated["next_dial_at"] == NOW + timedelta(minutes=60)


def test_rules_dnc_stop_and_bad_actions(conn):
    rules = [
        {"when": {"outcomes": ["CONNECTED"]}, "actions": [
            {"type": "stop"}, {"type": "dnc"}, {"type": "redial"}, {"type": "crm_push"}, {"type": "next_step"}]},
        {"when": {"tags": ["not_interested"]}, "actions": [{"type": "redial", "after_minutes": 30}]},
    ]
    call, camp, lead, uc, ds = _rules_setup(conn, tag="not_interested", rules=rules)

    res = apply_result_rules(conn, call, camp, lead, now=NOW)

    assert res == ["stop", "dnc", "redial:error:redial needs after_minutes",
                   "crm_push:error:unknown action type crm_push", "redial"]
    assert _one(conn, "select source from dnc_phone where phone = %s", P(1))["source"] == "rule"
    updated = _lead(conn, lead)
    assert updated["status"] == "DNC" and updated["next_dial_at"] is None  # a later redial never un-blocks
    assert _call(conn, call)["rules_applied_at"] == NOW


def test_rules_never_touch_a_queued_lead(conn):
    rules = [{"when": {"tags": ["not_interested"]},
              "actions": [{"type": "stop"}, {"type": "redial", "after_minutes": 30}]}]
    call, camp, lead, uc, ds = _rules_setup(conn, tag="not_interested", rules=rules)
    in_batch = NOW + timedelta(minutes=2)
    _set(conn, "lead", lead["id"], status="QUEUED", next_dial_at=in_batch)  # already in the next round's batch

    res = apply_result_rules(conn, call, camp, lead, now=NOW)

    assert res == ["stop:skipped:queued", "redial:skipped:queued"]
    updated = _lead(conn, lead)
    assert updated["status"] == "QUEUED" and updated["next_dial_at"] == in_batch


def test_stale_call_leaves_the_lead_but_still_blocks(conn):
    rules = [{"when": {"tags": ["not_interested"]},
              "actions": [{"type": "redial", "after_minutes": 30}, {"type": "stop"}, {"type": "dnc"}]}]
    call, camp, lead, uc, ds = _rules_setup(conn, tag="not_interested", rules=rules)
    _set(conn, "lead", lead["id"], status="CONNECTED", final_tag="qualified")  # set by a newer call

    newer = NOW - timedelta(minutes=5)
    _set(conn, "dataset_lead", lead["dataset_lead_id"], last_called_at=newer, last_outcome="CONNECTED",
         last_tag="qualified", total_attempts=2)

    res = apply_result_rules(conn, call, camp, lead, now=NOW, stale=True)

    assert res == ["redial:skipped:stale", "stop:skipped:stale", "dnc"]
    updated = _lead(conn, lead)
    assert updated["status"] == "DNC" and updated["next_dial_at"] is None  # DNC still applies
    assert _one(conn, "select source from dnc_phone where phone = %s", P(1))["source"] == "rule"
    row = _one(conn, "select * from dataset_lead where id = %s", lead["dataset_lead_id"])
    assert (row["last_called_at"], row["last_outcome"], row["last_tag"], row["total_attempts"]) == (
        newer, "CONNECTED", "qualified", 3)  # the newer call's fields stay; the attempt counts


def test_handoff_unique_per_phone_and_campaign(conn):
    import psycopg2

    call, camp, lead, uc, ds = _rules_setup(conn, tag="qualified")
    assert create_handoff(conn, call, camp, uc, lead, {}, now=NOW) is True
    assert create_handoff(conn, call, camp, uc, lead, {}, now=NOW) is False
    with pytest.raises(psycopg2.errors.UniqueViolation):  # the index holds for any writer, not just create_handoff
        _insert(conn, "handoff", department="SUPPLY", campaign_id=camp["id"], phone=P(1))
    conn.rollback()
    for _ in range(2):  # hand-offs without a campaign are not limited
        _insert(conn, "handoff", department="SUPPLY", phone=P(1))
    assert _one(conn, "select count(*) as n from handoff where campaign_id is null")["n"] == 2


def test_rules_wait_for_extraction(conn):
    call, camp, lead, uc, ds = _rules_setup(conn, tag=None, started_at=NOW - timedelta(minutes=5))

    assert apply_result_rules(conn, call, camp, lead, now=NOW) == []
    assert _call(conn, call)["rules_applied_at"] is None
    assert _dataset_row(conn, ds, P(1))["total_attempts"] == 0

    # still untagged after 10 minutes: the rules run with what there is
    assert apply_result_rules(conn, call, camp, lead, now=NOW + timedelta(minutes=6)) == []
    assert _call(conn, call)["rules_applied_at"] == NOW + timedelta(minutes=6)
    assert _dataset_row(conn, ds, P(1))["total_attempts"] == 1


def test_rules_skip_unfinished_calls(conn):
    call, camp, lead, uc, ds = _rules_setup(conn, tag="qualified", status="in-progress")
    assert apply_result_rules(conn, call, camp, lead, now=NOW) == []
    assert _call(conn, call)["rules_applied_at"] is None


WHATSAPP_RULE = [{"when": {"tags": ["qualified"]}, "actions": [{"type": "whatsapp", "template": "seller_followup"}]}]


def test_rules_whatsapp_error_recorded(conn, monkeypatch):
    call, camp, lead, uc, ds = _rules_setup(conn, tag="qualified", rules=WHATSAPP_RULE)

    def boom(*a, **k):
        raise RuntimeError("Interakt 500: upstream down")

    monkeypatch.setattr(whatsapp, "send_template", boom)
    res = apply_result_rules(conn, call, camp, lead, now=NOW)

    assert res == ["handoff", "whatsapp:error:Interakt 500: upstream down"]
    assert _call(conn, call)["rules_applied_at"] == NOW
    assert _one(conn, "select count(*) as n from handoff")["n"] == 1


def test_rules_whatsapp_sent_after_db_writes(conn, monkeypatch):
    call, camp, lead, uc, ds = _rules_setup(conn, tag="qualified", rules=WHATSAPP_RULE)
    sent = []

    def send(phone, template, body_values, language_code="en"):
        other = db.get_conn()  # what another connection sees at send time
        try:
            with other.cursor() as cur:
                cur.execute("select rules_applied_at from call where id = %s", (call["id"],))
                applied = cur.fetchone()["rules_applied_at"]
                cur.execute("select count(*) as n from handoff")
                handoffs = cur.fetchone()["n"]
        finally:
            other.close()
        sent.append((phone, template, body_values, applied, handoffs))
        return "{}"

    monkeypatch.setattr(whatsapp, "send_template", send)
    res = apply_result_rules(conn, call, camp, lead, now=NOW)

    assert res == ["handoff", "whatsapp:seller_followup"]
    assert sent == [(P(1), "seller_followup", ["Rajendra Kumar", "Gaur City"], NOW, 1)]


# ---------------------------------------------------------------- whatsapp client

class _Res:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def test_send_template_payload(monkeypatch):
    posts = []
    monkeypatch.setattr(config, "INTERAKT_API_KEY", "key123")
    monkeypatch.setattr(config, "INTERAKT_FROM", "")
    monkeypatch.setattr(whatsapp.requests, "post",
                        lambda url, **kw: posts.append((url, kw)) or _Res(201, '{"result": true}'))

    assert whatsapp.whatsapp_enabled() is True
    assert whatsapp.send_template("+919810043058", "seller_followup", ["Asha", "DLF"]) == '{"result": true}'

    [(url, kw)] = posts
    assert url == "https://api.interakt.ai/v1/public/message/"
    assert kw["headers"]["Authorization"] == "Basic key123"
    assert kw["json"] == {"countryCode": "+91", "phoneNumber": "9810043058", "callbackData": "calling-console",
                          "type": "Template",
                          "template": {"name": "seller_followup", "languageCode": "en", "bodyValues": ["Asha", "DLF"]}}
    assert kw["timeout"] > 0


def test_send_template_errors(monkeypatch):
    monkeypatch.setattr(config, "INTERAKT_API_KEY", "")
    assert whatsapp.whatsapp_enabled() is False
    with pytest.raises(RuntimeError, match="INTERAKT_API_KEY"):
        whatsapp.send_template("+919810043058", "t", [])

    monkeypatch.setattr(config, "INTERAKT_API_KEY", "key123")
    monkeypatch.setattr(config, "INTERAKT_FROM", "+918000000009")
    posts = []
    monkeypatch.setattr(whatsapp.requests, "post",
                        lambda url, **kw: posts.append(kw) or _Res(400, "template not approved"))
    with pytest.raises(RuntimeError, match="Interakt 400: template not approved"):
        whatsapp.send_template("+919810043058", "t", [])
    assert posts[0]["json"]["from"] == "+918000000009"
