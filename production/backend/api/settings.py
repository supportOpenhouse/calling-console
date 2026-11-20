"""Integrations status and the audit trail (owner only)."""
from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, g, jsonify, request

from .. import config
from ..db import get_conn
from ..services import rbac
from ..services.di_push import di_enabled
from ..services.errors import bad_request
from ..services.settings_store import get_setting
from ..services.storage import storage_enabled
from ..services.whatsapp import whatsapp_enabled
from .auth import require_auth
from .calls import _ist_day

bp = Blueprint("settings", __name__, url_prefix="/api/settings")

PAGE_SIZE = 50


@bp.get("/integrations")
@require_auth()
def integrations():
    rbac.require(g.user, "owner", "SUPPLY")
    conn = get_conn()
    try:
        return jsonify({
            "bolna": {"configured": bool(config.BOLNA_API_KEY)},
            "r2": {"configured": storage_enabled()},
            "di": {"configured": di_enabled(), "failures": get_setting(conn, "diPushFailures")},
            "interakt": {"configured": whatsapp_enabled()},
            "ingest": {"configured": bool(config.INGEST_TOKEN), "path": "/api/ingest/datasets/<dataset_id>/leads"},
            "sync": {"enabled": config.SYNC_ENABLED == "1", "last_report": get_setting(conn, "lastSyncReport"),
                     "last_full_sync_date": get_setting(conn, "lastFullSyncDate")},
        })
    finally:
        conn.close()


@bp.get("/audit")
@require_auth()
def audit_list():
    rbac.require(g.user, "owner", "SUPPLY")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        raise bad_request("page must be a number")
    # Filters: user_id ("system" = no user), action, entity, from/to (IST days, to inclusive), q (record id or detail).
    a = request.args
    where, params = ["TRUE"], []
    if a.get("user_id") == "system":
        where.append("l.user_id IS NULL")
    elif a.get("user_id"):
        where.append("l.user_id = %s")
        params.append(a["user_id"])
    for key in ("action", "entity"):
        if a.get(key):
            where.append(f"l.{key} = %s")
            params.append(a[key])
    if a.get("from"):
        where.append("l.created_at >= %s")
        params.append(_ist_day("from"))
    if a.get("to"):
        where.append("l.created_at < %s")
        params.append(_ist_day("to") + timedelta(days=1))
    q = (a.get("q") or "").strip()
    if q:
        where.append("(l.entity_id ILIKE %s OR l.meta::text ILIKE %s)")
        params += [f"%{q}%"] * 2
    w = " AND ".join(where)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM audit_log l WHERE {w}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT l.id, l.user_id, u.email AS user_email, l.action, l.entity, l.entity_id, l.meta, l.created_at "
                f'FROM audit_log l LEFT JOIN "user" u ON u.id = l.user_id WHERE {w} '
                "ORDER BY l.created_at DESC, l.id LIMIT %s OFFSET %s", [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE])
            items = cur.fetchall()
            # filter options: what occurs in the whole log
            cur.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
            actions = [r["action"] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT entity FROM audit_log WHERE entity IS NOT NULL ORDER BY entity")
            entities = [r["entity"] for r in cur.fetchall()]
            cur.execute('SELECT DISTINCT u.id, u.email FROM audit_log l JOIN "user" u ON u.id = l.user_id ORDER BY u.email')
            users = cur.fetchall()
            cur.execute("SELECT EXISTS (SELECT 1 FROM audit_log WHERE user_id IS NULL) AS any")
            has_system = cur.fetchone()["any"]
        return jsonify({"items": items, "total": total, "actions": actions, "entities": entities, "users": users,
                        "has_system": has_system})
    finally:
        conn.close()
