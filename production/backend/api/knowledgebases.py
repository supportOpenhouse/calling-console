"""Bolna knowledge bases (a PDF or a web page an agent can search): list, view, create, delete -- every endpoint of
https://www.bolna.ai/docs/api-reference/knowledgebase/overview. They live in Bolna only; nothing is stored here.
Anyone signed in may view; only owners create or delete (as with agents), and both are audited. Bolna has no update:
to change a knowledge base, create a new one and delete the old."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from flask import Blueprint, g, jsonify, request

from ..db import get_conn
from ..services import bolna, rbac
from ..services.audit import audit
from ..services.errors import ApiError, bad_request
from .auth import require_auth

bp = Blueprint("knowledgebases", __name__, url_prefix="/api")

_RAG_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}")
# Bolna's create options (their defaults apply to any left out): name -> smallest value
_INTS = {"chunk_size": 1, "similarity_top_k": 1, "overlapping": 0}
_LANGUAGES = ("multilingual",)
_MAX_PDF = 20 * 1024 * 1024  # Bolna's limit


def _rag_id(rag_id: str) -> str:
    if not _RAG_ID.fullmatch(rag_id):
        raise bad_request("not a knowledge base id")
    return rag_id


def _audit(action: str, rag_id: str | None, meta: dict) -> None:
    conn = get_conn()
    try:
        audit(conn, g.user["id"], action, "knowledgebase", rag_id, meta)
        conn.commit()
    finally:
        conn.close()


@bp.get("/knowledgebases")
@require_auth()
def list_knowledgebases():
    return jsonify({"items": bolna.list_knowledgebases()})


@bp.get("/knowledgebases/<rag_id>")
@require_auth()
def get_knowledgebase(rag_id):
    return jsonify(bolna.get_knowledgebase(_rag_id(rag_id)))


@bp.post("/knowledgebases")
@require_auth()
def create_knowledgebase():
    """multipart/form-data: `file` (a PDF, max 20 MB) or `url` (http/https), not both; optional chunk_size,
    similarity_top_k, overlapping (whole numbers) and language_support ('multilingual'). Answers Bolna's reply
    ({rag_id, file_name, source_type, status: processing, language_support})."""
    rbac.require(g.user, "owner", "SUPPLY")
    form, file = request.form, request.files.get("file")
    url = (form.get("url") or "").strip()
    if bool(file) == bool(url):
        raise bad_request("send a PDF file or a url, not both")
    fields: dict = {}
    pdf = None
    if file:
        data = file.read()
        if not (file.filename or "").lower().endswith(".pdf") or not data.startswith(b"%PDF"):
            raise bad_request("the file must be a PDF")
        if len(data) > _MAX_PDF:
            raise bad_request("the PDF is larger than 20 MB")
        pdf = (file.filename, data)
    else:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise bad_request("url must start with http:// or https://")
        fields["url"] = url
    for key, low in _INTS.items():
        raw = (form.get(key) or "").strip()
        if not raw:
            continue
        if not raw.isdigit() or int(raw) < low:
            raise bad_request(f"{key} must be a whole number of at least {low}")
        fields[key] = int(raw)
    lang = (form.get("language_support") or "").strip()
    if lang:
        if lang not in _LANGUAGES:
            raise bad_request("language_support must be multilingual (or left out for English)")
        fields["language_support"] = lang
    r = bolna.create_knowledgebase(fields, pdf)
    _audit("knowledgebase.create", r.get("rag_id"),
           {"file_name": r.get("file_name"), **({"pdf": pdf[0]} if pdf else {}), **fields})
    return jsonify(r)


@bp.delete("/knowledgebases/<rag_id>")
@require_auth()
def delete_knowledgebase(rag_id):
    """409 while an agent uses it (Bolna's agent_count / is_used_by_agents): detach it from the agent in Bolna first."""
    rbac.require(g.user, "owner", "SUPPLY")
    kb = bolna.get_knowledgebase(_rag_id(rag_id))
    if kb.get("is_used_by_agents") or kb.get("agent_count"):
        raise ApiError(409, f"{kb.get('file_name')} is used by {int(kb.get('agent_count') or 0)} agent(s): "
                            "remove it from the agent in Bolna first")
    r = bolna.delete_knowledgebase(rag_id)
    _audit("knowledgebase.delete", rag_id, {"file_name": kb.get("file_name")})
    return jsonify(r)
