"""Use cases (per department): list, create, update."""
from __future__ import annotations

import re

from flask import Blueprint, g, jsonify, request
from psycopg2 import errors
from psycopg2.extras import Json

from ..db import get_conn
from ..services import rbac
from ..services.audit import audit
from ..services.errors import bad_request, conflict
from .auth import require_auth

bp = Blueprint("use_cases", __name__, url_prefix="/api")

_TAXONOMY_KEYS = ("good", "soft", "terminal", "dnc", "retry")


def _slug(key) -> str:
    if not isinstance(key, str):
        raise bad_request("key is required")
    out = re.sub(r"[^a-z0-9_]+", "_", key.lower()).strip("_")
    if not out:
        raise bad_request("key must contain letters or digits")
    return out


def _check_taxonomy(tx) -> dict:
    if not isinstance(tx, dict) or set(tx) != set(_TAXONOMY_KEYS) or not all(
            isinstance(tx[k], list) and all(isinstance(t, str) for t in tx[k]) for k in _TAXONOMY_KEYS):
        raise bad_request("tag_taxonomy needs exactly the lists: " + ", ".join(_TAXONOMY_KEYS))
    return tx


def _check_agent(conn, agent_id, dept: str) -> None:
    if agent_id is None:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT vertical FROM agent WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if not row:
        raise bad_request("default_agent_id: unknown agent")
    if row["vertical"] not in (dept, "BOTH"):
        raise bad_request("default_agent_id: agent belongs to another department")


def _validated(body: dict, current: dict | None) -> dict:
    """Merge `body` over `current` (None on create) and validate every field."""
    if current is None and "tag_taxonomy" not in body:
        raise bad_request("tag_taxonomy required")
    out = dict(current) if current else {"description": None, "default_agent_id": None}
    for f in ("key", "name", "description", "default_agent_id", "tag_taxonomy", "priority", "color", "active"):
        if f in body:
            out[f] = body[f]
    out["key"] = _slug(out.get("key"))
    if not isinstance(out.get("name"), str) or not out["name"].strip():
        raise bad_request("name is required")
    out["name"] = out["name"].strip()
    if "priority" in out and (not isinstance(out["priority"], int) or isinstance(out["priority"], bool)):
        raise bad_request("priority must be an integer")
    if "color" in out and (not isinstance(out["color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", out["color"])):
        raise bad_request("color must look like #12315C")
    if "active" in out and not isinstance(out["active"], bool):
        raise bad_request("active must be true or false")
    if out["description"] is not None and not isinstance(out["description"], str):
        raise bad_request("description must be text")
    out["tag_taxonomy"] = _check_taxonomy(out["tag_taxonomy"])
    return out


@bp.get("/departments/<slug>/use-cases")
@require_auth()
def list_use_cases(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    restriction = rbac.use_case_restriction(g.user)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM use_case WHERE department = %s" + (" AND id = ANY(%s)" if restriction is not None else "")
                        + " ORDER BY priority, name", [dept] + ([restriction] if restriction is not None else []))
            return jsonify({"items": cur.fetchall()})
    finally:
        conn.close()


@bp.post("/departments/<slug>/use-cases")
@require_auth()
def create_use_case(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "admin", dept)
    v = _validated(request.get_json(silent=True) or {}, None)
    conn = get_conn()
    try:
        _check_agent(conn, v["default_agent_id"], dept)
        try:
            with conn.cursor() as cur:
                cols = {"department": dept, **{k: v[k] for k in v}}
                cols["tag_taxonomy"] = Json(v["tag_taxonomy"])
                cur.execute(
                    f"INSERT INTO use_case ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                    list(cols.values()))
                row = cur.fetchone()
        except errors.UniqueViolation:
            conn.rollback()
            raise conflict(f"use case key {v['key']!r} already exists in this department")
        audit(conn, g.user["id"], "use_case.create", "use_case", row["id"], {"key": row["key"]})
        conn.commit()
        return jsonify(row), 201
    finally:
        conn.close()


@bp.put("/use-cases/<id>")
@require_auth()
def update_use_case(id):
    conn = get_conn()
    try:
        cur_row = rbac.load_scoped(conn, "use_case", id, g.user, "admin")
        v = _validated(request.get_json(silent=True) or {}, cur_row)
        _check_agent(conn, v["default_agent_id"], cur_row["department"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE use_case SET key=%s, name=%s, description=%s, default_agent_id=%s, tag_taxonomy=%s, "
                    "priority=%s, color=%s, active=%s WHERE id=%s RETURNING *",
                    (v["key"], v["name"], v["description"], v["default_agent_id"], Json(v["tag_taxonomy"]),
                     v["priority"], v["color"], v["active"], id))
                row = cur.fetchone()
        except errors.UniqueViolation:
            conn.rollback()
            raise conflict(f"use case key {v['key']!r} already exists in this department")
        audit(conn, g.user["id"], "use_case.update", "use_case", id, {"key": row["key"]})
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()
