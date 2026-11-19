"""Integrations status and the audit trail (owner only)."""
from __future__ import annotations

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
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM audit_log")
            total = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT l.id, l.user_id, u.email AS user_email, l.action, l.entity, l.entity_id, l.meta, l.created_at "
                'FROM audit_log l LEFT JOIN "user" u ON u.id = l.user_id '
                "ORDER BY l.created_at DESC, l.id LIMIT %s OFFSET %s", (PAGE_SIZE, (page - 1) * PAGE_SIZE))
            return jsonify({"items": cur.fetchall(), "total": total})
    finally:
        conn.close()
