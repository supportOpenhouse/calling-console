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


# ---------------------------------------------------------------- use-case and dataset webhooks

def _hook(client, monkeypatch, path, body, token=TOKEN):
    from .. import config
    monkeypatch.setattr(config, "INGEST_TOKEN", TOKEN)
    return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


def _uc_body(**over):
    from ..services.taxonomy import DEFAULT_TAXONOMY
    return {"department": "SUPPLY", "key": "Meta Sellers", "name": "Meta sellers", "tag_taxonomy": DEFAULT_TAXONOMY, **over}


def test_use_case_hook_needs_the_token(client, conn, monkeypatch):
    from .. import config
    monkeypatch.setattr(config, "INGEST_TOKEN", "")
    assert client.post("/api/ingest/use-cases", json=_uc_body(), headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 503
    assert _hook(client, monkeypatch, "/api/ingest/use-cases", _uc_body(), token="nope").status_code == 401
    assert client.post("/api/ingest/use-cases", json=_uc_body()).status_code == 401
    assert _hook(client, monkeypatch, "/api/ingest/use-cases", _uc_body()).status_code == 200


def test_use_case_hook_creates_then_updates_by_department_and_key(client, conn, monkeypatch):
    agent = make_agent(conn, bolna_agent_id="ba-sup", vertical="SUPPLY")
    demand = make_agent(conn, bolna_agent_id="ba-dem", vertical="DEMAND")
    conn.commit()
    r = _hook(client, monkeypatch, "/api/ingest/use-cases", _uc_body(default_agent_bolna_id="ba-sup"))
    assert r.status_code == 200, r.get_json()
    first = r.get_json()
    assert first["created"] is True and first["use_case"]["key"] == "meta_sellers"
    assert first["use_case"]["default_agent_id"] == agent["id"] and first["use_case"]["department"] == "SUPPLY"
    # same department + key: an update of only the fields sent
    r = _hook(client, monkeypatch, "/api/ingest/use-cases", {"department": "supply", "key": "meta_sellers", "name": "Meta v2",
                                                             "priority": 2})
    second = r.get_json()
    assert r.status_code == 200 and second["created"] is False and second["use_case"]["id"] == first["use_case"]["id"]
    assert (second["use_case"]["name"], second["use_case"]["priority"]) == ("Meta v2", 2)
    assert second["use_case"]["default_agent_id"] == agent["id"]  # untouched
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM use_case")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT action, meta FROM audit_log WHERE action = 'use_case.ingest' ORDER BY created_at")
        assert [a["meta"]["created"] for a in cur.fetchall()] == [True, False]
    bad = lambda body: _hook(client, monkeypatch, "/api/ingest/use-cases", body)  # noqa: E731
    assert bad(_uc_body(key="other", tag_taxonomy=None) | {"tag_taxonomy": None}).status_code == 400
    assert bad({k: v for k, v in _uc_body(key="new_one").items() if k != "tag_taxonomy"}).status_code == 400  # create needs it
    assert bad(_uc_body(department="SALES")).status_code == 400
    assert bad(_uc_body(default_agent_bolna_id="ba-unknown")).status_code == 400
    assert bad(_uc_body(default_agent_bolna_id=demand["bolna_agent_id"])).status_code == 400  # another department's agent
    assert bad(_uc_body(color="orange")).status_code == 400
    assert bad([_uc_body()]).status_code == 400


def test_dataset_hook_creates_then_appends_by_department_and_name(client, conn, monkeypatch):
    uc = make_use_case(conn, key="meta_sellers")
    conn.commit()
    body = {"department": "SUPPLY", "name": " Meta Nov ", "use_case_key": "meta_sellers", "description": "from Meta",
            "leads": [{"phone": "9810043058", "name": "Ravi", "city": "Noida"}, {"phone": "9810043059"}]}
    r = _hook(client, monkeypatch, "/api/ingest/datasets", body)
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["created"] is True and j["stats"]["added"] == 2
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM dataset WHERE id = %s", (j["dataset_id"],))
        ds = cur.fetchone()
    assert (ds["name"], ds["department"], ds["use_case_id"], ds["description"], ds["source"]) == (
        "Meta Nov", "SUPPLY", uc["id"], "from Meta", "UPLOAD")
    r = _hook(client, monkeypatch, "/api/ingest/datasets", {"department": "SUPPLY", "name": "Meta Nov",
                                                            "leads": [{"phone": "9810043060"}, {"phone": "9810043058"}]})
    k = r.get_json()
    assert r.status_code == 200 and k["created"] is False and k["dataset_id"] == j["dataset_id"]
    assert (k["stats"]["added"], k["stats"]["updated"]) == (1, 1)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM dataset_lead WHERE dataset_id = %s", (j["dataset_id"],))
        assert cur.fetchone()["n"] == 3
        cur.execute("SELECT use_case_id, description FROM dataset WHERE id = %s", (j["dataset_id"],))
        assert dict(cur.fetchone()) == {"use_case_id": uc["id"], "description": "from Meta"}  # not sent: untouched
    empty = _hook(client, monkeypatch, "/api/ingest/datasets", {"department": "DEMAND", "name": "Empty list"}).get_json()
    assert empty["created"] is True and empty["stats"] is None
    bad = lambda body: _hook(client, monkeypatch, "/api/ingest/datasets", body)  # noqa: E731
    assert bad({"department": "SUPPLY", "name": "X", "use_case_key": "nope"}).status_code == 400
    assert bad({"department": "SUPPLY", "name": "X", "leads": [{"phone": "12"}]}).status_code == 400
    assert bad({"department": "SUPPLY", "name": "X", "leads": "9810043058"}).status_code == 400
    assert bad({"department": "SUPPLY", "name": ""}).status_code == 400
    assert bad({"department": "NOPE", "name": "X"}).status_code == 400
    assert _hook(client, monkeypatch, "/api/ingest/datasets", {"department": "SUPPLY", "name": "X"}, token="nope").status_code == 401


def test_dataset_hook_refuses_an_ambiguous_name(client, conn, monkeypatch):
    for _ in range(2):
        d = make_dataset(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE dataset SET name = 'Twins' WHERE id = %s", (d["id"],))
    conn.commit()
    r = _hook(client, monkeypatch, "/api/ingest/datasets", {"department": "SUPPLY", "name": "Twins", "leads": [{"phone": "9810043058"}]})
    assert r.status_code == 409 and "2 datasets" in r.get_json()["error"]
