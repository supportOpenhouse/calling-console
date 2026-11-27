import io

import pytest

from ..services import storage
from .conftest import auth_headers
from .factories import make_user

KB = "13d0e11b-75e3-43f2-80d2-eb6a49d79dd9"
PDF = b"%PDF-1.4 sample"


def _kb(**kw):
    return {"rag_id": KB, "file_name": "NCR_Society_Location_List.pdf", "status": "processed", "source_type": "pdf",
            "chunk_size": 512.0, "similarity_top_k": 15.0, "overlapping": 20.0, "language_support": None,
            "agent_count": 0, "is_used_by_agents": False, **kw}


@pytest.fixture
def r2(monkeypatch):
    """R2 configured, its knowledge-base copies in a dict."""
    copies = {}
    monkeypatch.setattr(storage, "storage_enabled", lambda: True)
    monkeypatch.setattr(storage, "save_kb_pdf", lambda rag_id, data: copies.__setitem__(rag_id, data))
    monkeypatch.setattr(storage, "load_kb_pdf", lambda rag_id: copies.get(rag_id))
    monkeypatch.setattr(storage, "delete_kb_pdf", lambda rag_id: copies.pop(rag_id, None))
    return copies


def _hdr(conn, role):
    u = make_user(conn, role=role, vertical="BOTH")
    conn.commit()
    return auth_headers(u)


def _audits(conn, action):
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, meta FROM audit_log WHERE action = %s", (action,))
        return cur.fetchall()


def test_anyone_signed_in_lists_and_views(client, conn, bolna_stub):
    bolna_stub.kbs = {KB: _kb()}
    h = _hdr(conn, "VIEWER")
    assert client.get("/api/knowledgebases", headers=h).json == {"items": [_kb()]}
    assert client.get(f"/api/knowledgebases/{KB}", headers=h).json["file_name"] == "NCR_Society_Location_List.pdf"
    assert client.get("/api/knowledgebases/all", headers=h).status_code == 400  # never a path into Bolna's /all
    assert client.get("/api/knowledgebases").status_code == 401


def test_owner_creates_from_a_pdf_or_a_url_with_every_option(client, conn, bolna_stub, r2):
    h = _hdr(conn, "ADMIN")
    r = client.post("/api/knowledgebases", headers=h, content_type="multipart/form-data", data={
        "file": (io.BytesIO(PDF), "Societies.pdf"), "chunk_size": "256", "similarity_top_k": "5", "overlapping": "0",
        "language_support": "multilingual"})
    assert r.status_code == 200 and r.json["status"] == "processing"
    assert bolna_stub.calls[-1] == ("create_knowledgebase", (
        {"chunk_size": 256, "similarity_top_k": 5, "overlapping": 0, "language_support": "multilingual"}, ("Societies.pdf", PDF)))
    assert r2 == {"3c90c3d0d444b5088228dd25736052af": PDF}  # our copy, to open it later
    r = client.post("/api/knowledgebases", headers=h, content_type="multipart/form-data",
                    data={"url": " https://openhouse.in/faq "})
    assert r.status_code == 200 and bolna_stub.calls[-1] == ("create_knowledgebase", ({"url": "https://openhouse.in/faq"}, None))
    assert [a["meta"].get("pdf") or a["meta"]["url"] for a in _audits(conn, "knowledgebase.create")] == \
        ["Societies.pdf", "https://openhouse.in/faq"]


def test_create_validates_before_calling_bolna(client, conn, bolna_stub):
    h = _hdr(conn, "ADMIN")
    def post(**data):
        return client.post("/api/knowledgebases", headers=h, content_type="multipart/form-data", data=data)
    assert "not both" in post().json["error"]
    assert "not both" in post(url="https://a.in", file=(io.BytesIO(PDF), "a.pdf")).json["error"]
    assert "PDF" in post(file=(io.BytesIO(b"hello"), "a.pdf")).json["error"]
    assert "PDF" in post(file=(io.BytesIO(PDF), "a.docx")).json["error"]
    assert "http" in post(url="ftp://a.in").json["error"]
    assert "chunk_size" in post(url="https://a.in", chunk_size="0").json["error"]
    assert "similarity_top_k" in post(url="https://a.in", similarity_top_k="1.5").json["error"]
    assert "language_support" in post(url="https://a.in", language_support="hindi").json["error"]
    assert not [c for c in bolna_stub.calls if c[0] == "create_knowledgebase"]


def test_only_owners_create_or_delete(client, conn, bolna_stub):
    bolna_stub.kbs = {KB: _kb()}
    h = _hdr(conn, "HEAD")
    assert client.post("/api/knowledgebases", headers=h, content_type="multipart/form-data",
                       data={"url": "https://a.in"}).status_code == 403
    assert client.delete(f"/api/knowledgebases/{KB}", headers=h).status_code == 403
    assert not [c for c in bolna_stub.calls if c[0] in ("create_knowledgebase", "delete_knowledgebase")]


def test_delete_refuses_one_an_agent_uses(client, conn, bolna_stub):
    h = _hdr(conn, "ADMIN")
    bolna_stub.kbs = {KB: _kb(agent_count=2.0, is_used_by_agents=True)}
    r = client.delete(f"/api/knowledgebases/{KB}", headers=h)
    assert r.status_code == 409 and "2 agent(s)" in r.json["error"]
    bolna_stub.kbs = {KB: _kb()}
    assert client.delete(f"/api/knowledgebases/{KB}", headers=h).json["state"] == "deleted"
    assert bolna_stub.calls[-1] == ("delete_knowledgebase", (KB,))
    assert [(a["entity_id"], a["meta"]["file_name"]) for a in _audits(conn, "knowledgebase.delete")] == \
        [(KB, "NCR_Society_Location_List.pdf")]


def test_pdf_upload_needs_r2_before_anything_reaches_bolna(client, conn, bolna_stub, monkeypatch):
    monkeypatch.setattr(storage, "storage_enabled", lambda: False)
    r = client.post("/api/knowledgebases", headers=_hdr(conn, "ADMIN"), content_type="multipart/form-data",
                    data={"file": (io.BytesIO(PDF), "a.pdf")})
    assert r.status_code == 503 and not [c for c in bolna_stub.calls if c[0] == "create_knowledgebase"]


def test_open_serves_our_copy_to_anyone_signed_in(client, conn, bolna_stub, r2):
    bolna_stub.kbs = {KB: _kb()}
    h = _hdr(conn, "VIEWER")
    r = client.get(f"/api/knowledgebases/{KB}/file", headers=h)
    assert r.status_code == 404 and "uploaded in Bolna directly" in r.json["error"]
    r2[KB] = PDF
    r = client.get(f"/api/knowledgebases/{KB}/file", headers=h)
    assert r.status_code == 200 and r.data == PDF and r.mimetype == "application/pdf"
    assert r.headers["Content-Disposition"].startswith("inline") and "NCR_Society_Location_List.pdf" in r.headers["Content-Disposition"]
    bolna_stub.kbs = {KB: _kb(file_name="सूची.pdf")}  # a non-ASCII name still makes a valid header
    assert client.get(f"/api/knowledgebases/{KB}/file", headers=h).status_code == 200


def test_delete_removes_our_copy(client, conn, bolna_stub, r2):
    bolna_stub.kbs = {KB: _kb()}
    r2[KB] = PDF
    assert client.delete(f"/api/knowledgebases/{KB}", headers=_hdr(conn, "ADMIN")).status_code == 200
    assert r2 == {}
