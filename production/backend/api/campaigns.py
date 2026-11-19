"""Campaigns: the upload wizard (preview, create) and the controls (plan, start now, pause, resume, stop, redial,
policy, delete). Every dial goes through services.redial.create_round. Start now, resume and redial dial now inside
the calling window and otherwise at its next start (Bolna holds the batch until then): start now and resume use the
department's window, redial the campaign's redial-policy window."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from zipfile import BadZipFile

from flask import Blueprint, g, jsonify, request
from psycopg2.extras import Json, execute_values

from ..db import get_conn
from ..services import bolna, rbac
from ..services.audit import audit
from ..services.errors import bad_request, conflict
from ..services.lead_import import build_leads, guess_column_map, parse_lead_file, spoken_bhk
from ..services.ist import in_window, next_window_start
from ..services.redial import ACTIVE_BATCH_STATUSES, create_round, eligible_redial_leads, policy_of, validate_policy
from ..services.scheduler import dept_config, plan_run, replan_department
from ..services.sync import CONSOLE_STOP, _unconfirmed_idle_batches, console_stop_stamp
from .auth import require_auth
from .workflows import parse_iso

bp = Blueprint("campaigns", __name__, url_prefix="/api")

PAGE_SIZE = 50
_START_MODES = ("draft", "now", "schedule")
_LEAD_STATUSES = ("PENDING", "QUEUED", "CONNECTED", "NOT_CONNECTED", "BLOCKED", "VOICEMAIL", "FAILED", "DONE", "DNC")
_CAMPAIGN_STATUSES = ("DRAFT", "SCHEDULED", "RUNNING", "PAUSED", "COMPLETED", "STOPPED")


def _one(conn, sql: str, *params) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(conn, sql: str, *params) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _parse_file(file) -> dict:
    if file is None:
        raise bad_request("file is required")
    try:
        return parse_lead_file(file.read(), file.filename or "")
    except (ValueError, BadZipFile, csv.Error, KeyError) as e:  # KeyError: a zip that is not an xlsx
        raise bad_request(str(e) if isinstance(e, ValueError) else "could not read the file")


def _agent_of(conn, dept: str, agent_id) -> dict:
    agent = _one(conn, "SELECT * FROM agent WHERE id = %s", agent_id) if isinstance(agent_id, str) else None
    if not agent or agent["vertical"] not in (dept, "BOTH"):
        raise bad_request("agent_id: not an agent of this department")
    return agent


# ---------------------------------------------------------------- list and wizard

@bp.get("/departments/<slug>/campaigns")
@require_auth()
def list_campaigns(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    restriction = rbac.use_case_restriction(g.user)
    status, use_case_id = request.args.get("status"), request.args.get("use_case_id")
    if status is not None and status not in _CAMPAIGN_STATUSES:
        raise bad_request("status must be one of " + ", ".join(_CAMPAIGN_STATUSES))
    where, params = ["c.vertical = %s"], [dept]
    if status:
        where.append("c.status = %s")
        params.append(status)
    if use_case_id:
        where.append("c.use_case_id = %s")
        params.append(use_case_id)
    if restriction is not None:
        where.append("c.use_case_id = ANY(%s)")
        params.append(restriction)
    conn = get_conn()
    try:
        rows = _all(
            conn,
            "SELECT c.*, u.name AS use_case_name, a.name AS agent_name, "
            "count(l.id) AS leads, count(l.id) FILTER (WHERE l.attempts > 0) AS dialed, "
            "count(l.id) FILTER (WHERE l.status = 'CONNECTED') AS connected, "
            "count(l.id) FILTER (WHERE u.id IS NOT NULL AND lower(l.final_tag) IN "
            "(SELECT lower(t) FROM jsonb_array_elements_text(u.tag_taxonomy->'good') t)) AS good, "
            "count(l.id) FILTER (WHERE l.status = 'PENDING') AS pending "
            "FROM campaign c JOIN agent a ON a.id = c.agent_id LEFT JOIN use_case u ON u.id = c.use_case_id "
            "LEFT JOIN lead l ON l.campaign_id = c.id WHERE " + " AND ".join(where)
            + " GROUP BY c.id, u.id, a.id ORDER BY c.created_at DESC, c.id", *params)
        return jsonify({"items": rows})
    finally:
        conn.close()


@bp.post("/departments/<slug>/campaigns/preview")
@require_auth()
def preview(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "manage", dept)
    parsed = _parse_file(request.files.get("file"))
    conn = get_conn()
    try:
        agent = _agent_of(conn, dept, request.form.get("agent_id"))
    finally:
        conn.close()
    variables = agent["variables"]
    return jsonify({"columns": parsed["columns"], "sample": parsed["rows"][:5], "total": parsed["total"],
                    "column_map": guess_column_map(parsed["columns"], variables), "variables": variables})


def _payload() -> dict:
    try:
        p = json.loads(request.form.get("payload") or "")
    except ValueError:
        raise bad_request("payload must be JSON")
    if not isinstance(p, dict):
        raise bad_request("payload must be an object")
    return p


def _text(p: dict, key: str, min_len: int) -> str:
    v = p.get(key)
    if not isinstance(v, str) or len(v.strip()) < min_len:
        raise bad_request(f"{key} must be at least {min_len} characters")
    return v.strip()


def _optional_text(p: dict, key: str) -> str | None:
    v = p.get(key)
    if v is not None and not isinstance(v, str):
        raise bad_request(f"{key} must be text")
    return v


@bp.post("/departments/<slug>/campaigns")
@require_auth()
def create_campaign(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "manage", dept)
    p = _payload()
    name, from_number = _text(p, "name", 2), _text(p, "from_number", 8)
    notes, purpose, use_case_id = _optional_text(p, "notes"), _optional_text(p, "purpose"), p.get("use_case_id")
    column_map = p.get("column_map")
    if not isinstance(column_map, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                   for k, v in column_map.items()):
        raise bad_request("column_map must map each field to a column name")
    if "redial_policy" not in p:
        raise bad_request("redial_policy is required")
    try:
        policy = validate_policy(p["redial_policy"])
    except ValueError as e:
        raise bad_request(str(e))
    mode = p.get("start_mode")
    if mode not in _START_MODES:
        raise bad_request("start_mode must be one of " + ", ".join(_START_MODES))
    when = None
    if mode == "schedule":
        if p.get("scheduled_at") is None:
            raise bad_request("scheduled_at is required to schedule a campaign")
        when = parse_iso(p["scheduled_at"], "scheduled_at")
    speak_bhk = p.get("speak_bhk", False)
    if not isinstance(speak_bhk, bool):
        raise bad_request("speak_bhk must be true or false")
    parsed = _parse_file(request.files.get("file"))

    conn = get_conn()
    try:
        agent = _agent_of(conn, dept, p.get("agent_id"))
        if use_case_id is not None:
            uc = _one(conn, "SELECT department FROM use_case WHERE id = %s", use_case_id) \
                if isinstance(use_case_id, str) else None
            if not uc or uc["department"] != dept:
                raise bad_request("use_case_id: not a use case of this department")
            rbac.require(g.user, "manage", dept, use_case_id)
        built = build_leads(parsed["rows"], column_map, agent["variables"])
        if not built["leads"]:
            raise bad_request(f"no valid phone numbers found (invalid: {built['invalid']})")
        if speak_bhk:
            for lead in built["leads"]:
                if lead["variables"].get("bhk"):
                    lead["variables"]["bhk"] = spoken_bhk(lead["variables"]["bhk"])
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO campaign (name, agent_id, vertical, from_number, notes, column_map, redial_policy, "
                "created_by_id, status, use_case_id, purpose) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'DRAFT',%s,%s) "
                "RETURNING *", (name, agent["id"], dept, from_number, notes, Json(column_map), Json(policy),
                                g.user["id"], use_case_id, purpose))
            camp = cur.fetchone()
            execute_values(cur, "INSERT INTO lead (campaign_id, phone, name, variables) VALUES %s",
                           [(camp["id"], l["phone"], l["name"], Json(l["variables"])) for l in built["leads"]])
        audit(conn, g.user["id"], "campaign.create", "campaign", camp["id"],
              {"leads": len(built["leads"]), "invalid": built["invalid"], "duplicates": built["duplicates"]})
        if mode != "draft":
            run = plan_run(conn, dept=dept, campaign_id=camp["id"], use_case_id=use_case_id, purpose=purpose,
                           priority=camp["priority"], requested_start=when, user_id=g.user["id"])
            audit(conn, g.user["id"], "campaign.plan", "campaign", camp["id"],
                  {"when": when.isoformat() if when else "asap", "run_id": run["id"]})
        conn.commit()
        return jsonify({"id": camp["id"], "created": len(built["leads"]), "invalid": built["invalid"],
                        "duplicates": built["duplicates"]}), 201
    finally:
        conn.close()


# ---------------------------------------------------------------- one campaign

@bp.get("/campaigns/<id>")
@require_auth()
def get_campaign(id):
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "view")
        by_status = {r["status"]: r["n"] for r in _all(
            conn, "SELECT status::text AS status, count(*) AS n FROM lead WHERE campaign_id = %s GROUP BY status", id)}
        uc = _one(conn, "SELECT * FROM use_case WHERE id = %s", camp["use_case_id"]) if camp["use_case_id"] else None
        good = _one(conn, "SELECT count(*) AS n FROM lead WHERE campaign_id = %s AND lower(final_tag) = ANY(%s)", id,
                    [t.lower() for t in uc["tag_taxonomy"]["good"]] if uc else [])["n"]
        return jsonify({
            "campaign": camp,
            "agent": _one(conn, "SELECT * FROM agent WHERE id = %s", camp["agent_id"]),
            "use_case": uc,
            "counters": {"by_status": by_status,
                         "dialed": _one(conn, "SELECT count(*) AS n FROM lead WHERE campaign_id = %s AND attempts > 0",
                                        id)["n"],
                         "connected": by_status.get("CONNECTED", 0), "good": good},
            "batches": _all(conn, "SELECT * FROM batch WHERE campaign_id = %s ORDER BY round, created_at, id", id),
            "run": _one(conn, "SELECT * FROM scheduled_run WHERE campaign_id = %s ORDER BY created_at DESC, id DESC "
                              "LIMIT 1", id)})
    finally:
        conn.close()


@bp.get("/campaigns/<id>/leads")
@require_auth()
def campaign_leads(id):
    status = request.args.get("status")
    if status is not None and status not in _LEAD_STATUSES:
        raise bad_request("status must be one of " + ", ".join(_LEAD_STATUSES))
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        raise bad_request("page must be a number")
    if page < 1:
        raise bad_request("page must be at least 1")
    conn = get_conn()
    try:
        rbac.load_scoped(conn, "campaign", id, g.user, "view")
        where, params = "campaign_id = %s", [id]
        if status:
            where += " AND status = %s"
            params.append(status)
        total = _one(conn, f"SELECT count(*) AS n FROM lead WHERE {where}", *params)["n"]
        items = _all(conn, f"SELECT * FROM lead WHERE {where} ORDER BY created_at, id LIMIT %s OFFSET %s",
                     *params, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        return jsonify({"items": items, "total": total})
    finally:
        conn.close()


# ---------------------------------------------------------------- controls

@bp.post("/campaigns/<id>/plan")
@require_auth()
def plan_campaign(id):
    body = request.get_json(silent=True) or {}
    when = body.get("scheduled_at")
    when = parse_iso(when, "scheduled_at") if when is not None else None
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "manage")
        if camp["status"] != "DRAFT":
            raise conflict("only a draft campaign can be planned")
        run = plan_run(conn, dept=camp["vertical"], campaign_id=id, use_case_id=camp["use_case_id"],
                       purpose=camp["purpose"], priority=camp["priority"], requested_start=when, user_id=g.user["id"])
        audit(conn, g.user["id"], "campaign.plan", "campaign", id,
              {"when": when.isoformat() if when else "asap", "run_id": run["id"]})
        conn.commit()
        return jsonify(run)
    finally:
        conn.close()


def _dial_at(window_start: str, window_end: str, now: datetime) -> datetime | None:
    """None (dial now) inside the window, else its next start."""
    return None if in_window(now, window_start, window_end) else next_window_start(now, window_start, window_end)


def _start_round(conn, camp: dict, now: datetime, note: str | None = None, on_fail=None) -> dict:
    """A FIRST round over the PENDING leads (in the department's window), audited whether or not it succeeds."""
    cfg = dept_config(conn, camp["vertical"])
    when = _dial_at(cfg["window_start"], cfg["window_end"], now)
    pending = _all(conn, "SELECT * FROM lead WHERE campaign_id = %s AND status = 'PENDING' ORDER BY created_at, id",
                   camp["id"])
    meta = {"when": when.isoformat() if when else "now", **({"note": note} if note else {})}
    try:
        res = create_round(conn, camp, pending, "FIRST", when, now=now)
    except Exception as e:
        conn.rollback()
        if on_fail:
            on_fail()
        audit(conn, g.user["id"], "campaign.start", "campaign", camp["id"], {**meta, "error": str(e)})
        conn.commit()
        raise
    audit(conn, g.user["id"], "campaign.start", "campaign", camp["id"],
          {**meta, "leads": res["leads"], "batches": res["batch_ids"]})
    conn.commit()
    return res


@bp.post("/campaigns/<id>/start-now")
@require_auth()
def start_now(id):
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "admin")
        return jsonify(_start_round(conn, camp, datetime.now(timezone.utc)))
    finally:
        conn.close()


