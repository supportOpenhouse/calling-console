"""Token-authenticated lead ingest (Meta lead webhook -> console). Model: Direct_Inventory ai_calls `_token_ok`."""
from __future__ import annotations

import hmac
import json

from flask import Blueprint, jsonify, request

from .. import config
from ..db import get_conn
from ..services import datasets as svc
from ..services.classify import normalize_phone
from ..services.errors import ApiError, bad_request, not_found
from ..services.workflows import fire_triggers

bp = Blueprint("ingest", __name__, url_prefix="/api/ingest")

_HINTS = {"phone": "phone", "name": "name", "city": "city", "society": "society", "segment": "segment"}


def _check_token() -> None:
    if not config.INGEST_TOKEN:
        raise ApiError(503, "lead ingest is not configured (INGEST_TOKEN is unset)")
    auth = request.headers.get("Authorization", "")
    given = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    if not hmac.compare_digest(given.encode(), config.INGEST_TOKEN.encode()):
        raise ApiError(401, "invalid token")


def _flat(lead: dict) -> dict:
    return {k: "" if v is None else json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v)
            for k, v in lead.items()}


@bp.post("/datasets/<id>/leads")
def ingest_leads(id):
    _check_token()
    body = request.get_json(silent=True)
    leads = body if isinstance(body, list) else [body]
    if not leads or not all(isinstance(x, dict) for x in leads):
        raise bad_request("body must be a lead object or an array of lead objects")
    if not any(normalize_phone(x.get("phone")) for x in leads):
        raise bad_request("no lead has a valid phone")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM dataset WHERE id = %s", (id,))
            if not cur.fetchone():
                raise not_found()
        stats = svc.upsert_dataset_rows(conn, id, [_flat(x) for x in leads], _HINTS)
        conn.commit()
        result = fire_triggers(conn, dataset_id=id)
        return jsonify({"stats": stats, "fired": result["fired"], "errors": result["errors"]})
    finally:
        conn.close()
