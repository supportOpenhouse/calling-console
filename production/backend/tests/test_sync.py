import json
import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from psycopg2.extras import Json

from .. import config
from ..scripts import run_sync as run_sync_script
from ..services import bolna, di_push, redial, scheduler, sync, workflows
from ..services.ist import IST
from ..services.settings_store import get_setting, set_setting
from ..services.sync import (
    LOCK_KEY, close_stale_queued, cursor_key, is_full_due, lead_update_from_call, new_report, norm_batch_status,
    retry_rules, rollup_campaigns, run_sync, start_background_sync, sync_agents, sync_batches, sync_executions,
    upsert_execution,
)
from .factories import DEFAULT_REDIAL_POLICY as FACTORY_POLICY
from .factories import (
    _insert, bolna_execution, make_agent, make_batch, make_call, make_campaign, make_dataset, make_lead, make_use_case,
)


def ist(h, m=0, day=0):
    return datetime(2026, 11, 19 + day, h, m, tzinfo=IST).astimezone(timezone.utc)


NOW = ist(11)  # Thursday 19 Nov 2026, 11:00 IST: inside the factory policy's 10:00-19:00 window


def T(h):
    return datetime(2026, 11, 18, h, tzinfo=timezone.utc)


def P(n: int) -> str:
    return f"+91981000{n:04d}"


def call(**kw):
    """The classify_execution fields lead_update_from_call reads."""
    return {"outcome": "CONNECTED", "tag": None, "started_at": T(13), **kw}


def LEAD(**kw):
    return {"status": "CONNECTED", "attempts": 2, "last_call_at": T(12), "last_outcome": "CONNECTED",
            "final_tag": "callback_requested", "next_dial_at": T(15), **kw}


def ex(bolna_agent_id, phone, *, at=NOW - timedelta(minutes=30), **kw):
    """A Bolna execution of `bolna_agent_id` to `phone`, created at `at` (qualified, connected by default)."""
    return bolna_execution(agent_id=bolna_agent_id, created_at=at.isoformat(), telephony_data={
        "to_number": phone, "from_number": "+918000000001", "hangup_provider_code": 4000,
        "recording_url": "https://example.com/rec.mp3"}, **kw)


def _parse(iso):
    return datetime.fromisoformat(iso)