def _active_batches(conn, campaign_id: str) -> list[dict]:
    return _all(conn, "SELECT * FROM batch WHERE campaign_id = %s AND status = ANY(%s) ORDER BY created_at, id",
                campaign_id, ACTIVE_BATCH_STATUSES)


def _halt(conn, camp: dict, now: datetime) -> dict:
    """Stop the campaign's Bolna batches that may still dial (active, or completed on the idle rule while Bolna may
    still be dialling), stamping CONSOLE_STOP; set PAUSED (paused_at = now). Does not commit.

    Only leads whose batch had not started (next_dial_at after now) go back to PENDING: a lead of a started batch may
    have a call in progress or a result not synced yet, so it stays QUEUED for the sync's close_stale_queued, which
    hands the ones Bolna never reached back without using an attempt. If any batch may still dial, every QUEUED lead
    stays QUEUED."""
    errors: list[str] = []
    stopped = 0
    with conn.cursor() as cur:
        idle = _unconfirmed_idle_batches(cur, camp["id"], now)
    for b in _active_batches(conn, camp["id"]) + idle:
        if not b["bolna_batch_id"]:
            errors.append(f"batch {b['id']} was being created and may still dial")
            continue
        try:
            bolna.stop_batch(b["bolna_batch_id"])
        except Exception as e:
            errors.append(f"batch {b['id']}: {e}")
            continue
        with conn.cursor() as cur:
            cur.execute("UPDATE batch SET status = 'stopped', stats = coalesce(stats, '{}'::jsonb) || %s WHERE id = %s",
                        (Json({CONSOLE_STOP: console_stop_stamp(datetime.now(timezone.utc))}), b["id"]))  # after the stop
        stopped += 1
    with conn.cursor() as cur:
        if errors:  # a batch may still dial its leads: they stay QUEUED so no later round takes them again
            errors.append("leads left QUEUED: a batch may still be dialling")
        else:
            cur.execute("UPDATE lead SET status = 'PENDING', next_dial_at = NULL "
                        "WHERE campaign_id = %s AND status = 'QUEUED' AND next_dial_at > %s", (camp["id"], now))
        cur.execute("UPDATE campaign SET status = 'PAUSED', paused_at = %s, updated_at = now() WHERE id = %s",
                    (now, camp["id"]))
    audit(conn, g.user["id"], "campaign.pause", "campaign", camp["id"], {"stopped": stopped, "errors": errors})
    return {"stopped": stopped, "errors": errors}


