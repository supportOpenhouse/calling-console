"""Hand-offs: the per-department queue and the RM's updates."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..db import get_conn
from ..services import rbac
from ..services.audit import audit
from ..services.errors import bad_request
from .auth import require_auth

bp = Blueprint("handoffs", __name__, url_prefix="/api")

_STATUSES = ("OPEN", "CONTACTED", "VISIT_FIXED", "CLOSED_WON", "CLOSED_LOST", "INVALID")


@bp.get("/departments/<slug>/handoffs")
@require_auth()
def list_handoffs(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    status, use_case_id = request.args.get("status"), request.args.get("use_case_id")
    if status is not None and status not in _STATUSES:
        raise bad_request("status must be one of " + ", ".join(_STATUSES))
    where, params = ["h.department = %s"], [dept]
    if status:
        where.append("h.status = %s")
        params.append(status)
    if use_case_id:
        where.append("h.use_case_id = %s")
        params.append(use_case_id)
    restriction = rbac.use_case_restriction(g.user)
    if restriction is not None:
        where.append("h.use_case_id = ANY(%s)")
        params.append(restriction)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT h.*, u.name AS use_case_name FROM handoff h "
                        "LEFT JOIN use_case u ON u.id = h.use_case_id WHERE " + " AND ".join(where)
                        + " ORDER BY h.created_at DESC, h.id", params)
            return jsonify({"items": cur.fetchall()})
    finally:
        conn.close()


@bp.patch("/handoffs/<id>")
@require_auth()
def update_handoff(id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise bad_request("body must be a JSON object")
    sets, params = [], []
    if "status" in body:
        if body["status"] not in _STATUSES:
            raise bad_request("status must be one of " + ", ".join(_STATUSES))
        sets.append("status = %s")
        params.append(body["status"])
    if "rm_notes" in body:
        if body["rm_notes"] is not None and not isinstance(body["rm_notes"], str):
            raise bad_request("rm_notes must be text")
        sets.append("rm_notes = %s")
        params.append(body["rm_notes"])
    conn = get_conn()
    try:
        ho = rbac.load_scoped(conn, "handoff", id, g.user, "manage")
        if "assigned_to_id" in body:
            who = body["assigned_to_id"]
            if who is not None:
                u = None
                if isinstance(who, str):
                    with conn.cursor() as cur:
                        cur.execute('SELECT role, vertical, active FROM "user" WHERE id = %s', (who,))
                        u = cur.fetchone()
                if not u or not u["active"] or not rbac.can_access_dept(u, ho["department"]):
                    raise bad_request("assigned_to_id: not an active user of this department")
            sets.append("assigned_to_id = %s")
            params.append(who)
        if not sets:
            raise bad_request("nothing to update")
        with conn.cursor() as cur:
            cur.execute(f"UPDATE handoff SET {', '.join(sets)}, updated_at = now() WHERE id = %s RETURNING *",
                        [*params, id])
            row = cur.fetchone()
        audit(conn, g.user["id"], "handoff.update", "handoff", id, {k: body[k] for k in body if k in
                                                                       ("status", "assigned_to_id", "rm_notes")})
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()