def one(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def lead_row(conn, lead):
    return one(conn, "SELECT * FROM lead WHERE id = %s", lead["id"])


def call_row(conn, execution):
    return one(conn, "SELECT * FROM call WHERE bolna_execution_id = %s", execution["id"])


def status_of(conn, table, row):
    return one(conn, f'SELECT status FROM "{table}" WHERE id = %s', row["id"])["status"]


def set_col(conn, table, row, **cols):
    sets = ", ".join(f"{k} = %s" for k in cols)
    with conn.cursor() as cur:
        cur.execute(f'UPDATE "{table}" SET {sets} WHERE id = %s', [*cols.values(), row["id"]])


def dialled(bolna_stub):
    return [args for name, args in bolna_stub.calls if name == "create_batch"]


def synced(conn):
    """Every agent: as if sync_executions synced them all fully this run."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM agent")
        return {r["id"] for r in cur.fetchall()}


def started(conn, camp, at=NOW - timedelta(days=1)):
    """The campaign has started dialling (the phone fallback only links calls to started campaigns)."""
    set_col(conn, "campaign", camp, started_at=at)
    return camp


def freeze(monkeypatch, at=NOW):
    """run_sync and the services it calls read the clock through their modules' `datetime`."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is None else at.astimezone(tz)

    for mod in (sync, redial, workflows, scheduler, di_push):
        monkeypatch.setattr(mod, "datetime", Frozen)


# ---------------------------------------------------------------- lead_update_from_call (pure)

def test_older_call_does_not_overwrite_newer():
    assert lead_update_from_call(LEAD(), call(started_at=T(9), outcome="NOT_PICKED"),
                                 first_link=False, rules_applied=False, terminal=True) is None


def test_first_link_counts_attempt_even_if_older():
    assert lead_update_from_call(LEAD(), call(started_at=T(9), outcome="NOT_PICKED"),
                                 first_link=True, rules_applied=False, terminal=True) == {"attempts": 3}


def test_newer_call_overwrites_and_resets_next_dial():
    assert lead_update_from_call(LEAD(), call(started_at=T(13), outcome="NOT_PICKED"),
                                 first_link=True, rules_applied=True, terminal=True) == {
        "attempts": 3, "last_call_at": T(13), "last_outcome": "NOT_PICKED", "status": "NOT_CONNECTED",
        "final_tag": "callback_requested", "next_dial_at": None}
    # a lead never called before, and a call without a start time, count as newer
    fresh = LEAD(status="QUEUED", attempts=0, last_call_at=None, final_tag=None)
    assert lead_update_from_call(fresh, call(started_at=T(9), tag="qualified"), first_link=True,
                                 rules_applied=False, terminal=True)["status"] == "DONE"
    assert lead_update_from_call(LEAD(), call(started_at=None, outcome="VOICEMAIL"), first_link=False,
                                 rules_applied=True, terminal=True)["status"] == "VOICEMAIL"


def test_same_call_after_rules_leaves_lead_alone():
    same = call(started_at=T(12), tag="qualified")
    assert lead_update_from_call(LEAD(), same, first_link=False, rules_applied=True, terminal=True) is None
    # before its rules ran (the extraction wait) the same call still updates the lead, keeping next_dial_at
    assert lead_update_from_call(LEAD(), same, first_link=False, rules_applied=False, terminal=True) == {
        "attempts": 2, "last_call_at": T(12), "last_outcome": "CONNECTED", "status": "DONE",
        "final_tag": "qualified", "next_dial_at": T(15)}


def test_same_call_never_takes_a_lead_out_of_its_next_batch():
    requeued = LEAD(status="QUEUED", next_dial_at=T(15))  # put into the next round after that call
    out = lead_update_from_call(requeued, call(started_at=T(12), outcome="NOT_PICKED"), first_link=False,
                                rules_applied=False, terminal=True)
    assert (out["status"], out["next_dial_at"]) == ("QUEUED", T(15))
    # QUEUED by this very call while it was in progress (next_dial_at cleared): it moves on once the call ends
    flying = LEAD(status="QUEUED", next_dial_at=None)
    assert lead_update_from_call(flying, call(started_at=T(12), outcome="NOT_PICKED"), first_link=False,
                                 rules_applied=False, terminal=True)["status"] == "NOT_CONNECTED"


def test_balance_low_not_counted_and_pending():
    lead = LEAD(status="QUEUED", attempts=0, last_call_at=None, last_outcome=None, final_tag=None,
                next_dial_at=T(12))
    assert lead_update_from_call(lead, call(outcome="BALANCE_LOW"), first_link=True, rules_applied=False,
                                 terminal=True) == {
        "attempts": 0, "last_call_at": T(13), "last_outcome": "BALANCE_LOW", "status": "PENDING",
        "final_tag": None, "next_dial_at": None}


def test_done_and_dnc_are_sticky():
    for status in ("DONE", "DNC"):
        out = lead_update_from_call(LEAD(status=status), call(outcome="NOT_PICKED"), first_link=True,
                                    rules_applied=False, terminal=True)
        assert (out["status"], out["last_outcome"], out["attempts"]) == (status, "NOT_PICKED", 3)
        out = lead_update_from_call(LEAD(status=status), call(outcome="UNKNOWN"), first_link=False,
                                    rules_applied=False, terminal=False)
        assert out["status"] == status


def test_non_terminal_call_sets_queued():
    out = lead_update_from_call(LEAD(status="PENDING"), call(outcome="UNKNOWN"), first_link=True,
                                rules_applied=False, terminal=False)
    assert (out["status"], out["attempts"], out["next_dial_at"]) == ("QUEUED", 3, None)


def test_norm_batch_status():
    raw = ("Executed", "in_progress", "queued", "paused", "canceled", "error", "created")
    got = [norm_batch_status(s) for s in raw]
    assert got == ["completed", "running", "scheduled", "scheduled", "stopped", "failed", "created"]
    for unknown in ("weird", None, ""):
        with pytest.raises(ValueError, match=f"unknown Bolna status {unknown}"):
            norm_batch_status(unknown)


def test_new_report_keys():
    assert set(new_report()) == {
        "started_at", "finished_at", "full", "agents", "batches_checked", "executions_seen", "calls_upserted",
        "recordings_archived", "redial_rounds", "campaigns_completed", "runs_started", "triggers_fired",
        "rules_applied", "di_pushed", "errors", "window_stops"}


# ---------------------------------------------------------------- upsert_execution

def test_upsert_links_by_batch_and_applies_rules(conn):
    agent = make_agent(conn)
    uc = make_use_case(conn)
    camp = make_campaign(conn, agent=agent, use_case_id=uc["id"])
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED")
    batch = make_batch(conn, camp, status="running", bolna_batch_id="bb-1")
    e = ex(agent["bolna_agent_id"], P(1), batch_id="bb-1")
    report = new_report()
    assert upsert_execution(conn, e, now=NOW, report=report) == "inserted"
    c = call_row(conn, e)
    assert (c["campaign_id"], c["lead_id"], c["batch_id"], c["agent_id"]) == (camp["id"], lead["id"], batch["id"],
                                                                             agent["id"])
    assert (c["phone"], c["outcome"], c["tag"], c["bolna_batch_id"]) == (P(1), "CONNECTED", "qualified", "bb-1")
    assert c["rules_applied_at"] == NOW
    row = lead_row(conn, lead)
    assert (row["status"], row["attempts"], row["last_outcome"], row["final_tag"]) == ("DONE", 1, "CONNECTED",
                                                                                     "qualified")
    assert row["last_call_at"] == NOW - timedelta(minutes=30) and row["next_dial_at"] is None
    assert one(conn, "SELECT count(*) AS n FROM handoff WHERE call_id = %s", c["id"])["n"] == 1
    assert report["rules_applied"] == 1 and report["errors"] == []
    # the next sync sees the execution again: updated, the attempt not counted twice, the rules not run again
    assert upsert_execution(conn, e, now=NOW, report=report) == "updated"
    assert lead_row(conn, lead)["attempts"] == 1 and report["rules_applied"] == 1


def test_upsert_links_a_call_without_batch_by_phone_and_keeps_the_link(conn):
    agent = make_agent(conn)
    finished = make_campaign(conn, agent=agent, status="COMPLETED")
    make_lead(conn, finished, phone=P(1))
    live = started(conn, make_campaign(conn, agent=agent))
    lead = make_lead(conn, live, phone=P(1))
    e = ex(agent["bolna_agent_id"], P(1), status="no-answer")
    assert upsert_execution(conn, e, now=NOW) == "inserted"
    c = call_row(conn, e)
    assert (c["campaign_id"], c["lead_id"], c["batch_id"]) == (live["id"], lead["id"], None)
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("NOT_CONNECTED", 1)
    # the campaign finishes; re-reading the execution does not unlink the call
    set_col(conn, "campaign", live, status="COMPLETED")
    conn.commit()
    assert upsert_execution(conn, e, now=NOW) == "updated"
    assert call_row(conn, e)["lead_id"] == lead["id"]


def test_upsert_never_links_a_call_older_than_the_campaign(conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    set_col(conn, "campaign", camp, started_at=NOW - timedelta(hours=1))
    lead = make_lead(conn, camp, phone=P(1))
    old = ex(agent["bolna_agent_id"], P(1), at=NOW - timedelta(days=3))  # e.g. a history call of a full sync
    upsert_execution(conn, old, now=NOW)
    assert call_row(conn, old)["lead_id"] is None and call_row(conn, old)["rules_applied_at"] is None
    assert lead_row(conn, lead)["attempts"] == 0


def _dnc_tag(tag):
    return {"Seller Details": {"call_tag": {"objective": tag}}}


def test_unlinked_finished_call_with_a_dnc_tag_blocks_the_phone(conn):
    agent = make_agent(conn)  # no campaign at all: e.g. a test call
    done = ex(agent["bolna_agent_id"], P(1), extracted_data=_dnc_tag("Do_Not_Call"))
    live = ex(agent["bolna_agent_id"], P(2), extracted_data=_dnc_tag("do_not_call"), status="in-progress")
    nice = ex(agent["bolna_agent_id"], P(3))  # qualified
    for e in (done, live, nice):
        upsert_execution(conn, e, now=NOW)
    assert call_row(conn, done)["campaign_id"] is None
    row = one(conn, "SELECT * FROM dnc_phone WHERE phone = %s", P(1))
    assert row is not None and (row["source"], row["reason"]) == ("call_tag", "Do_Not_Call")
    assert one(conn, "SELECT count(*) AS n FROM dnc_phone")["n"] == 1  # unfinished and other tags: nothing


def test_dnc_of_a_phone_still_queued_elsewhere_reaches_the_report(conn):
    agent = make_agent(conn)
    camp = started(conn, make_campaign(conn, agent=agent))
    make_lead(conn, camp, phone=P(1))
    other = make_campaign(conn, agent=make_agent(conn))
    make_lead(conn, other, phone=P(1), status="QUEUED")
    e = ex(agent["bolna_agent_id"], P(1), extracted_data=_dnc_tag("angry"))
    report = new_report()
    upsert_execution(conn, e, now=NOW, report=report)
    assert report["errors"] == [f"rules {e['id']}: dnc:queued_in:{other['name']} ({other['id']})"]


def _workflow(conn, agent, uc, rules):
    return _insert(conn, "workflow", department="SUPPLY", use_case_id=uc["id"], name="WF", agent_id=agent["id"],
                   from_number="+918000000001", trigger=Json({"type": "MANUAL"}), redial_policy=Json({}),
                   rules=Json(rules))


def test_upsert_never_links_a_call_to_a_campaign_not_started(conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="SCHEDULED")  # on the calendar: started_at NULL
    lead = make_lead(conn, camp, phone=P(1))
    e = ex(agent["bolna_agent_id"], P(1))
    upsert_execution(conn, e, now=NOW)
    assert call_row(conn, e)["lead_id"] is None and lead_row(conn, lead)["attempts"] == 0


def test_call_seen_in_progress_then_finished_moves_the_lead_on(conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    make_batch(conn, camp, status="running", bolna_batch_id="bb-1", scheduled_at=NOW - timedelta(minutes=35))
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED")
    set_col(conn, "lead", lead, next_dial_at=NOW - timedelta(minutes=35))  # as create_round leaves it
    e = ex(agent["bolna_agent_id"], P(1), batch_id="bb-1", status="in-progress")
    upsert_execution(conn, e, now=NOW)
    row = lead_row(conn, lead)
    assert (row["status"], row["attempts"], row["next_dial_at"]) == ("QUEUED", 1, None)
    upsert_execution(conn, {**e, "status": "completed"}, now=NOW)
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("DONE", 1)


def test_older_call_rules_never_undo_the_newer_call(conn):
    agent = make_agent(conn)
    uc = make_use_case(conn)
    wf = _workflow(conn, agent, uc, [{"when": {"outcomes": ["NOT_PICKED"]},
                                      "actions": [{"type": "redial", "after_minutes": 30}]}])
    camp = make_campaign(conn, agent=agent, use_case_id=uc["id"], workflow_id=wf["id"])
    make_batch(conn, camp, status="running", bolna_batch_id="bb-1")
    ds = make_dataset(conn, use_case_id=uc["id"], rows=[{"phone": P(1)}])
    row_id = one(conn, "SELECT id FROM dataset_lead WHERE dataset_id = %s", ds["id"])["id"]
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", dataset_lead_id=row_id)
    newer = ex(agent["bolna_agent_id"], P(1), batch_id="bb-1", at=NOW - timedelta(minutes=10))  # qualified
    older = ex(agent["bolna_agent_id"], P(1), batch_id="bb-1", at=NOW - timedelta(minutes=40), status="no-answer",
               extracted_data=None)
    upsert_execution(conn, newer, now=NOW)
    report = new_report()
    upsert_execution(conn, older, now=NOW, report=report)  # Bolna lists it later (newest first)
    row = lead_row(conn, lead)
    assert (row["status"], row["next_dial_at"], row["final_tag"], row["attempts"]) == ("DONE", None, "qualified", 2)
    assert row["last_call_at"] == NOW - timedelta(minutes=10)
    assert report["errors"] == [] and call_row(conn, older)["rules_applied_at"] == NOW
    mirror = one(conn, "SELECT * FROM dataset_lead WHERE id = %s", row_id)
    assert (mirror["last_called_at"], mirror["last_outcome"], mirror["last_tag"], mirror["total_attempts"]) == (
        NOW - timedelta(minutes=10), "CONNECTED", "qualified", 2)


def test_rule_errors_reach_the_report(conn, monkeypatch):
    monkeypatch.setattr(config, "INTERAKT_API_KEY", "")
    agent = make_agent(conn)
    uc = make_use_case(conn)
    wf = _workflow(conn, agent, uc, [{"when": {"outcomes": ["CONNECTED"]},
                                      "actions": [{"type": "whatsapp", "template": "hello"}]}])
    camp = started(conn, make_campaign(conn, agent=agent, use_case_id=uc["id"], workflow_id=wf["id"]))
    make_lead(conn, camp, phone=P(1))
    e = ex(agent["bolna_agent_id"], P(1))
    report = new_report()
    upsert_execution(conn, e, now=NOW, report=report)
    assert report["errors"] == [f"rules {e['id']}: whatsapp:error:WhatsApp not configured (INTERAKT_API_KEY)"]
    assert report["rules_applied"] == 1


def test_rule_failure_is_rolled_back_and_retried(conn, monkeypatch):
    agent = make_agent(conn)
    camp = started(conn, make_campaign(conn, agent=agent))
    lead = make_lead(conn, camp, phone=P(1))

    def boom(conn, call, campaign, lead, now=None, stale=False):
        with conn.cursor() as cur:
            cur.execute("UPDATE call SET rules_applied_at = now() WHERE id = %s", (call["id"],))
            cur.execute("SELECT 1/0")

    monkeypatch.setattr(workflows, "apply_result_rules", boom)
    e = ex(agent["bolna_agent_id"], P(1), status="no-answer")
    report = new_report()
    assert upsert_execution(conn, e, now=NOW, report=report) == "inserted"
    assert report["errors"] == [f"rules {e['id']}: division by zero"]
    # the call and the lead were saved before the rules; the claim was rolled back, so the next sync retries
    assert call_row(conn, e)["rules_applied_at"] is None
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("NOT_CONNECTED", 1)
    with pytest.raises(psycopg2.Error):  # without a report the failure propagates
        upsert_execution(conn, e, now=NOW)


# ---------------------------------------------------------------- close_stale_queued

def test_close_stale_queued_becomes_failed_attempt(conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    confirmed = make_batch(conn, camp, status="completed", bolna_batch_id="bb-1", scheduled_at=NOW - timedelta(hours=1))
    set_col(conn, "batch", confirmed, stats=Json({"status": "completed"}))  # Bolna said so: qualifies at once
    stale = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", stale, next_dial_at=NOW - timedelta(hours=1))
    ahead = make_lead(conn, camp, phone=P(2), status="QUEUED", attempts=1)  # its batch start is still ahead
    set_col(conn, "lead", ahead, next_dial_at=NOW + timedelta(hours=1))
    flying = make_lead(conn, camp, phone=P(3), status="QUEUED", attempts=1,
                       last_call_at=NOW - timedelta(minutes=2))  # reached by a call still in progress
    make_call(conn, phone=P(3), started_at=NOW - timedelta(minutes=2), status="in-progress", outcome="UNKNOWN",
              campaign=camp, lead=flying)
    never = make_lead(conn, camp, phone=P(4), status="QUEUED", attempts=0)  # no batch time, no call
    assert close_stale_queued(conn, camp["id"], NOW) == 2
    for lead, attempts in ((stale, 2), (never, 1)):
        row = lead_row(conn, lead)
        assert (row["status"], row["last_outcome"], row["attempts"], row["next_dial_at"]) == (
            "FAILED", "FAILED", attempts, None)
    for lead in (ahead, flying):
        assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)


def test_close_stale_queued_waits_for_every_batch_that_may_dial(conn):
    agent = make_agent(conn)
    for status in ("running", "scheduled", "created", "creating"):
        camp = make_campaign(conn, agent=agent)
        make_batch(conn, camp, status="completed", bolna_batch_id=f"bb-done-{status}")
        make_batch(conn, camp, status=status, bolna_batch_id=None if status == "creating" else f"bb-{status}")
        lead = make_lead(conn, camp, phone=P(1), status="QUEUED")
        assert close_stale_queued(conn, camp["id"], NOW) == 0
        assert lead_row(conn, lead)["status"] == "QUEUED"


IDLE = sync.IDLE_COMPLETED_AT


def idle_stats(raw, completed_at=None):
    """Batch stats as the sync leaves them: Bolna's last status, and when the idle rule completed the batch."""
    return Json({"status": raw, **({IDLE: completed_at.isoformat()} if completed_at else {})})


def test_close_stale_queued_waits_for_a_young_idle_completed_batch(conn):
    camp = make_campaign(conn, agent=make_agent(conn))
    idle = make_batch(conn, camp, status="completed", bolna_batch_id="bb-1", scheduled_at=NOW - timedelta(days=3))
    set_col(conn, "batch", idle, stats=idle_stats("running", NOW - timedelta(hours=1)))  # our idle rule, not Bolna
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED")
    assert close_stale_queued(conn, camp["id"], NOW) == 0  # however old its start: the completion is an hour old
    assert close_stale_queued(conn, camp["id"], NOW + timedelta(hours=24)) == 1  # 25 h after the idle completion
    set_col(conn, "lead", lead, status="QUEUED")
    set_col(conn, "batch", idle, stats=Json({"status": "stopped"}))  # Bolna confirms: closed at once
    assert close_stale_queued(conn, camp["id"], NOW) == 1


@pytest.mark.parametrize("raw, idle_h, closes", [
    ("completed", None, True),  # (a) Bolna reports a final status: at once
    ("Stopped", None, True),
    ("failed", 1, True),
    ("running", 25, True),  # (b) Bolna's last status is running and the idle completion is over 24 h old
    ("in-progress", 25, True),
    ("running", 23, False),
    ("running", 24, False),  # exactly 24 h: not more than
    ("running", None, False),  # never stamped
    ("on_hold", 25, False),  # anything else, at any age
    ("on_hold", 24 * 30, False),
    ("paused", 25, False),
    ("scheduled", 25, False),
    (None, 25, False),
])
def test_close_stale_queued_closes_only_for_a_final_or_old_running_bolna_status(conn, raw, idle_h, closes):
    camp = make_campaign(conn, agent=make_agent(conn))
    start = NOW - timedelta(days=40)  # the batch's start never counts
    batch = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-1", scheduled_at=start)
    set_col(conn, "batch", batch, stats=idle_stats(raw, None if idle_h is None else NOW - timedelta(hours=idle_h)))
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=start)
    assert close_stale_queued(conn, camp["id"], NOW) == (1 if closes else 0)
    assert lead_row(conn, lead)["status"] == ("FAILED" if closes else "QUEUED")


@pytest.mark.parametrize("stats", [{}, None])
def test_close_stale_queued_never_trusts_a_batch_bolna_never_reported(conn, stats):
    camp = make_campaign(conn, agent=make_agent(conn))
    batch = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-1",
                       scheduled_at=NOW - timedelta(days=3))
    if stats is not None:
        set_col(conn, "batch", batch, stats=Json(stats))
    make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    assert close_stale_queued(conn, camp["id"], NOW) == 0


# ---------------------------------------------------------------- batches

def test_sync_batches_refreshes_status(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    late = make_batch(conn, camp, status="scheduled", bolna_batch_id="bb-a", scheduled_at=NOW - timedelta(minutes=10))
    ahead = make_batch(conn, camp, status="scheduled", bolna_batch_id="bb-b", scheduled_at=NOW + timedelta(hours=1))
    running = make_batch(conn, camp, status="running", bolna_batch_id="bb-c")
    done = make_batch(conn, camp, status="completed", bolna_batch_id="bb-d")
    set_col(conn, "batch", done, stats=Json({"status": "completed"}))  # Bolna confirmed it: not asked again
    bolna_stub.batches = {"bb-a": {"status": "scheduled"}, "bb-b": {"status": "Queued"},
                          "bb-c": {"status": "executed", "n": 3}}
    report = new_report()
    sync_batches(conn, report, NOW)
    assert [status_of(conn, "batch", b) for b in (late, ahead, running, done)] == [
        "running", "scheduled", "completed", "completed"]
    assert report["batches_checked"] == 3 and report["errors"] == []
    row = one(conn, "SELECT stats, last_synced_at FROM batch WHERE id = %s", running["id"])
    assert row["stats"] == {"status": "executed", "n": 3} and row["last_synced_at"] == NOW
    assert sorted(a[0] for n, a in bolna_stub.calls if n == "get_batch") == ["bb-a", "bb-b", "bb-c"]


def test_bolna_paused_is_active_and_unknown_status_is_kept(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    paused = make_batch(conn, camp, status="running", bolna_batch_id="bb-a")
    odd = make_batch(conn, camp, status="running", bolna_batch_id="bb-b")
    bolna_stub.batches = {"bb-a": {"status": "paused"}, "bb-b": {"status": "partially_done"}}
    report = new_report()
    sync_batches(conn, report, NOW)
    assert status_of(conn, "batch", paused) == "scheduled" and status_of(conn, "batch", odd) == "running"
    assert report["errors"] == ["batch bb-b: unknown Bolna status partially_done"]


def test_batch_poll_failure_is_reported(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    b = make_batch(conn, camp, status="running", bolna_batch_id="bb-a")
    bolna_stub.fail_on = {"get_batch"}
    report = new_report()
    sync_batches(conn, report, NOW)
    assert report["errors"] == ["batch bb-a: Bolna 500: <html>"] and status_of(conn, "batch", b) == "running"


def test_stuck_creating_batch_is_reported(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    stuck = make_batch(conn, camp, status="creating")
    fresh = make_batch(conn, camp, status="creating")
    set_col(conn, "batch", stuck, created_at=NOW - timedelta(minutes=15))
    set_col(conn, "batch", fresh, created_at=NOW - timedelta(minutes=5))
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED")
    for _ in range(2):  # loudly, on every sync
        report = new_report()
        sync_batches(conn, report, NOW)
        assert report["errors"] == [
            f"batch {stuck['id']} stuck in creating since {(NOW - timedelta(minutes=15)).isoformat()}"]
    assert status_of(conn, "batch", stuck) == "creating" and lead_row(conn, lead)["status"] == "QUEUED"
    assert bolna_stub.calls == []


# ---------------------------------------------------------------- executions

def test_sync_executions_pages_back_to_the_lookback(conn, bolna_stub):
    agent = make_agent(conn, bolna_agent_id="ba-1")
    make_agent(conn, bolna_agent_id="ba-idle")  # no live campaign: not synced
    make_campaign(conn, agent=agent)
    set_setting(conn, cursor_key(agent["id"]), (NOW - timedelta(hours=1)).isoformat())  # lookback: NOW - 1h45
    new = ex("ba-1", P(1), at=NOW - timedelta(minutes=10))
    overlap = ex("ba-1", P(5), at=NOW - timedelta(minutes=100))  # before the cursor, inside the 45-minute overlap
    old = ex("ba-1", P(2), at=NOW - timedelta(minutes=110))
    bolna_stub.executions = {"ba-1": [[new, overlap, old], [ex("ba-1", P(3), at=NOW - timedelta(hours=6))]],
                             "ba-idle": [[ex("ba-idle", P(4))]]}
    report = new_report()
    assert sync_executions(conn, report, max_pages=6, now=NOW) == {agent["id"]}  # reached the lookback
    assert [a for n, a in bolna_stub.calls if n == "list_agent_executions"] == [("ba-1", 1, 50)]
    assert sync.LOOKBACK_OVERLAP == timedelta(minutes=45)
    assert (report["agents"], report["executions_seen"], report["calls_upserted"]) == (1, 3, 2)
    assert call_row(conn, new) and call_row(conn, overlap) and call_row(conn, old) is None
    assert get_setting(conn, cursor_key(agent["id"])) == NOW.isoformat()


def test_sync_executions_page_cap(conn, bolna_stub):
    agent = make_agent(conn, bolna_agent_id="ba-1")
    make_campaign(conn, agent=agent)
    pages = [[ex("ba-1", P(i), at=NOW - timedelta(minutes=10 * i))] for i in range(1, 4)]
    pages.append([ex("ba-1", P(9), at=NOW - timedelta(hours=49))])  # first run: 48 h lookback
    bolna_stub.executions = {"ba-1": pages}
    report = new_report()
    assert sync_executions(conn, report, max_pages=2, now=NOW) == set()  # capped: not synced fully
    assert report["calls_upserted"] == 2
    assert report["errors"] == [f"executions {agent['name']} (ba-1): capped at 2 pages before reaching lookback"]
    assert get_setting(conn, cursor_key(agent["id"])) is None  # and the cursor stays
    report = new_report()
    assert sync_executions(conn, report, max_pages=None, now=NOW) == {agent["id"]}  # no cap
    assert (report["executions_seen"], report["calls_upserted"], report["errors"]) == (4, 3, [])
    assert get_setting(conn, cursor_key(agent["id"])) == NOW.isoformat()


def test_full_sync_pages_every_agent_past_its_lookback(conn, bolna_stub):
    agent = make_agent(conn, bolna_agent_id="ba-1")  # no live campaign
    set_setting(conn, cursor_key(agent["id"]), NOW.isoformat())
    bolna_stub.executions = {"ba-1": [[ex("ba-1", P(1), at=NOW - timedelta(days=30))],
                                      [ex("ba-1", P(2), at=NOW - timedelta(days=31))]]}
    report = new_report()
    assert sync_executions(conn, report, full=True, max_pages=40, now=NOW) == {agent["id"]}
    assert (report["agents"], report["calls_upserted"]) == (1, 2)  # kept paging to backfill


def test_capped_agent_keeps_its_cursor_and_the_other_moves(conn, bolna_stub):
    busy, calm = make_agent(conn, bolna_agent_id="ba-busy"), make_agent(conn, bolna_agent_id="ba-calm")
    before = (NOW - timedelta(hours=1)).isoformat()  # lookback: NOW - 1h45
    for a in (busy, calm):
        make_campaign(conn, agent=a)
        set_setting(conn, cursor_key(a["id"]), before)
    bolna_stub.executions = {
        "ba-busy": [[ex("ba-busy", P(i), at=NOW - timedelta(minutes=i))] for i in range(1, 4)],  # all inside it
        "ba-calm": [[ex("ba-calm", P(10), at=NOW - timedelta(minutes=5)),
                     ex("ba-calm", P(11), at=NOW - timedelta(hours=5))], [ex("ba-calm", P(12))]],
    }
    report = new_report()
    assert sync_executions(conn, report, max_pages=2, now=NOW) == {calm["id"]}
    assert report["errors"] == [f"executions {busy['name']} (ba-busy): capped at 2 pages before reaching lookback"]
    assert get_setting(conn, cursor_key(busy["id"])) == before
    assert get_setting(conn, cursor_key(calm["id"])) == NOW.isoformat()


def test_busy_agent_reaching_its_lookback_within_the_cap_is_synced(conn, bolna_stub):
    agent = make_agent(conn, bolna_agent_id="ba-busy")
    make_campaign(conn, agent=agent)
    now = datetime.now(timezone.utc)  # run_sync runs on the wall clock
    set_setting(conn, cursor_key(agent["id"]), (now - timedelta(minutes=30)).isoformat())  # lookback: now - 75 min
    conn.commit()
    pages = [[ex("ba-busy", P(100 + i), at=now - timedelta(minutes=i))] for i in range(1, sync.MAX_PAGES)]
    pages.append([ex("ba-busy", P(999), at=now - timedelta(hours=4))])  # page 40 passes the lookback
    bolna_stub.executions = {"ba-busy": pages}
    r = run_sync()  # the default cap
    assert sync.MAX_PAGES == 40 and r["errors"] == [] and r["calls_upserted"] == 39
    assert [a[1] for n, a in bolna_stub.calls if n == "list_agent_executions"] == list(range(1, 41))
    assert _parse(get_setting(conn, cursor_key(agent["id"]))) > now


def test_failed_agent_keeps_the_cursor(conn, bolna_stub):
    agent = make_agent(conn, bolna_agent_id="ba-1")
    make_campaign(conn, agent=agent)
    before = (NOW - timedelta(hours=1)).isoformat()
    set_setting(conn, cursor_key(agent["id"]), before)
    bolna_stub.fail_agents = {"ba-1"}
    report = new_report()
    assert sync_executions(conn, report, max_pages=6, now=NOW) == set()
    assert report["errors"] == ["executions Agent ba-1 (ba-1): Bolna 502: <html>"]
    assert get_setting(conn, cursor_key(agent["id"])) == before


def test_failed_execution_does_not_stop_the_agent(conn, bolna_stub):
    agent = make_agent(conn, bolna_agent_id="ba-1")
    make_campaign(conn, agent=agent)
    bad = ex("ba-1", P(1), status=None)  # call.status is NOT NULL
    good = ex("ba-1", P(2), at=NOW - timedelta(minutes=40))
    bolna_stub.executions = {"ba-1": [[bad, good]]}
    report = new_report()
    assert sync_executions(conn, report, max_pages=6, now=NOW) == set()
    assert len(report["errors"]) == 1 and report["errors"][0].startswith(f"execution {bad['id']}: ")
    assert report["calls_upserted"] == 1 and call_row(conn, good) is not None
    assert get_setting(conn, cursor_key(agent["id"])) is None


# ---------------------------------------------------------------- rollup

def test_campaign_completes(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-1")
    make_lead(conn, camp, phone=P(1), status="DONE", attempts=1)
    tried = make_lead(conn, camp, phone=P(2), status="NOT_CONNECTED", attempts=3, last_outcome="NOT_PICKED",
                      last_call_at=NOW - timedelta(hours=2))
    report = new_report()
    rollup_campaigns(conn, report, NOW, synced_agents=synced(conn))
    row = one(conn, "SELECT status, completed_at FROM campaign WHERE id = %s", camp["id"])
    assert (row["status"], row["completed_at"]) == ("COMPLETED", NOW)
    assert lead_row(conn, tried)["status"] == "DONE"
    assert (report["campaigns_completed"], report["redial_rounds"], report["errors"]) == (1, 0, [])
    assert dialled(bolna_stub) == []


def test_campaign_waits_for_a_call_awaiting_its_rules(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    make_batch(conn, camp, status="completed", bolna_batch_id="bb-1")
    lead = make_lead(conn, camp, phone=P(1), status="CONNECTED", attempts=1, last_outcome="CONNECTED",
                     last_call_at=NOW - timedelta(minutes=2))
    c = make_call(conn, phone=P(1), started_at=NOW - timedelta(minutes=2), campaign=camp, lead=lead)  # untagged
    rollup_campaigns(conn, new_report(), NOW, synced_agents=synced(conn))
    assert status_of(conn, "campaign", camp) == "RUNNING"  # its agent stays synced until the tag lands
    set_col(conn, "call", c, rules_applied_at=NOW)
    rollup_campaigns(conn, new_report(), NOW, synced_agents=synced(conn))
    assert status_of(conn, "campaign", camp) == "COMPLETED"


def test_balance_low_lead_keeps_campaign_alive_until_redialled(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    batch = make_batch(conn, camp, status="running", bolna_batch_id="bb-1", scheduled_at=NOW - timedelta(minutes=40))
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED")
    upsert_execution(conn, ex(agent["bolna_agent_id"], P(1), batch_id="bb-1", status="balance-low"), now=NOW)
    row = lead_row(conn, lead)
    assert (row["status"], row["attempts"], row["last_outcome"]) == ("PENDING", 0, "BALANCE_LOW")
    report = new_report()
    rollup_campaigns(conn, report, NOW, synced_agents=synced(conn))
    assert status_of(conn, "batch", batch) == "completed"  # its one contact has a finished call
    assert status_of(conn, "campaign", camp) == "RUNNING" and report["campaigns_completed"] == 0
    assert dialled(bolna_stub) == []  # the 60-minute gap has not passed
    report = new_report()
    rollup_campaigns(conn, report, NOW + timedelta(hours=2), synced_agents=synced(conn))
    assert report["redial_rounds"] == 1 and len(dialled(bolna_stub)) == 1 and report["errors"] == []
    assert lead_row(conn, lead)["status"] == "QUEUED" and status_of(conn, "campaign", camp) == "RUNNING"


def test_rollup_counts_batch_calls_and_completes_finished_batches(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    full = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-1")
    open_ = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-2")
    idle = make_batch(conn, camp, status="running", total=5, bolna_batch_id="bb-3",
                      scheduled_at=NOW - timedelta(hours=4))
    set_col(conn, "batch", idle, stats=Json({"status": "running"}))
    quiet = {}
    for raw in ("paused", "scheduled", None):  # Bolna paused / still scheduled / never polled: never idle-completed
        quiet[raw] = make_batch(conn, camp, status="running", total=5, bolna_batch_id=f"bb-q-{raw}",
                                scheduled_at=NOW - timedelta(hours=4))
        if raw:
            set_col(conn, "batch", quiet[raw], stats=Json({"status": raw}))
    make_call(conn, phone=P(1), started_at=NOW - timedelta(hours=1), campaign=camp, batch=full)
    make_call(conn, phone=P(2), started_at=NOW - timedelta(hours=1), campaign=camp, batch=full, outcome="NOT_PICKED",
              status="no-answer")
    make_call(conn, phone=P(3), started_at=NOW - timedelta(hours=1), campaign=camp, batch=open_)
    make_call(conn, phone=P(4), started_at=NOW - timedelta(minutes=1), campaign=camp, batch=open_,
              status="in-progress", outcome="UNKNOWN")
    make_call(conn, phone=P(5), started_at=NOW - timedelta(hours=1), campaign=camp, batch=idle)
    twice = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-4")
    for minutes in (50, 40):  # one contact called twice: the other has not been called
        make_call(conn, phone=P(6), started_at=NOW - timedelta(minutes=minutes), campaign=camp, batch=twice,
                  outcome="NOT_PICKED", status="no-answer")
    rollup_campaigns(conn, new_report(), NOW, synced_agents=synced(conn))
    rows = {r["id"]: r for r in [one(conn, "SELECT * FROM batch WHERE id = %s", b["id"]) for b in (full, open_, idle)]}
    assert (rows[full["id"]]["dialed"], rows[full["id"]]["connected"], rows[full["id"]]["status"]) == (2, 1,
                                                                                                    "completed")
    assert (rows[open_["id"]]["dialed"], rows[open_["id"]]["status"]) == (2, "running")  # one call still live
    assert rows[idle["id"]]["status"] == "completed"  # 4 h past its start, no call for 30 min
    assert status_of(conn, "batch", twice) == "running"  # finished calls are counted per phone
    assert [status_of(conn, "batch", b) for b in quiet.values()] == ["running"] * 3


def test_idle_completed_batch_keeps_its_leads_queued_for_a_day(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    start = NOW - timedelta(hours=4)
    batch = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-1", scheduled_at=start)
    set_col(conn, "batch", batch, stats=Json({"status": "running"}))  # Bolna never said it finished
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=start)
    report = new_report()
    rollup_campaigns(conn, report, NOW, synced_agents=synced(conn))
    assert status_of(conn, "batch", batch) == "completed"  # the idle rule: 4 h past its start, no call
    assert one(conn, "SELECT stats FROM batch WHERE id = %s", batch["id"])["stats"][IDLE] == NOW.isoformat()
    assert lead_row(conn, lead)["status"] == "QUEUED" and dialled(bolna_stub) == []
    assert report["errors"] == [f"batch {batch['id']}: completed on the idle rule with 1 QUEUED lead(s) in its "
                                f"campaign; not closed before {(NOW + timedelta(hours=24)).isoformat()}"]
    report = new_report()
    rollup_campaigns(conn, report, NOW + timedelta(hours=25), synced_agents=synced(conn))
    row = lead_row(conn, lead)
    assert (row["last_outcome"], row["attempts"], report["errors"]) == ("FAILED", 2, [])


def test_idle_trust_counts_from_the_first_idle_completion_while_bolna_keeps_saying_running(conn, bolna_stub):
    """Bolna keeps reporting 'running': every poll revives the batch and every rollup idle-completes it again. The
    day of trust runs from the first idle completion, not from the batch's (long past) start."""
    camp = make_campaign(conn, agent=make_agent(conn))
    start = NOW - timedelta(hours=30)
    batch = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-1", scheduled_at=start)
    set_col(conn, "batch", batch, stats=Json({"status": "running"}))
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=start)
    bolna_stub.batches = {"bb-1": {"status": "running"}}
    for t in (NOW, NOW + timedelta(minutes=5), NOW + timedelta(hours=12), NOW + timedelta(hours=23)):
        report = new_report()
        sync_batches(conn, report, t)
        rollup_campaigns(conn, report, t, synced_agents=synced(conn))
        row = one(conn, "SELECT status, stats FROM batch WHERE id = %s", batch["id"])
        assert (row["status"], row["stats"][IDLE]) == ("completed", NOW.isoformat())
        assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)
    report = new_report()
    t = NOW + timedelta(hours=25)
    sync_batches(conn, report, t)
    rollup_campaigns(conn, report, t, synced_agents=synced(conn))
    assert (lead_row(conn, lead)["last_outcome"], lead_row(conn, lead)["attempts"]) == ("FAILED", 2)  # closed


def test_idle_completion_is_restamped_after_new_calls(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    start = NOW - timedelta(hours=40)
    batch = make_batch(conn, camp, status="running", total=3, bolna_batch_id="bb-1", scheduled_at=start)
    set_col(conn, "batch", batch, stats=idle_stats("running", NOW - timedelta(hours=30)))  # idle once, then revived
    make_call(conn, phone=P(2), started_at=NOW - timedelta(hours=2), campaign=camp, batch=batch)  # dialled since
    make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    rollup_campaigns(conn, new_report(), NOW, synced_agents=synced(conn))
    assert one(conn, "SELECT stats FROM batch WHERE id = %s", batch["id"])["stats"][IDLE] == NOW.isoformat()


def test_bolna_paused_batch_keeps_its_leads_queued_and_is_reported(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    start = NOW - timedelta(hours=4)
    batch = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-1", scheduled_at=start)
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=start)
    bolna_stub.batches = {"bb-1": {"status": "paused"}}
    errors = {}
    for t in (NOW, NOW + timedelta(hours=25)):  # 4 h, then 29 h past its start, no call
        report = new_report()
        sync_batches(conn, report, t)
        rollup_campaigns(conn, report, t, synced_agents=synced(conn))
        errors[t] = report["errors"]
    assert errors[NOW] == []
    assert errors[NOW + timedelta(hours=25)] == [f"batch {batch['id']} paused at Bolna since {NOW.isoformat()}"]
    assert status_of(conn, "batch", batch) == "running"  # active: it can resume
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)
    assert dialled(bolna_stub) == []


def test_idle_completed_batch_is_polled_again_and_revived_by_bolna(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    start = NOW - timedelta(hours=4)
    idle = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-1", scheduled_at=start)
    old = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-2",
                     scheduled_at=NOW - timedelta(hours=30))  # idle-completed 25 h ago: past the trust, not asked
    confirmed = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-3", scheduled_at=start)
    long_ago = NOW - timedelta(days=30)  # not running at Bolna: asked however old
    odd = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-4", scheduled_at=long_ago)
    paused = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-5", scheduled_at=long_ago)
    for b, raw in ((idle, "running"), (confirmed, "completed"), (odd, "on_hold"), (paused, "paused")):
        set_col(conn, "batch", b, stats=idle_stats(raw, NOW - timedelta(hours=1)))
    set_col(conn, "batch", old, stats=idle_stats("running", NOW - timedelta(hours=25)))
    bolna_stub.batches = {"bb-1": {"status": "running"}, "bb-4": {"status": "on_hold"}, "bb-5": {"status": "paused"}}
    report = new_report()
    sync_batches(conn, report, NOW)
    assert [status_of(conn, "batch", b) for b in (idle, old, confirmed, odd, paused)] == [
        "running", "completed", "completed", "completed", "running"]
    assert sorted(a[0] for n, a in bolna_stub.calls if n == "get_batch") == ["bb-1", "bb-4", "bb-5"]
    assert report["batches_checked"] == 3 and report["errors"] == ["batch bb-4: unknown Bolna status on_hold"]
    for b in (idle, odd, paused):  # Bolna's answer replaces the stats; the idle completion is carried over
        assert one(conn, "SELECT stats FROM batch WHERE id = %s", b["id"])["stats"][IDLE] == \
            (NOW - timedelta(hours=1)).isoformat()


def test_unknown_bolna_status_never_idle_completes_the_batch(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn), redial_policy={
        **FACTORY_POLICY, "retry_outcomes": ["FAILED"], "gap_minutes": 15})  # a closed lead would be redialled
    start = NOW - timedelta(hours=4)
    batch = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-1", scheduled_at=start)
    set_col(conn, "batch", batch, stats=Json({"status": "running"}))  # what Bolna said on the last poll
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=start)
    bolna_stub.batches = {"bb-1": {"status": "on_hold", "n": 1}}
    for t in (NOW, NOW + timedelta(hours=25)):  # 4 h, then 29 h past its start, no call
        report = new_report()
        sync_batches(conn, report, t)
        rollup_campaigns(conn, report, t, synced_agents=synced(conn))
        assert report["errors"] == ["batch bb-1: unknown Bolna status on_hold"]  # on every sync, and only that
        row = one(conn, "SELECT status, stats FROM batch WHERE id = %s", batch["id"])
        assert (row["status"], row["stats"]) == ("running", {"status": "on_hold", "n": 1})  # status kept
    assert [a[0] for n, a in bolna_stub.calls if n == "get_batch"] == ["bb-1", "bb-1"]  # still polled
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)
    assert dialled(bolna_stub) == []


def test_idle_completed_batch_bolna_puts_on_hold_keeps_its_leads_until_bolna_finishes(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn), redial_policy={
        **FACTORY_POLICY, "retry_outcomes": ["FAILED"], "gap_minutes": 15})  # a closed lead would be redialled
    start = NOW - timedelta(hours=4)
    batch = make_batch(conn, camp, status="completed", total=2, bolna_batch_id="bb-1", scheduled_at=start)
    set_col(conn, "batch", batch, stats=Json({"status": "running"}))  # completed on the idle rule
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=start)
    bolna_stub.batches = {"bb-1": {"status": "on_hold"}}
    for t in (NOW, start + timedelta(hours=25), start + timedelta(days=30)):  # polled and held at any age
        report = new_report()
        sync_batches(conn, report, t)
        rollup_campaigns(conn, report, t, synced_agents=synced(conn))
        assert report["errors"] == [
            "batch bb-1: unknown Bolna status on_hold",
            f"batch {batch['id']}: completed on the idle rule with 1 QUEUED lead(s) in its campaign; "
            "not closed while Bolna reports on_hold"]
        assert status_of(conn, "batch", batch) == "completed"
    assert [a[0] for n, a in bolna_stub.calls if n == "get_batch"] == ["bb-1"] * 3
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)
    assert dialled(bolna_stub) == []
    bolna_stub.batches = {"bb-1": {"status": "completed"}}  # Bolna confirms: the lead is closed at once
    t = start + timedelta(days=30, hours=4)  # 11:00 IST: inside the window, so auto-redial may open a round
    report = new_report()
    sync_batches(conn, report, t)
    rollup_campaigns(conn, report, t, synced_agents=synced(conn))
    assert report["errors"] == [] and lead_row(conn, lead)["attempts"] == 2 and len(dialled(bolna_stub)) == 1


def test_batch_bolna_never_starts_is_reported_a_day_past_its_start(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    start, later = NOW - timedelta(hours=4), NOW + timedelta(hours=25)
    late = make_batch(conn, camp, status="scheduled", total=2, bolna_batch_id="bb-late", scheduled_at=start)
    paused = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-paused", scheduled_at=start)
    live = make_batch(conn, camp, status="running", total=2, bolna_batch_id="bb-live", scheduled_at=start)
    make_call(conn, phone=P(2), started_at=later - timedelta(minutes=5), campaign=camp, batch=live)  # dialling
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=start)
    bolna_stub.batches = {"bb-late": {"status": "Queued"}, "bb-paused": {"status": "paused"},
                          "bb-live": {"status": "running"}}
    errors = {}
    for t in (NOW, later):  # 4 h, then 29 h past their start
        report = new_report()
        sync_batches(conn, report, t)
        rollup_campaigns(conn, report, t, synced_agents=synced(conn))
        errors[t] = report["errors"]
    assert errors[NOW] == []
    assert sorted(errors[later]) == sorted([
        f"batch {late['id']} still Queued at Bolna since {start.isoformat()}",
        f"batch {paused['id']} paused at Bolna since {NOW.isoformat()}"])  # a paused batch: its own report only
    assert [status_of(conn, "batch", b) for b in (late, paused, live)] == ["running"] * 3  # active
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)
    assert dialled(bolna_stub) == []


