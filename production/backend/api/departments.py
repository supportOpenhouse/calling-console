"""Department settings and the per-department dashboard."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..db import get_conn
from ..services import rbac
from ..services.audit import audit
from ..services.errors import bad_request, not_found
from ..services.ist import validate_window
from ..services.scheduler import dept_config, replan_department
from ..services.settings_store import get_setting
from .auth import require_auth

bp = Blueprint("departments", __name__, url_prefix="/api/departments")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@bp.get("/<slug>")
@require_auth()
def get_department(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    conn = get_conn()
    try:
        return jsonify(dept_config(conn, dept))
    finally:
        conn.close()


@bp.put("/<slug>")
@require_auth()
def update_department(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "admin", dept)
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise bad_request("name is required")
    line_share, cooling_days = body.get("line_share"), body.get("cooling_days")
    if not _is_int(line_share) or not 1 <= line_share <= 10:
        raise bad_request("line_share must be an integer from 1 to 10")
    if not _is_int(cooling_days) or not 0 <= cooling_days <= 90:
        raise bad_request("cooling_days must be an integer from 0 to 90")
    try:
        validate_window(body.get("window_start"), body.get("window_end"))
    except ValueError as e:
        raise bad_request(str(e))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE department SET name=%s, window_start=%s, window_end=%s, line_share=%s, cooling_days=%s, "
                "updated_at=now() WHERE key=%s",
                (name.strip(), body["window_start"], body["window_end"], line_share, cooling_days, dept))
        audit(conn, g.user["id"], "department.update", "department", dept,
              {k: body[k] for k in ("window_start", "window_end", "line_share", "cooling_days")})
        replan_department(conn, dept)
        row = dept_config(conn, dept)
        conn.commit()
        return jsonify(row)
    finally:
        conn.close()


@bp.get("/<slug>/dashboard")
@require_auth()
def dashboard(slug):
    dept = rbac.dept_from_slug(slug)
    u = g.user
    rbac.require(u, "view", dept)
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        raise bad_request("days must be 1, 7 or 30")
    if days not in (1, 7, 30):
        raise bad_request("days must be 1, 7 or 30")
    uc_id = request.args.get("use_case_id") or None
    restriction = rbac.use_case_restriction(u)
    conn = get_conn()
    try:
        if uc_id:
            uc = rbac.load_scoped(conn, "use_case", uc_id, u, "view")
            if uc["department"] != dept:
                raise not_found()
        # SQL fragment + params narrowing calls to the chosen / permitted use cases. A call without a campaign (made
        # outside the console, "previous") has no use case: out when one is chosen, visible under a restriction
        # (rbac.can_see_use_case).
        if uc_id:
            ucf, ucp = " AND ca.use_case_id = %s", [uc_id]
        elif restriction is not None:
            ucf, ucp = " AND (c.campaign_id IS NULL OR ca.use_case_id = ANY(%s))", [restriction]
        else:
            ucf, ucp = "", []
        since = "now() - make_interval(days => %s)"
        # The department's calls in range: its campaigns' calls, and previous calls by its agents (calls.py _FROM).
        calls_from = ("FROM call c LEFT JOIN campaign ca ON ca.id = c.campaign_id LEFT JOIN agent ag ON ag.id = c.agent_id "
                      f"WHERE c.started_at >= {since} AND (ca.vertical = %s OR (c.campaign_id IS NULL "
                      "AND ag.vertical IN (%s, 'BOTH')))" + ucf)
        calls_p = [days, dept, dept] + ucp
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM use_case WHERE department = %s" + (" AND id = ANY(%s)" if restriction is not None else "")
                + " ORDER BY priority, name", [dept] + ([restriction] if restriction is not None else []))
            ucs = [r for r in cur.fetchall() if not uc_id or r["id"] == uc_id]

            cur.execute(
                "SELECT count(*) AS dialed, count(*) FILTER (WHERE c.outcome='CONNECTED') AS connected, "
                "count(*) FILTER (WHERE c.outcome='CARRIER_BLOCKED') AS blocked, "
                "count(*) FILTER (WHERE c.outcome='NOT_PICKED') AS not_picked, "
                "count(*) FILTER (WHERE c.outcome='VOICEMAIL') AS voicemail, "
                "count(*) FILTER (WHERE c.outcome IN ('FAILED','BALANCE_LOW')) AS failed, "
                "coalesce(sum(c.duration_sec),0)/60 AS minutes, coalesce(sum(c.cost),0) AS cost, "
                "count(DISTINCT c.phone) AS unique_phones, count(*) FILTER (WHERE c.campaign_id IS NULL) AS previous "
                + calls_from, calls_p)
            stats = cur.fetchone()

            funnel = []
            for u_c in ucs:
                tx = u_c["tag_taxonomy"]
                good = [t.lower() for t in tx.get("good", [])]
                soft = [t.lower() for t in tx.get("soft", [])]
                cur.execute(
                    "SELECT count(*) AS dialed, count(*) FILTER (WHERE c.outcome='CONNECTED') AS connected, "
                    "count(*) FILTER (WHERE lower(coalesce(c.tag,'')) = ANY(%s)) AS good, "
                    "count(*) FILTER (WHERE lower(coalesce(c.tag,'')) = ANY(%s)) AS soft "
                    f"FROM call c JOIN campaign ca ON ca.id = c.campaign_id "
                    f"WHERE c.started_at >= {since} AND ca.use_case_id = %s", [good, soft, days, u_c["id"]])
                c = cur.fetchone()
                cur.execute(
                    f"SELECT count(*) AS n FROM lead l JOIN campaign ca ON ca.id = l.campaign_id "
                    f"WHERE ca.use_case_id = %s AND ca.created_at >= {since}", [u_c["id"], days])
                leads = cur.fetchone()["n"]
                cur.execute(
                    f"SELECT count(*) AS n, count(*) FILTER (WHERE status='OPEN') AS open FROM handoff "
                    f"WHERE use_case_id = %s AND created_at >= {since}", [u_c["id"], days])
                h = cur.fetchone()
                funnel.append({"use_case_id": u_c["id"], "name": u_c["name"], "color": u_c["color"], "leads": leads,
                               "dialed": c["dialed"], "connected": c["connected"], "good": c["good"], "soft": c["soft"],
                               "handoffs": h["n"], "open": h["open"]})

            cur.execute(
                "SELECT coalesce(c.tag,'untagged') AS tag, count(*) AS n " + calls_from
                + " AND c.outcome = 'CONNECTED' GROUP BY 1 ORDER BY n DESC, tag LIMIT 12", calls_p)
            tags = cur.fetchall()

            # agents of this department (or both), counting the department's calls in range (previous ones too)
            cur.execute(
                "SELECT a.id, a.name, count(c.id) AS dialed, count(c.id) FILTER (WHERE c.outcome='CONNECTED') AS connected "
                "FROM agent a LEFT JOIN (SELECT c.* " + calls_from + ") c ON c.agent_id = a.id "
                "WHERE a.vertical IN (%s, 'BOTH') GROUP BY a.id ORDER BY dialed DESC, a.name",
                calls_p + [dept])
            agents = cur.fetchall()

            rf, rp = (" AND r.use_case_id = ANY(%s)", [restriction]) if restriction is not None else ("", [])
            cur.execute(
                "SELECT r.*, ca.name AS campaign_name, uc.name AS use_case_name FROM scheduled_run r "
                "LEFT JOIN campaign ca ON ca.id = r.campaign_id LEFT JOIN use_case uc ON uc.id = r.use_case_id "
                "WHERE r.department = %s AND r.status IN ('PLANNED','RUNNING')" + rf
                + " ORDER BY r.planned_start LIMIT 8", [dept] + rp)
            up_next = cur.fetchall()

            hf, hp = (" AND h.use_case_id = ANY(%s)", [restriction]) if restriction is not None else ("", [])
            if uc_id:
                hf, hp = " AND h.use_case_id = %s", [uc_id]
            cur.execute(
                "SELECT h.*, uc.name AS use_case_name FROM handoff h LEFT JOIN use_case uc ON uc.id = h.use_case_id "
                "WHERE h.department = %s AND h.status = 'OPEN'" + hf + " ORDER BY h.created_at DESC LIMIT 8",
                [dept] + hp)
            open_handoffs = cur.fetchall()
        last_sync = get_setting(conn, "lastSyncReport")
    finally:
        conn.close()
    outcomes = [{"key": "CONNECTED", "n": stats["connected"]}, {"key": "NOT_PICKED", "n": stats["not_picked"]},
                {"key": "CARRIER_BLOCKED", "n": stats["blocked"]}, {"key": "VOICEMAIL", "n": stats["voicemail"]},
                {"key": "FAILED", "n": stats["failed"]}]
    return jsonify({"days": days, "stats": stats, "funnel": funnel, "outcomes": outcomes, "tags": tags,
                    "agents": agents, "up_next": up_next, "open_handoffs": open_handoffs, "last_sync": last_sync})
