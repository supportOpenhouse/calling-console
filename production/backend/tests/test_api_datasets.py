import io

from ..tests.conftest import auth_headers
from datetime import datetime, timezone

from psycopg2.extras import Json

from .factories import _insert, make_agent, make_campaign, make_dataset, make_lead, make_use_case, make_user

PHONE = "+919810043058"
CSV = b"phone,name,city\n9810043058,Asha,Gurgaon\n9810043059,Ravi,Delhi\nbad,Nobody,X\n"


def _hdr(conn, **kw):
    u = make_user(conn, **{"role": "MANAGER", "vertical": "SUPPLY", **kw})
    conn.commit()
    return u, auth_headers(u)


def _one(conn, sql, *p):
    with conn.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchone()


def test_create_dataset_upload_and_rows(client, conn):
    u, h = _hdr(conn)
    uc = make_use_case(conn)
    conn.commit()
    r = client.post("/api/departments/supply/datasets", headers=h, content_type="multipart/form-data", data={
        "name": "Sellers", "use_case_id": uc["id"], "source": "UPLOAD", "file": (io.BytesIO(CSV), "s.csv")})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["stats"]["added"] == 2 and body["stats"]["invalid"] == 1
    ds = body["dataset"]
    assert ds["department"] == "SUPPLY" and ds["row_count"] == 2
    rows = client.get(f"/api/datasets/{ds['id']}/rows", headers=h).get_json()
    assert rows["total"] == 2 and all(x["dnc"] is False for x in rows["items"])
    assert client.get(f"/api/datasets/{ds['id']}/rows?q=ravi", headers=h).get_json()["total"] == 1
    stats = client.get(f"/api/datasets/{ds['id']}", headers=h).get_json()["stats"]
    assert stats == {"total": 2, "never": 2, "connected": 0, "blocked": 0, "dnc": 0, "attempts": 0}
    items = client.get("/api/departments/supply/datasets", headers=h).get_json()["items"]
    assert [(i["never_called"], i["dnc"]) for i in items] == [(2, 0)]
    assert client.get(f"/api/datasets/{ds['id']}/facets", headers=h).get_json()["cities"]


def test_create_validation(client, conn):
    u, h = _hdr(conn)
    other = make_use_case(conn, department="DEMAND", key="x")
    conn.commit()
    url = "/api/departments/supply/datasets"
    f = {"content_type": "multipart/form-data", "headers": h}
    assert client.post(url, data={"source": "UPLOAD"}, **f).status_code == 400  # no name
    assert client.post(url, data={"name": "a", "source": "NOPE"}, **f).status_code == 400
    assert client.post(url, data={"name": "a", "source": "CRM"}, **f).status_code == 400  # no url
    assert client.post(url, data={"name": "a", "use_case_id": other["id"]}, **f).status_code == 400
    bad = client.post(url, data={"name": "a", "file": (io.BytesIO(b"x"), "a.txt")}, **f)
    assert bad.status_code == 400
    assert _one(conn, "select count(*) n from dataset")["n"] == 0


def test_failed_pull_returns_dataset_with_error(client, conn, monkeypatch):
    from ..services import datasets as svc

    def boom(conn, ds):
        raise ValueError("source 500: nope")
    monkeypatch.setattr(svc, "pull_dataset_from_source", boom)
    u, h = _hdr(conn)
    r = client.post("/api/departments/supply/datasets", headers=h, content_type="multipart/form-data",
                    data={"name": "CRM", "source": "CRM", "url": "http://x", "phone_field": "mobile", "token": "t"})
    assert r.status_code == 201
    assert r.get_json()["stats"] == {"error": "source 500: nope"}
    cfg = r.get_json()["dataset"]["source_config"]
    assert cfg == {"url": "http://x", "phone_field": "mobile"} and r.get_json()["dataset"]["has_token"] is True
    assert _one(conn, "select count(*) n from dataset")["n"] == 1
    assert _one(conn, "select source_config from dataset")["source_config"]["token"] == "t"  # kept, never sent


