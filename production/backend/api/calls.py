"""Calls: department list, detail, re-tag, DNC, recording link and the CSV export."""
from __future__ import annotations

import csv
import io
import re
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, g, jsonify, request

from ..db import get_conn
from ..services import rbac, storage
from ..services.audit import audit
from ..services.classify import flatten_extracted
from ..services.datasets import mark_dnc, queued_in
from ..services.errors import bad_request, not_found
from ..services.ist import IST, fmt_ist
from .auth import require_auth

bp = Blueprint("calls", __name__, url_prefix="/api")

PAGE_SIZE = 50
EXPORT_LIMIT = 20000
_OUTCOMES = ("CONNECTED", "NOT_PICKED", "CARRIER_BLOCKED", "VOICEMAIL", "FAILED", "BALANCE_LOW", "UNKNOWN")
_BASE_COLS = ["called_at", "source", "campaign", "agent", "phone", "name", "status", "outcome", "hangup_code",
              "duration_sec", "human_turns", "tag"]
_PLAIN_NUMBER = re.compile(r"^[+-]?[0-9 ().-]+$")  # phones and signed numbers stay as they are
# A department's calls: those of its campaigns ("console"), and calls made outside the console -- no campaign, e.g.
# Bolna history -- by its agents ("previous"; an agent of BOTH departments shows in both).
_FROM = ("FROM call c LEFT JOIN campaign k ON k.id = c.campaign_id LEFT JOIN agent a ON a.id = c.agent_id "
         "LEFT JOIN lead l ON l.id = c.lead_id")
_SOURCE = "CASE WHEN c.campaign_id IS NULL THEN 'previous' ELSE 'console' END AS source"


def _ist_day(name: str) -> datetime:
    try:
        d = date.fromisoformat(request.args[name])
    except ValueError:
        raise bad_request(f"{name} must be a date (YYYY-MM-DD)")
    return datetime(d.year, d.month, d.day, tzinfo=IST)


def _filters(dept: str) -> tuple[str, list]:
    """WHERE clause (without the keyword) for the department's calls and the request filters."""
    a = request.args
    where = ["(k.vertical = %s OR (c.campaign_id IS NULL AND a.vertical IN (%s, 'BOTH')))"]
    params = [dept, dept]
    restriction = rbac.use_case_restriction(g.user)
    if restriction is not None:  # a call without a use case stays visible (rbac.can_see_use_case)
        where.append("(c.campaign_id IS NULL OR k.use_case_id = ANY(%s))")
        params.append(restriction)
    for arg, col in (("agent_id", "c.agent_id"), ("campaign_id", "c.campaign_id"), ("tag", "c.tag")):
        if a.get(arg):
            where.append(f"{col} = %s")
            params.append(a[arg])
    if a.get("outcome"):
        if a["outcome"] not in _OUTCOMES:
            raise bad_request("outcome must be one of " + ", ".join(_OUTCOMES))
        where.append("c.outcome = %s")
        params.append(a["outcome"])
    if a.get("from"):
        where.append("c.started_at >= %s")
        params.append(_ist_day("from"))
    if a.get("to"):
        where.append("c.started_at < %s")
        params.append(_ist_day("to") + timedelta(days=1))
    q = (a.get("q") or "").strip()
    if q:
        where.append("(c.phone ILIKE %s OR l.name ILIKE %s OR c.transcript ILIKE %s)")
        params += [f"%{q}%"] * 3
    return " AND ".join(where), params


@bp.get("/departments/<slug>/calls")
@require_auth()
def list_calls(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        raise bad_request("page must be a number")
    where, params = _filters(dept)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n {_FROM} WHERE {where}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT c.id, c.phone, c.status, c.outcome, c.hangup_code, c.duration_sec, c.human_turns, c.tag, "
                "c.summary, c.started_at, c.agent_id, c.campaign_id, c.lead_id, c.recording_url IS NOT NULL "
                f"OR c.recording_key IS NOT NULL AS has_recording, {_SOURCE}, k.name AS campaign_name, a.name AS agent_name, "
                f"l.name AS lead_name {_FROM} WHERE {where} "
                "ORDER BY c.started_at DESC NULLS LAST, c.id LIMIT %s OFFSET %s",
                [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE])
            return jsonify({"items": cur.fetchall(), "total": total})
    finally:
        conn.close()


def _cell(v) -> str:
    """CSV cell text, neutralising spreadsheet formulas (staff open these in Excel)."""
    s = "" if v is None else str(v)
    if not isinstance(v, str) or not s:
        return s
    risky = s[0] in ("=", "@", "\t", "\r") or (s[0] in "+-" and not _PLAIN_NUMBER.match(s))
    return "'" + s if risky else s


