"""Datasets: row upsert, CRM/API pull, facets, segment selection, cooling, and DNC.

Services take `conn` and never commit; the caller commits.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import requests
from psycopg2.extras import Json, execute_values

from .classify import normalize_phone
from .lead_import import parse_lead_file


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _pick_col(columns: list[str], *needles: str) -> str | None:
    cols = [(c, _norm(c)) for c in columns]
    for c, n in cols:
        if n in needles:
            return c
    for c, n in cols:
        if any(nd in n for nd in needles):
            return c
    return None


def upsert_dataset_rows(conn, dataset_id: str, rows: list[dict], hints: dict | None = None) -> dict:
    """Insert/refresh rows into a dataset. Keeps every column in `data`."""
    hints = hints or {}
    columns = list(rows[0].keys()) if rows else []
    phone_col = hints.get("phone") or _pick_col(columns, "phone", "mobile", "contactnumber", "contact", "phonenumber", "mobileno", "whatsapp", "number")
    name_col = hints.get("name") or _pick_col(columns, "ownername", "name", "fullname", "cpname", "customername")
    city_col = hints.get("city") or _pick_col(columns, "city", "location")
    soc_col = hints.get("society") or _pick_col(columns, "societyname", "society", "project", "projectname")
    seg_col = hints.get("segment") or _pick_col(columns, "segment", "category", "activitycategory", "cpstatus", "level", "tier", "source", "list")
    if not phone_col:
        raise ValueError(f"No phone column found in: {', '.join(columns)}")
    stats = {"added": 0, "updated": 0, "invalid": 0, "duplicates": 0}
    seen: set[str] = set()
    batch: list[tuple] = []
    for r in rows:
        phone = normalize_phone(r.get(phone_col))
        if not phone:
            stats["invalid"] += 1
            continue
        if phone in seen:
            stats["duplicates"] += 1
            continue
        seen.add(phone)
        batch.append((dataset_id, phone,
                      (r.get(name_col) or None) if name_col else None, Json(r),
                      (r.get(city_col) or None) if city_col else None,
                      (r.get(soc_col) or None) if soc_col else None,
                      (r.get(seg_col) or None) if seg_col else None))
    sql = (
        "INSERT INTO dataset_lead (dataset_id, phone, name, data, city, society, segment) VALUES %s "
        "ON CONFLICT (dataset_id, phone) DO UPDATE SET data = excluded.data, "
        "name = coalesce(excluded.name, dataset_lead.name), city = coalesce(excluded.city, dataset_lead.city), "
        "society = coalesce(excluded.society, dataset_lead.society), segment = coalesce(excluded.segment, dataset_lead.segment) "
        "RETURNING (xmax = 0) AS added"
    )
    with conn.cursor() as cur:
        for i in range(0, len(batch), 500):
            res = execute_values(cur, sql, batch[i:i + 500], page_size=500, fetch=True)
            for x in res:
                stats["added" if x["added"] else "updated"] += 1
        cur.execute("SELECT count(*) AS n FROM dataset_lead WHERE dataset_id = %s", (dataset_id,))
        n = cur.fetchone()["n"]
        cur.execute("SELECT columns FROM dataset WHERE id = %s", (dataset_id,))
        prev = cur.fetchone()
        all_cols = list(dict.fromkeys([*((prev or {}).get("columns") or []), *columns]))
        cur.execute("UPDATE dataset SET row_count = %s, columns = %s, last_synced_at = now() WHERE id = %s",
                    (int(n), Json(all_cols), dataset_id))
    return stats


def import_file_into_dataset(conn, dataset_id: str, data: bytes, filename: str) -> dict:
    parsed = parse_lead_file(data, filename)
    return upsert_dataset_rows(conn, dataset_id, parsed["rows"])


def pull_dataset_from_source(conn, dataset: dict) -> dict:
    """Pull rows from the CRM/API endpoint described by the dataset's source_config."""
    cfg = dataset.get("source_config") or {}
    if not cfg.get("url"):
        raise ValueError("dataset has no source URL")
    method = (cfg.get("method") or "GET").upper()
    headers = {"Accept": "application/json"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
        headers["X-Ingest-Token"] = cfg["token"]
    kwargs = {}
    if method == "POST":
        headers["Content-Type"] = "application/json"
        kwargs["data"] = cfg.get("body") or "{}"
    res = requests.request(method, cfg["url"], headers=headers, timeout=30, **kwargs)
    if not 200 <= res.status_code < 300:
        raise ValueError(f"source {res.status_code}: {res.text[:200]}")
    body = res.json()
    if cfg.get("path"):
        for k in cfg["path"].split("."):
            body = body.get(k) if isinstance(body, dict) else None
    if not isinstance(body, list):
        raise ValueError("source did not return an array (set `path` to the array inside the response)")
    rows = []
    for o in body:
        flat = {k: "" if v is None else json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v)
                for k, v in o.items()}
        for variable, field in (cfg.get("map") or {}).items():
            flat[variable] = flat.get(field, "")
        rows.append(flat)
    return upsert_dataset_rows(conn, dataset["id"], rows,
                               {"phone": cfg.get("phone_field"), "name": cfg.get("name_field")})


def dataset_facets(conn, dataset_id: str) -> dict:
    out: dict = {}
    with conn.cursor() as cur:
        for key, col in (("cities", "city"), ("societies", "society"), ("segments", "segment")):
            cur.execute(f"SELECT {col} AS v, count(*) AS n FROM dataset_lead WHERE dataset_id = %s "
                        f"AND {col} IS NOT NULL AND {col} <> '' GROUP BY {col} ORDER BY count(*) DESC LIMIT 60",
                        (dataset_id,))
            out[key] = [{"v": r["v"], "n": int(r["n"])} for r in cur.fetchall()]
        cur.execute("SELECT last_tag AS v, count(*) AS n FROM dataset_lead WHERE dataset_id = %s "
                    "AND last_tag IS NOT NULL GROUP BY last_tag", (dataset_id,))
        out["tags"] = [{"v": r["v"], "n": int(r["n"])} for r in cur.fetchall()]
    return out