def test_dataset_responses_never_carry_the_source_token(client, conn):
    u, h = _hdr(conn)
    _insert(conn, "dataset", department="SUPPLY", name="CRM", source="CRM",
            source_config=Json({"url": "http://x", "token": "secret"}))
    _insert(conn, "dataset", department="SUPPLY", name="Upload")
    conn.commit()
    items = client.get("/api/departments/supply/datasets", headers=h).get_json()["items"]
    by_name = {d["name"]: d for d in items}
    assert by_name["CRM"]["source_config"] == {"url": "http://x"} and by_name["CRM"]["has_token"] is True
    assert by_name["Upload"]["source_config"] is None and by_name["Upload"]["has_token"] is False
    d = client.get(f"/api/datasets/{by_name['CRM']['id']}", headers=h).get_json()["dataset"]
    assert d["source_config"] == {"url": "http://x"} and d["has_token"] is True


def test_dataset_of_other_dept_403(client, conn):
    ds = make_dataset(conn, department="SUPPLY", rows=[{"phone": PHONE}])
    u, h = _hdr(conn, vertical="DEMAND")
    conn.commit()
    i = ds["id"]
    for method, path, kw in (
            ("get", "", {}), ("get", "/rows", {}), ("get", "/facets", {}),
            ("post", "/append", {"data": {"file": (io.BytesIO(CSV), "s.csv")}, "content_type": "multipart/form-data"}),
            ("post", "/pull", {}), ("post", "/preview", {"json": {}}), ("delete", "", {})):
        r = getattr(client, method)(f"/api/datasets/{i}{path}", headers=h, **kw)
        assert r.status_code == 403, (method, path)
    assert client.post("/api/departments/supply/datasets", headers=h, data={"name": "a"}).status_code == 403
    assert client.get("/api/datasets/nope", headers=h).status_code == 404


def test_append_preview_delete(client, conn):
    u, h = _hdr(conn, role="HEAD")
    ds = make_dataset(conn, rows=[{"phone": "+919810043059", "name": "Old", "city": "Delhi"}])
    conn.commit()
    i = ds["id"]
    r = client.post(f"/api/datasets/{i}/append", headers=h, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(CSV), "s.csv")})
    assert r.status_code == 200 and r.get_json()["added"] == 1 and r.get_json()["updated"] == 1
    assert client.post(f"/api/datasets/{i}/append", headers=h, content_type="multipart/form-data").status_code == 400
    client.put(f"/api/dnc/{PHONE}", headers=h, json={"reason": "asked"})
    p = client.post(f"/api/datasets/{i}/preview", headers=h, json={"segment": {}}).get_json()
    assert p["selected"] == 1 and p["cooled"] == 0 and len(p["sample"]) == 1  # the DNC row is excluded
    assert client.post(f"/api/datasets/{i}/pull", headers=h).status_code == 400  # no source URL
    assert client.delete(f"/api/datasets/{i}", headers=h).status_code == 204
    assert _one(conn, "select count(*) n from dataset")["n"] == 0


def test_delete_needs_admin(client, conn):
    u, h = _hdr(conn, role="MANAGER")
    ds = make_dataset(conn)
    conn.commit()
    assert client.delete(f"/api/datasets/{ds['id']}", headers=h).status_code == 403


def test_row_filters(client, conn):
    u, h = _hdr(conn)
    ds = make_dataset(conn, rows=[{"phone": "+919810000001"}, {"phone": "+919810000002"}, {"phone": PHONE}])
    with conn.cursor() as cur:
        cur.execute("UPDATE dataset_lead SET last_called_at=now(), last_outcome='CONNECTED', total_attempts=2 "
                    "WHERE phone='+919810000001'")
    conn.commit()
    client.put(f"/api/dnc/{PHONE}", headers=h, json={})
    g = lambda f: client.get(f"/api/datasets/{ds['id']}/rows?f={f}", headers=h).get_json()["total"]
    assert (g("never"), g("connected"), g("dnc")) == (2, 1, 1)
    assert client.get(f"/api/datasets/{ds['id']}/rows?f=bogus", headers=h).status_code == 400
    s = client.get(f"/api/datasets/{ds['id']}", headers=h).get_json()["stats"]
    assert s == {"total": 3, "never": 2, "connected": 1, "blocked": 0, "dnc": 1, "attempts": 2}


