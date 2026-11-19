"""The department calendar: runs in a window, the queue, cancel, re-prioritise, replan."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..db import get_conn
from ..services import rbac, scheduler
from ..services.audit import audit
from ..services.errors import bad_request, not_found
from .auth import require_auth
from .workflows import parse_iso

bp = Blueprint("schedule", __name__, url_prefix="/api")


@bp.get("/departments/<slug>/schedule")
@require_auth()
def get_schedule(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    start, end = (request.args.get(k) for k in ("from", "to"))
    if not start or not end:
        raise bad_request("from and to are required")
    start, end = parse_iso(start, "from"), parse_iso(end, "to")
    restriction = rbac.use_case_restriction(g.user)
    conn = get_conn()
    try:
        runs = scheduler.runs_between(conn, start, end)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.*, c.name AS campaign_name, c.status AS campaign_status FROM scheduled_run r "
                "LEFT JOIN campaign c ON c.id = r.campaign_id WHERE r.department = %s AND r.status = 'PLANNED' "
                + ("AND r.use_case_id = ANY(%s) " if restriction is not None else "")
                + "ORDER BY r.planned_start, r.priority, r.created_at",
                [dept] + ([restriction] if restriction is not None else []))
            queue = cur.fetchall()
        if restriction is not None:  # a use-case-restricted user sees the other use cases' runs as bare slots
            runs = [r if r["use_case_id"] in restriction else {k: r[k] for k in (
                "id", "department", "status", "planned_start", "planned_end", "priority") } for r in runs]
        return jsonify({"runs": runs, "queue": queue, "window": {"start": start, "end": end}})
    finally:
        conn.close()


@bp.post("/runs/<id>/cancel")
@require_auth()
def cancel(id):
    conn = get_conn()
    try:
        rbac.load_scoped(conn, "run", id, g.user, "manage")
        scheduler.cancel_run(conn, id)
        audit(conn, g.user["id"], "run.cancel", "scheduled_run", id)
        conn.commit()
        return "", 204
    finally:
        conn.close()


@bp.post("/runs/<id>/priority")
@require_auth()
def set_priority(id):
    body = request.get_json(silent=True) or {}
    priority = body.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 9:
        raise bad_request("priority must be a whole number from 1 to 9")
    start = body.get("requested_start")
    start = parse_iso(start, "requested_start") if start is not None else None
    conn = get_conn()
    try:
        rbac.load_scoped(conn, "run", id, g.user, "admin")
        try:
            plan = scheduler.set_run_priority(conn, id, priority, start)
        except LookupError:
            conn.rollback()
            raise not_found()
        audit(conn, g.user["id"], "run.priority", "scheduled_run", id, {"priority": priority})
        conn.commit()
        return jsonify({"items": plan})
    finally:
        conn.close()


@bp.post("/departments/<slug>/replan")
@require_auth()
def replan(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "manage", dept)
    conn = get_conn()
    try:
        plan = scheduler.replan_department(conn, dept)
        audit(conn, g.user["id"], "department.replan", "department", dept)
        conn.commit()
        return jsonify({"items": plan})
    finally:
        conn.close()