@bp.get("/departments/<slug>/calls/export.csv")
@require_auth()
def export_calls(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    where, params = _filters(dept)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT c.*, {_SOURCE}, k.name AS campaign_name, a.name AS agent_name, l.name AS lead_name "
                f"{_FROM} WHERE {where} ORDER BY c.started_at DESC NULLS LAST, c.id LIMIT %s", [*params, EXPORT_LIMIT])
            rows = cur.fetchall()
    finally:
        conn.close()
    flats = [flatten_extracted(r["extracted"]) for r in rows]
    extra = list(dict.fromkeys(k for f in flats for k in f))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow([*_BASE_COLS, *extra, "recording_url", "transcript"])
    for r, f in zip(rows, flats):
        w.writerow([_cell(v) for v in (
            fmt_ist(r["started_at"], seconds=True), r["source"], r["campaign_name"], r["agent_name"], r["phone"], r["lead_name"],
            r["status"], r["outcome"], r["hangup_code"], r["duration_sec"], r["human_turns"], r["tag"],
            *(f.get(k, "") for k in extra), r["recording_url"], r["transcript"])])
    return Response(out.getvalue().encode("utf-8"), headers={
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="calls_{int(time.time())}.csv"'})


@bp.get("/calls/<id>")
@require_auth()
def get_call(id):
    conn = get_conn()
    try:
        call = rbac.load_scoped(conn, "call", id, g.user, "view")
        call.pop("raw", None)
        with conn.cursor() as cur:
            def one(table, key):
                if not key:
                    return None
                cur.execute(f'SELECT * FROM "{table}" WHERE id = %s', (key,))
                return cur.fetchone()
            campaign, agent, lead = one("campaign", call["campaign_id"]), one("agent", call["agent_id"]), one("lead", call["lead_id"])
            history = []
            if lead:
                cur.execute("SELECT id, started_at, status, outcome, duration_sec, tag, summary FROM call "
                            "WHERE lead_id = %s ORDER BY started_at DESC NULLS LAST, id", (lead["id"],))
                history = cur.fetchall()
        return jsonify({"call": call, "campaign": campaign, "agent": agent, "lead": lead,
                        "fields": flatten_extracted(call["extracted"]), "history": history})
    finally:
        conn.close()


@bp.post("/calls/<id>/tag")
@require_auth()
def retag(id):
    tag = (request.get_json(silent=True) or {}).get("tag")
    if not isinstance(tag, str) or not tag.strip():
        raise bad_request("tag is required")
    tag = tag.strip()
    conn = get_conn()
    try:
        call = rbac.load_scoped(conn, "call", id, g.user, "manage")
        with conn.cursor() as cur:
            cur.execute("UPDATE call SET tag = %s, updated_at = now() WHERE id = %s RETURNING id, tag", (tag, id))
            row = cur.fetchone()
            if call["lead_id"]:
                cur.execute("UPDATE lead SET final_tag = %s WHERE id = %s", (tag, call["lead_id"]))
        audit(conn, g.user["id"], "call.tag", "call", id, {"from": call["tag"], "to": tag})
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()


@bp.post("/calls/<id>/dnc")
@require_auth()
def call_dnc(id):
    """Block the call's phone everywhere. Answers {queued_in}: live batches that still hold it."""
    conn = get_conn()
    try:
        call = rbac.load_scoped(conn, "call", id, g.user, "manage")
        held = queued_in(conn, call["phone"])  # before the call's own lead is marked: its batch may still hold it
        mark_dnc(conn, call["phone"], "manual", "marked from call", g.user["id"])
        with conn.cursor() as cur:
            if call["lead_id"]:
                cur.execute("UPDATE lead SET status = 'DNC' WHERE id = %s", (call["lead_id"],))
        audit(conn, g.user["id"], "call.dnc", "call", id, {"phone": call["phone"]})
        conn.commit()
        return jsonify({"queued_in": held})
    finally:
        conn.close()


@bp.get("/calls/<id>/recording")
@require_auth()
def recording(id):
    conn = get_conn()
    try:
        call = rbac.load_scoped(conn, "call", id, g.user, "view")
    finally:
        conn.close()
    if call["recording_key"] and storage.storage_enabled():
        return jsonify({"url": storage.recording_url(call["recording_key"])})
    if call["recording_url"]:
        return jsonify({"url": call["recording_url"]})
    raise not_found("no recording for this call")