@bp.post("/campaigns/<id>/pause")
@require_auth()
def pause(id):
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "manage")
        if camp["status"] not in ("SCHEDULED", "RUNNING"):
            raise conflict("only a scheduled or running campaign can be paused")
        res = _halt(conn, camp, datetime.now(timezone.utc))
        conn.commit()
        return jsonify(res)
    finally:
        conn.close()


@bp.post("/campaigns/<id>/resume")
@require_auth()
def resume(id):
    """409 while a batch may still dial; else a FIRST round over the PENDING leads. QUEUED leads (of a batch the pause
    stopped) are never handed back here: the sync's close_stale_queued closes the ones Bolna never reached once it has
    fetched every call made before the stop, and auto-redial takes them."""
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "manage")
        if camp["status"] != "PAUSED":
            raise conflict("only a paused campaign can be resumed")
        if _active_batches(conn, id):
            raise conflict("a batch may still be dialling")
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:  # create_round refuses a PAUSED campaign
            cur.execute("UPDATE campaign SET status = 'RUNNING', paused_at = NULL, updated_at = now() WHERE id = %s "
                        "AND status = 'PAUSED'", (id,))
            if cur.rowcount == 0:
                raise conflict("the campaign is no longer paused")
        conn.commit()

        def back_to_paused():  # nothing dialled: the pause, and when it happened, still stand
            if not _active_batches(conn, id):
                with conn.cursor() as cur:
                    cur.execute("UPDATE campaign SET status = 'PAUSED', paused_at = %s WHERE id = %s "
                                "AND status = 'RUNNING'", (camp["paused_at"], id))

        return jsonify(_start_round(conn, {**camp, "status": "RUNNING"}, now, note="resume", on_fail=back_to_paused))
    finally:
        conn.close()