def test_lead_stuck_behind_an_unfinished_call_is_reported(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    done = make_batch(conn, camp, status="completed", bolna_batch_id="bb-1")
    set_col(conn, "batch", done, stats=Json({"status": "completed"}))
    stuck = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1, last_call_at=NOW - timedelta(hours=25))
    make_call(conn, phone=P(1), started_at=NOW - timedelta(hours=25), status="in-progress", outcome="UNKNOWN",
              campaign=camp, lead=stuck, bolna_execution_id="ex-stuck")
    recent = make_lead(conn, camp, phone=P(2), status="QUEUED", attempts=1, last_call_at=NOW - timedelta(hours=1))
    make_call(conn, phone=P(2), started_at=NOW - timedelta(hours=1), status="in-progress", outcome="UNKNOWN",
              campaign=camp, lead=recent)
    report = new_report()
    rollup_campaigns(conn, report, NOW, synced_agents=synced(conn))
    assert report["errors"] == [f"lead {stuck['id']} stuck QUEUED behind unfinished call ex-stuck"]
    assert [lead_row(conn, x)["status"] for x in (stuck, recent)] == ["QUEUED", "QUEUED"]


def test_lead_stuck_behind_an_unstarted_call_after_an_earlier_finished_one_is_reported(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    done = make_batch(conn, camp, status="completed", bolna_batch_id="bb-1")
    set_col(conn, "batch", done, stats=Json({"status": "completed"}))
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=2, last_call_at=NOW - timedelta(hours=30))
    make_call(conn, phone=P(1), started_at=NOW - timedelta(hours=30), status="no-answer", outcome="NOT_PICKED",
              campaign=camp, lead=lead)  # an earlier attempt, finished
    make_call(conn, phone=P(1), started_at=None, status="in-progress", outcome="UNKNOWN", campaign=camp, lead=lead,
              bolna_execution_id="ex-no-start")  # the latest: Bolna gave no created_at
    report = new_report()
    rollup_campaigns(conn, report, NOW, synced_agents=synced(conn))
    assert report["errors"] == [f"lead {lead['id']} stuck QUEUED behind unfinished call ex-no-start"]
    assert lead_row(conn, lead)["status"] == "QUEUED"


