"""Datasets (per department): list, create, upload/append/pull, rows, facets, segment preview, delete; global DNC."""
from __future__ import annotations

import csv
from zipfile import BadZipFile

import requests
from flask import Blueprint, g, jsonify, request
from psycopg2.extras import Json

from ..db import get_conn
from ..services import datasets as svc
from ..services import rbac
from ..services.audit import audit
from ..services.classify import normalize_phone
from ..services.errors import bad_request, forbidden
from .auth import require_auth

bp = Blueprint("datasets", __name__, url_prefix="/api")

PAGE_SIZE = 50
_SOURCES = ("UPLOAD", "CRM", "API")
_CONFIG_FIELDS = ("url", "token", "path", "method", "phone_field", "name_field")
_FILTERS = {
    "never": "dl.last_called_at IS NULL",
    "connected": "dl.last_outcome = 'CONNECTED'",
    "dnc": "EXISTS (SELECT 1 FROM dnc_phone d WHERE d.phone = dl.phone)",
}
_DNC = "EXISTS (SELECT 1 FROM dnc_phone d WHERE d.phone = dl.phone)"


def public_dataset(row: dict) -> dict:
    """A dataset row as every response sends it: the source token stays on the server, `has_token` says if one is
    set."""
    cfg = row.get("source_config")
    return {**row, "source_config": {k: v for k, v in cfg.items() if k != "token"} if cfg is not None else None,
            "has_token": bool(cfg and cfg.get("token"))}


def _import_upload(conn, dataset_id: str, file) -> dict:
    try:
        return svc.import_file_into_dataset(conn, dataset_id, file.read(), file.filename or "")
    except (ValueError, BadZipFile, csv.Error, KeyError) as e:  # KeyError: a zip that is not an xlsx
        conn.rollback()
        raise bad_request(str(e) if isinstance(e, ValueError) else "could not read the file")


def _pull(conn, dataset: dict) -> dict:
    try:
        return svc.pull_dataset_from_source(conn, dataset)
    except (ValueError, requests.RequestException) as e:
        conn.rollback()
        raise bad_request(str(e))


@bp.get("/departments/<slug>/datasets")
@require_auth()
def list_datasets(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "view", dept)
    restriction = rbac.use_case_restriction(g.user)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ds.*, "
                "(SELECT count(*) FROM dataset_lead dl WHERE dl.dataset_id = ds.id AND dl.last_called_at IS NULL) AS never_called, "
                f"(SELECT count(*) FROM dataset_lead dl WHERE dl.dataset_id = ds.id AND {_DNC}) AS dnc "
                "FROM dataset ds WHERE ds.department = %s"
                + (" AND (ds.use_case_id IS NULL OR ds.use_case_id = ANY(%s))" if restriction is not None else "")
                + " ORDER BY ds.created_at DESC, ds.id", [dept] + ([restriction] if restriction is not None else []))
            return jsonify({"items": [public_dataset(r) for r in cur.fetchall()]})
    finally:
        conn.close()


@bp.post("/departments/<slug>/datasets")
@require_auth()
def create_dataset(slug):
    dept = rbac.dept_from_slug(slug)
    rbac.require(g.user, "manage", dept)
    form = request.form
    name = (form.get("name") or "").strip()
    if not name:
        raise bad_request("name is required")
    source = form.get("source") or "UPLOAD"
    if source not in _SOURCES:
        raise bad_request("source must be one of " + ", ".join(_SOURCES))
    use_case_id = form.get("use_case_id") or None
    config = {k: form[k].strip() for k in _CONFIG_FIELDS if (form.get(k) or "").strip()}
    if source != "UPLOAD" and "url" not in config:
        raise bad_request("url is required for a CRM or API dataset")
    if config.get("method") and config["method"].upper() not in ("GET", "POST"):
        raise bad_request("method must be GET or POST")
    file = request.files.get("file")
    conn = get_conn()
    try:
        if use_case_id:
            with conn.cursor() as cur:
                cur.execute("SELECT department FROM use_case WHERE id = %s", (use_case_id,))
                uc = cur.fetchone()
            if not uc or uc["department"] != dept:
                raise bad_request("use_case_id: unknown use case for this department")
            rbac.require(g.user, "manage", dept, use_case_id)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dataset (department, use_case_id, name, description, source, source_config, created_by_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (dept, use_case_id, name, form.get("description") or None, source,
                 Json(config) if config else None, g.user["id"]))
            ds = cur.fetchone()
        stats: dict = {}
        if file:
            stats = _import_upload(conn, ds["id"], file)
        audit(conn, g.user["id"], "dataset.create", "dataset", ds["id"], {"name": name, "source": source})
        conn.commit()
        if source != "UPLOAD":
            try:
                stats = svc.pull_dataset_from_source(conn, ds)
                conn.commit()
            except (ValueError, requests.RequestException) as e:
                conn.rollback()
                stats = {"error": str(e)}
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dataset WHERE id = %s", (ds["id"],))
            ds = cur.fetchone()
        return jsonify({"dataset": public_dataset(ds), "stats": stats}), 201
    finally:
        conn.close()


