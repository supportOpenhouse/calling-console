"""Key/value settings in the `setting` table. Does not commit."""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json


def get_setting(conn, key: str) -> Any | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM setting WHERE key = %s", (key,))
        row = cur.fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO setting (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, Json(value)),
        )