@bp.post("/campaigns/<id>/stop")
@require_auth()
def stop(id):
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "manage")
        if camp["status"] in ("COMPLETED", "STOPPED"):
            raise conflict("the campaign is already finished")
        res = _halt(conn, camp, datetime.now(timezone.utc))
        with conn.cursor() as cur:
            cur.execute("UPDATE campaign SET status = 'STOPPED', completed_at = now() WHERE id = %s", (id,))
            cur.execute("UPDATE scheduled_run SET status = 'CANCELLED' WHERE campaign_id = %s "
                        "AND status IN ('PLANNED', 'QUEUED')", (id,))
        replan_department(conn, camp["vertical"])
        audit(conn, g.user["id"], "campaign.stop", "campaign", id)
        conn.commit()
        return jsonify(res)
    finally:
        conn.close()


@bp.post("/campaigns/<id>/redial")
@require_auth()
def redial(id):
    ignore_gap = (request.get_json(silent=True) or {}).get("ignore_gap", False)
    if not isinstance(ignore_gap, bool):
        raise bad_request("ignore_gap must be true or false")
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "manage")
        if camp["status"] != "RUNNING":
            raise conflict("only a running campaign can be redialled")
        now = datetime.now(timezone.utc)
        eligible = eligible_redial_leads(conn, camp, now + timedelta(days=365) if ignore_gap else now)
        policy = policy_of(camp)
        res = create_round(conn, camp, eligible, "REDIAL", _dial_at(policy["window_start"], policy["window_end"], now),
                           now=now)
        audit(conn, g.user["id"], "campaign.redial", "campaign", id,
              {"leads": res["leads"], "batches": res["batch_ids"], "ignore_gap": ignore_gap})
        conn.commit()
        return jsonify(res)
    finally:
        conn.close()


