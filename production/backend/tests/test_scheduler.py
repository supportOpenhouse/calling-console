from datetime import datetime, timedelta, timezone

import pytest

from ..services.datasets import mark_dnc
from ..services import scheduler, settings_store
from ..services.ist import IST
from ..services.scheduler import estimate_minutes, place_runs
from .factories import _insert, make_batch, make_agent, make_campaign, make_lead

def ist(h, m=0, day=0):
    return (datetime(2026, 11, 18 + day, h, m, tzinfo=IST)).astimezone(timezone.utc)


NOW = ist(10, 30)


def _run(rid, requested, minutes, leads=0):
    return {"id": rid, "requested_start": requested, "est_minutes": minutes, "est_leads": leads}


def _place(runs, now, busy_until=None):
    return place_runs(runs, now=now, busy_until=busy_until, window_start="10:00", window_end="19:00", line_share=5)


def test_estimate_minutes():
    assert estimate_minutes(0, 5) == 3
    assert estimate_minutes(100, 5) == max(3, -(-100 * 55 // 5 // 60) + 2)
    assert estimate_minutes(10, 0) == estimate_minutes(10, 1)


def test_place_runs_back_to_back_inside_window():
    out = _place([_run("a", ist(10), 60), _run("b", ist(10), 30)], ist(10))
    assert out == [("a", ist(10), ist(11)), ("b", ist(11), ist(11, 30))]


def test_place_runs_spills_to_next_morning():
    out = _place([_run("a", ist(18), 120)], ist(18))
    assert out == [("a", ist(18), ist(11, day=1))]


def test_place_runs_waits_for_running_end():
    out = _place([_run("a", ist(10), 30)], ist(10), busy_until=ist(12))
    assert out[0][1] == ist(12)


def test_place_runs_rejects_bad_window():
    with pytest.raises(ValueError):
        place_runs([_run("a", ist(10), 30)], now=ist(10), busy_until=None,
                   window_start="19:00", window_end="10:00", line_share=5)


def _db_run(conn, campaign, *, dept="SUPPLY", priority=5, requested=NOW, status="PLANNED", minutes=30,
            planned_start=None):
    return _insert(conn, "scheduled_run", department=dept, campaign_id=campaign["id"], priority=priority,
                   requested_start=requested, est_minutes=minutes, status=status, planned_start=planned_start)


def _row(conn, rid):
    with conn.cursor() as cur:
        cur.execute("select * from scheduled_run where id = %s", (rid,))
        return cur.fetchone()


def test_replan_orders_priority_then_requested(conn):
    ag = make_agent(conn)
    c1, c2, c3 = (make_campaign(conn, agent=ag, status="SCHEDULED") for _ in range(3))
    low = _db_run(conn, c1, priority=5, requested=NOW)
    high_late = _db_run(conn, c2, priority=1, requested=NOW + timedelta(minutes=10))
    high_early = _db_run(conn, c3, priority=1, requested=NOW)
    plan = scheduler.replan_department(conn, "SUPPLY", now=NOW)
    assert [r["id"] for r in plan] == [high_early["id"], high_late["id"], low["id"]]
    assert plan[0]["planned_start"] == NOW
    assert plan[0]["planned_end"] == NOW + timedelta(minutes=30)
    assert plan[1]["planned_start"] == NOW + timedelta(minutes=30)
    stored = _row(conn, low["id"])
    assert stored["planned_start"] == plan[2]["planned_start"] and stored["lines"] == 5


def test_replan_respects_running_run(conn):
    ag = make_agent(conn)
    c1, c2 = (make_campaign(conn, agent=ag) for _ in range(2))
    _db_run(conn, c1, status="RUNNING", planned_start=NOW)
    with conn.cursor() as cur:
        cur.execute("update scheduled_run set planned_end = %s", (NOW + timedelta(hours=1),))
    p = _db_run(conn, c2)
    plan = scheduler.replan_department(conn, "SUPPLY", now=NOW)
    assert plan[0]["id"] == p["id"] and plan[0]["planned_start"] == NOW + timedelta(hours=1)


def test_dispatch_starts_due_and_cancels_stopped(conn, bolna_stub):
    ag = make_agent(conn)
    ok = make_campaign(conn, agent=ag, status="SCHEDULED")
    make_lead(conn, ok, phone="+919810000001")
    stopped = make_campaign(conn, agent=ag, status="STOPPED")
    r_ok = _db_run(conn, ok, priority=1)
    r_stopped = _db_run(conn, stopped, dept="DEMAND")
    started = scheduler.dispatch_due_runs(conn, now=NOW)
    assert started == [r_ok["id"]]
    assert _row(conn, r_ok["id"])["status"] == "RUNNING"
    assert _row(conn, r_stopped["id"])["status"] == "CANCELLED"
    assert any(name == "create_batch" for name, _ in bolna_stub.calls)


def test_dispatch_completes_campaign_with_nothing_to_dial(conn, bolna_stub):
    ag = make_agent(conn)
    c = make_campaign(conn, agent=ag, status="SCHEDULED")
    for i in range(2):
        make_lead(conn, c, phone=f"+91981000000{i}")
        mark_dnc(conn, f"+91981000000{i}", "manual")
    r = _db_run(conn, c)
    assert scheduler.dispatch_due_runs(conn, now=NOW) == []
    assert _row(conn, r["id"])["status"] == "DONE"
    with conn.cursor() as cur:
        cur.execute("select status, completed_at from campaign where id = %s", (c["id"],))
        assert dict(cur.fetchone()) == {"status": "COMPLETED", "completed_at": NOW}
    assert not any(name == "create_batch" for name, _ in bolna_stub.calls)


def test_dispatch_records_start_failure(conn, bolna_stub):
    ag = make_agent(conn)
    c = make_campaign(conn, agent=ag, status="SCHEDULED")
    make_lead(conn, c, phone="+919810000001")
    r = _db_run(conn, c)
    bolna_stub.fail_on.add("create_batch")
    errors = []
    assert scheduler.dispatch_due_runs(conn, now=NOW, errors=errors) == []
    row = _row(conn, r["id"])
    assert row["status"] == "PLANNED" and row["note"].startswith("start failed: ")
    assert errors == [f"run {r['id']}: start failed: Bolna 500: <html>"]
    assert scheduler.dispatch_due_runs(conn, now=NOW) == []  # without a list the note still records it


def test_dispatch_closes_runs_of_finished_campaigns(conn):
    ag = make_agent(conn)
    done = make_campaign(conn, agent=ag, status="COMPLETED")
    stopped = make_campaign(conn, agent=ag, status="STOPPED")
    r1 = _db_run(conn, done, status="RUNNING")
    r2 = _db_run(conn, stopped, status="RUNNING")
    scheduler.dispatch_due_runs(conn, now=NOW)
    assert _row(conn, r1["id"])["status"] == "DONE"
    assert _row(conn, r2["id"])["status"] == "CANCELLED"


def test_cancel_run_returns_campaign_to_draft(conn):
    ag = make_agent(conn)
    c = make_campaign(conn, agent=ag, status="SCHEDULED")
    r = _db_run(conn, c)
    scheduler.cancel_run(conn, r["id"], now=NOW)
    assert _row(conn, r["id"])["status"] == "CANCELLED"
    with conn.cursor() as cur:
        cur.execute("select status, scheduled_at from campaign where id = %s", (c["id"],))
        assert dict(cur.fetchone()) == {"status": "DRAFT", "scheduled_at": None}


def test_plan_run_counts_pending_and_schedules(conn):
    ag = make_agent(conn)
    c = make_campaign(conn, agent=ag, status="DRAFT")
    for i in range(3):
        make_lead(conn, c, phone=f"+91981000000{i}")
    make_lead(conn, c, phone="+919810000009", status="DONE")
    run = scheduler.plan_run(conn, dept="SUPPLY", campaign_id=c["id"], requested_start=NOW, now=NOW)
    assert run["est_leads"] == 3 and run["est_minutes"] == estimate_minutes(3, 5)
    assert run["planned_start"] == NOW
    with conn.cursor() as cur:
        cur.execute("select status, scheduled_at from campaign where id = %s", (c["id"],))
        row = cur.fetchone()
    assert row["status"] == "SCHEDULED" and row["scheduled_at"] == NOW


def test_set_run_priority_and_runs_between(conn):
    ag = make_agent(conn)
    c1, c2 = (make_campaign(conn, agent=ag, status="SCHEDULED") for _ in range(2))
    a = _db_run(conn, c1)
    b = _db_run(conn, c2)
    plan = scheduler.set_run_priority(conn, b["id"], 1, now=NOW)
    assert plan[0]["id"] == b["id"]
    rows = scheduler.runs_between(conn, NOW - timedelta(hours=1), NOW + timedelta(hours=3), dept="SUPPLY")
    assert {r["id"] for r in rows} == {a["id"], b["id"]}
    assert rows[0]["campaign_status"] == "SCHEDULED" and rows[0]["campaign_name"].startswith("Campaign")
    assert scheduler.runs_between(conn, NOW - timedelta(hours=1), NOW + timedelta(hours=3), dept="DEMAND") == []


def test_settings_roundtrip(conn):
    assert settings_store.get_setting(conn, "x") is None
    settings_store.set_setting(conn, "x", {"a": 1})
    settings_store.set_setting(conn, "x", [2])
    assert settings_store.get_setting(conn, "x") == [2]


def test_dispatch_failure_with_live_batch_marks_run_running(conn, bolna_stub):
    ag = make_agent(conn)
    c = make_campaign(conn, agent=ag, status="SCHEDULED")
    make_lead(conn, c, phone="+919810000001")
    r = _db_run(conn, c)
    bolna_stub.fail_on = {"schedule_batch", "stop_batch"}
    assert scheduler.dispatch_due_runs(conn, now=NOW) == []
    row = _row(conn, r["id"])
    assert row["status"] == "RUNNING" and row["note"].startswith("start failed: ")
    scheduler.dispatch_due_runs(conn, now=NOW + timedelta(minutes=5))
    with conn.cursor() as cur:
        cur.execute("select status from campaign where id = %s", (c["id"],))
        assert cur.fetchone()["status"] != "COMPLETED"
    assert _row(conn, r["id"])["status"] == "RUNNING"


def test_no_eligible_with_active_batch_does_not_complete(conn, bolna_stub):
    ag = make_agent(conn)
    c = make_campaign(conn, agent=ag, status="SCHEDULED")
    make_batch(conn, c, status="scheduled")
    r = _db_run(conn, c)
    scheduler.dispatch_due_runs(conn, now=NOW)
    assert _row(conn, r["id"])["status"] == "RUNNING"
    with conn.cursor() as cur:
        cur.execute("select status from campaign where id = %s", (c["id"],))
        assert cur.fetchone()["status"] == "SCHEDULED"
