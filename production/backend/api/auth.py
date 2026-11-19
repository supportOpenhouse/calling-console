"""Google OAuth login + JWT issuance (DI's auth.py), with per-request user loading."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Blueprint, g, jsonify, request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from .. import config
from ..db import get_conn
from ..services.audit import audit

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def issue_jwt(user: dict) -> str:
    payload = {
        "sub": user["id"],
        "email": user["email"],
        "exp": datetime.now(tz=timezone.utc) + timedelta(hours=config.JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "image": row["image"],
        "role": row["role"],
        "vertical": row["vertical"],
        "use_case_ids": row["use_case_ids"] or [],
        "title": row["title"],
        "active": row["active"],
    }


def _load_user(user_id: str) -> dict | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM "user" WHERE id = %s', (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def require_auth(*roles: str):
    """Require a valid JWT whose user is active (loaded from the DB) and has one of `roles`."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "missing bearer token"}), 401
            try:
                payload = jwt.decode(auth[len("Bearer "):], config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM],
                                     options={"require": ["exp", "sub"]})
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "token expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "invalid token"}), 401
            row = _load_user(payload.get("sub") or "")
            if not row or not row["active"]:
                return jsonify({"error": "unauthorized"}), 401
            g.user = public_user(row)
            if roles and g.user["role"] not in roles:
                return jsonify({"error": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def _find_or_create(conn, email: str, name: str) -> dict:
    role = "ADMIN" if email in config.ADMIN_EMAILS else "VIEWER"
    with conn.cursor() as cur:
        cur.execute(
            'INSERT INTO "user" (email, name, role, vertical) VALUES (%s,%s,%s,\'BOTH\') '
            "ON CONFLICT (email) DO NOTHING RETURNING *",
            (email, name, role),
        )
        row = cur.fetchone()
        if row:
            audit(conn, row["id"], "user.create", "user", row["id"], {"role": role})
            return row
        cur.execute('SELECT * FROM "user" WHERE email = %s', (email,))
        return cur.fetchone()


def _login_response(conn, row: dict):
    audit(conn, row["id"], "login", "user", row["id"], {"ip": request.remote_addr})
    conn.commit()
    return jsonify({"token": issue_jwt(row), "user": public_user(row)})


@bp.post("/google")
def google_login():
    body = request.get_json(silent=True) or {}
    token = body.get("id_token")
    if not token:
        return jsonify({"error": "missing id_token"}), 400
    if not config.GOOGLE_OAUTH_CLIENT_ID:
        return jsonify({"error": "server not configured for Google OAuth"}), 500
    try:
        info = id_token.verify_oauth2_token(token, google_requests.Request(), config.GOOGLE_OAUTH_CLIENT_ID)
    except ValueError as e:
        return jsonify({"error": f"invalid id_token: {e}"}), 401

    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified"):
        return jsonify({"error": "email not verified"}), 401
    if not email.endswith("@" + config.ALLOWED_EMAIL_DOMAIN):
        return jsonify({"error": f"only @{config.ALLOWED_EMAIL_DOMAIN} accounts allowed"}), 403

    conn = get_conn()
    try:
        row = _find_or_create(conn, email, info.get("name") or email.split("@")[0])
        if not row["active"]:
            conn.rollback()
            return jsonify({"error": "account disabled"}), 403
        return _login_response(conn, row)
    finally:
        conn.close()


@bp.post("/dev")
def dev_login():
    """Passwordless sign-in for seeded demo accounts; local development only."""
    if config.GOOGLE_OAUTH_CLIENT_ID or config.DEV_LOGIN != "1":
        return jsonify({"error": "not found"}), 404
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM "user" WHERE email = %s AND active', (email,))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        return _login_response(conn, row)
    finally:
        conn.close()


@bp.get("/me")
@require_auth()
def me():
    return jsonify({"user": g.user})
