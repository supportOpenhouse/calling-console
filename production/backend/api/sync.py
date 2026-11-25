"""Sync now, the last sync's report, and the pulse the frontend polls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from .. import config
from ..db import get_conn
from ..services import rbac
from ..services import sync as sync_svc
from ..services.audit import audit
from ..services.errors import bad_request, conflict, forbidden
from ..services.settings_store import get_setting
from .auth import require_auth
from .calls import _FROM, _filters

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


# No quick pull reaching Bolna for this long while the sync runs = "connection problem" in the frontend. Longer than a
# 5-minute sync with live campaigns usually holds the lock.
PULL_STALE_AFTER = timedelta(minutes=2)


@bp.get("/sync/pulse")
@require_auth()
def pulse():
    """What the frontend polls every few seconds: `latest` = when the department's newest call (as the Calls page
    scopes them) was saved, as epoch seconds (null with none); `stale` = the background sync is on but no quick pull
    has reached Bolna for PULL_STALE_AFTER."""
    dept = rbac.dept_from_slug(request.args.get("dept", ""))
    rbac.require(g.user, "view", dept)
    where, params = _filters(dept)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT extract(epoch FROM max(c.created_at))::float AS latest {_FROM} WHERE {where}", params)
            latest = cur.fetchone()["latest"]
        last = get_setting(conn, sync_svc.LAST_PULL)
    finally:
        conn.close()
    stale = config.SYNC_ENABLED == "1" and (
        last is None or datetime.now(timezone.utc) - datetime.fromisoformat(last) > PULL_STALE_AFTER)
    return jsonify({"latest": latest, "last_pull_at": last, "stale": stale})
