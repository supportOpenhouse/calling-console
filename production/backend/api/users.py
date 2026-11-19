"""Users and department teams."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from psycopg2.extras import Json

from .. import config
from ..db import get_conn
from ..services import rbac
from ..services.audit import audit
from ..services.errors import bad_request, forbidden, not_found
from .auth import public_user, require_auth

bp = Blueprint("users", __name__, url_prefix="/api")

ROLES = ("ADMIN", "HEAD", "MANAGER", "VIEWER")
VERTICALS = ("SUPPLY", "DEMAND", "BOTH")


@bp.get("/users")
@require_auth()
def list_users():
    rbac.require(g.user, "owner", "SUPPLY")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM "user" ORDER BY email')
            return jsonify({"items": [public_user(r) for r in cur.fetchall()]})
    finally:
        conn.close()


@bp.get("/departments/<slug>/team")
@require_auth()
def team(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "admin", dept)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM "user" WHERE vertical IN (%s, \'BOTH\') ORDER BY email', (dept,))
            return jsonify({"items": [public_user(r) for r in cur.fetchall()]})
    finally:
        conn.close()


def _fields(body: dict) -> tuple[str, str, list[str], str | None, bool]:
    role, vertical = body.get("role"), body.get("vertical")
    if role not in ROLES:
        raise bad_request("role must be one of " + ", ".join(ROLES))
    if vertical not in VERTICALS:
        raise bad_request("vertical must be one of " + ", ".join(VERTICALS))
    ids = body.get("use_case_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise bad_request("use_case_ids must be a list of ids")
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise bad_request("title must be text or null")
    active = body.get("active")
    if not isinstance(active, bool):
        raise bad_request("active must be true or false")
    return role, vertical, list(dict.fromkeys(ids)), (title.strip() or None) if title else None, active


def _check_use_cases(conn, ids: list[str], vertical: str) -> None:
    if not ids:
        return
    depts = list(rbac.DEPTS) if vertical == "BOTH" else [vertical]
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM use_case WHERE id = ANY(%s) AND department = ANY(%s::vertical[])", (ids, depts))
        found = {r["id"] for r in cur.fetchall()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise bad_request("use cases not in the user's department: " + ", ".join(missing))


def _save(conn, target: dict | None, email: str, body: dict):
    role, vertical, ids, title, active = _fields(body)
    if target is not None and target["id"] == g.user["id"] and not active:
        raise forbidden("you cannot deactivate yourself")
    rbac.check_user_edit(g.user, target, role, vertical)
    _check_use_cases(conn, ids, vertical)
    with conn.cursor() as cur:
        if target is None:
            cur.execute(
                'INSERT INTO "user" (email, name, role, vertical, use_case_ids, title, active) '
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (email, email.split("@")[0], role, vertical, Json(ids), title, active))
        else:
            cur.execute(
                'UPDATE "user" SET role=%s, vertical=%s, use_case_ids=%s, title=%s, active=%s '
                "WHERE id=%s RETURNING *", (role, vertical, Json(ids), title, active, target["id"]))
        row = cur.fetchone()
    before = None if target is None else {k: target[k] for k in ("role", "vertical", "use_case_ids", "title", "active")}
    audit(conn, g.user["id"], "user.create" if target is None else "user.update", "user", row["id"],
          {"email": row["email"], "role": role, "vertical": vertical, "use_case_ids": ids, "title": title,
           "active": active, "before": before})
    conn.commit()
    return jsonify(public_user(row))


@bp.post("/users")
@require_auth()
def upsert_user():
    if g.user["role"] not in ("ADMIN", "HEAD"):
        raise forbidden("department head or product owner required")
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower() if isinstance(body.get("email"), str) else ""
    if not email or not email.endswith("@" + config.ALLOWED_EMAIL_DOMAIN) or email.count("@") != 1:
        raise bad_request(f"email must be an @{config.ALLOWED_EMAIL_DOMAIN} address")
    if email in config.ADMIN_EMAILS and not rbac.is_owner(g.user):
        raise forbidden("only the product owner can pre-assign an owner email")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM "user" WHERE email = %s FOR UPDATE', (email,))
            target = cur.fetchone()
        return _save(conn, target, email, body)
    finally:
        conn.close()


@bp.put("/users/<user_id>")
@require_auth()
def update_user(user_id):
    if g.user["role"] not in ("ADMIN", "HEAD"):
        raise forbidden("department head or product owner required")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM "user" WHERE id = %s FOR UPDATE', (user_id,))
            target = cur.fetchone()
        if not target:
            raise not_found()
        return _save(conn, target, target["email"], request.get_json(silent=True) or {})
    finally:
        conn.close()
