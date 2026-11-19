"""Seed a local/dev database with demo users, one agent, use case, dataset and MANUAL workflow per department.

Usage (from production/):
    ALLOW_SEED=1 backend/.venv/bin/python -m backend.scripts.seed

Refuses (exit 2) unless ALLOW_SEED=1, so it cannot be run against production by accident. Never calls Bolna: the
agents are local rows with made-up Bolna ids. Safe to re-run.
"""
from __future__ import annotations

import os
import sys

from psycopg2.extras import Json

from ..db import get_conn
from ..services.redial import DEFAULT_REDIAL_POLICY
from ..services.taxonomy import DEFAULT_TAXONOMY

USERS = [
    ("owner@openhouse.in", "Owner", "ADMIN", "BOTH"),
    ("supply.head@openhouse.in", "Supply Head", "HEAD", "SUPPLY"),
    ("demand.head@openhouse.in", "Demand Head", "HEAD", "DEMAND"),
    ("supply.manager@openhouse.in", "Supply Manager", "MANAGER", "SUPPLY"),
    ("demand.manager@openhouse.in", "Demand Manager", "MANAGER", "DEMAND"),
    ("viewer@openhouse.in", "Viewer", "VIEWER", "BOTH"),
]
# department -> (use case key, agent template variables, phone prefix)
DEPARTMENTS = {
    "SUPPLY": ("seller_qualification", ["owner_name", "society_name"], "+91990001"),
    "DEMAND": ("buyer_qualification", ["name", "city"], "+91990002"),
}
FROM_NUMBER = "+918000000001"


def _one(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchone()["id"]


def _seed_department(cur, dept: str, key: str, variables: list[str], prefix: str) -> None:
    low = dept.lower()
    cur.execute("INSERT INTO agent (bolna_agent_id, name, vertical, variables, default_from_number) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (bolna_agent_id) DO NOTHING",
                (f"seed-{low}-agent", f"Seed {dept.title()} Agent", dept, Json(variables), FROM_NUMBER))
    agent_id = _one(cur, "SELECT id FROM agent WHERE bolna_agent_id = %s", (f"seed-{low}-agent",))
    cur.execute("INSERT INTO use_case (department, key, name, tag_taxonomy) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (department, key) DO NOTHING",
                (dept, key, key.replace("_", " ").title(), Json(DEFAULT_TAXONOMY)))
    use_case_id = _one(cur, "SELECT id FROM use_case WHERE department = %s AND key = %s", (dept, key))
    name = f"Seed {dept.title()} Leads"
    cur.execute("SELECT id FROM dataset WHERE department = %s AND name = %s", (dept, name))
    row = cur.fetchone()
    if row:
        dataset_id = row["id"]
    else:
        dataset_id = _one(cur, "INSERT INTO dataset (department, use_case_id, name) VALUES (%s, %s, %s) "
                               "RETURNING id", (dept, use_case_id, name))
    for i in range(1, 21):
        cur.execute("INSERT INTO dataset_lead (dataset_id, phone, name, city, society) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (dataset_id, phone) DO NOTHING",
                    (dataset_id, f"{prefix}{i:04d}", f"Lead {i}", "Gurgaon", f"Society {i % 5 + 1}"))
    cur.execute("UPDATE dataset SET row_count = (SELECT count(*) FROM dataset_lead WHERE dataset_id = %s) "
                "WHERE id = %s", (dataset_id, dataset_id))
    wf_name = f"Seed {dept.title()} Workflow"
    cur.execute("SELECT 1 FROM workflow WHERE department = %s AND name = %s", (dept, wf_name))
    if not cur.fetchone():
        cur.execute("INSERT INTO workflow (department, use_case_id, name, agent_id, dataset_id, from_number, trigger, "
                    "redial_policy) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (dept, use_case_id, wf_name, agent_id, dataset_id, FROM_NUMBER, Json({"type": "MANUAL"}),
                     Json(DEFAULT_REDIAL_POLICY)))


def main() -> int:
    if os.environ.get("ALLOW_SEED") != "1":
        print("refusing to seed: set ALLOW_SEED=1 to confirm this is not production", file=sys.stderr)
        return 2
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for email, name, role, vertical in USERS:
                cur.execute('INSERT INTO "user" (name, email, role, vertical) VALUES (%s,%s,%s,%s) '
                            "ON CONFLICT (email) DO NOTHING", (name, email, role, vertical))
            for dept, (key, variables, prefix) in DEPARTMENTS.items():
                _seed_department(cur, dept, key, variables, prefix)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"seeded {len(USERS)} users and {len(DEPARTMENTS)} departments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
