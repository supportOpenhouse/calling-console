"""Permission rules (port of the old lib/rbac.ts plus spec 5.3 head rules).

All checks take the user as the dict from `public_user` (role, vertical, use_case_ids).
"""
from __future__ import annotations

from .errors import forbidden, not_found

DEPTS = ("SUPPLY", "DEMAND")
_LEVELS = ("view", "manage", "admin", "owner")


def dept_from_slug(slug: str) -> str:
    if slug == "supply":
        return "SUPPLY"
    if slug == "demand":
        return "DEMAND"
    raise not_found("unknown department")


def dept_slug(d: str) -> str:
    return d.lower()


def user_depts(u: dict) -> list[str]:
    if u["role"] == "ADMIN" or u["vertical"] == "BOTH":
        return list(DEPTS)
    return [u["vertical"]]


def can_access_dept(u: dict, d: str) -> bool:
    return d in user_depts(u)


def is_owner(u: dict) -> bool:
    return u["role"] == "ADMIN"


def _is_head_of(u: dict, d: str) -> bool:
    return u["role"] == "HEAD" and can_access_dept(u, d)


def can_manage_dept(u: dict, d: str) -> bool:
    return is_owner(u) or (u["role"] in ("HEAD", "MANAGER") and can_access_dept(u, d))


def can_admin_dept(u: dict, d: str) -> bool:
    return is_owner(u) or _is_head_of(u, d)


def use_case_restriction(u: dict) -> list[str] | None:
    ids = list(u.get("use_case_ids") or [])
    if is_owner(u) or u["role"] == "HEAD" or not ids:
        return None
    return ids


def can_see_use_case(u: dict, use_case_id: str | None) -> bool:
    restriction = use_case_restriction(u)
    return restriction is None or not use_case_id or use_case_id in restriction


def require(u: dict, level: str, dept: str, use_case_id: str | None = None) -> None:
    """Raise 403 unless `u` has `level` on `dept` (and may see `use_case_id`).

    A dept of "BOTH" (a record shared by both departments) needs access to both.
    """
    if level not in _LEVELS:
        raise ValueError(f"unknown level {level!r}")
    depts = list(DEPTS) if dept == "BOTH" else [dept]
    if level == "owner":
        ok = is_owner(u)
    else:
        check = {"view": can_access_dept, "manage": can_manage_dept, "admin": can_admin_dept}[level]
        ok = all(check(u, d) for d in depts)
    if not ok:
        raise forbidden("forbidden")
    if not can_see_use_case(u, use_case_id):
        raise forbidden("forbidden: use case not allowed")


# kind -> (table, department column, use-case column or None; "id" means the row itself)
_SCOPES = {
    "campaign": ("campaign", "vertical", "use_case_id"),
    "dataset": ("dataset", "department", "use_case_id"),
    "workflow": ("workflow", "department", "use_case_id"),
    "run": ("scheduled_run", "department", "use_case_id"),
    "handoff": ("handoff", "department", "use_case_id"),
    "use_case": ("use_case", "department", "id"),
}


def _fetch(conn, table: str, id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{table}" WHERE id = %s', (id,))
        return cur.fetchone()


def load_scoped(conn, kind: str, id: str, u: dict, level: str) -> dict:
    """Load a record and check `u` against the record's own department/use case."""
    if kind in _SCOPES:
        table, dcol, ucol = _SCOPES[kind]
        row = _fetch(conn, table, id)
        if not row:
            raise not_found()
        dept, uc = row[dcol], row[ucol]
    elif kind == "dataset_lead":
        row = _fetch(conn, "dataset_lead", id)
        if not row:
            raise not_found()
        parent = _fetch(conn, "dataset", row["dataset_id"])
        dept, uc = parent["department"], parent["use_case_id"]
    elif kind == "call":
        row = _fetch(conn, "call", id)
        if not row:
            raise not_found()
        camp = _fetch(conn, "campaign", row["campaign_id"]) if row.get("campaign_id") else None
        if camp:
            dept, uc = camp["vertical"], camp["use_case_id"]
        else:
            agent = _fetch(conn, "agent", row["agent_id"]) if row.get("agent_id") else None
            if not agent:
                raise forbidden("forbidden: call has no department")
            dept, uc = agent["vertical"], None
    else:
        raise ValueError(f"unknown kind {kind!r}")
    require(u, level, dept, uc)
    return row


def check_user_edit(actor: dict, target: dict | None, role: str, vertical: str) -> None:
    """Who may create/edit a user (target None = new) as `role`/`vertical` (spec 5.3)."""
    if target is not None and target["id"] == actor["id"] and (
        role != target["role"] or vertical != target["vertical"]
    ):
        raise forbidden("you cannot change your own role")
    if is_owner(actor):
        return
    if actor["role"] != "HEAD":
        raise forbidden("department head or product owner required")
    if role not in ("MANAGER", "VIEWER"):
        raise forbidden("a head can only assign manager or viewer")
    actor_depts = user_depts(actor)
    new_depts = list(DEPTS) if vertical == "BOTH" else [vertical]
    if not all(d in actor_depts for d in new_depts):
        raise forbidden("outside your department")
    if target is not None:
        if target["role"] in ("ADMIN", "HEAD"):
            raise forbidden("a head cannot edit an owner or head")
        claimable = target["role"] == "VIEWER" and target["vertical"] == "BOTH"
        t_depts = list(DEPTS) if target["vertical"] == "BOTH" else [target["vertical"]]
        if not claimable and not all(d in actor_depts for d in t_depts):
            raise forbidden("outside your department")
