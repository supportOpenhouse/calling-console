"""Sync now and the last sync's report."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..db import get_conn
from ..services import rbac
from ..services import sync as sync_svc
from ..services.audit import audit
from ..services.errors import bad_request, conflict, forbidden
from ..services.settings_store import get_setting
from .auth import require_auth

bp = Blueprint("sync", __name__, url_prefix="/api")


@bp.post("/sync")
@require_auth()
def sync_now():
    if not any(rbac.can_manage_dept(g.user, d) for d in rbac.DEPTS):
        raise forbidden("forbidden")
    full = (request.get_json(silent=True) or {}).get("full", False)
    if not isinstance(full, bool):
        raise bad_request("full must be true or false")
    report = sync_svc.run_sync(full=full)
    if report is None:
        raise conflict("a sync is already running")
    conn = get_conn()
    try:
        audit(conn, g.user["id"], "sync.run", None, None, {"full": full})
        conn.commit()
    finally:
        conn.close()
    return jsonify(report)


@bp.get("/sync/status")
@require_auth()
def sync_status():
    conn = get_conn()
    try:
        return jsonify({"last_report": get_setting(conn, "lastSyncReport"),
                        "last_full_sync_date": get_setting(conn, "lastFullSyncDate")})
    finally:
        conn.close()