def segment_where(dataset_id: str, segment: dict) -> tuple[str, list]:
    """SQL fragment over alias `dl`. Always excludes phones in dnc_phone."""
    w = ["dl.dataset_id = %s", "NOT EXISTS (select 1 from dnc_phone d where d.phone = dl.phone)"]
    p: list = [dataset_id]
    s = segment or {}
    for key, col in (("cities", "city"), ("societies", "society"), ("segments", "segment"), ("last_tag_in", "last_tag")):
        if s.get(key):
            w.append(f"dl.{col} = ANY(%s)")
            p.append(list(s[key]))
    if s.get("last_outcome_in"):
        w.append("dl.last_outcome::text = ANY(%s)")
        p.append(list(s["last_outcome_in"]))
    if s.get("never_called"):
        w.append("dl.last_called_at IS NULL")
    if s.get("max_attempts") is not None:
        w.append("dl.total_attempts <= %s")
        p.append(int(s["max_attempts"]))
    return " AND ".join(w), p


def recently_spoken_phones(conn, phones: list[str], since: datetime, exclude_campaign_id: str | None = None) -> set[str]:
    """Phones with a CONNECTED or VOICEMAIL call started at/after `since` (NOT_PICKED etc. never reached the person)."""
    if not phones:
        return set()
    sql = ("SELECT DISTINCT phone FROM call WHERE phone = ANY(%s) AND started_at >= %s "
           "AND outcome IN ('CONNECTED','VOICEMAIL')")
    params: list = [list(phones), since]
    if exclude_campaign_id:
        sql += " AND (campaign_id IS NULL OR campaign_id <> %s)"
        params.append(exclude_campaign_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {r["phone"] for r in cur.fetchall()}


# The most leads one run takes when neither the caller nor the segment sets a limit (spec: a cap per run).
MAX_RUN_LEADS = 5000


def select_leads_for_run(conn, dept: str, dataset_id: str, segment: dict, *,
                         ignore_cooling: bool = False, limit: int | None = None) -> tuple[list[dict], int]:
    """Segment filter + cross-use-case cooling guard (the department's cooling_days). Returns (leads, cooled_count).
    At most `limit`, else the segment's `limit`, else MAX_RUN_LEADS rows. Raises when the department row is missing."""
    where, params = segment_where(dataset_id, segment)
    with conn.cursor() as cur:
        cur.execute("SELECT cooling_days FROM department WHERE key = %s", (dept,))
        d = cur.fetchone()
        if d is None:
            raise RuntimeError(f"department {dept} not found")
        cooling_days = d["cooling_days"]
        cur.execute(f"SELECT dl.* FROM dataset_lead dl WHERE {where} ORDER BY dl.last_called_at ASC NULLS FIRST LIMIT %s",
                    [*params, limit or (segment or {}).get("limit") or MAX_RUN_LEADS])
        rows = cur.fetchall()
    if not rows or ignore_cooling:
        return rows, 0
    since = datetime.now(timezone.utc) - timedelta(days=cooling_days)
    recent = recently_spoken_phones(conn, [r["phone"] for r in rows], since)
    leads = [r for r in rows if r["phone"] not in recent]
    return leads, len(rows) - len(leads)


def touch_dataset_lead(conn, dataset_lead_id: str | None, phone: str, outcome: str, tag: str | None, when: datetime,
                       *, stale: bool = False) -> None:
    """Mirror a call result onto the dataset row so never-called / last-tag segments stay true. A `stale` call (older
    than the lead's last call) only counts its attempt: the last_* fields belong to the newer call."""
    col, val = ("id", dataset_lead_id) if dataset_lead_id else ("phone", phone)
    with conn.cursor() as cur:
        if stale:
            cur.execute(f"UPDATE dataset_lead SET total_attempts = total_attempts + 1 WHERE {col} = %s", (val,))
            return
        cur.execute(f"UPDATE dataset_lead SET last_called_at = %s, last_outcome = %s, "
                    f"last_tag = coalesce(%s, last_tag), total_attempts = total_attempts + 1 WHERE {col} = %s",
                    (when, outcome, tag, val))


def mark_dnc(conn, phone: str, source: str, reason: str | None = None, user_id: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dnc_phone (phone, reason, source, created_by_id) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (phone) DO NOTHING", (phone, reason, source, user_id))


def queued_in(conn, phone: str) -> list[dict]:
    """Campaigns whose live batch still holds `phone` (its lead there is QUEUED): [{campaign_id, campaign_name,
    scheduled_at}]. Marking a phone DNC never takes it out of a batch Bolna already has; pausing the campaign does."""
    with conn.cursor() as cur:
        cur.execute("SELECT c.id AS campaign_id, c.name AS campaign_name, l.next_dial_at AS scheduled_at "
                    "FROM lead l JOIN campaign c ON c.id = l.campaign_id WHERE l.phone = %s AND l.status = 'QUEUED' "
                    "ORDER BY l.next_dial_at NULLS FIRST, c.id", (phone,))
        return cur.fetchall()


def unmark_dnc(conn, phone: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM dnc_phone WHERE phone = %s", (phone,))


def dnc_phones(conn, phones) -> set[str]:
    phones = list(phones)
    if not phones:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT phone FROM dnc_phone WHERE phone = ANY(%s)", (phones,))
        return {r["phone"] for r in cur.fetchall()}
