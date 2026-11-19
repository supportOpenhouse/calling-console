from datetime import datetime, timedelta, timezone

import pytest

from ..services import datasets as ds
from .factories import make_agent, make_call, make_campaign, make_dataset

NOW = datetime.now(timezone.utc)


def _lead(conn, dsid, phone):
    with conn.cursor() as cur:
        cur.execute("select * from dataset_lead where dataset_id=%s and phone=%s", (dsid, phone))
        return cur.fetchone()


def test_upsert_counts_and_keeps_all_columns(conn):
    d = make_dataset(conn)
    rows = [{"Mobile": "9810043058", "Name": "A", "Extra": "x"}, {"Mobile": "9810043059", "Name": "B", "Extra": "y"},
            {"Mobile": "9810043058", "Name": "A2", "Extra": "z"}, {"Mobile": "12", "Name": "C", "Extra": "w"}]
    assert ds.upsert_dataset_rows(conn, d["id"], rows) == {"added": 2, "updated": 0, "invalid": 1, "duplicates": 1}
    assert ds.upsert_dataset_rows(conn, d["id"], rows[:1]) == {"added": 0, "updated": 1, "invalid": 0, "duplicates": 0}
    lead = _lead(conn, d["id"], "+919810043058")
    assert lead["data"]["Extra"] == "x" and lead["name"] == "A"
    with conn.cursor() as cur:
        cur.execute("select row_count, columns from dataset where id=%s", (d["id"],))
        r = cur.fetchone()
    assert r["row_count"] == 2 and "Extra" in r["columns"]


def test_upsert_requires_phone_column(conn):
    d = make_dataset(conn)
    with pytest.raises(ValueError, match="^No phone column found"):
        ds.upsert_dataset_rows(conn, d["id"], [{"foo": "1"}])


def test_segment_filters_and_dnc_exclusion(conn):
    d = make_dataset(conn, rows=[
        {"phone": "9810000001", "city": "Gurgaon", "segment": "A"},
        {"phone": "9810000002", "city": "Noida", "segment": "B"},
        {"phone": "9810000003", "city": "Gurgaon", "segment": "B"},
    ])

    def sel(seg):
        rows, _ = ds.select_leads_for_run(conn, "SUPPLY", d["id"], seg)
        return {r["phone"] for r in rows}
    assert sel({}) == {"9810000001", "9810000002", "9810000003"}
    assert sel({"cities": ["Gurgaon"]}) == {"9810000001", "9810000003"}
    assert sel({"segments": ["B"], "cities": ["Gurgaon"]}) == {"9810000003"}
    ds.touch_dataset_lead(conn, None, "9810000001", "CONNECTED", "good", NOW)
    ds.touch_dataset_lead(conn, None, "9810000001", "NOT_PICKED", None, NOW)
    l1 = _lead(conn, d["id"], "9810000001")
    assert l1["total_attempts"] == 2 and l1["last_tag"] == "good"
    # an older call reported late only counts its attempt
    ds.touch_dataset_lead(conn, None, "9810000001", "VOICEMAIL", "angry", NOW - timedelta(days=1), stale=True)
    stale = _lead(conn, d["id"], "9810000001")
    assert stale["total_attempts"] == 3
    assert (stale["last_called_at"], stale["last_outcome"], stale["last_tag"]) == (l1["last_called_at"], "NOT_PICKED",
                                                                                   "good")
    assert "9810000001" not in sel({"never_called": True})
    assert sel({"last_tag_in": ["good"]}) == {"9810000001"}
    assert sel({"last_outcome_in": ["NOT_PICKED"]}) == {"9810000001"}
    assert sel({"max_attempts": 1}) == {"9810000002", "9810000003"}
    assert len(sel({"limit": 1})) == 1
    ds.mark_dnc(conn, "9810000002", "manual")
    assert "9810000002" not in sel({})
    assert ds.dataset_facets(conn, d["id"])["cities"][0] == {"v": "Gurgaon", "n": 2}


