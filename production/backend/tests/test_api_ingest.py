from .factories import make_agent, make_dataset, make_use_case
from .test_workflows import make_workflow

TOKEN = "ingest-secret"


def _setup(conn):
    agent, uc = make_agent(conn), make_use_case(conn)
    ds = make_dataset(conn, use_case_id=uc["id"])
    wf = make_workflow(conn, agent=agent, use_case=uc, dataset=ds, trigger={"type": "ON_NEW_LEAD", "within_minutes": 2})
    conn.commit()
    return ds, wf


def _url(ds):
    return f"/api/ingest/datasets/{ds['id']}/leads"


def _batches(stub):
    return [c for c in stub.calls if c[0] == "create_batch"]


def test_ingest_requires_token(client, conn, monkeypatch):
    ds = make_dataset(conn)
    conn.commit()
    from .. import config
    monkeypatch.setattr(config, "INGEST_TOKEN", "")
    assert client.post(_url(ds), json={"phone": "9810043058"}, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 503
    monkeypatch.setattr(config, "INGEST_TOKEN", TOKEN)
    assert client.post(_url(ds), json={"phone": "9810043058"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post(_url(ds), json={"phone": "9810043058"}).status_code == 401
    assert client.post(_url(ds), json={"phone": "9810043058"},
                       headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_ingest_unknown_dataset_404(client, conn, monkeypatch):
    from .. import config
    monkeypatch.setattr(config, "INGEST_TOKEN", TOKEN)
    r = client.post("/api/ingest/datasets/nope/leads", json={"phone": "9810043058"},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404


def test_ingest_dials_new_lead_once(client, conn, bolna_stub, monkeypatch):
    from .. import config
    monkeypatch.setattr(config, "INGEST_TOKEN", TOKEN)
    ds, wf = _setup(conn)
    tok = {"Authorization": f"Bearer {TOKEN}"}
    r1 = client.post(_url(ds), json={"phone": "9810043058", "name": "A", "society": "X"}, headers=tok)
    assert r1.status_code == 200, r1.get_json()
    assert r1.get_json()["stats"]["added"] == 1 and r1.get_json()["fired"] == [
        {"workflow": wf["name"], "type": "ON_NEW_LEAD", "leads": 1}]
    r2 = client.post(_url(ds), json={"phone": "+91 98100 43058", "name": "A"}, headers=tok)
    assert r2.status_code == 200 and r2.get_json()["stats"]["updated"] == 1 and r2.get_json()["fired"] == []
    assert len(_batches(bolna_stub)) == 1


def test_ingest_array_and_partial_invalid(client, conn, monkeypatch):
    from .. import config
    monkeypatch.setattr(config, "INGEST_TOKEN", TOKEN)
    ds = make_dataset(conn)
    conn.commit()
    r = client.post(_url(ds), headers={"Authorization": f"Bearer {TOKEN}"}, json=[
        {"phone": 9810043058, "name": "A", "city": "Delhi"}, {"phone": "junk"}, {"phone": "9810043059"}])
    assert r.status_code == 200
    assert r.get_json()["stats"]["added"] == 2 and r.get_json()["stats"]["invalid"] == 1
    with conn.cursor() as cur:
        cur.execute("select city from dataset_lead where phone='+919810043058'")
        assert cur.fetchone()["city"] == "Delhi"


def test_ingest_invalid_phone_400(client, conn, monkeypatch):
    from .. import config
    monkeypatch.setattr(config, "INGEST_TOKEN", TOKEN)
    ds = make_dataset(conn)
    conn.commit()
    tok = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post(_url(ds), json={"phone": "123"}, headers=tok).status_code == 400
    assert client.post(_url(ds), json={"name": "no phone"}, headers=tok).status_code == 400
    assert client.post(_url(ds), json=[], headers=tok).status_code == 400
    assert client.post(_url(ds), json="str", headers=tok).status_code == 400
    assert client.post(_url(ds), data="notjson", headers=tok).status_code == 400
