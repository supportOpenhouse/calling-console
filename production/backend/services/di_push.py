"""Push finished Supply calls to Direct_Inventory's /api/ai-calls/ingest (NORMALISED shape)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from .. import config
from .classify import TERMINAL_STATUSES, flatten_extracted
from .settings_store import get_setting, set_setting

TIMEOUT = 30
EXTRACTION_WAIT = timedelta(minutes=10)
MAX_FAILURES = 50
# DI is down or not answering: every further call would wait out the timeout too, so the run stops.
UNREACHABLE = (requests.Timeout, requests.ConnectionError)


def di_enabled() -> bool:
    return bool(config.DI_AI_CALLS_URL and config.DI_AI_CALLS_TOKEN)


def _first(fields: dict, *keys):
    for k in keys:
        if fields.get(k):
            return fields[k]
    return None


def _tag_confidence(extracted) -> float | None:
    for group in (extracted or {}).values():
        field = group.get("call_tag") if isinstance(group, dict) else None
        if isinstance(field, dict):
            try:
                return float(field["confidence"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def build_di_payload(call: dict, campaign: dict, lead: dict | None) -> dict:
    fields = flatten_extracted(call.get("extracted"))
    v = (lead or {}).get("variables") or {}
    started = call.get("started_at")
    return {
        "call_id": call["bolna_execution_id"],
        "source": "calling-console",
        "campaign": campaign["name"],
        "batch_id": call.get("bolna_batch_id"),
        "called_at": started.astimezone(timezone.utc).isoformat() if started else None,
        "phone": call["phone"],
        "caller_id": call.get("from_number"),
        "contact": {
            "name": _first(v, "owner_name", "name") or _first(fields, "owner_name"),
            "society": _first(v, "society_name", "society") or _first(fields, "society_and_location", "society"),
            "city": _first(v, "city"),
        },
        "outcome": call["outcome"].lower(),
        "outcome_detail": call.get("hangup_reason"),
        "talk_seconds": call.get("duration_sec"),
        "carrier_blocked": call["outcome"] == "CARRIER_BLOCKED",
        "call_tag": call.get("tag"),
        "tag_confidence": _tag_confidence(call.get("extracted")),
        "property": {
            "configuration": fields.get("configuration"),
            "size_sqft": fields.get("size_sqft"),
            "asking_price_raw": _first(fields, "ask_price", "asking_price"),
            "facing": _first(fields, "exit_facing", "facing"),
        },
        "timeline": _first(fields, "sell_later_period", "timeline"),
        "callback_at": fields.get("callback_time"),
        "summary": call.get("summary"),
        "transcript": call.get("transcript"),
        "recording_url": call.get("recording_url"),
        "cost_usd": call.get("cost"),
    }


def _record_failure(conn, call_id: str, error: str, now: datetime) -> None:
    failures = [f for f in (get_setting(conn, "diPushFailures") or []) if f["call_id"] != call_id]
    failures.append({"call_id": call_id, "error": error, "at": now.isoformat()})
    set_setting(conn, "diPushFailures", failures[-MAX_FAILURES:])


def _clear_failure(conn, call_id: str) -> None:
    failures = get_setting(conn, "diPushFailures") or []
    kept = [f for f in failures if f["call_id"] != call_id]
    if len(kept) != len(failures):
        set_setting(conn, "diPushFailures", kept)


def push_calls_to_di(conn, report: dict, limit: int = 200, now: datetime | None = None) -> int:
    """Push up to `limit` finished Supply calls, oldest first -- calls that failed recently (diPushFailures) last, so
    one DI keeps rejecting never starves the newer ones. A rejection is recorded per call; a timeout or connection
    error stops the run with one error, "di: unreachable — stopping this run"."""
    if not di_enabled():
        return 0
    now = now or datetime.now(timezone.utc)
    failed = [f["call_id"] for f in get_setting(conn, "diPushFailures") or []]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.*, to_jsonb(cm) AS _campaign, to_jsonb(l) AS _lead FROM call c "
            "JOIN campaign cm ON cm.id = c.campaign_id LEFT JOIN lead l ON l.id = c.lead_id "
            "WHERE cm.vertical = 'SUPPLY' AND c.pushed_to_di_at IS NULL "
            "AND lower(c.status) = ANY(%s) "
            "AND NOT (c.outcome = 'CONNECTED' AND c.tag IS NULL AND COALESCE(c.started_at > %s, FALSE)) "
            "ORDER BY c.bolna_execution_id = ANY(%s::text[]), c.started_at LIMIT %s",
            (sorted(TERMINAL_STATUSES), now - EXTRACTION_WAIT, failed, limit))
        rows = cur.fetchall()
    pushed = 0
    for row in rows:
        campaign, lead = row.pop("_campaign"), row.pop("_lead")
        cid = row["bolna_execution_id"]
        try:
            res = requests.post(config.DI_AI_CALLS_URL, json=build_di_payload(row, campaign, lead),
                                headers={"X-AI-Calls-Token": config.DI_AI_CALLS_TOKEN}, timeout=TIMEOUT)
            error = None if 200 <= res.status_code < 300 else f"{res.status_code} {res.text[:200]}"
        except UNREACHABLE as e:
            report["errors"].append("di: unreachable — stopping this run")
            _record_failure(conn, cid, str(e), now)
            conn.commit()
            break
        except requests.RequestException as e:
            error = str(e)
        if error:
            report["errors"].append(f"di {cid}: {error}")
            _record_failure(conn, cid, error, now)
        else:
            with conn.cursor() as cur:
                cur.execute("UPDATE call SET pushed_to_di_at = %s WHERE id = %s", (now, row["id"]))
            _clear_failure(conn, cid)
            pushed += 1
        conn.commit()
    return pushed
