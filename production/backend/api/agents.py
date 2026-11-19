"""Agents, test calls and phone numbers."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from psycopg2.extras import Json

from ..db import get_conn
from ..services import bolna, rbac
from ..services import sync as sync_svc
from ..services.audit import audit
from ..services.classify import normalize_phone
from ..services.errors import bad_request, forbidden, not_found
from ..services.redial import place_test_call
from .auth import require_auth

bp = Blueprint("agents", __name__, url_prefix="/api")

_LIST_COLS = ("id, bolna_agent_id, name, vertical, description, caller_name, default_from_number, variables, "
              "welcome_message, last_synced_at")
_VERTICALS = ("SUPPLY", "DEMAND", "BOTH")


def _visible(u: dict, agent: dict) -> bool:
    return agent["vertical"] == "BOTH" or agent["vertical"] in rbac.user_depts(u)


def _load(conn, agent_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM agent WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if not row:
        raise not_found()
    return row


@bp.get("/agents")
@require_auth()
def list_agents():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_LIST_COLS} FROM agent WHERE vertical = 'BOTH' OR vertical = ANY(%s::vertical[]) "
                        "ORDER BY name", (rbac.user_depts(g.user),))
            return jsonify({"items": cur.fetchall()})
    finally:
        conn.close()


@bp.get("/agents/<agent_id>")
@require_auth()
def get_agent(agent_id):
    conn = get_conn()
    try:
        row = _load(conn, agent_id)
        if not _visible(g.user, row):
            raise forbidden("forbidden")
        return jsonify(row)
    finally:
        conn.close()


@bp.post("/agents/refresh")
@require_auth()
def refresh_agents():
    rbac.require(g.user, "owner", "SUPPLY")
    conn = get_conn()
    try:
        count = sync_svc.sync_agents(conn)
        audit(conn, g.user["id"], "agent.refresh", None, None, {"count": count})
        conn.commit()
        return jsonify({"count": count})
    finally:
        conn.close()


def _text_or_none(body: dict, key: str):
    v = body[key]
    if v is not None and not isinstance(v, str):
        raise bad_request(f"{key} must be text or null")
    return (v.strip() or None) if v else None


@bp.put("/agents/<agent_id>")
@require_auth()
def update_agent(agent_id):
    rbac.require(g.user, "owner", "SUPPLY")
    body = request.get_json(silent=True) or {}
    if body.get("vertical") not in _VERTICALS:
        raise bad_request("vertical must be one of " + ", ".join(_VERTICALS))
    data = {"vertical": body["vertical"]}
    for key in ("description", "caller_name"):
        if key in body:
            data[key] = _text_or_none(body, key)
    if "default_from_number" in body:
        raw = _text_or_none(body, "default_from_number")
        data["default_from_number"] = None if raw is None else normalize_phone(raw)
        if raw is not None and data["default_from_number"] is None:
            raise bad_request("default_from_number is not a valid phone number")
    conn = get_conn()
    try:
        _load(conn, agent_id)
        sets = ", ".join(f"{k} = %s" for k in data)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE agent SET {sets} WHERE id = %s RETURNING *", [*data.values(), agent_id])
            row = cur.fetchone()
        audit(conn, g.user["id"], "agent.meta", "agent", agent_id, data)
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()


@bp.put("/agents/<agent_id>/prompt")
@require_auth()
def update_prompt(agent_id):
    rbac.require(g.user, "owner", "SUPPLY")
    body = request.get_json(silent=True) or {}
    prompt, welcome = body.get("system_prompt"), body.get("welcome_message")
    if not isinstance(prompt, str) or not prompt.strip():
        raise bad_request("system_prompt is required")
    if welcome is not None and not isinstance(welcome, str):
        raise bad_request("welcome_message must be text")
    conn = get_conn()
    try:
        row = _load(conn, agent_id)
        remote = bolna.get_agent(row["bolna_agent_id"])
        before = ((remote.get("agent_prompts") or {}).get("task_1") or {}).get("system_prompt") or ""
        if welcome is not None:
            remote["agent_welcome_message"] = welcome
        bolna.update_agent(row["bolna_agent_id"], remote, prompt)
        audit(conn, g.user["id"], "agent.prompt", "agent", agent_id,
              {"before_length": len(before), "after_length": len(prompt), "before": before})
        conn.commit()
        sync_svc.sync_agents(conn)
        return jsonify(_load(conn, agent_id))
    finally:
        conn.close()


@bp.post("/agents/<agent_id>/test-call")
@require_auth()
def test_call(agent_id):
    body = request.get_json(silent=True) or {}
    to, from_number = normalize_phone(body.get("to")), normalize_phone(body.get("from_number"))
    if not to:
        raise bad_request("to is not a valid phone number")
    if not from_number:
        raise bad_request("from_number is not a valid phone number")
    variables = body.get("variables")
    if not isinstance(variables, dict):
        raise bad_request("variables must be an object")
    conn = get_conn()
    try:
        agent = _load(conn, agent_id)
        if agent["vertical"] == "BOTH":
            rbac.require(g.user, "owner", "SUPPLY")
        else:
            rbac.require(g.user, "manage", agent["vertical"])
        result = place_test_call(conn, agent, to, from_number, variables)
        audit(conn, g.user["id"], "agent.testcall", "agent", agent_id,
              {"to": to, "from": from_number, "execution": result.get("execution_id")})
        conn.commit()
        return jsonify(result)
    finally:
        conn.close()


@bp.get("/numbers")
@require_auth()
def list_numbers():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM phone_number ORDER BY number")
            return jsonify({"items": cur.fetchall()})
    finally:
        conn.close()


@bp.post("/numbers/refresh")
@require_auth()
def refresh_numbers():
    rbac.require(g.user, "owner", "SUPPLY")
    count = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for n in bolna.list_phone_numbers():
                if not n.get("phone_number"):
                    continue
                cur.execute(
                    "INSERT INTO phone_number (number, provider, bolna_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT (number) DO UPDATE SET provider = EXCLUDED.provider, bolna_id = EXCLUDED.bolna_id",
                    (n["phone_number"], n.get("telephony_provider"), n.get("id")))
                count += 1
        audit(conn, g.user["id"], "number.refresh", None, None, {"count": count})
        conn.commit()
        return jsonify({"count": count})
    finally:
        conn.close()


@bp.put("/numbers/<number_id>")
@require_auth()
def update_number(number_id):
    rbac.require(g.user, "owner", "SUPPLY")
    body = request.get_json(silent=True) or {}
    data = {}
    if "label" in body:
        label = body["label"]
        if label is not None and not isinstance(label, str):
            raise bad_request("label must be text or null")
        data["label"] = (label.strip() or None) if label else None
    if "active" in body:
        if not isinstance(body["active"], bool):
            raise bad_request("active must be true or false")
        data["active"] = body["active"]
    if not data:
        raise bad_request("label or active is required")
    conn = get_conn()
    try:
        sets = ", ".join(f"{k} = %s" for k in data)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE phone_number SET {sets} WHERE id = %s RETURNING *", [*data.values(), number_id])
            row = cur.fetchone()
        if not row:
            raise not_found()
        audit(conn, g.user["id"], "number.update", "phone_number", number_id, data)
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()