def test_scheduled_campaign_becomes_running_once_a_batch_runs(conn, bolna_stub):
    agent = make_agent(conn)
    started = make_campaign(conn, agent=agent, status="SCHEDULED")
    make_batch(conn, started, status="running", bolna_batch_id="bb-1", total=2)
    make_lead(conn, started, phone=P(1), status="QUEUED")
    waiting = make_campaign(conn, agent=agent, status="SCHEDULED")
    make_batch(conn, waiting, status="scheduled", bolna_batch_id="bb-2", scheduled_at=NOW + timedelta(hours=1))
    make_lead(conn, waiting, phone=P(2), status="QUEUED")
    rollup_campaigns(conn, new_report(), NOW, synced_agents=synced(conn))
    assert status_of(conn, "campaign", started) == "RUNNING" and status_of(conn, "campaign", waiting) == "SCHEDULED"


def test_rollup_failure_in_one_campaign_does_not_stop_others(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    bad, good = make_campaign(conn, agent=agent), make_campaign(conn, agent=agent)
    set_col(conn, "campaign", bad, created_at=NOW - timedelta(days=1))  # rolled up first
    for camp in (bad, good):
        make_batch(conn, camp, status="completed", bolna_batch_id=f"bb-{camp['id']}")
        make_lead(conn, camp, phone=P(1), status="DONE", attempts=1)
    real = redial.maybe_auto_redial

    def flaky(conn, campaign, now):
        if campaign["id"] == bad["id"]:
            with conn.cursor() as cur:
                cur.execute("SELECT 1/0")  # leaves the transaction aborted
        return real(conn, campaign, now)

    monkeypatch.setattr(redial, "maybe_auto_redial", flaky)
    report = new_report()
    rollup_campaigns(conn, report, NOW, synced_agents=synced(conn))
    assert report["errors"] == [f"rollup {bad['id']}: division by zero"]
    assert status_of(conn, "campaign", bad) == "RUNNING" and status_of(conn, "campaign", good) == "COMPLETED"


# ---------------------------------------------------------------- retry_rules

def test_retry_rules_applies_rules_calls_missed(conn, monkeypatch):
    monkeypatch.setattr(config, "INTERAKT_API_KEY", "")
    agent = make_agent(conn)
    uc = make_use_case(conn)
    wf = _workflow(conn, agent, uc, [{"when": {"outcomes": ["NOT_PICKED"]}, "actions": [
        {"type": "redial", "after_minutes": 30}, {"type": "whatsapp", "template": "hi"}]}])
    camp = make_campaign(conn, agent=agent, use_case_id=uc["id"], workflow_id=wf["id"])
    lead = make_lead(conn, camp, phone=P(1), status="DONE", attempts=2, last_call_at=NOW - timedelta(minutes=10),
                     final_tag="qualified")
    late = make_call(conn, phone=P(1), started_at=NOW - timedelta(minutes=30), campaign=camp, lead=lead,
                     outcome="NOT_PICKED", status="no-answer")  # older than the lead's last call: stale
    waiting = make_call(conn, phone=P(2), started_at=NOW - timedelta(minutes=5), campaign=camp,
                        outcome="NOT_PICKED", status="no-answer")
    live = make_call(conn, phone=P(3), started_at=NOW - timedelta(minutes=30), campaign=camp, outcome="UNKNOWN",
                     status="in-progress")
    report = new_report()
    retry_rules(conn, report, NOW, attempted=set())
    stamps = [one(conn, "SELECT rules_applied_at FROM call WHERE id = %s", c["id"])["rules_applied_at"]
              for c in (late, waiting, live)]
    assert stamps == [NOW, None, None]
    row = lead_row(conn, lead)
    assert (row["status"], row["next_dial_at"]) == ("DONE", None)  # the stale redial was skipped
    assert report["rules_applied"] == 1
    assert report["errors"] == [
        f"rules {late['bolna_execution_id']}: whatsapp:error:WhatsApp not configured (INTERAKT_API_KEY)"]


def test_retry_rules_failure_is_rolled_back_and_the_rest_carry_on(conn, monkeypatch):
    camp = make_campaign(conn, agent=make_agent(conn))
    bad = make_call(conn, phone=P(1), started_at=NOW - timedelta(hours=2), campaign=camp, outcome="NOT_PICKED",
                    status="no-answer")
    good = make_call(conn, phone=P(2), started_at=NOW - timedelta(hours=1), campaign=camp, outcome="NOT_PICKED",
                     status="no-answer")
    real = workflows.apply_result_rules

    def flaky(conn, call, campaign, lead, now=None, stale=False):
        if call["id"] == bad["id"]:
            with conn.cursor() as cur:
                cur.execute("UPDATE call SET rules_applied_at = now() WHERE id = %s", (call["id"],))
                cur.execute("SELECT 1/0")
        return real(conn, call, campaign, lead, now=now, stale=stale)

    monkeypatch.setattr(workflows, "apply_result_rules", flaky)
    report, attempted = new_report(), set()
    retry_rules(conn, report, NOW, attempted=attempted)
    assert report["errors"] == [f"rules {bad['bolna_execution_id']}: division by zero"]
    assert one(conn, "SELECT rules_applied_at FROM call WHERE id = %s", bad["id"])["rules_applied_at"] is None
    assert one(conn, "SELECT rules_applied_at FROM call WHERE id = %s", good["id"])["rules_applied_at"] == NOW
    assert attempted == {bad["id"], good["id"]}
    report = new_report()
    retry_rules(conn, report, NOW, attempted=attempted)  # later in the same run: not tried (or reported) again
    assert report["errors"] == []
    retry_rules(conn, report, NOW, attempted=set())  # the next run tries it again
    assert report["errors"] == [f"rules {bad['bolna_execution_id']}: division by zero"]


def test_rules_failing_in_the_executions_step_are_reported_once_per_run(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    make_lead(conn, camp, phone=P(1), status="QUEUED")
    make_batch(conn, camp, status="running", bolna_batch_id="bb-1")
    conn.commit()
    now = datetime.now(timezone.utc)  # run_sync runs on the wall clock
    e = ex(agent["bolna_agent_id"], P(1), at=now - timedelta(minutes=30), batch_id="bb-1")  # past the wait
    bolna_stub.executions = {agent["bolna_agent_id"]: [[e]]}

    def broken(conn, call, campaign, lead, now=None, stale=False):
        raise RuntimeError("rules down")

    monkeypatch.setattr(workflows, "apply_result_rules", broken)
    for _ in range(2):  # a lasting failure: once per run, not again in the rules step
        r = run_sync()
        assert r["errors"] == [f"rules {e['id']}: rules down"]
    assert call_row(conn, e)["rules_applied_at"] is None


def test_a_call_without_a_start_time_counts_as_old(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    done = make_batch(conn, camp, status="completed", bolna_batch_id="bb-1")
    set_col(conn, "batch", done, stats=Json({"status": "completed"}))
    unstarted = make_call(conn, phone=P(1), started_at=None, campaign=camp, outcome="NOT_PICKED", status="no-answer")
    older = make_call(conn, phone=P(2), started_at=NOW - timedelta(hours=1), campaign=camp, outcome="NOT_PICKED",
                      status="no-answer")
    retry_rules(conn, new_report(), NOW, attempted=set(), limit=1)  # past the wait, and the oldest
    assert [one(conn, "SELECT rules_applied_at FROM call WHERE id = %s", c["id"])["rules_applied_at"]
            for c in (unstarted, older)] == [NOW, None]
    stuck = make_lead(conn, camp, phone=P(3), status="QUEUED", attempts=1)
    make_call(conn, phone=P(3), started_at=None, status="in-progress", outcome="UNKNOWN", campaign=camp, lead=stuck,
              bolna_execution_id="ex-unstarted")
    report = new_report()
    rollup_campaigns(conn, report, NOW, synced_agents=synced(conn))
    assert report["errors"] == [f"lead {stuck['id']} stuck QUEUED behind unfinished call ex-unstarted"]
    assert lead_row(conn, stuck)["status"] == "QUEUED"


def test_a_cursor_that_cannot_be_saved_does_not_stop_other_agents(conn, bolna_stub, monkeypatch):
    a, b = make_agent(conn, bolna_agent_id="ba-a"), make_agent(conn, bolna_agent_id="ba-b")
    for agent in (a, b):
        make_campaign(conn, agent=agent)
    set_col(conn, "agent", a, created_at=NOW - timedelta(days=1))  # synced first
    real = sync.set_setting

    def flaky(conn, key, value):
        if key == cursor_key(a["id"]):
            with conn.cursor() as cur:
                cur.execute("SELECT 1/0")
        return real(conn, key, value)

    monkeypatch.setattr(sync, "set_setting", flaky)
    report = new_report()
    assert sync_executions(conn, report, max_pages=2, now=NOW) == {b["id"]}
    assert report["errors"] == [f"executions {a['name']} (ba-a): division by zero"]
    assert get_setting(conn, cursor_key(a["id"])) is None
    assert get_setting(conn, cursor_key(b["id"])) == NOW.isoformat()


# ---------------------------------------------------------------- the window end

def _live_at_window_end(conn, agent=None, *, kind="FIRST", start=ist(18)):
    """A RUNNING campaign with a batch Bolna is dialling since `start`: `untouched` was never reached, `called` got a
    no-answer call (the sync rolls it up as usual)."""
    agent = agent or make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    batch = _insert(conn, "batch", campaign_id=camp["id"], status="running", total=2, bolna_batch_id="bb-1",
                    scheduled_at=start, kind=kind, from_number=camp["from_number"], stats=Json({"status": "running"}))
    untouched = make_lead(conn, camp, phone=P(1), status="QUEUED")
    called = make_lead(conn, camp, phone=P(2), status="QUEUED")
    for lead in (untouched, called):
        set_col(conn, "lead", lead, next_dial_at=start)
    conn.commit()
    upsert_execution(conn, bolna_execution(
        agent_id=agent["bolna_agent_id"], created_at=(start + timedelta(minutes=20)).isoformat(), batch_id="bb-1",
        status="no-answer", telephony_data={"to_number": P(2), "from_number": "+918000000001",
                                           "hangup_provider_code": 3020}), now=start + timedelta(minutes=30))
    return camp, batch, untouched, called


def _sync_at(conn, bolna_stub, t, *, synced_agents=None):
    report = new_report()
    sync_batches(conn, report, t)
    agents = synced(conn) if synced_agents is None else synced_agents
    for a in agents:  # what sync_executions stamps for an agent it fetched fully from t
        set_setting(conn, cursor_key(a), t.isoformat())
    conn.commit()
    rollup_campaigns(conn, report, t, synced_agents=agents)
    return report


def test_batch_live_at_window_end_is_stopped_and_its_untouched_leads_wait_for_the_next_window(conn, bolna_stub):
    camp, batch, untouched, called = _live_at_window_end(conn)
    bolna_stub.batches = {"bb-1": {"status": "running"}}
    assert lead_row(conn, called)["status"] == "NOT_CONNECTED"  # rolled up as usual
    report = _sync_at(conn, bolna_stub, ist(18, 55))  # inside the window: nothing happens
    assert report["window_stops"] == [] and ("stop_batch", ("bb-1",)) not in bolna_stub.calls

    report = _sync_at(conn, bolna_stub, ist(19, 5))
    assert ("stop_batch", ("bb-1",)) in bolna_stub.calls
    row = one(conn, "SELECT status, stats FROM batch WHERE id = %s", batch["id"])
    assert row["status"] == "stopped" and row["stats"]["console_stopped"] == sync._iso(ist(19, 5) + sync.STOP_SETTLE)
    assert report["window_stops"] == [f"batch {batch['id']}: stopped at window end"] and report["errors"] == []
    assert lead_row(conn, untouched)["status"] == "QUEUED"  # closed once a later sync has fetched every call

    report = _sync_at(conn, bolna_stub, ist(19, 10))
    row = lead_row(conn, untouched)
    assert (row["status"], row["last_outcome"], row["attempts"], row["next_dial_at"]) == (
        "NOT_CONNECTED", None, 0, None)  # no attempt burned
    assert (lead_row(conn, called)["status"], lead_row(conn, called)["attempts"]) == ("NOT_CONNECTED", 1)
    assert dialled(bolna_stub) == [] and status_of(conn, "campaign", camp) == "RUNNING"  # outside: no round
    camp_row = one(conn, "SELECT * FROM campaign WHERE id = %s", camp["id"])
    assert untouched["id"] in {x["id"] for x in redial.eligible_redial_leads(conn, camp_row, ist(10, 5, day=1))}

    report = _sync_at(conn, bolna_stub, ist(10, 5, day=1))  # the next window: auto-redial takes it
    assert report["redial_rounds"] == 1 and report["errors"] == []
    [(_, _, csv_text, _)] = dialled(bolna_stub)
    assert P(1) in csv_text and lead_row(conn, untouched)["status"] == "QUEUED"


def test_window_stopped_leads_wait_for_a_full_sync_of_the_agent(conn, bolna_stub):
    camp, batch, untouched, called = _live_at_window_end(conn)
    bolna_stub.batches = {"bb-1": {"status": "running"}}
    _sync_at(conn, bolna_stub, ist(19, 5))
    _sync_at(conn, bolna_stub, ist(19, 10), synced_agents=set())
    assert lead_row(conn, untouched)["status"] == "QUEUED"


def test_window_stop_never_touches_a_batch_scheduled_for_the_next_window(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))
    batch = make_batch(conn, camp, status="scheduled", bolna_batch_id="bb-1", scheduled_at=ist(10, day=1))
    early = make_batch(conn, camp, status="scheduled", bolna_batch_id="bb-2", scheduled_at=ist(9, 59, day=1))
    bolna_stub.batches = {"bb-1": {"status": "scheduled"}, "bb-2": {"status": "scheduled"}}
    for t in (ist(20), ist(9, 59, day=1) + timedelta(seconds=30)):  # overnight; a dispatch a minute early
        report = _sync_at(conn, bolna_stub, t)
        assert report["window_stops"] == [] and report["errors"] == []
    assert [c for c in bolna_stub.calls if c[0] == "stop_batch"] == []
    assert [status_of(conn, "batch", b) for b in (batch, early)] == ["scheduled", "scheduled"]


def test_window_stop_catches_a_batch_that_started_just_after_the_window_closed(conn, bolna_stub):
    camp = make_campaign(conn, agent=make_agent(conn))  # e.g. a round opened at 18:59:30, scheduled 60 s later
    late = make_batch(conn, camp, status="scheduled", bolna_batch_id="bb-1", scheduled_at=ist(19, 0) +
                      timedelta(seconds=30))
    bolna_stub.batches = {"bb-1": {"status": "running"}}
    report = _sync_at(conn, bolna_stub, ist(19, 5))
    assert report["window_stops"] == [f"batch {late['id']}: stopped at window end"]
    assert status_of(conn, "batch", late) == "stopped"


def test_redial_batches_stop_at_the_policy_window_end_first_rounds_at_the_department_window_end(conn, bolna_stub):
    with conn.cursor() as cur:  # department window 09:00-21:00; the campaigns' redial policy window 10:00-19:00
        cur.execute("UPDATE department SET window_start = '09:00', window_end = '21:00' WHERE key = 'SUPPLY'")
    agent = make_agent(conn)
    first = make_campaign(conn, agent=agent)
    redial_camp = make_campaign(conn, agent=agent)
    for camp, kind, bid in ((first, "FIRST", "bb-f"), (redial_camp, "REDIAL", "bb-r")):
        _insert(conn, "batch", campaign_id=camp["id"], status="running", total=1, bolna_batch_id=bid,
                scheduled_at=ist(18, 30), kind=kind, from_number=camp["from_number"],
                stats=Json({"status": "running"}))
    bolna_stub.batches = {"bb-f": {"status": "running"}, "bb-r": {"status": "running"}}
    _sync_at(conn, bolna_stub, ist(19, 30))
    assert [a[0] for n, a in bolna_stub.calls if n == "stop_batch"] == ["bb-r"]
    _sync_at(conn, bolna_stub, ist(21, 5))
    assert [a[0] for n, a in bolna_stub.calls if n == "stop_batch"] == ["bb-r", "bb-f"]


def test_window_stop_failure_is_reported_and_the_batch_stays_live(conn, bolna_stub):
    camp, batch, untouched, called = _live_at_window_end(conn)
    bolna_stub.batches = {"bb-1": {"status": "running"}}
    bolna_stub.fail_on = {"stop_batch"}
    report = _sync_at(conn, bolna_stub, ist(19, 5))
    assert report["window_stops"] == []
    assert report["errors"] == [f"batch {batch['id']}: stop at window end failed: Bolna 500: <html>"]
    assert status_of(conn, "batch", batch) == "running" and lead_row(conn, untouched)["status"] == "QUEUED"


# ---------------------------------------------------------------- recordings

def test_archive_recordings(conn, monkeypatch):
    camp = make_campaign(conn, agent=make_agent(conn))
    calls = [make_call(conn, phone=P(i), started_at=NOW - timedelta(hours=i), campaign=camp) for i in (1, 2, 3)]
    set_col(conn, "call", calls[0], recording_url="https://example.com/a.mp3")
    set_col(conn, "call", calls[1], recording_url="https://example.com/gone.mp3")
    set_col(conn, "call", calls[2], recording_url="https://example.com/c.mp3", recording_key="recordings/old.mp3")
    archived = []

    def archive(execution_id, url, when):
        if "gone" in url:
            raise RuntimeError("recording fetch 404")
        archived.append((execution_id, when))
        return f"recordings/{execution_id}.mp3"

    monkeypatch.setattr(sync.storage, "storage_enabled", lambda: True)
    monkeypatch.setattr(sync.storage, "archive_recording", archive)
    report = new_report()
    sync.archive_recordings(conn, report)
    first, gone = calls[0]["bolna_execution_id"], calls[1]["bolna_execution_id"]
    assert archived == [(first, NOW - timedelta(hours=1))] and archived[0][1].tzinfo == timezone.utc
    assert report["recordings_archived"] == 1 and report["errors"] == [f"recording {gone}: recording fetch 404"]
    assert one(conn, "SELECT recording_key FROM call WHERE id = %s", calls[0]["id"])["recording_key"] == \
        f"recordings/{first}.mp3"
    monkeypatch.setattr(sync.storage, "storage_enabled", lambda: False)
    sync.archive_recordings(conn, new_report())  # not configured: nothing fetched
    assert len(archived) == 1


@pytest.mark.parametrize("exc", ["timeout", "connection"])
def test_unreachable_recording_host_stops_archiving_for_the_run(conn, monkeypatch, exc):
    import botocore.exceptions
    import requests
    camp = make_campaign(conn, agent=make_agent(conn))
    for i in (1, 2, 3):
        c = make_call(conn, phone=P(i), started_at=NOW - timedelta(hours=i), campaign=camp)
        set_col(conn, "call", c, recording_url=f"https://example.com/{i}.mp3")
    tried = []

    def archive(execution_id, url, when):
        tried.append(url)
        if exc == "timeout":
            raise requests.Timeout("read timed out")
        raise botocore.exceptions.EndpointConnectionError(endpoint_url="https://r2.example")

    monkeypatch.setattr(sync.storage, "storage_enabled", lambda: True)
    monkeypatch.setattr(sync.storage, "archive_recording", archive)
    report = new_report()
    sync.archive_recordings(conn, report)
    assert len(tried) == 1 and report["errors"] == ["recordings: unreachable — stopping this run"]
    assert report["recordings_archived"] == 0


# ---------------------------------------------------------------- run_sync

def test_run_sync_returns_none_when_locked(conn):
    other = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        other.cursor().execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
        assert LOCK_KEY == 42100001
        assert run_sync() is None
    finally:
        other.close()
    assert run_sync() is not None


def _try_lock_elsewhere() -> bool:
    """Whether a separate session can take the sync lock right now (released again at once)."""
    other = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with other.cursor() as cur:
            cur.execute("select pg_try_advisory_xact_lock(%s)", (LOCK_KEY,))
            return cur.fetchone()[0]
    finally:
        other.rollback()
        other.close()


def test_lock_released_after_success_is_reacquirable(conn):
    assert run_sync() is not None
    assert _try_lock_elsewhere() is True
    assert run_sync() is not None


def test_lock_is_held_for_the_whole_run(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(sync, "archive_recordings", lambda conn, report: seen.append(_try_lock_elsewhere()))
    assert run_sync() is not None
    assert seen == [False]  # a late step still holds the lock, although the work connection committed many times
    with sync.sync_lock() as got:
        assert got is True and _try_lock_elsewhere() is False
        with sync.sync_lock() as again:
            assert again is False
    assert _try_lock_elsewhere() is True


def test_lock_released_when_the_run_raises(conn, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sync, "set_setting", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        run_sync()
    monkeypatch.undo()
    assert run_sync() is not None


def test_one_agent_failing_does_not_stop_others(conn, bolna_stub, monkeypatch):
    freeze(monkeypatch)
    a, b = make_agent(conn, bolna_agent_id="ba-A"), make_agent(conn, bolna_agent_id="ba-B")
    make_lead(conn, make_campaign(conn, agent=a), phone=P(1))
    cb = make_campaign(conn, agent=b)
    lead = make_lead(conn, cb, phone=P(2), status="QUEUED")
    make_batch(conn, cb, status="running", bolna_batch_id="bb-B")
    conn.commit()
    bolna_stub.fail_agents = {"ba-A"}
    bolna_stub.executions = {"ba-B": [[bolna_execution(agent_id="ba-B", batch_id="bb-B", created_at=(
        NOW - timedelta(minutes=30)).isoformat(), telephony_data={
        "to_number": P(2), "from_number": "+918000000001", "hangup_provider_code": 4000})]]}
    r = run_sync()
    assert r["errors"] == ["executions Agent ba-A (ba-A): Bolna 502: <html>"]
    assert any("executions" in e and "502" in e for e in r["errors"]) and r["calls_upserted"] >= 1  # agent B synced
    assert lead_row(conn, lead)["status"] == "DONE" and r["campaigns_completed"] == 1
    assert run_sync() is not None  # the lock was released


def _stale_batch(conn, agent):
    """A RUNNING campaign of `agent` whose batch Bolna reports completed, with a QUEUED lead no call in the database
    reached, and a policy that redials FAILED leads at once (run with the clock frozen at NOW)."""
    now = NOW
    camp = make_campaign(conn, agent=agent, redial_policy={**FACTORY_POLICY, "retry_outcomes": ["FAILED"],
                                                           "gap_minutes": 15})
    make_batch(conn, camp, status="running", bolna_batch_id=f"bb-{agent['bolna_agent_id']}",
               scheduled_at=now - timedelta(hours=2))
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED", attempts=1)
    set_col(conn, "lead", lead, next_dial_at=now - timedelta(hours=2))
    conn.commit()
    return lead


def test_failed_executions_sync_never_closes_queued_leads(conn, bolna_stub, monkeypatch):
    freeze(monkeypatch)
    lead = _stale_batch(conn, make_agent(conn, bolna_agent_id="ba-A"))
    bolna_stub.fail_agents = {"ba-A"}
    r = run_sync()
    assert r["errors"] == ["executions Agent ba-A (ba-A): Bolna 502: <html>"]
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)
    assert dialled(bolna_stub) == [] and r["redial_rounds"] == 0
    # once ba-A syncs fully and still no call reached the lead, it is closed and redialled
    bolna_stub.fail_agents = set()
    r = run_sync()
    assert r["errors"] == [] and r["redial_rounds"] == 1 and len(dialled(bolna_stub)) == 1
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 2)


def test_capped_executions_sync_never_closes_queued_leads(conn, bolna_stub, monkeypatch):
    freeze(monkeypatch)
    agent = make_agent(conn, bolna_agent_id="ba-A")
    lead = _stale_batch(conn, agent)
    now = NOW
    bolna_stub.executions = {"ba-A": [[ex("ba-A", P(8), at=now - timedelta(minutes=10))],
                                      [ex("ba-A", P(9), at=now - timedelta(minutes=20))]]}
    r = run_sync(max_pages=1)
    assert r["errors"] == ["executions Agent ba-A (ba-A): capped at 1 pages before reaching lookback"]
    assert r["calls_upserted"] == 1
    assert (lead_row(conn, lead)["status"], lead_row(conn, lead)["attempts"]) == ("QUEUED", 1)
    assert dialled(bolna_stub) == []
    assert get_setting(conn, cursor_key(agent["id"])) is None  # the cursor stays


def test_sync_pushes_to_di_and_saves_report(conn, bolna_stub, monkeypatch):
    freeze(monkeypatch)
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    sent = []

    class Res:
        status_code = 200
        text = "ok"

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return Res()

    monkeypatch.setattr(di_push.requests, "post", post)
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    make_lead(conn, camp, phone=P(1), status="QUEUED")
    make_batch(conn, camp, status="running", bolna_batch_id="bb-1")
    conn.commit()
    e = bolna_execution(agent_id=agent["bolna_agent_id"], batch_id="bb-1", created_at=(NOW - timedelta(minutes=30)
                                                                                         ).isoformat(),
                        telephony_data={"to_number": P(1), "from_number": "+918000000001", "hangup_provider_code": 4000})
    bolna_stub.executions = {agent["bolna_agent_id"]: [[e]]}
    r = run_sync()
    assert r["errors"] == [] and r["di_pushed"] == 1 and r["calls_upserted"] == 1 and r["full"] is False
    assert r["finished_at"] is not None and [p["call_id"] for p in sent] == [e["id"]]
    assert get_setting(conn, "lastSyncReport") == r
    assert get_setting(conn, "lastFullSyncDate") is None


def test_full_sync_syncs_agents_and_records_the_date(conn, bolna_stub):
    bolna_stub.agents = [{
        "agent_id": "ba-new", "agent_name": "Seller Qualifier", "agent_welcome_message": "Namaste {{owner_name}}",
        "agent_prompts": {"task_1": {"system_prompt": "Ask about {{society_name}}"}},
        "tasks": [{"tools_config": {"llm_agent": {"llm_config": {"model": "x"}}}, "task_config": {"t": 1}}],
    }]
    before = datetime.now(IST).date().isoformat()
    r = run_sync(full=True)
    after = datetime.now(IST).date().isoformat()
    assert r["full"] is True and r["errors"] == []
    row = one(conn, "SELECT * FROM agent WHERE bolna_agent_id = 'ba-new'")
    assert (row["name"], row["variables"], row["welcome_message"]) == (
        "Seller Qualifier", ["owner_name", "society_name"], "Namaste {{owner_name}}")
    assert row["config"]["llm"] == {"model": "x"} and row["config"]["prompt"] == "Ask about {{society_name}}"
    assert row["config"]["task"] == {"t": 1} and row["vertical"] == "SUPPLY"
    assert get_setting(conn, "lastFullSyncDate") in {before, after}
    assert ("list_agent_executions", ("ba-new", 1, 50)) in bolna_stub.calls


def test_sync_agents_reports_an_agent_it_could_not_read(conn, bolna_stub, monkeypatch):
    bolna_stub.agents = [{"agent_id": "ba-1", "agent_name": "One"}, {"agent_id": "ba-2", "agent_name": "Two"}]

    def get_agent(agent_id):
        if agent_id == "ba-2":
            raise bolna.BolnaError(404, "not found")
        return {"agent_id": agent_id, "agent_name": "One"}

    monkeypatch.setattr(bolna, "get_agent", get_agent)
    with pytest.raises(RuntimeError, match="ba-2: Bolna 404"):
        sync_agents(conn)
    assert one(conn, "SELECT name FROM agent WHERE bolna_agent_id = 'ba-1'")["name"] == "One"
    assert one(conn, "SELECT count(*) AS n FROM agent")["n"] == 1


def test_is_full_due():
    assert is_full_due(ist(2, 59), "2026-11-18") is False
    assert is_full_due(ist(3, 1), "2026-11-18") is True
    assert is_full_due(ist(3, 1), "2026-11-19") is False
    assert is_full_due(ist(3, 1), None) is True
    assert is_full_due(ist(2, 59), None) is False


# ---------------------------------------------------------------- background thread and CLI

REQUIRED = ("BOLNA_API_KEY", "R2_BLOB_PHONE_CALLS_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
            "R2_PHONE_CALLS_PUBLIC_URL", "DI_AI_CALLS_URL", "DI_AI_CALLS_TOKEN")


def _configure(monkeypatch, **overrides):
    monkeypatch.setattr(config, "SYNC_ENABLED", "1")
    for k in REQUIRED:
        monkeypatch.setattr(config, k, "set")
    for k, v in overrides.items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(sync, "_started", False)
    ran = []
    monkeypatch.setattr(sync, "_loop", lambda: ran.append(1))
    return ran


def _join_sync_threads():
    for t in threading.enumerate():
        if t.name == "sync":
            t.join(timeout=5)


def test_background_thread_needs_integrations(monkeypatch):
    ran = _configure(monkeypatch, R2_BLOB_PHONE_CALLS_BUCKET="", DI_AI_CALLS_TOKEN="")
    with pytest.raises(RuntimeError, match="R2_BLOB_PHONE_CALLS_BUCKET, DI_AI_CALLS_TOKEN"):
        start_background_sync()
    _join_sync_threads()
    assert ran == [] and sync._started is False


def test_background_thread_off_when_disabled(monkeypatch):
    ran = _configure(monkeypatch, SYNC_ENABLED="0", BOLNA_API_KEY="")
    start_background_sync()
    _join_sync_threads()
    assert ran == []


def test_background_thread_starts_once(monkeypatch):
    ran = _configure(monkeypatch)
    start_background_sync()
    start_background_sync()
    _join_sync_threads()
    assert ran == [1]


def test_tests_never_see_production_integrations():
    """conftest blanks every backend/.env key: a test run once POSTed fake calls to production DI."""
    for k in ("DI_AI_CALLS_URL", "DI_AI_CALLS_TOKEN", "R2_BLOB_PHONE_CALLS_BUCKET", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY", "R2_PHONE_CALLS_PUBLIC_URL", "INTERAKT_API_KEY", "INGEST_TOKEN"):
        assert getattr(config, k) == "", k
    assert "localhost" in config.DATABASE_URL


def _load_file(name):
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    spec = importlib.util.spec_from_file_location(f"_t_{name.replace('.', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_importing_wsgi_never_starts_the_sync_thread(monkeypatch):
    """--preload imports wsgi.py in the gunicorn master; a thread started there deadlocked a forked worker."""
    ran = _configure(monkeypatch)
    _load_file("wsgi.py")
    _join_sync_threads()
    assert ran == [] and sync._started is False
    _configure(monkeypatch, DI_AI_CALLS_TOKEN="")
    with pytest.raises(RuntimeError, match="DI_AI_CALLS_TOKEN"):  # a missing integration still fails the deploy
        _load_file("wsgi.py")


def test_gunicorn_post_fork_starts_the_sync_thread_in_the_worker(monkeypatch):
    ran = _configure(monkeypatch)
    _load_file("gunicorn.conf.py").post_fork(None, None)
    _join_sync_threads()
    assert ran == [1]


@pytest.mark.parametrize("report, code", [(None, 1), ({"errors": []}, 0), ({"errors": ["batch bb-1: x"]}, 1)])
def test_run_sync_script(monkeypatch, capsys, report, code):
    seen = []

    def fake(*, full=False, max_pages=None):
        seen.append((full, max_pages))
        return report

    monkeypatch.setattr(sync, "run_sync", fake)
    assert run_sync_script.main(["--full"]) == code
    assert json.loads(capsys.readouterr().out) == (report or {"error": "a sync is already running"})
    assert run_sync_script.main(["--max-pages", "5"]) == code
    assert seen == [(True, None), (False, 5)]
    with pytest.raises(SystemExit):
        run_sync_script.main(["--max-pages", "-1"])


def test_run_sync_script_max_pages_0_pages_past_the_cap(conn, bolna_stub, capsys):
    agent = make_agent(conn, bolna_agent_id="ba-stuck")
    make_campaign(conn, agent=agent)
    conn.commit()
    now = datetime.now(timezone.utc)  # no cursor: lookback 48 h, all 45 pages are inside it
    bolna_stub.executions = {"ba-stuck": [[ex("ba-stuck", P(200 + i), at=now - timedelta(minutes=i))]
                                          for i in range(1, 46)]}
    assert run_sync_script.main(["--max-pages", "0"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["errors"] == [] and report["calls_upserted"] == 45
    assert [a[1] for n, a in bolna_stub.calls if n == "list_agent_executions"] == list(range(1, 46))
    assert get_setting(conn, cursor_key(agent["id"])) is not None  # synced fully: the cursor moved
    with pytest.raises(ValueError, match="max_pages"):
        run_sync(max_pages=-1)


# ---------------------------------------------------------------- halted campaigns (re-review of the final fix wave)

def test_late_dnc_tag_on_a_linked_call_blocks_the_phone(conn):
    agent = make_agent(conn)
    camp = started(conn, make_campaign(conn, agent=agent))
    make_lead(conn, camp, phone=P(1))
    e = ex(agent["bolna_agent_id"], P(1))
    upsert_execution(conn, e, now=NOW)
    set_col(conn, "call", call_row(conn, e), rules_applied_at=NOW)  # rules ran before the tag arrived
    conn.commit()
    upsert_execution(conn, {**e, "extracted_data": _dnc_tag("do_not_call")}, now=NOW + timedelta(hours=1))
    assert call_row(conn, e)["campaign_id"] == camp["id"]
    row = one(conn, "SELECT * FROM dnc_phone WHERE phone = %s", P(1))
    assert row is not None and row["source"] == "call_tag"


def _stopped_with_untouched_lead(conn, status):
    agent = make_agent(conn, bolna_agent_id="ba-1")
    camp = make_campaign(conn, agent=agent, status=status)
    start = NOW - timedelta(hours=1)
    b = make_batch(conn, camp, status="stopped", bolna_batch_id="bb-1", scheduled_at=start)
    lead = make_lead(conn, camp, phone=P(1), status="QUEUED")
    set_col(conn, "lead", lead, next_dial_at=start)
    set_col(conn, "batch", b, stats=Json({sync.CONSOLE_STOP: (NOW - timedelta(minutes=10)).isoformat()}))
    conn.commit()
    return agent, camp, lead


def test_halted_campaign_hands_back_untouched_leads_only_once_fetched_after_the_stop(conn):
    for status in ("STOPPED", "PAUSED"):
        agent, camp, lead = _stopped_with_untouched_lead(conn, status)
        report = new_report()
        rollup_campaigns(conn, report, NOW, synced_agents={agent["id"]})  # no cursor at all: a bug, said loudly
        assert lead_row(conn, lead)["status"] == "QUEUED"
        assert report["errors"] == [f"rollup {camp['id']}: campaign {camp['id']}: its agent has no executions cursor"]
        set_setting(conn, cursor_key(agent["id"]), NOW.isoformat())
        conn.commit()
        rollup_campaigns(conn, new_report(), NOW, synced_agents=set())  # agent not synced: "no call" proves nothing
        assert lead_row(conn, lead)["status"] == "QUEUED"
        set_setting(conn, cursor_key(agent["id"]), (NOW - timedelta(minutes=10)).isoformat())  # fetched at the stop
        conn.commit()
        rollup_campaigns(conn, new_report(), NOW, synced_agents={agent["id"]})
        assert lead_row(conn, lead)["status"] == "QUEUED"
        set_setting(conn, cursor_key(agent["id"]), NOW.isoformat())
        conn.commit()
        report = new_report()
        rollup_campaigns(conn, report, NOW, synced_agents={agent["id"]})
        row = lead_row(conn, lead)
        assert (row["status"], row["last_outcome"], row["attempts"], row["next_dial_at"]) == \
            ("NOT_CONNECTED", None, 0, None)
        assert report["errors"] == [] and report["redial_rounds"] == 0  # a halted campaign never dials
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lead; DELETE FROM batch; DELETE FROM campaign; DELETE FROM setting; "
                        "DELETE FROM agent")
        conn.commit()


def test_halted_campaign_keeps_a_lead_whose_call_came_in(conn):
    agent, camp, lead = _stopped_with_untouched_lead(conn, "STOPPED")
    make_call(conn, phone=P(1), started_at=NOW - timedelta(minutes=30), status="in-progress", outcome="UNKNOWN",
              campaign=camp, lead=lead)
    set_setting(conn, cursor_key(agent["id"]), NOW.isoformat())
    conn.commit()
    rollup_campaigns(conn, new_report(), NOW, synced_agents={agent["id"]})
    assert lead_row(conn, lead)["status"] == "QUEUED"


def test_stopped_campaign_agent_is_synced_while_it_has_queued_leads(conn, bolna_stub):
    agent, camp, lead = _stopped_with_untouched_lead(conn, "STOPPED")
    bolna_stub.executions = {"ba-1": [[]]}
    assert sync_executions(conn, new_report(), max_pages=6, now=NOW) == {agent["id"]}
    set_col(conn, "lead", lead, status="NOT_CONNECTED")
    conn.commit()
    assert sync_executions(conn, new_report(), max_pages=6, now=NOW) == set()