@bp.get("/datasets/<id>")
@require_auth()
def get_dataset(id):
    conn = get_conn()
    try:
        ds = rbac.load_scoped(conn, "dataset", id, g.user, "view")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE dl.last_called_at IS NULL) AS never, "
                "count(*) FILTER (WHERE dl.last_outcome = 'CONNECTED') AS connected, "
                "count(*) FILTER (WHERE dl.last_outcome = 'CARRIER_BLOCKED') AS blocked, "
                f"count(*) FILTER (WHERE {_DNC}) AS dnc, "
                "coalesce(sum(dl.total_attempts), 0) AS attempts FROM dataset_lead dl WHERE dl.dataset_id = %s", (id,))
            stats = {k: int(v) for k, v in cur.fetchone().items()}
        return jsonify({"dataset": public_dataset(ds), "stats": stats})
    finally:
        conn.close()


@bp.get("/datasets/<id>/rows")
@require_auth()
def dataset_rows(id):
    f, q = request.args.get("f"), (request.args.get("q") or "").strip()
    if f and f not in _FILTERS:
        raise bad_request("f must be one of " + ", ".join(_FILTERS))
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        raise bad_request("page must be a number")
    where, params = ["dl.dataset_id = %s"], [id]
    if f:
        where.append(_FILTERS[f])
    if q:
        where.append("(dl.phone ILIKE %s OR dl.name ILIKE %s OR dl.society ILIKE %s OR dl.city ILIKE %s)")
        params += [f"%{q}%"] * 4
    conn = get_conn()
    try:
        rbac.load_scoped(conn, "dataset", id, g.user, "view")
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM dataset_lead dl WHERE {' AND '.join(where)}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(f"SELECT dl.*, {_DNC} AS dnc FROM dataset_lead dl WHERE {' AND '.join(where)} "
                        "ORDER BY dl.created_at, dl.id LIMIT %s OFFSET %s", [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE])
            return jsonify({"items": cur.fetchall(), "total": total})
    finally:
        conn.close()


@bp.get("/datasets/<id>/facets")
@require_auth()
def facets(id):
    conn = get_conn()
    try:
        rbac.load_scoped(conn, "dataset", id, g.user, "view")
        return jsonify(svc.dataset_facets(conn, id))
    finally:
        conn.close()


@bp.post("/datasets/<id>/append")
@require_auth()
def append(id):
    file = request.files.get("file")
    conn = get_conn()
    try:
        rbac.load_scoped(conn, "dataset", id, g.user, "manage")
        if not file:
            raise bad_request("file is required")
        stats = _import_upload(conn, id, file)
        audit(conn, g.user["id"], "dataset.append", "dataset", id, stats)
        conn.commit()
        return jsonify(stats)
    finally:
        conn.close()


@bp.post("/datasets/<id>/pull")
@require_auth()
def pull(id):
    conn = get_conn()
    try:
        ds = rbac.load_scoped(conn, "dataset", id, g.user, "manage")
        stats = _pull(conn, ds)
        audit(conn, g.user["id"], "dataset.pull", "dataset", id, stats)
        conn.commit()
        return jsonify(stats)
    finally:
        conn.close()


@bp.post("/datasets/<id>/preview")
@require_auth()
def preview(id):
    segment = (request.get_json(silent=True) or {}).get("segment") or {}
    if not isinstance(segment, dict):
        raise bad_request("segment must be an object")
    conn = get_conn()
    try:
        ds = rbac.load_scoped(conn, "dataset", id, g.user, "manage")
        rows, cooled = svc.select_leads_for_run(conn, ds["department"], id, segment)
        return jsonify({"selected": len(rows), "cooled": cooled, "sample": rows[:5]})
    finally:
        conn.close()


@bp.delete("/datasets/<id>")
@require_auth()
def delete_dataset(id):
    conn = get_conn()
    try:
        ds = rbac.load_scoped(conn, "dataset", id, g.user, "admin")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dataset WHERE id = %s", (id,))
        audit(conn, g.user["id"], "dataset.delete", "dataset", id, {"name": ds["name"]})
        conn.commit()
        return "", 204
    finally:
        conn.close()


def _dnc_phone(raw: str, can) -> str:
    """The +91 form of `raw`; 403 unless `can(user, dept)` holds for a department of the user."""
    phone = normalize_phone(raw)
    if not phone:
        raise bad_request("invalid phone number")
    if not any(can(g.user, d) for d in rbac.user_depts(g.user)):
        raise forbidden("forbidden")
    return phone


@bp.put("/dnc/<phone>")
@require_auth()
def add_dnc(phone):
    """Block the phone everywhere (a manager, head or owner). Answers {queued_in}: live batches that still hold it
    (DNC never takes a phone out of a batch Bolna already has; pausing that campaign does)."""
    phone = _dnc_phone(phone, rbac.can_manage_dept)
    reason = (request.get_json(silent=True) or {}).get("reason")
    if reason is not None and not isinstance(reason, str):
        raise bad_request("reason must be text")
    conn = get_conn()
    try:
        svc.mark_dnc(conn, phone, "manual", reason or None, g.user["id"])
        held = svc.queued_in(conn, phone)
        audit(conn, g.user["id"], "dnc.add", "dnc_phone", phone, {"reason": reason})
        conn.commit()
        return jsonify({"queued_in": held})
    finally:
        conn.close()


@bp.delete("/dnc/<phone>")
@require_auth()
def remove_dnc(phone):
    """Unblock the phone: a head (of any department) or the owner only."""
    phone = _dnc_phone(phone, rbac.can_admin_dept)
    conn = get_conn()
    try:
        svc.unmark_dnc(conn, phone)
        audit(conn, g.user["id"], "dnc.remove", "dnc_phone", phone)
        conn.commit()
        return "", 204
    finally:
        conn.close()
