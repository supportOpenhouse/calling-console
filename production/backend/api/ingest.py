"""Token-authenticated ingest webhooks (Bearer INGEST_TOKEN; model: Direct_Inventory ai_calls `_token_ok`):

- POST /api/ingest/datasets/<id>/leads  append leads to an existing dataset (the Meta landing page)
- POST /api/ingest/use-cases            create or update a use case, matched by department + key
- POST /api/ingest/datasets             create or update a dataset, matched by department + name, and append leads
"""
from __future__ import annotations

import hmac
import json

from flask import Blueprint, jsonify, request
from psycopg2 import errors
from psycopg2.extras import Json

from .. import config
from ..db import get_conn
from ..services import datasets as svc
from ..services.classify import normalize_phone
from ..services.audit import audit
from ..services.errors import ApiError, bad_request, conflict, not_found
from ..services.workflows import fire_triggers
from .use_cases import _check_agent, _slug, _validated

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


def _dept(body: dict) -> str:
    d = body.get("department")
    if not isinstance(d, str) or d.upper() not in ("SUPPLY", "DEMAND"):
        raise bad_request("department must be SUPPLY or DEMAND")
    return d.upper()


@bp.post("/use-cases")
def ingest_use_case():
    """Create or update a use case, matched by department + key (slugged as in the app). Same fields and rules as the
    app's use-case API, except the default agent is named by its Bolna agent id (`default_agent_bolna_id`, null to
    clear). A new use case needs tag_taxonomy; an update changes only the fields sent. 200 {use_case, created}."""
    _check_token()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise bad_request("body must be a use-case object")
    dept, key = _dept(body), _slug(body.get("key"))
    fields = {k: body[k] for k in ("key", "name", "description", "tag_taxonomy", "priority", "color", "active") if k in body}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if "default_agent_bolna_id" in body:
                bid = body["default_agent_bolna_id"]
                if bid is None:
                    fields["default_agent_id"] = None
                else:
                    cur.execute("SELECT id FROM agent WHERE bolna_agent_id = %s", (bid,))
                    agent = cur.fetchone()
                    if not agent:
                        raise bad_request("default_agent_bolna_id: unknown Bolna agent (refresh agents first)")
                    fields["default_agent_id"] = agent["id"]
            cur.execute("SELECT * FROM use_case WHERE department = %s AND key = %s FOR UPDATE", (dept, key))
            current = cur.fetchone()
        v = _validated(fields, current)
        _check_agent(conn, v["default_agent_id"], dept)
        try:
            with conn.cursor() as cur:
                if current:
                    cur.execute(
                        "UPDATE use_case SET key=%s, name=%s, description=%s, default_agent_id=%s, tag_taxonomy=%s, "
                        "priority=%s, color=%s, active=%s WHERE id=%s RETURNING *",
                        (v["key"], v["name"], v["description"], v["default_agent_id"], Json(v["tag_taxonomy"]),
                         v["priority"], v["color"], v["active"], current["id"]))
                else:
                    cols = {"department": dept, **v, "tag_taxonomy": Json(v["tag_taxonomy"])}
                    cur.execute(f"INSERT INTO use_case ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                                "RETURNING *", list(cols.values()))
                row = cur.fetchone()
        except errors.UniqueViolation:  # renamed onto another use case's key, or two creates at once
            conn.rollback()
            raise conflict(f"use case key {v['key']!r} already exists in this department")
        audit(conn, None, "use_case.ingest", "use_case", row["id"], {"key": row["key"], "created": current is None})
        conn.commit()
        return jsonify({"use_case": row, "created": current is None})
    finally:
        conn.close()


@bp.post("/datasets")
def ingest_dataset():
    """Create or update a dataset, matched by department + name, and append `leads` (optional; same handling as the
    leads webhook, including ON_NEW_LEAD triggers). `use_case_key` (department's use case; null to clear) and
    `description` change only when sent. Source is UPLOAD (rows are pushed, never pulled). 409 when two datasets of
    the department share the name. 200 {dataset_id, created, stats, fired, errors}."""
    _check_token()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise bad_request("body must be a dataset object")
    dept = _dept(body)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise bad_request("name is required")
    name = name.strip()
    if "description" in body and body["description"] is not None and not isinstance(body["description"], str):
        raise bad_request("description must be text")
    leads = body.get("leads", [])
    if not isinstance(leads, list) or not all(isinstance(x, dict) for x in leads):
        raise bad_request("leads must be an array of lead objects")
    if leads and not any(normalize_phone(x.get("phone")) for x in leads):
        raise bad_request("no lead has a valid phone")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            uc_id = None
            if body.get("use_case_key") is not None:
                cur.execute("SELECT id FROM use_case WHERE department = %s AND key = %s", (dept, _slug(body["use_case_key"])))
                uc = cur.fetchone()
                if not uc:
                    raise bad_request("use_case_key: no such use case in this department")
                uc_id = uc["id"]
            # one create per department + name at a time (no unique index: the app allows repeated names)
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"dataset-ingest:{dept}:{name}",))
            cur.execute("SELECT * FROM dataset WHERE department = %s AND name = %s FOR UPDATE", (dept, name))
            found = cur.fetchall()
            if len(found) > 1:
                raise conflict(f"{len(found)} datasets in {dept} are named {name!r}: rename one in the app")
            if found:
                ds = found[0]
                sets = {}
                if "description" in body:
                    sets["description"] = body["description"]
                if "use_case_key" in body:
                    sets["use_case_id"] = uc_id
                if sets:
                    cur.execute(f"UPDATE dataset SET {', '.join(f'{k} = %s' for k in sets)}, updated_at = now() "
                                "WHERE id = %s RETURNING *", [*sets.values(), ds["id"]])
                    ds = cur.fetchone()
            else:
                cur.execute("INSERT INTO dataset (department, use_case_id, name, description, source) "
                            "VALUES (%s, %s, %s, %s, 'UPLOAD') RETURNING *", (dept, uc_id, name, body.get("description")))
                ds = cur.fetchone()
        stats = svc.upsert_dataset_rows(conn, ds["id"], [_flat(x) for x in leads], _HINTS) if leads else None
        audit(conn, None, "dataset.ingest", "dataset", ds["id"], {"name": name, "created": not found, "stats": stats})
        conn.commit()
        result = fire_triggers(conn, dataset_id=ds["id"]) if leads else {"fired": [], "errors": []}
        return jsonify({"dataset_id": ds["id"], "created": not found, "stats": stats,
                        "fired": result["fired"], "errors": result["errors"]})
    finally:
        conn.close()
