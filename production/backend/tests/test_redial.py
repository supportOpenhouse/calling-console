from datetime import datetime, timedelta, timezone

import threading

import psycopg2
import pytest
from psycopg2 import errors as pg_errors

from .. import db
from ..services import bolna, redial
from ..services.bolna import BolnaError
from ..services.datasets import mark_dnc
from ..services.errors import ApiError
from ..services.ist import IST
from ..services.redial import (
    DEFAULT_REDIAL_POLICY, create_round, eligible_redial_leads, maybe_auto_redial, place_test_call, validate_policy,
)
from .factories import make_agent, make_batch, make_call, make_campaign, make_lead

NOW = datetime.now(timezone.utc)


def P(n: int) -> str:
    return f"+91981000{n:04d}"


def _lead(conn, lead_id):
    with conn.cursor() as cur:
        cur.execute("select * from lead where id = %s", (lead_id,))
        return cur.fetchone()


def _campaign(conn, campaign_id):
    with conn.cursor() as cur:
        cur.execute("select * from campaign where id = %s", (campaign_id,))
        return cur.fetchone()


def _batches(conn, campaign):
    with conn.cursor() as cur:
        cur.execute("select * from batch where campaign_id = %s order by round", (campaign["id"],))
        return cur.fetchall()


def _calls(stub, name):
    return [args for n, args in stub.calls if n == name]


def _csv_phones(csv_text: str) -> list[str]:
    return [line.split(",")[0] for line in csv_text.splitlines()[1:]]


def _abc(conn, *, skip_cooling=False, status="PENDING", attempts=0, last_outcome=None, campaign_status="DRAFT"):
    """A clean, B on the DNC list, C spoken to (CONNECTED) in another campaign 2 days ago."""
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status=campaign_status, skip_cooling=skip_cooling)
    other = make_campaign(conn, agent=agent)
    kw = dict(status=status, attempts=attempts, last_outcome=last_outcome,
              last_call_at=NOW - timedelta(days=1) if attempts else None)
    a = make_lead(conn, camp, phone=P(1), variables={"owner_name": "Asha", "society_name": "DLF"}, **kw)
    b = make_lead(conn, camp, phone=P(2), **kw)
    c = make_lead(conn, camp, phone=P(3), **kw)
    mark_dnc(conn, P(2), "manual")
    make_call(conn, phone=P(3), outcome="CONNECTED", started_at=NOW - timedelta(days=2), campaign=other)
    return agent, camp, a, b, c


# ---------------------------------------------------------------- the dial guard

def test_first_round_skips_dnc_and_cooled(conn, bolna_stub):
    agent, camp, a, b, c = _abc(conn)
    before = datetime.now(timezone.utc)
    r = create_round(conn, camp, [a, b, c], "FIRST", None)

    assert r["leads"] == 1 and r["dnc"] == [P(2)] and r["cooled"] == [P(3)] and r["skipped"] is None
    assert _lead(conn, b["id"])["status"] == "DNC"
    cooled = _lead(conn, c["id"])
    assert cooled["status"] == "DONE" and cooled["final_tag"] == "skipped_cooling"
    queued = _lead(conn, a["id"])
    assert queued["status"] == "QUEUED" and queued["next_dial_at"] == r["scheduled_at"]

    [(agent_id, from_number, csv_text, filename)] = _calls(bolna_stub, "create_batch")
    assert agent_id == agent["bolna_agent_id"] and from_number == camp["from_number"]
    assert csv_text == f"contact_number,owner_name,society_name\n{P(1)},Asha,DLF"
    assert filename.endswith("_r1.csv")
    [(batch_id, when)] = _calls(bolna_stub, "schedule_batch")
    assert batch_id == "bb1" and when == r["scheduled_at"]
    assert timedelta(seconds=59) < r["scheduled_at"] - before < timedelta(seconds=70)

    [batch] = _batches(conn, camp)
    assert r["batch_ids"] == [batch["id"]]
    assert (batch["status"], batch["bolna_batch_id"], batch["kind"], batch["round"], batch["total"], batch["from_number"]) \
        == ("scheduled", "bb1", "FIRST", 1, 1, camp["from_number"])
    assert batch["scheduled_at"] == r["scheduled_at"]
    row = _campaign(conn, camp["id"])
    assert row["status"] == "RUNNING" and row["started_at"] is not None


