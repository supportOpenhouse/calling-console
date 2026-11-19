"""Workflows (per department): builder data, list, create, update, detail, toggle, delete, run now."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, g, jsonify, request
from psycopg2.extras import Json

from ..db import get_conn
from ..services import rbac
from ..services.audit import audit
from ..services.datasets import dataset_facets
from ..services.errors import bad_request, conflict
from ..services.ist import parse_hhmm
from ..services.redial import DEFAULT_REDIAL_POLICY, validate_policy
from ..services.workflows import LIVE_CAMPAIGN_STATUSES, run_workflow
from .auth import require_auth
from .datasets import public_dataset

bp = Blueprint("workflows", __name__, url_prefix="/api")

_TRIGGERS = ("MANUAL", "SCHEDULE", "ON_NEW_LEAD", "AFTER_STEP")
_ACTIONS = ("redial", "whatsapp", "handoff", "next_step", "dnc", "stop")
_FIELDS = ("use_case_id", "name", "purpose", "agent_id", "dataset_id", "segment", "from_number", "variable_map",
           "trigger", "redial_policy", "rules", "parent_id", "step_order", "priority", "active")


def _positive(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _integer(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_iso(v, field: str) -> datetime:
    """An ISO-8601 timestamp with a UTC offset (a trailing Z is accepted)."""
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00")) if isinstance(v, str) else None
    except ValueError:
        t = None
    if t is None or t.tzinfo is None:
        raise bad_request(f"{field} must be an ISO-8601 timestamp with a timezone")
    return t


def _one(conn, sql: str, *params) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _check_trigger(trigger, has_dataset: bool, has_parent: bool) -> dict:
    if not isinstance(trigger, dict) or trigger.get("type") not in _TRIGGERS:
        raise bad_request("trigger.type must be one of " + ", ".join(_TRIGGERS))
    kind = trigger["type"]
    if kind == "SCHEDULE":
        if trigger.get("time") is not None:
            try:
                parse_hhmm(trigger["time"])
            except ValueError as e:
                raise bad_request(f"trigger.time: {e}")
        elif not _positive(trigger.get("every_minutes")):
            raise bad_request("a SCHEDULE trigger needs a time or every_minutes above 0")
    elif kind == "AFTER_STEP":
        if not has_parent:
            raise bad_request("an AFTER_STEP workflow needs a parent_id")
        if not _positive(trigger.get("delay_hours")):
            raise bad_request("trigger.delay_hours must be above 0")
        if not (trigger.get("when_tags") or trigger.get("when_outcomes")):  # else every parent call matches
            raise bad_request("an AFTER_STEP trigger needs when_tags or when_outcomes")
    elif kind == "ON_NEW_LEAD" and not has_dataset:
        raise bad_request("an ON_NEW_LEAD workflow needs a dataset")
    return trigger


def _check_rules(rules) -> list:
    if not isinstance(rules, list):
        raise bad_request("rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("when"), dict) or not isinstance(rule.get("actions"), list):
            raise bad_request("each rule needs when and actions")
        for a in rule["actions"]:
            if not isinstance(a, dict) or a.get("type") not in _ACTIONS:
                raise bad_request("action type must be one of " + ", ".join(_ACTIONS))
            if a["type"] == "redial" and not _positive(a.get("after_minutes")):
                raise bad_request("a redial action needs after_minutes above 0")
            if a["type"] == "whatsapp" and not (isinstance(a.get("template"), str) and a["template"].strip()):
                raise bad_request("a whatsapp action needs a template")
    return rules


def _check_one_new_lead(conn, v: dict, self_id: str | None) -> None:
    if v["active"] and v["trigger"]["type"] == "ON_NEW_LEAD" and _one(
            conn, "SELECT 1 FROM workflow WHERE active AND trigger->>'type' = 'ON_NEW_LEAD' AND dataset_id = %s "
                  "AND id IS DISTINCT FROM %s", v["dataset_id"], self_id):
        raise bad_request("another active ON_NEW_LEAD workflow already uses this dataset")


def _validated(conn, dept: str, body: dict, current: dict | None) -> dict:
    """Merge `body` over `current` (None on create) and validate every field against `dept`."""
    if current is None:
        for f in ("use_case_id", "name", "agent_id", "from_number", "trigger", "redial_policy"):
            if f not in body:
                raise bad_request(f"{f} is required")
    out = dict(current) if current else {
        "purpose": None, "dataset_id": None, "segment": {}, "variable_map": {}, "rules": [],
        "parent_id": None, "step_order": 0, "priority": 5, "active": True}
    out.update({f: body[f] for f in _FIELDS if f in body})
    if not isinstance(out["name"], str) or not out["name"].strip():
        raise bad_request("name is required")
    out["name"] = out["name"].strip()
    if not isinstance(out["from_number"], str) or not out["from_number"].strip():
        raise bad_request("from_number is required")
    if out["purpose"] is not None and not isinstance(out["purpose"], str):
        raise bad_request("purpose must be text")
    for f in ("segment", "variable_map"):
        if not isinstance(out[f], dict):
            raise bad_request(f"{f} must be an object")
    for f in ("step_order", "priority"):
        if not _integer(out[f]):
            raise bad_request(f"{f} must be an integer")
    if not isinstance(out["active"], bool):
        raise bad_request("active must be true or false")

    uc = _one(conn, "SELECT department FROM use_case WHERE id = %s", out["use_case_id"])
    if not uc or uc["department"] != dept:
        raise bad_request("use_case_id: not a use case of this department")
    agent = _one(conn, "SELECT vertical FROM agent WHERE id = %s", out["agent_id"])
    if not agent or agent["vertical"] not in (dept, "BOTH"):
        raise bad_request("agent_id: not an agent of this department")
    if out["dataset_id"] is not None:
        ds = _one(conn, "SELECT department FROM dataset WHERE id = %s", out["dataset_id"])
        if not ds or ds["department"] != dept:
            raise bad_request("dataset_id: not a dataset of this department")
    if out["parent_id"] is not None:
        parent = _one(conn, "SELECT department FROM workflow WHERE id = %s", out["parent_id"])
        if not parent or parent["department"] != dept or (current and out["parent_id"] == current["id"]):
            raise bad_request("parent_id: not another workflow of this department")

    out["trigger"] = _check_trigger(out["trigger"], out["dataset_id"] is not None, out["parent_id"] is not None)
    if out["dataset_id"] is None and out["trigger"]["type"] != "AFTER_STEP":
        raise bad_request("dataset_id is required (only an AFTER_STEP workflow may have none)")
    out["rules"] = _check_rules(out["rules"])
    try:
        out["redial_policy"] = validate_policy(out["redial_policy"])
    except ValueError as e:
        raise bad_request(str(e))
    _check_one_new_lead(conn, out, current["id"] if current else None)
    return out


@bp.get("/departments/<slug>/builder-data")
@require_auth()
def builder_data(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "manage", dept)
    restriction = rbac.use_case_restriction(g.user)
    scope, params = ("", [dept]) if restriction is None else (" AND use_case_id = ANY(%s)", [dept, restriction])
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM use_case WHERE department = %s AND active"
                        + (" AND id = ANY(%s)" if restriction is not None else "") + " ORDER BY priority, name", params)
            use_cases = cur.fetchall()
            cur.execute("SELECT * FROM agent WHERE vertical IN (%s::vertical, 'BOTH') ORDER BY name", (dept,))
            agents = cur.fetchall()
            cur.execute("SELECT * FROM dataset WHERE department = %s" + scope + " ORDER BY name", params)
            datasets = cur.fetchall()
            cur.execute("SELECT * FROM phone_number WHERE active ORDER BY number")
            numbers = cur.fetchall()
            cur.execute("SELECT * FROM workflow WHERE department = %s AND parent_id IS NULL" + scope
                        + " ORDER BY name", params)
            parents = cur.fetchall()
        facets = {d["id"]: dataset_facets(conn, d["id"]) for d in datasets}
        return jsonify({"use_cases": use_cases, "agents": agents, "datasets": [public_dataset(d) for d in datasets],
                        "facets": facets,
                        "numbers": numbers, "parents": parents, "default_policy": DEFAULT_REDIAL_POLICY})
    finally:
        conn.close()


@bp.get("/departments/<slug>/workflows")
@require_auth()
def list_workflows(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    restriction = rbac.use_case_restriction(g.user)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT w.*, u.name AS use_case_name, a.name AS agent_name, d.name AS dataset_name, "
                "(SELECT count(*) FROM campaign c WHERE c.workflow_id = w.id)::int AS runs, "
                "(SELECT count(*) FROM campaign c WHERE c.workflow_id = w.id AND c.status::text = ANY(%s))::int AS live "
                "FROM workflow w JOIN use_case u ON u.id = w.use_case_id JOIN agent a ON a.id = w.agent_id "
                "LEFT JOIN dataset d ON d.id = w.dataset_id WHERE w.department = %s"
                + (" AND w.use_case_id = ANY(%s)" if restriction is not None else "")
                + " ORDER BY w.step_order, w.created_at",
                [LIVE_CAMPAIGN_STATUSES, dept] + ([restriction] if restriction is not None else []))
            return jsonify({"items": cur.fetchall()})
    finally:
        conn.close()


def _write(conn, v: dict, id: str | None, dept: str) -> dict:
    cols = {**{f: v[f] for f in _FIELDS}, "segment": Json(v["segment"]), "variable_map": Json(v["variable_map"]),
            "trigger": Json(v["trigger"]), "redial_policy": Json(v["redial_policy"]), "rules": Json(v["rules"])}
    with conn.cursor() as cur:
        if id is None:
            cols.update(department=dept, created_by_id=g.user["id"])
            cur.execute(f"INSERT INTO workflow ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                        list(cols.values()))
        else:
            cur.execute(f"UPDATE workflow SET {', '.join(f'{c} = %s' for c in cols)}, updated_at = now() "
                        "WHERE id = %s RETURNING *", [*cols.values(), id])
        return cur.fetchone()


@bp.post("/departments/<slug>/workflows")
@require_auth()
def create_workflow(slug):
    dept = rbac.dept_from_slug(slug)
    body = request.get_json(silent=True) or {}
    rbac.require(g.user, "manage", dept, body.get("use_case_id"))
    conn = get_conn()
    try:
        v = _validated(conn, dept, body, None)
        row = _write(conn, v, None, dept)
        audit(conn, g.user["id"], "workflow.create", "workflow", row["id"], {"name": row["name"]})
        conn.commit()
        return jsonify(row), 201
    finally:
        conn.close()


@bp.put("/workflows/<id>")
@require_auth()
def update_workflow(id):
    conn = get_conn()
    try:
        cur_row = rbac.load_scoped(conn, "workflow", id, g.user, "manage")
        body = request.get_json(silent=True) or {}
        v = _validated(conn, cur_row["department"], body, cur_row)
        rbac.require(g.user, "manage", cur_row["department"], v["use_case_id"])
        row = _write(conn, v, id, cur_row["department"])
        audit(conn, g.user["id"], "workflow.update", "workflow", id, {"name": row["name"]})
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()


@bp.get("/workflows/<id>")
@require_auth()
def get_workflow(id):
    conn = get_conn()
    try:
        wf = rbac.load_scoped(conn, "workflow", id, g.user, "view")
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM use_case WHERE id = %s", (wf["use_case_id"],))
            use_case = cur.fetchone()
            cur.execute("SELECT * FROM agent WHERE id = %s", (wf["agent_id"],))
            agent = cur.fetchone()
            cur.execute("SELECT * FROM dataset WHERE id = %s", (wf["dataset_id"],))
            dataset = cur.fetchone()
            cur.execute("SELECT * FROM workflow WHERE parent_id = %s ORDER BY step_order, created_at", (id,))
            children = cur.fetchall()
            cur.execute(
                "SELECT c.*, count(l.*) FILTER (WHERE l.attempts > 0)::int AS dialed, "
                "count(l.*) FILTER (WHERE l.status = 'CONNECTED')::int AS connected, count(l.*)::int AS leads "
                "FROM campaign c LEFT JOIN lead l ON l.campaign_id = c.id WHERE c.workflow_id = %s "
                "GROUP BY c.id ORDER BY c.created_at DESC", (id,))
            runs = cur.fetchall()
        stats = {"runs": len(runs), "leads": sum(r["leads"] for r in runs), "dialed": sum(r["dialed"] for r in runs),
                 "connected": sum(r["connected"] for r in runs)}
        return jsonify({"workflow": wf, "use_case": use_case, "agent": agent,
                        "dataset": public_dataset(dataset) if dataset else None,
                        "children": children, "runs": runs, "stats": stats})
    finally:
        conn.close()


@bp.post("/workflows/<id>/toggle")
@require_auth()
def toggle_workflow(id):
    conn = get_conn()
    try:
        wf = rbac.load_scoped(conn, "workflow", id, g.user, "manage")
        active = (request.get_json(silent=True) or {}).get("active")
        if not isinstance(active, bool):
            raise bad_request("active must be true or false")
        _check_one_new_lead(conn, {**wf, "active": active}, id)
        with conn.cursor() as cur:
            cur.execute("UPDATE workflow SET active = %s, updated_at = now() WHERE id = %s RETURNING *", (active, id))
            row = cur.fetchone()
        audit(conn, g.user["id"], "workflow.toggle", "workflow", id, {"active": active})
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()


@bp.delete("/workflows/<id>")
@require_auth()
def delete_workflow(id):
    conn = get_conn()
    try:
        wf = rbac.load_scoped(conn, "workflow", id, g.user, "admin")
        # Finished campaigns keep the workflow id as history; a live one still needs its rules and steps.
        if _one(conn, "SELECT 1 FROM campaign WHERE workflow_id = %s AND status::text = ANY(%s) LIMIT 1",
                id, LIVE_CAMPAIGN_STATUSES):
            raise conflict("the workflow has live campaigns")
        if _one(conn, "SELECT 1 FROM workflow WHERE parent_id = %s LIMIT 1", id):
            raise conflict("the workflow has follow-up steps")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM workflow WHERE id = %s", (id,))
        audit(conn, g.user["id"], "workflow.delete", "workflow", id, {"name": wf["name"]})
        conn.commit()
        return "", 204
    finally:
        conn.close()


@bp.post("/workflows/<id>/run")
@require_auth()
def run_now(id):
    body = request.get_json(silent=True) or {}
    start = body.get("requested_start")
    start = parse_iso(start, "requested_start") if start is not None else None
    ignore_cooling = body.get("ignore_cooling", False)
    if not isinstance(ignore_cooling, bool):
        raise bad_request("ignore_cooling must be true or false")
    conn = get_conn()
    try:
        wf = rbac.load_scoped(conn, "workflow", id, g.user, "manage")
        try:
            res = run_workflow(conn, wf, requested_start=start, user_id=g.user["id"], ignore_cooling=ignore_cooling)
        except ValueError as e:
            conn.rollback()
            raise bad_request(str(e))
        if res is not None:
            audit(conn, g.user["id"], "workflow.run", "workflow", id, {"campaign_id": res["campaign_id"]})
        conn.commit()
        return jsonify(res if res is not None else {"campaign_id": None, "leads": 0})
    finally:
        conn.close()