def test_dnc_toggle_global(client, conn):
    u, h = _hdr(conn)
    a = make_dataset(conn, department="SUPPLY", rows=[{"phone": PHONE}])
    b = make_dataset(conn, department="DEMAND", rows=[{"phone": PHONE}])
    conn.commit()
    assert client.put("/api/dnc/98100", headers=h, json={}).status_code == 400
    r = client.put("/api/dnc/9810043058", headers=h, json={"reason": "asked"})
    assert r.status_code == 200 and r.get_json() == {"queued_in": []}
    ha = auth_headers(u)
    assert client.get(f"/api/datasets/{a['id']}/rows", headers=ha).get_json()["items"][0]["dnc"] is True
    owner = make_user(conn, role="ADMIN", vertical="BOTH")
    conn.commit()
    assert client.get(f"/api/datasets/{b['id']}/rows", headers=auth_headers(owner)).get_json()["items"][0]["dnc"] is True
    d = _one(conn, "select * from dnc_phone where phone=%s", PHONE)
    assert d["source"] == "manual" and d["reason"] == "asked" and d["created_by_id"] == u["id"]
    assert _one(conn, "select count(*) n from audit_log where action='dnc.add'")["n"] == 1
    head, hh = _hdr(conn, role="HEAD")
    r = client.delete(f"/api/dnc/{PHONE}", headers=h)  # a manager may block, never unblock
    assert r.status_code == 403
    assert client.delete("/api/dnc/98100", headers=hh).status_code == 400
    assert client.delete(f"/api/dnc/{PHONE}", headers=hh).status_code == 204
    assert client.get(f"/api/datasets/{a['id']}/rows", headers=ha).get_json()["items"][0]["dnc"] is False
    assert _one(conn, "select count(*) n from audit_log where action='dnc.remove'")["n"] == 1


def test_dnc_names_live_batches_still_holding_the_phone(client, conn):
    u, h = _hdr(conn)
    camp = make_campaign(conn, agent=make_agent(conn))
    lead = make_lead(conn, camp, phone=PHONE, status="QUEUED")
    at = datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("UPDATE lead SET next_dial_at = %s WHERE id = %s", (at, lead["id"]))
    conn.commit()
    r = client.put(f"/api/dnc/{PHONE}", headers=h, json={})
    assert r.status_code == 200
    [q] = r.get_json()["queued_in"]
    assert (q["campaign_id"], q["campaign_name"], datetime.fromisoformat(q["scheduled_at"])) == (
        camp["id"], camp["name"], at)


def test_dnc_requires_auth(client, conn):
    assert client.put(f"/api/dnc/{PHONE}", json={}).status_code == 401


def _create(client, h, filename, data):
    return client.post("/api/departments/supply/datasets", headers=h, content_type="multipart/form-data",
                       data={"name": "a", "file": (io.BytesIO(data), filename)})


def test_oversized_upload_413_json(client, conn):
    u, h = _hdr(conn)
    r = _create(client, h, "big.csv", b"phone\n" + b"9" * (20 * 1024 * 1024 + 1))
    assert r.status_code == 413
    assert r.get_json() == {"error": "file too large (max 20 MB)"}


def test_unparseable_files_400_without_dataset(client, conn):
    import zipfile
    u, h = _hdr(conn)
    assert _create(client, h, "n.csv", b"phone,name\n9810043058,A\x00B\n").status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    assert _create(client, h, "n.xlsx", buf.getvalue()).status_code == 400
    assert _one(conn, "select count(*) n from dataset")["n"] == 0