def test_cooling_ignores_calls_in_the_same_campaign(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    d = make_lead(conn, camp, phone=P(4))
    make_call(conn, phone=P(4), outcome="CONNECTED", started_at=NOW - timedelta(days=2), campaign=camp)
    r = create_round(conn, camp, [d], "FIRST", None)
    assert r["leads"] == 1 and r["cooled"] == []
    assert _lead(conn, d["id"])["status"] == "QUEUED"


def test_cooling_uses_department_cooling_days(conn, bolna_stub):
    with conn.cursor() as cur:
        cur.execute("update department set cooling_days = 1 where key = 'SUPPLY'")
        cur.execute("update department set cooling_days = 30 where key = 'DEMAND'")
    agent = make_agent(conn)
    other = make_campaign(conn, agent=agent)
    supply = make_campaign(conn, agent=agent, status="DRAFT", vertical="SUPPLY")
    demand = make_campaign(conn, agent=agent, status="DRAFT", vertical="DEMAND")
    s = make_lead(conn, supply, phone=P(5))
    d = make_lead(conn, demand, phone=P(6))
    make_call(conn, phone=P(5), outcome="CONNECTED", started_at=NOW - timedelta(days=2), campaign=other)
    make_call(conn, phone=P(6), outcome="VOICEMAIL", started_at=NOW - timedelta(days=10), campaign=other)
    assert create_round(conn, supply, [s], "FIRST", None)["leads"] == 1
    r = create_round(conn, demand, [d], "FIRST", None)
    assert r["leads"] == 0 and r["cooled"] == [P(6)] and r["skipped"] == "no eligible leads"


def test_redial_round_ignores_cooling(conn, bolna_stub):
    _, camp, a, b, c = _abc(conn, status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED",
                            campaign_status="RUNNING")
    r = create_round(conn, camp, [a, b, c], "REDIAL", None)
    assert r["leads"] == 2 and r["dnc"] == [P(2)] and r["cooled"] == []
    assert _lead(conn, c["id"])["status"] == "QUEUED"
    assert _lead(conn, b["id"])["status"] == "DNC"
    [(_, _, csv_text, _)] = _calls(bolna_stub, "create_batch")
    assert sorted(_csv_phones(csv_text)) == [P(1), P(3)]
    assert _batches(conn, camp)[0]["kind"] == "REDIAL"


def test_skip_cooling_campaign(conn, bolna_stub):
    _, camp, a, b, c = _abc(conn, skip_cooling=True)
    r = create_round(conn, camp, [a, b, c], "FIRST", None)
    assert r["leads"] == 2 and r["dnc"] == [P(2)] and r["cooled"] == []
    assert _lead(conn, c["id"])["status"] == "QUEUED"
    assert _lead(conn, b["id"])["status"] == "DNC"
    [(_, _, csv_text, _)] = _calls(bolna_stub, "create_batch")
    assert sorted(_csv_phones(csv_text)) == [P(1), P(3)]


def test_nothing_dialable_skips_without_batch(conn, bolna_stub):
    _, camp, a, b, c = _abc(conn)
    r = create_round(conn, camp, [b, c], "FIRST", None)
    assert r == {"batch_ids": [], "leads": 0, "scheduled_at": None, "skipped": "no eligible leads",
                 "dnc": [P(2)], "cooled": [P(3)]}
    assert bolna_stub.calls == [] and _batches(conn, camp) == []
    assert _campaign(conn, camp["id"])["status"] == "DRAFT"


# ---------------------------------------------------------------- never twice

def test_same_leads_twice_are_dialled_once(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    other = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    stranger = make_lead(conn, other, phone=P(7))
    assert create_round(conn, camp, [a], "FIRST", None)["leads"] == 1
    # a stale copy of the same row (still says PENDING) must not dial again
    r = create_round(conn, camp, [a], "FIRST", None)
    assert r["leads"] == 0 and r["batch_ids"] == [] and r["skipped"] == "no eligible leads"
    # a lead that belongs to a different campaign is never dialled from this one
    assert create_round(conn, camp, [stranger], "FIRST", None)["leads"] == 0
    assert len(_calls(bolna_stub, "create_batch")) == 1 and len(_batches(conn, camp)) == 1
    assert _lead(conn, stranger["id"])["status"] == "PENDING"


def test_lead_marked_queued_before_bolna_call(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    seen = {}

    def create_batch(*args):
        other = db.get_conn()
        try:
            with other.cursor() as cur:
                cur.execute("select status, next_dial_at from lead where id = %s", (a["id"],))
                seen["lead"] = cur.fetchone()
                cur.execute("select status, bolna_batch_id, scheduled_at, kind, round, total from batch "
                            "where campaign_id = %s", (camp["id"],))
                seen["batches"] = cur.fetchall()
        finally:
            other.close()
        return bolna_stub.create_batch(*args)

    monkeypatch.setattr(bolna, "create_batch", create_batch)
    r = create_round(conn, camp, [a], "FIRST", None)
    assert seen["lead"]["status"] == "QUEUED"
    [batch] = seen["batches"]
    assert (batch["status"], batch["bolna_batch_id"], batch["kind"], batch["round"], batch["total"]) \
        == ("creating", None, "FIRST", 1, 1)
    assert seen["lead"]["next_dial_at"] == batch["scheduled_at"] == r["scheduled_at"]


def test_bolna_failure_restores_leads_and_marks_batch_failed(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    b = make_lead(conn, camp, phone=P(2), status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED",
                  last_call_at=NOW - timedelta(days=1))
    earlier = NOW - timedelta(hours=5)
    with conn.cursor() as cur:
        cur.execute("update lead set next_dial_at = %s where id = %s", (earlier, b["id"]))
    b = _lead(conn, b["id"])
    bolna_stub.fail_on = {"create_batch"}

    with pytest.raises(BolnaError):
        create_round(conn, camp, [a, b], "FIRST", None)

    assert (_lead(conn, a["id"])["status"], _lead(conn, a["id"])["next_dial_at"]) == ("PENDING", None)
    assert (_lead(conn, b["id"])["status"], _lead(conn, b["id"])["next_dial_at"]) == ("NOT_CONNECTED", earlier)
    [batch] = _batches(conn, camp)
    assert batch["status"] == "failed" and batch["bolna_batch_id"] is None
    assert _calls(bolna_stub, "schedule_batch") == []
    assert _campaign(conn, camp["id"])["status"] == "DRAFT"
    conn.rollback()  # the failure path committed; nothing may be left pending
    assert _lead(conn, a["id"])["status"] == "PENDING" and _batches(conn, camp)[0]["status"] == "failed"


def test_schedule_failure_stops_batch_and_restores(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    bolna_stub.fail_on = {"schedule_batch"}
    with pytest.raises(BolnaError):
        create_round(conn, camp, [a], "FIRST", None)
    # Bolna created the batch, so it is stopped before the leads are handed back
    assert _calls(bolna_stub, "stop_batch") == [("bb1",)]
    assert (_lead(conn, a["id"])["status"], _lead(conn, a["id"])["next_dial_at"]) == ("PENDING", None)
    [batch] = _batches(conn, camp)
    assert batch["status"] == "failed" and batch["bolna_batch_id"] == "bb1"
    assert _campaign(conn, camp["id"])["status"] == "DRAFT"


def test_schedule_and_stop_failure_keeps_leads_queued(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    bolna_stub.fail_on = {"schedule_batch", "stop_batch"}
    with pytest.raises(BolnaError) as e:
        create_round(conn, camp, [a], "FIRST", None)
    # the schedule error is the one re-raised; the stop failure rides along as a note
    frames = [entry.name for entry in e.traceback]
    assert "schedule_batch" in frames and "stop_batch" not in frames
    assert any("stop_batch" in note for note in getattr(e.value, "__notes__", []))
    assert _calls(bolna_stub, "stop_batch") == [("bb1",)]

    conn.rollback()  # everything below must have been committed
    # Bolna may still dial this batch: it stays tracked and its lead is never handed back for another round
    [batch] = _batches(conn, camp)
    assert (batch["status"], batch["bolna_batch_id"]) == ("scheduled", "bb1")
    lead = _lead(conn, a["id"])
    assert lead["status"] == "QUEUED" and lead["next_dial_at"] == batch["scheduled_at"]
    assert _campaign(conn, camp["id"])["status"] == "RUNNING"
    assert create_round(conn, camp, [a], "FIRST", None)["leads"] == 0
    assert len(_calls(bolna_stub, "create_batch")) == 1


def test_one_batch_per_from_number_with_rotation(conn, bolna_stub):
    agent = make_agent(conn)
    policy = {**DEFAULT_REDIAL_POLICY, "rotate_numbers": ["+918000000002", "+918000000003"]}
    camp = make_campaign(conn, agent=agent, redial_policy=policy, from_number="+918000000001")
    make_batch(conn, camp, status="completed", round=1)
    old = NOW - timedelta(days=1)
    b1 = make_lead(conn, camp, phone=P(1), status="BLOCKED", attempts=1, last_outcome="CARRIER_BLOCKED", last_call_at=old)
    b2 = make_lead(conn, camp, phone=P(2), status="BLOCKED", attempts=1, last_outcome="CARRIER_BLOCKED", last_call_at=old)
    n1 = make_lead(conn, camp, phone=P(3), status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED", last_call_at=old)

    r = create_round(conn, camp, [b1, b2, n1], "REDIAL", None)

    calls = _calls(bolna_stub, "create_batch")
    assert len(calls) == 2 and len(r["batch_ids"]) == 2 and r["leads"] == 3
    by_from = {from_number: sorted(_csv_phones(csv_text)) for _, from_number, csv_text, _ in calls}
    assert by_from == {"+918000000002": [P(1), P(2)], "+918000000001": [P(3)]}
    batches = _batches(conn, camp)[1:]
    assert [(x["round"], x["kind"], x["status"]) for x in batches] == [(2, "REDIAL", "scheduled"), (3, "REDIAL", "scheduled")]
    assert {x["from_number"]: x["total"] for x in batches} == {"+918000000002": 2, "+918000000001": 1}
    assert {x["bolna_batch_id"] for x in batches} == {"bb1", "bb2"}


def test_second_group_failure_keeps_first_batch_and_starts_campaign(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    policy = {**DEFAULT_REDIAL_POLICY, "rotate_numbers": ["+918000000002"]}
    camp = make_campaign(conn, agent=agent, redial_policy=policy, status="SCHEDULED")
    old = NOW - timedelta(days=1)
    blocked = make_lead(conn, camp, phone=P(1), status="BLOCKED", attempts=1, last_outcome="CARRIER_BLOCKED", last_call_at=old)
    missed = make_lead(conn, camp, phone=P(2), status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED", last_call_at=old)

    def create_batch(*args):
        if _calls(bolna_stub, "create_batch"):
            raise BolnaError(502, "<html>")
        return bolna_stub.create_batch(*args)

    monkeypatch.setattr(bolna, "create_batch", create_batch)
    with pytest.raises(BolnaError):
        create_round(conn, camp, [blocked, missed], "REDIAL", None)

    # groups are dialled in the order their first lead appears: the rotated number first
    first, second = _batches(conn, camp)
    assert (first["from_number"], first["status"], first["bolna_batch_id"]) == ("+918000000002", "scheduled", "bb1")
    assert (second["from_number"], second["status"], second["bolna_batch_id"]) == (camp["from_number"], "failed", None)
    assert _lead(conn, blocked["id"])["status"] == "QUEUED"
    assert (_lead(conn, missed["id"])["status"], _lead(conn, missed["id"])["next_dial_at"]) == ("NOT_CONNECTED", None)
    # one batch is live on Bolna, so the campaign must say so
    assert _campaign(conn, camp["id"])["status"] == "RUNNING"


def test_future_round_marks_campaign_scheduled(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    when = NOW + timedelta(hours=3)
    r = create_round(conn, camp, [a], "FIRST", when, now=NOW)
    assert r["scheduled_at"] == when and _calls(bolna_stub, "schedule_batch")[0][1] == when
    row = _campaign(conn, camp["id"])
    assert row["status"] == "SCHEDULED" and row["scheduled_at"] == when and row["started_at"] == NOW


# ---------------------------------------------------------------- hardening (R6)

def _commit_elsewhere(sql, params):
    """Another session's write, committed while create_round is running."""
    other = db.get_conn()
    try:
        with other.cursor() as cur:
            cur.execute(sql, params)
        other.commit()
    finally:
        other.close()


@pytest.mark.parametrize("status", ["PAUSED", "STOPPED", "COMPLETED"])
def test_inactive_campaign_dials_nobody(conn, bolna_stub, status):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="RUNNING")
    a = make_lead(conn, camp, phone=P(1))
    with conn.cursor() as cur:  # paused after the caller read the campaign row
        cur.execute("update campaign set status = %s where id = %s", (status, camp["id"]))
    r = create_round(conn, camp, [a], "FIRST", None)
    assert r == {"batch_ids": [], "leads": 0, "scheduled_at": None, "skipped": "campaign not active",
                 "dnc": [], "cooled": []}
    assert bolna_stub.calls == [] and _batches(conn, camp) == []
    assert _campaign(conn, camp["id"])["status"] == status and _lead(conn, a["id"])["status"] == "PENDING"


def test_campaign_paused_mid_round_stays_paused(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))

    def create_batch(*args):
        _commit_elsewhere("update campaign set status = 'PAUSED' where id = %s", (camp["id"],))
        return bolna_stub.create_batch(*args)

    monkeypatch.setattr(bolna, "create_batch", create_batch)
    assert create_round(conn, camp, [a], "FIRST", None)["leads"] == 1
    assert _campaign(conn, camp["id"])["status"] == "PAUSED"


def test_missing_agent_raises_before_any_batch(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    with pytest.raises(RuntimeError, match="agent missing-agent"):
        create_round(conn, {**camp, "agent_id": "missing-agent"}, [a], "FIRST", None)
    conn.rollback()
    assert _batches(conn, camp) == [] and _lead(conn, a["id"])["status"] == "PENDING" and bolna_stub.calls == []


def test_lead_reattempted_since_read_is_not_dialled(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent)
    a = make_lead(conn, camp, phone=P(1), status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED",
                  last_call_at=NOW - timedelta(days=1))
    with conn.cursor() as cur:  # another attempt landed after the caller read the row; status unchanged
        cur.execute("update lead set attempts = 2 where id = %s", (a["id"],))
    r = create_round(conn, camp, [a], "REDIAL", None)
    assert r["leads"] == 0 and r["skipped"] == "no eligible leads" and bolna_stub.calls == []
    assert _lead(conn, a["id"])["status"] == "NOT_CONNECTED"


def _dnc_after_guard(monkeypatch, phone):
    real_guard = redial.guard_leads

    def guard_then_dnc(*args):
        out = real_guard(*args)
        _commit_elsewhere("insert into dnc_phone (phone, source) values (%s, 'manual')", (phone,))
        return out

    monkeypatch.setattr(redial, "guard_leads", guard_then_dnc)


def test_phone_marked_dnc_after_guard_is_not_sent(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    b = make_lead(conn, camp, phone=P(2))
    _dnc_after_guard(monkeypatch, P(2))
    r = create_round(conn, camp, [a, b], "FIRST", None)
    assert r["leads"] == 1 and r["dnc"] == [P(2)]
    [(_, _, csv_text, _)] = _calls(bolna_stub, "create_batch")
    assert _csv_phones(csv_text) == [P(1)] and _batches(conn, camp)[0]["total"] == 1
    assert _lead(conn, b["id"])["status"] == "DNC"


def test_only_lead_marked_dnc_after_guard_sends_nothing(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    _dnc_after_guard(monkeypatch, P(1))
    r = create_round(conn, camp, [a], "FIRST", None)
    assert r["skipped"] == "no eligible leads" and r["dnc"] == [P(1)] and bolna_stub.calls == []
    conn.rollback()  # the DNC mark was committed
    assert _lead(conn, a["id"])["status"] == "DNC" and _batches(conn, camp) == []


def test_schedule_failure_after_start_time_keeps_leads_queued(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    bolna_stub.fail_on = {"schedule_batch"}
    started = datetime.now(timezone.utc) - timedelta(minutes=10)  # so its start time (+60s) has passed
    with pytest.raises(BolnaError) as e:
        create_round(conn, camp, [a], "FIRST", None, now=started)
    # stopped all the same, but calls may already have begun: never hand the lead back
    assert _calls(bolna_stub, "stop_batch") == [("bb1",)]
    assert any("may already have dialled" in note for note in e.value.__notes__)
    conn.rollback()
    [batch] = _batches(conn, camp)
    assert (batch["status"], batch["bolna_batch_id"]) == ("scheduled", "bb1")
    assert _lead(conn, a["id"])["status"] == "QUEUED"
    assert _campaign(conn, camp["id"])["status"] == "RUNNING"


def test_failure_to_record_bolna_id_notes_live_batch(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    make_batch(conn, make_campaign(conn, agent=agent), bolna_batch_id="bb1")  # Bolna hands back an id we hold
    a = make_lead(conn, camp, phone=P(1))
    with pytest.raises(pg_errors.UniqueViolation) as e:
        create_round(conn, camp, [a], "FIRST", None)
    assert "bolna batch bb1 is live" in e.value.__notes__
    conn.rollback()
    [batch] = _batches(conn, camp)
    assert (batch["status"], batch["bolna_batch_id"]) == ("creating", None)
    assert _lead(conn, a["id"])["status"] == "QUEUED"
    assert _campaign(conn, camp["id"])["status"] == "RUNNING"


def test_restore_keeps_changes_made_meanwhile(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    b = make_lead(conn, camp, phone=P(2))

    def create_batch(*args):
        _commit_elsewhere("update lead set status = 'DONE' where id = %s", (b["id"],))
        raise BolnaError(502, "<html>")

    monkeypatch.setattr(bolna, "create_batch", create_batch)
    with pytest.raises(BolnaError):
        create_round(conn, camp, [a, b], "FIRST", None)
    assert _lead(conn, a["id"])["status"] == "PENDING"
    assert _lead(conn, b["id"])["status"] == "DONE"
    assert _batches(conn, camp)[0]["status"] == "failed"


def test_db_error_leaves_connection_clean(conn, bolna_stub):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    a = make_lead(conn, camp, phone=P(1))
    with pytest.raises(pg_errors.NotNullViolation):
        create_round(conn, {**camp, "from_number": None}, [a], "FIRST", None)
    assert conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_IDLE
    assert _lead(conn, a["id"])["status"] == "PENDING" and _batches(conn, camp) == [] and bolna_stub.calls == []


def test_phone_queued_in_another_campaign_counts_as_cooled(conn, bolna_stub):
    agent = make_agent(conn)
    x = make_campaign(conn, agent=agent)
    y = make_campaign(conn, agent=agent, status="DRAFT")
    make_lead(conn, x, phone=P(1), status="QUEUED")
    mine = make_lead(conn, y, phone=P(1))
    r = create_round(conn, y, [mine], "FIRST", None)
    assert r["cooled"] == [P(1)] and r["leads"] == 0 and bolna_stub.calls == []
    row = _lead(conn, mine["id"])
    assert (row["status"], row["final_tag"]) == ("DONE", "skipped_in_flight")


def test_skip_cooling_still_blocks_in_flight_phone(conn, bolna_stub):
    agent = make_agent(conn)
    x = make_campaign(conn, agent=agent)
    y = make_campaign(conn, agent=agent, status="DRAFT", skip_cooling=True)
    make_lead(conn, x, phone=P(1), status="QUEUED")
    busy = make_lead(conn, y, phone=P(1))
    free = make_lead(conn, y, phone=P(2))
    r = create_round(conn, y, [busy, free], "FIRST", None)
    # skip_cooling waives past conversations, never a simultaneous dial
    assert r["cooled"] == [P(1)] and r["leads"] == 1
    [(_, _, csv_text, _)] = _calls(bolna_stub, "create_batch")
    assert _csv_phones(csv_text) == [P(2)]
    row = _lead(conn, busy["id"])
    assert (row["status"], row["final_tag"]) == ("DONE", "skipped_in_flight")


def test_in_flight_and_recently_spoken_phone_is_tagged_cooling(conn, bolna_stub):
    agent = make_agent(conn)
    x = make_campaign(conn, agent=agent)
    y = make_campaign(conn, agent=agent, status="DRAFT")
    make_lead(conn, x, phone=P(1), status="QUEUED")
    make_call(conn, phone=P(1), outcome="CONNECTED", started_at=NOW - timedelta(days=2), campaign=x)
    mine = make_lead(conn, y, phone=P(1))
    assert create_round(conn, y, [mine], "FIRST", None)["cooled"] == [P(1)]
    # both reasons hold; the lasting one (spoken to recently) is recorded
    assert _lead(conn, mine["id"])["final_tag"] == "skipped_cooling"


def _redial_leads_with_one_in_flight(conn):
    """Campaign B has P(1) QUEUED; `camp` has P(1) and P(2) waiting for a redial (NOT_PICKED, past the gap)."""
    agent = make_agent(conn)
    other = make_campaign(conn, agent=agent)
    camp = make_campaign(conn, agent=agent, redial_policy=dict(DEFAULT_REDIAL_POLICY))
    make_lead(conn, other, phone=P(1), status="QUEUED")
    kw = dict(status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED", last_call_at=NOW - timedelta(days=1))
    busy = make_lead(conn, camp, phone=P(1), **kw)
    free = make_lead(conn, camp, phone=P(2), **kw)
    return camp, busy, free


def _untouched(conn, lead):
    row = _lead(conn, lead["id"])
    return (row["status"], row["attempts"], row["final_tag"], row["next_dial_at"]) == \
        (lead["status"], lead["attempts"], lead["final_tag"], lead["next_dial_at"])


def test_redial_round_skips_in_flight_phone_and_leaves_lead(conn, bolna_stub):
    camp, busy, _ = _redial_leads_with_one_in_flight(conn)
    assert busy["id"] in {x["id"] for x in eligible_redial_leads(conn, camp, NOW)}
    r = create_round(conn, camp, [busy], "REDIAL", None)
    # held back for this round only: not dialled, not marked, not reported as cooled
    assert r == {"batch_ids": [], "leads": 0, "scheduled_at": None, "skipped": "no eligible leads",
                 "dnc": [], "cooled": []}
    assert bolna_stub.calls == [] and _batches(conn, camp) == []
    assert _untouched(conn, busy)
    assert _campaign(conn, camp["id"])["status"] == "RUNNING"
    # still waiting for a later round
    assert busy["id"] in {x["id"] for x in eligible_redial_leads(conn, camp, NOW)}


def test_redial_round_dials_others_when_one_phone_in_flight(conn, bolna_stub):
    camp, busy, free = _redial_leads_with_one_in_flight(conn)
    r = create_round(conn, camp, [busy, free], "REDIAL", None)
    assert r["leads"] == 1 and r["cooled"] == [] and r["dnc"] == [] and r["skipped"] is None
    [(_, _, csv_text, _)] = _calls(bolna_stub, "create_batch")
    assert _csv_phones(csv_text) == [P(2)]
    assert _batches(conn, camp)[0]["total"] == 1
    assert _untouched(conn, busy)
    assert _lead(conn, free["id"])["status"] == "QUEUED"


def _queued_elsewhere_after_guard(monkeypatch, other_campaign, phone):
    """Another campaign queues `phone` after guard_leads ran: the race guard_leads alone cannot see."""
    real_guard = redial.guard_leads

    def guard_then_queue(*args):
        out = real_guard(*args)
        _commit_elsewhere("insert into lead (campaign_id, phone, status) values (%s, %s, 'QUEUED')",
                          (other_campaign["id"], phone))
        return out

    monkeypatch.setattr(redial, "guard_leads", guard_then_queue)


def test_first_round_claim_skips_a_phone_queued_elsewhere_after_the_guard(conn, bolna_stub, monkeypatch):
    agent = make_agent(conn)
    other = make_campaign(conn, agent=agent)
    camp = make_campaign(conn, agent=agent, status="DRAFT")
    busy, free = make_lead(conn, camp, phone=P(1)), make_lead(conn, camp, phone=P(2))
    conn.commit()
    _queued_elsewhere_after_guard(monkeypatch, other, P(1))
    r = create_round(conn, camp, [busy, free], "FIRST", None)
    assert r["leads"] == 1 and r["cooled"] == [P(1)]
    [(_, _, csv_text, _)] = _calls(bolna_stub, "create_batch")
    assert _csv_phones(csv_text) == [P(2)] and _batches(conn, camp)[0]["total"] == 1
    row = _lead(conn, busy["id"])
    assert (row["status"], row["final_tag"]) == ("DONE", "skipped_in_flight")


def test_redial_round_claim_leaves_a_phone_queued_elsewhere_after_the_guard(conn, bolna_stub, monkeypatch):
    camp, busy, free = _redial_leads_with_one_in_flight(conn)
    with conn.cursor() as cur:  # nobody holds P(1) when the guard runs
        cur.execute("delete from lead where phone = %s and campaign_id <> %s", (P(1), camp["id"]))
    other = make_campaign(conn, agent=make_agent(conn))
    conn.commit()
    _queued_elsewhere_after_guard(monkeypatch, other, P(1))
    r = create_round(conn, camp, [busy, free], "REDIAL", None)
    assert r["leads"] == 1 and r["cooled"] == []
    [(_, _, csv_text, _)] = _calls(bolna_stub, "create_batch")
    assert _csv_phones(csv_text) == [P(2)]
    assert _untouched(conn, busy)


def test_concurrent_rounds_of_two_campaigns_never_queue_one_phone_twice(conn, bolna_stub):
    """The claim waits on a per-phone lock: a round holding it (and queuing the phone) blocks the other until it
    commits, which then sees the phone QUEUED."""
    agent = make_agent(conn)
    x = make_campaign(conn, agent=agent)
    y = make_campaign(conn, agent=agent, status="DRAFT")
    mine = make_lead(conn, y, phone=P(1))
    conn.commit()
    other = db.get_conn()
    result = {}
    worker = threading.Thread(target=lambda: result.update(r=create_round(conn, y, [mine], "FIRST", None)))
    try:
        with other.cursor() as cur:  # x's round: holds P(1)'s lock and has queued it, not committed yet
            cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (redial.PHONE_LOCK, P(1)))
            cur.execute("INSERT INTO lead (campaign_id, phone, status) VALUES (%s, %s, 'QUEUED')", (x["id"], P(1)))
        worker.start()
        worker.join(0.5)
        assert worker.is_alive()  # waiting for the phone
        other.commit()
        worker.join(5)
    finally:
        other.close()
    assert not worker.is_alive()
    assert result["r"]["leads"] == 0 and result["r"]["cooled"] == [P(1)] and _calls(bolna_stub, "create_batch") == []
    row = _lead(conn, mine["id"])
    assert (row["status"], row["final_tag"]) == ("DONE", "skipped_in_flight")


# ---------------------------------------------------------------- eligibility

def test_eligible_redial(conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, redial_policy=dict(DEFAULT_REDIAL_POLICY))  # gap 180, max 3
    past, inside = NOW - timedelta(hours=4), NOW - timedelta(hours=1)

    def lead(n, **kw):
        return make_lead(conn, camp, phone=P(n), **kw)["id"]

    yes = {
        lead(1, status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED", last_call_at=past),
        lead(2, status="PENDING", attempts=1, last_outcome="BALANCE_LOW", last_call_at=past),
        lead(3, status="CONNECTED", attempts=1, last_outcome="CONNECTED", final_tag="Callback_Requested", last_call_at=past),
        lead(4, status="BLOCKED", attempts=2, last_outcome="CARRIER_BLOCKED", last_call_at=past),
        lead(5, status="NOT_CONNECTED", attempts=0),  # its batch was stopped at the window end before reaching it
        lead(6, status="NOT_CONNECTED", attempts=1, last_call_at=past),  # the same, on a redial round
    }
    no = {
        lead(10, status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED", last_call_at=inside),  # inside the gap
        lead(11, status="NOT_CONNECTED", attempts=3, last_outcome="NOT_PICKED", last_call_at=past),    # attempts == max
        lead(12, status="PENDING"),                                                                     # never dialled
        lead(13, status="PENDING", attempts=1, last_outcome="NOT_PICKED", last_call_at=past),         # paused, not balance-low
        lead(14, status="CONNECTED", attempts=1, last_outcome="CONNECTED", final_tag="qualified", last_call_at=past),
        lead(15, status="DONE", attempts=1, last_outcome="NOT_PICKED", last_call_at=past),
        lead(16, status="QUEUED", attempts=1, last_outcome="NOT_PICKED", last_call_at=past),
        lead(17, status="DNC", attempts=1, last_outcome="NOT_PICKED", last_call_at=past),
        lead(18, status="NOT_CONNECTED", attempts=1, last_outcome="UNKNOWN", last_call_at=past),     # not a retry outcome
    }
    future = lead(19, status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED", last_call_at=past)
    with conn.cursor() as cur:
        cur.execute("update lead set next_dial_at = %s where id = %s", (NOW + timedelta(hours=1), future))
    no.add(future)

    got = {x["id"] for x in eligible_redial_leads(conn, camp, NOW)}
    assert got == yes
    assert not got & no


def test_rule_redial_next_dial_at_is_eligible(conn):
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, redial_policy=dict(DEFAULT_REDIAL_POLICY))  # gap 180, max 3
    inside_gap = NOW - timedelta(minutes=30)
    # a "redial after N minutes" rule on a CONNECTED call: NOT_CONNECTED, last outcome CONNECTED (not a retry
    # outcome), last call inside the gap, next_dial_at set
    rule = dict(status="NOT_CONNECTED", attempts=1, last_outcome="CONNECTED", last_call_at=inside_gap)
    due = make_lead(conn, camp, phone=P(1), **rule)["id"]
    due_now = make_lead(conn, camp, phone=P(2), **rule)["id"]
    later = make_lead(conn, camp, phone=P(3), **rule)["id"]
    maxed = make_lead(conn, camp, phone=P(4), **{**rule, "attempts": 3})["id"]
    done = make_lead(conn, camp, phone=P(5), **{**rule, "status": "DONE"})["id"]
    # a paused, never-dialled lead keeps its old batch's next_dial_at; it still belongs to a FIRST round
    paused = make_lead(conn, camp, phone=P(6), status="PENDING")["id"]
    with conn.cursor() as cur:
        for lead_id, at in ((due, NOW - timedelta(minutes=1)), (due_now, NOW), (later, NOW + timedelta(hours=1)),
                            (maxed, NOW - timedelta(minutes=1)), (done, NOW - timedelta(minutes=1)),
                            (paused, NOW - timedelta(hours=1))):
            cur.execute("update lead set next_dial_at = %s where id = %s", (at, lead_id))
    assert {x["id"] for x in eligible_redial_leads(conn, camp, NOW)} == {due, due_now}


# ---------------------------------------------------------------- auto redial

def _redial_ready(conn, now, **campaign_kw):
    """A campaign with one lead whose last call was a day before `now` -- the same `now` the test passes to
    maybe_auto_redial, so the test never depends on the date it runs on."""
    agent = make_agent(conn)
    camp = make_campaign(conn, agent=agent, redial_policy=dict(DEFAULT_REDIAL_POLICY), **campaign_kw)
    lead = make_lead(conn, camp, phone=P(1), status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED",
                     last_call_at=now - timedelta(days=1))
    return camp, lead


def test_auto_redial_outside_window_waits_for_the_window(conn, bolna_stub):
    """Nothing is pre-scheduled at Bolna overnight (a phone put on the DNC list meanwhile could not be taken out of
    such a batch): a sync inside the window opens the round."""
    now = datetime(2026, 11, 18, 20, 0, tzinfo=IST).astimezone(timezone.utc)
    camp, lead = _redial_ready(conn, now)
    assert maybe_auto_redial(conn, camp, now) is None
    assert bolna_stub.calls == [] and _batches(conn, camp) == []
    assert _untouched(conn, lead) and _campaign(conn, camp["id"])["status"] == "RUNNING"
    morning = datetime(2026, 11, 19, 10, 5, tzinfo=IST).astimezone(timezone.utc)
    assert maybe_auto_redial(conn, camp, morning)["leads"] == 1


def test_auto_redial_inside_window_dials_now(conn, bolna_stub):
    now = datetime(2026, 11, 18, 12, 0, tzinfo=IST).astimezone(timezone.utc)
    camp, _ = _redial_ready(conn, now)
    r = maybe_auto_redial(conn, camp, now)
    assert r["scheduled_at"] == now + timedelta(seconds=60)
    assert _campaign(conn, camp["id"])["status"] == "RUNNING"


@pytest.mark.parametrize("batch_status", ["creating", "created", "scheduled", "running"])
def test_auto_redial_waits_for_active_batch(conn, bolna_stub, batch_status):
    camp, _ = _redial_ready(conn, NOW)
    make_batch(conn, camp, status=batch_status)
    assert maybe_auto_redial(conn, camp, NOW) is None and bolna_stub.calls == []


def test_auto_redial_needs_running_campaign_and_auto_policy(conn, bolna_stub):
    paused, _ = _redial_ready(conn, NOW, status="PAUSED")
    assert maybe_auto_redial(conn, paused, NOW) is None
    manual = make_campaign(conn, agent=make_agent(conn), redial_policy={**DEFAULT_REDIAL_POLICY, "auto_redial": False})
    make_lead(conn, manual, phone=P(2), status="NOT_CONNECTED", attempts=1, last_outcome="NOT_PICKED",
              last_call_at=NOW - timedelta(days=1))
    assert maybe_auto_redial(conn, manual, NOW) is None
    assert bolna_stub.calls == []


# ---------------------------------------------------------------- test call

def test_test_call_blocked_for_dnc(conn, bolna_stub):
    agent = make_agent(conn)
    mark_dnc(conn, P(9), "manual")
    for to in (P(9), "98100 00009", "09810000009"):
        with pytest.raises(ApiError) as e:
            place_test_call(conn, agent, to, "+918000000001", {"owner_name": "Asha"})
        assert e.value.status == 409 and e.value.message == "number is on the do-not-call list"
    assert _calls(bolna_stub, "place_call") == []


def test_test_call_places_call(conn, bolna_stub):
    agent = make_agent(conn)
    r = place_test_call(conn, agent, P(8), "+918000000001", {"owner_name": "Asha"})
    assert r["execution_id"] == "ex1"
    assert _calls(bolna_stub, "place_call") == [(agent["bolna_agent_id"], P(8), "+918000000001", {"owner_name": "Asha"})]


# ---------------------------------------------------------------- policy

def test_validate_policy_rejects_bad_window():
    for bad in ({"window_start": "19:00", "window_end": "10:00"},
                {"window_start": "10:00", "window_end": "10:00"},
                {"window_end": "25:00"},
                {"window_start": "ten"}):
        with pytest.raises(ValueError):
            validate_policy(bad)


def test_validate_policy_merges_and_bounds():
    assert validate_policy({}) == DEFAULT_REDIAL_POLICY
    assert validate_policy({"max_attempts": 5, "rotate_numbers": ["+918000000002"]}) == {
        **DEFAULT_REDIAL_POLICY, "max_attempts": 5, "rotate_numbers": ["+918000000002"]}
    assert validate_policy({"max_attempts": "8", "gap_minutes": 15})["max_attempts"] == 8
    assert validate_policy({"max_attempts": 1, "gap_minutes": 10080})["gap_minutes"] == 10080
    for bad in ({"max_attempts": 0}, {"max_attempts": 9}, {"gap_minutes": 14}, {"gap_minutes": 10081},
                {"max_attempts": 2.5}, {"max_attempts": True}, {"max_attempts": "three"},
                {"retry_outcomes": "NOT_PICKED"}, {"rotate_numbers": [1]}, {"auto_redial": "yes"}, []):
        with pytest.raises(ValueError):
            validate_policy(bad)