@bp.put("/campaigns/<id>/policy")
@require_auth()
def set_policy(id):
    body = request.get_json(silent=True) or {}
    if "redial_policy" not in body:
        raise bad_request("redial_policy is required")
    try:
        policy = validate_policy(body["redial_policy"])
    except ValueError as e:
        raise bad_request(str(e))
    conn = get_conn()
    try:
        rbac.load_scoped(conn, "campaign", id, g.user, "manage")
        with conn.cursor() as cur:
            cur.execute("UPDATE campaign SET redial_policy = %s, updated_at = now() WHERE id = %s RETURNING *",
                        (Json(policy), id))
            row = cur.fetchone()
        audit(conn, g.user["id"], "campaign.policy", "campaign", id, policy)
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()


@bp.delete("/campaigns/<id>")
@require_auth()
def delete_campaign(id):
    conn = get_conn()
    try:
        camp = rbac.load_scoped(conn, "campaign", id, g.user, "owner")
        if camp["status"] not in ("DRAFT", "COMPLETED", "STOPPED"):
            raise conflict("stop the campaign first")
        if _active_batches(conn, id):  # e.g. a stop Bolna refused: its leads must stay tracked
            raise conflict("a batch may still be dialling")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM campaign WHERE id = %s", (id,))
        audit(conn, g.user["id"], "campaign.delete", "campaign", id, {"name": camp["name"]})
        conn.commit()
        return "", 204
    finally:
        conn.close()