def test_cooling_skips_spoken_not_ringing(conn):
    d = make_dataset(conn, rows=[{"phone": f"981000000{i}"} for i in range(1, 5)])
    make_call(conn, phone="9810000001", outcome="CONNECTED", started_at=NOW - timedelta(days=2))
    make_call(conn, phone="9810000002", outcome="NOT_PICKED", started_at=NOW - timedelta(days=2))
    make_call(conn, phone="9810000003", outcome="CONNECTED", started_at=NOW - timedelta(days=8))
    rows, cooled = ds.select_leads_for_run(conn, "SUPPLY", d["id"], {})
    assert {r["phone"] for r in rows} == {"9810000002", "9810000003", "9810000004"} and cooled == 1
    rows, cooled = ds.select_leads_for_run(conn, "SUPPLY", d["id"], {}, ignore_cooling=True)
    assert len(rows) == 4 and cooled == 0


def test_select_leads_needs_the_department_row_and_caps_the_run(conn, monkeypatch):
    d = make_dataset(conn, rows=[{"phone": f"981000000{i}"} for i in range(1, 6)])
    monkeypatch.setattr(ds, "MAX_RUN_LEADS", 3)
    assert len(ds.select_leads_for_run(conn, "SUPPLY", d["id"], {})[0]) == 3  # no limit given: the cap
    assert len(ds.select_leads_for_run(conn, "SUPPLY", d["id"], {"limit": 2})[0]) == 2
    assert len(ds.select_leads_for_run(conn, "SUPPLY", d["id"], {"limit": 2}, limit=4)[0]) == 4
    with conn.cursor() as cur:
        cur.execute("DELETE FROM department WHERE key = 'SUPPLY'")
    with pytest.raises(RuntimeError, match="department SUPPLY not found"):
        ds.select_leads_for_run(conn, "SUPPLY", d["id"], {})
    with pytest.raises(RuntimeError, match="department SUPPLY not found"):
        ds.select_leads_for_run(conn, "SUPPLY", d["id"], {}, ignore_cooling=True)
    conn.rollback()


def test_recently_spoken_excludes_campaign(conn):
    camp = make_campaign(conn, agent=make_agent(conn))
    make_call(conn, phone="9810000001", outcome="VOICEMAIL", started_at=NOW, campaign=camp)
    since = NOW - timedelta(days=1)
    assert ds.recently_spoken_phones(conn, ["9810000001"], since) == {"9810000001"}
    assert ds.recently_spoken_phones(conn, ["9810000001"], since, exclude_campaign_id=camp["id"]) == set()


def test_mark_dnc_is_idempotent(conn):
    ds.mark_dnc(conn, "9810000001", "manual", "asked")
    ds.mark_dnc(conn, "9810000001", "rule")
    assert ds.dnc_phones(conn, ["9810000001", "9810000002"]) == {"9810000001"}
    with conn.cursor() as cur:
        cur.execute("select source from dnc_phone")
        assert [r["source"] for r in cur.fetchall()] == ["manual"]
    ds.unmark_dnc(conn, "9810000001")
    assert ds.dnc_phones(conn, ["9810000001"]) == set()


def test_pull_dataset_from_source(conn, monkeypatch):
    d = make_dataset(conn)
    seen = {}

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"leads": [{"mobile": "9810043058", "nm": "A"}]}}

    def fake(method, url, **kw):
        seen.update(method=method, **kw)
        return R()
    monkeypatch.setattr(ds.requests, "request", fake)
    d["source_config"] = {"url": "http://x", "token": "t", "path": "data.leads", "phone_field": "mobile", "name_field": "nm"}
    assert ds.pull_dataset_from_source(conn, d)["added"] == 1
    assert seen["headers"]["Authorization"] == "Bearer t" and seen["headers"]["X-Ingest-Token"] == "t"
    assert _lead(conn, d["id"], "+919810043058")["name"] == "A"

    class Bad(R):
        status_code = 500
        text = "boom" * 100
    monkeypatch.setattr(ds.requests, "request", lambda *a, **k: Bad())
    with pytest.raises(ValueError, match="source 500: boomboom"):
        ds.pull_dataset_from_source(conn, d)
