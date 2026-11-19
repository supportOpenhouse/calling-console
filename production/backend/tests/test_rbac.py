from datetime import datetime, timezone

import pytest

from backend.services import rbac
from backend.services.audit import audit
from backend.services.errors import ApiError
from backend.tests.factories import (make_agent, make_campaign, make_dataset, make_use_case, make_user,
                                     make_call)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _u(role="MANAGER", vertical="SUPPLY", ucs=(), id="u1"):
    return {"id": id, "role": role, "vertical": vertical, "use_case_ids": list(ucs)}


def _status(fn, *a):
    try:
        fn(*a)
    except ApiError as e:
        return e.status
    return 200


def test_dept_slugs():
    assert rbac.dept_from_slug("supply") == "SUPPLY" and rbac.dept_slug("DEMAND") == "demand"
    assert _status(rbac.dept_from_slug, "both") == 404


def test_depts_and_levels():
    assert rbac.user_depts(_u("ADMIN", "BOTH")) == ["SUPPLY", "DEMAND"]
    assert rbac.user_depts(_u("VIEWER", "DEMAND")) == ["DEMAND"]
    assert _status(rbac.require, _u("VIEWER"), "view", "SUPPLY") == 200
    assert _status(rbac.require, _u("VIEWER"), "manage", "SUPPLY") == 403
    assert _status(rbac.require, _u("MANAGER"), "admin", "SUPPLY") == 403
    assert _status(rbac.require, _u("HEAD"), "admin", "SUPPLY") == 200
    assert _status(rbac.require, _u("HEAD"), "owner", "SUPPLY") == 403
    assert _status(rbac.require, _u("ADMIN", "BOTH"), "owner", "DEMAND") == 200
    assert _status(rbac.require, _u("MANAGER"), "view", "DEMAND") == 403


def test_load_scoped_uses_record_department(conn):
    ag = make_agent(conn)
    c = make_campaign(conn, agent=ag, vertical="SUPPLY")
    assert _status(rbac.load_scoped, conn, "campaign", c["id"], _u(vertical="DEMAND"), "view") == 403
    assert rbac.load_scoped(conn, "campaign", c["id"], _u(), "manage")["id"] == c["id"]
    assert _status(rbac.load_scoped, conn, "campaign", "missing", _u(), "view") == 404


def test_use_case_restricted_manager(conn):
    ag = make_agent(conn)
    uc1 = make_use_case(conn, key="a")
    uc2 = make_use_case(conn, key="b")
    c1 = make_campaign(conn, agent=ag, use_case_id=uc1["id"])
    c2 = make_campaign(conn, agent=ag, use_case_id=uc2["id"])
    u = _u(ucs=[uc1["id"]])
    assert rbac.load_scoped(conn, "campaign", c1["id"], u, "manage")
    assert _status(rbac.load_scoped, conn, "campaign", c2["id"], u, "manage") == 403
    assert _status(rbac.load_scoped, conn, "use_case", uc2["id"], u, "view") == 403
    assert rbac.use_case_restriction(_u("HEAD", ucs=[uc1["id"]])) is None


def test_load_scoped_dataset_and_calls(conn):
    ds = make_dataset(conn, department="DEMAND", rows=[{"phone": "+919000000001"}])
    lead_id = None
    with conn.cursor() as cur:
        cur.execute("select id from dataset_lead where dataset_id=%s", (ds["id"],))
        lead_id = cur.fetchone()["id"]
    assert _status(rbac.load_scoped, conn, "dataset_lead", lead_id, _u(vertical="SUPPLY"), "view") == 403
    assert rbac.load_scoped(conn, "dataset_lead", lead_id, _u(vertical="DEMAND"), "view")
    ag = make_agent(conn, vertical="DEMAND")
    call = make_call(conn, phone="+919000000002", started_at=NOW, agent=ag)
    assert _status(rbac.load_scoped, conn, "call", call["id"], _u(vertical="SUPPLY"), "view") == 403
    assert rbac.load_scoped(conn, "call", call["id"], _u(vertical="DEMAND"), "view")
    both = make_call(conn, phone="+919000000003", started_at=NOW, agent=make_agent(conn, vertical="BOTH"))
    assert _status(rbac.load_scoped, conn, "call", both["id"], _u(vertical="SUPPLY"), "view") == 403
    assert rbac.load_scoped(conn, "call", both["id"], _u("VIEWER", "BOTH"), "view")


def test_head_cannot_edit_owner_or_head():
    head = _u("HEAD", "SUPPLY", id="h")
    assert _status(rbac.check_user_edit, head, _u("ADMIN", "BOTH", id="o"), "VIEWER", "SUPPLY") == 403
    assert _status(rbac.check_user_edit, head, _u("HEAD", "SUPPLY", id="h2"), "VIEWER", "SUPPLY") == 403
    assert _status(rbac.check_user_edit, head, _u("MANAGER", "SUPPLY", id="m"), "VIEWER", "SUPPLY") == 200
    assert _status(rbac.check_user_edit, head, None, "HEAD", "SUPPLY") == 403
    assert _status(rbac.check_user_edit, head, None, "MANAGER", "SUPPLY") == 200
    assert _status(rbac.check_user_edit, head, None, "MANAGER", "DEMAND") == 403
    assert _status(rbac.check_user_edit, _u("MANAGER"), None, "VIEWER", "SUPPLY") == 403
    assert _status(rbac.check_user_edit, head, _u("MANAGER", "DEMAND", id="m"), "MANAGER", "SUPPLY") == 403


def test_head_can_claim_both_viewer():
    head = _u("HEAD", "SUPPLY", id="h")
    t = _u("VIEWER", "BOTH", id="v")
    assert _status(rbac.check_user_edit, head, t, "MANAGER", "SUPPLY") == 200
    assert _status(rbac.check_user_edit, head, t, "MANAGER", "DEMAND") == 403
    assert _status(rbac.check_user_edit, head, t, "MANAGER", "BOTH") == 403


def test_nobody_changes_own_role():
    for r in ("ADMIN", "HEAD", "MANAGER"):
        me = _u(r, "BOTH", id="me")
        assert _status(rbac.check_user_edit, me, me, "VIEWER", "BOTH") == 403
    owner = _u("ADMIN", "BOTH", id="me")
    assert _status(rbac.check_user_edit, owner, owner, "ADMIN", "BOTH") == 200
    assert _status(rbac.check_user_edit, _u("ADMIN", "BOTH"), _u("HEAD", "SUPPLY", id="x"), "HEAD", "DEMAND") == 200


def test_audit_writes_without_committing(conn):
    u = make_user(conn)
    audit(conn, u["id"], "thing", "campaign", "c1", {"a": 1})
    with conn.cursor() as cur:
        cur.execute("select action, meta from audit_log")
        assert cur.fetchone() == {"action": "thing", "meta": {"a": 1}}
