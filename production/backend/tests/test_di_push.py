from datetime import datetime, timedelta, timezone

import pytest
import requests
from psycopg2.extras import Json

from .. import config
from ..services import di_push
from ..services.di_push import build_di_payload, push_calls_to_di
from ..services.settings_store import get_setting, set_setting
from .factories import make_agent, make_call, make_campaign, make_lead

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=2)

DI_KEYS = {"call_id", "source", "campaign", "batch_id", "called_at", "phone", "caller_id", "contact", "outcome",
           "outcome_detail", "talk_seconds", "carrier_blocked", "call_tag", "property", "timeline", "callback_at",
           "summary", "transcript", "recording_url", "cost_usd", "tag_confidence"}


def _call_row(conn, campaign, lead=None, **kw):
    call = make_call(conn, phone="+919999900001", started_at=OLD, campaign=campaign, lead=lead, tag="qualified", **kw)
    with conn.cursor() as cur:
        cur.execute("UPDATE call SET extracted = %s, duration_sec = 90, hangup_reason = 'llm_hangup', cost = 0.4 "
                    "WHERE id = %s RETURNING *", (Json({"Seller": {
                        "call_tag": {"objective": "qualified", "confidence": 0.9},
                        "configuration": {"objective": "3 BHK"}, "size_sqft": {"objective": "1400"},
                        "ask_price": {"objective": "1.5 Cr"}, "exit_facing": {"objective": "East"},
                        "sell_later_period": {"objective": "now"}, "callback_time": {"objective": "5pm"},
                        "owner_name": {"objective": "Extracted Name"},
                        "society_and_location": {"objective": "Extracted Society"}}}), call["id"]))
        return cur.fetchone()


def test_build_di_payload_shape(conn):
    camp = make_campaign(conn, agent=make_agent(conn))
    lead = make_lead(conn, camp, phone="+919999900001", variables={
        "owner_name": "Rajendra Kumar", "society_name": "Gaur City 1st Avenue", "city": "Noida"})
    call = _call_row(conn, camp, lead)
    p = build_di_payload(call, camp, lead)
    assert set(p) == DI_KEYS
    assert p["call_id"] == call["bolna_execution_id"] and p["source"] == "calling-console"
    assert p["contact"] == {"name": "Rajendra Kumar", "society": "Gaur City 1st Avenue", "city": "Noida"}
    assert p["outcome"] == "connected" and p["carrier_blocked"] is False and p["campaign"] == camp["name"]
    assert set(p["property"]) == {"configuration", "size_sqft", "asking_price_raw", "facing"}
    assert p["property"] == {"configuration": "3 BHK", "size_sqft": "1400", "asking_price_raw": "1.5 Cr",
                             "facing": "East"}
    assert (p["timeline"], p["callback_at"], p["call_tag"], p["tag_confidence"]) == ("now", "5pm", "qualified", 0.9)
    assert (p["talk_seconds"], p["outcome_detail"], p["cost_usd"]) == (90, "llm_hangup", 0.4)
    assert p["called_at"] == OLD.isoformat()


def test_contact_falls_back_to_extracted(conn):
    camp = make_campaign(conn, agent=make_agent(conn))
    call = _call_row(conn, camp)
    p = build_di_payload(call, camp, None)
    assert p["contact"] == {"name": "Extracted Name", "society": "Extracted Society", "city": None}


def _stub_post(monkeypatch, status=200):
    sent = []

    class Res:
        status_code = status
        text = "boom"

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return Res()

    monkeypatch.setattr(di_push.requests, "post", post)
    return sent


def test_push_only_finished_supply_calls_once(conn, monkeypatch):
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    sup = make_campaign(conn, agent=make_agent(conn))
    dem = make_campaign(conn, agent=make_agent(conn, vertical="DEMAND"), vertical="DEMAND")
    good = make_call(conn, phone="+919999900001", started_at=OLD, campaign=sup, tag="qualified")
    make_call(conn, phone="+919999900002", started_at=OLD, campaign=dem, tag="qualified")
    make_call(conn, phone="+919999900003", started_at=OLD, campaign=sup, status="in-progress")
    make_call(conn, phone="+919999900004", started_at=NOW - timedelta(minutes=5), campaign=sup)
    make_call(conn, phone="+919999900005", started_at=OLD, agent=make_agent(conn))
    sent = _stub_post(monkeypatch)
    report = {"errors": []}
    assert push_calls_to_di(conn, report, now=NOW) == 1
    assert len(sent) == 1 and report["errors"] == []
    url, body, headers = sent[0]
    assert url == "https://di.example/ingest" and body["call_id"] == good["bolna_execution_id"]
    assert headers["X-AI-Calls-Token"] == "tok"
    with conn.cursor() as cur:
        cur.execute("SELECT pushed_to_di_at FROM call WHERE id = %s", (good["id"],))
        assert cur.fetchone()["pushed_to_di_at"] is not None
    assert push_calls_to_di(conn, report, now=NOW) == 0
    assert len(sent) == 1


