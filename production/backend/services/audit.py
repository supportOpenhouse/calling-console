"""Audit trail. Writes on the caller's connection; the caller commits."""
from __future__ import annotations

from psycopg2.extras import Json


def audit(conn, user_id, action, entity=None, entity_id=None, meta=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (user_id, action, entity, entity_id, meta) VALUES (%s,%s,%s,%s,%s)",
            (user_id, action, entity, entity_id, Json(meta) if meta is not None else None),
        )