def test_push_failure_recorded_and_retried(conn, monkeypatch):
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    camp = make_campaign(conn, agent=make_agent(conn))
    call = make_call(conn, phone="+919999900001", started_at=OLD, campaign=camp, tag="qualified")
    _stub_post(monkeypatch, status=500)
    report = {"errors": []}
    assert push_calls_to_di(conn, report, now=NOW) == 0
    cid = call["bolna_execution_id"]
    assert report["errors"] == [f"di {cid}: 500 boom"]
    with conn.cursor() as cur:
        cur.execute("SELECT pushed_to_di_at FROM call WHERE id = %s", (call["id"],))
        assert cur.fetchone()["pushed_to_di_at"] is None
    fails = get_setting(conn, "diPushFailures")
    assert [f["call_id"] for f in fails] == [cid] and fails[0]["error"] == "500 boom"
    assert push_calls_to_di(conn, {"errors": []}, now=NOW) == 0
    assert len(get_setting(conn, "diPushFailures")) == 1  # de-duplicated per call
    sent = _stub_post(monkeypatch)
    assert push_calls_to_di(conn, {"errors": []}, now=NOW) == 1 and len(sent) == 1
    assert get_setting(conn, "diPushFailures") == []


def test_ineligible_rows_do_not_starve_limit(conn, monkeypatch):
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    camp = make_campaign(conn, agent=make_agent(conn))
    for i in range(3):
        make_call(conn, phone=f"+91999990010{i}", started_at=OLD - timedelta(hours=1), campaign=camp,
                  status="in-progress")
    done = make_call(conn, phone="+919999900200", started_at=OLD, campaign=camp, tag="qualified")
    sent = _stub_post(monkeypatch)
    assert push_calls_to_di(conn, {"errors": []}, limit=2, now=NOW) == 1
    assert sent[0][1]["call_id"] == done["bolna_execution_id"]


def test_di_enabled(monkeypatch):
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "t")
    assert di_push.di_enabled() is False
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://x")
    assert di_push.di_enabled() is True


def test_untagged_connected_without_started_at_is_pushed(conn, monkeypatch):
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    camp = make_campaign(conn, agent=make_agent(conn))
    call = make_call(conn, phone="+919999900300", started_at=OLD, campaign=camp)
    with conn.cursor() as cur:
        cur.execute("UPDATE call SET started_at = NULL WHERE id = %s", (call["id"],))
    sent = _stub_post(monkeypatch)
    assert push_calls_to_di(conn, {"errors": []}, now=NOW) == 1
    assert sent[0][1]["called_at"] is None


@pytest.mark.parametrize("exc", [requests.Timeout("read timed out"), requests.ConnectionError("refused")])
def test_unreachable_di_stops_the_run_after_one_try(conn, monkeypatch, exc):
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    camp = make_campaign(conn, agent=make_agent(conn))
    for i in range(3):
        make_call(conn, phone=f"+91999990040{i}", started_at=OLD - timedelta(minutes=i), campaign=camp, tag="qualified")
    tried = []

    def post(url, json=None, headers=None, timeout=None):
        tried.append(json["call_id"])
        raise exc

    monkeypatch.setattr(di_push.requests, "post", post)
    report = {"errors": []}
    assert push_calls_to_di(conn, report, now=NOW) == 0
    assert len(tried) == 1 and report["errors"] == ["di: unreachable — stopping this run"]
    assert [f["call_id"] for f in get_setting(conn, "diPushFailures")] == tried  # tried last next time
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM call WHERE pushed_to_di_at IS NOT NULL")
        assert cur.fetchone()["n"] == 0


def test_recently_failed_calls_are_pushed_last(conn, monkeypatch):
    monkeypatch.setattr(config, "DI_AI_CALLS_URL", "https://di.example/ingest")
    monkeypatch.setattr(config, "DI_AI_CALLS_TOKEN", "tok")
    camp = make_campaign(conn, agent=make_agent(conn))
    rejected = make_call(conn, phone="+919999900501", started_at=OLD - timedelta(hours=5), campaign=camp,
                         tag="qualified")  # the oldest, but DI keeps rejecting it
    newer = make_call(conn, phone="+919999900502", started_at=OLD, campaign=camp, tag="qualified")
    set_setting(conn, "diPushFailures", [{"call_id": rejected["bolna_execution_id"], "error": "422 bad", "at": "x"}])
    sent = _stub_post(monkeypatch)
    assert push_calls_to_di(conn, {"errors": []}, limit=1, now=NOW) == 1
    assert [b["call_id"] for _, b, _ in sent] == [newer["bolna_execution_id"]]
