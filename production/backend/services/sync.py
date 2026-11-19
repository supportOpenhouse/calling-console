"""The 5-minute sync job (port of the old lib/sync.ts): pull Bolna call results, roll them up to leads, apply the
result rules, redial, fire triggers, start calendar runs, archive recordings and push Supply calls to
Direct_Inventory.

`run_sync` runs the steps in order: agents (full sync only), batches, executions, rollup, triggers, scheduler,
recordings, di. A step that raises is rolled back and recorded in `report["errors"]` as "<step>: <msg>", and the
next step still runs. Inside the steps every agent, batch, execution, campaign and recording is handled on its
own: a failure is rolled back, recorded as "<step> <id>: <msg>" and the loop continues.

One sync runs at a time: `run_sync` takes `sync_lock` (advisory xact lock LOCK_KEY in a transaction held open on
a dedicated connection). The background thread of every gunicorn worker (`start_background_sync`, called from
wsgi.py), the CLI (scripts/run_sync.py) and "Sync now" (POST /api/sync) all go through it.

Unlike the other services these functions commit as they go (each call, batch and campaign on its own), so one
failure never takes the others' writes with it. The exceptions are `close_stale_queued` (the caller commits) and
the pure `lead_update_from_call`.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from time import sleep

import psycopg2
from psycopg2.extras import Json

from .. import config
from ..db import get_conn
from . import bolna, di_push, redial, scheduler, storage, workflows
from .classify import (
    DNC_TAGS, TERMINAL_STATUSES, _parse_utc, classify_execution, is_terminal_status, lead_status_for, normalize_phone,
)
from .datasets import mark_dnc
from .ist import IST, in_window, next_window_start
from .settings_store import get_setting, set_setting

log = logging.getLogger("sync")

LOCK_KEY = 42100001
SYNC_SECONDS = 300
FULL_SYNC_HOUR_IST = 3  # the first tick after 03:00 IST each night is a full sync
PAGE_SIZE = 50
MAX_PAGES = 40  # per agent and run, normal and full
# Each agent's executions are paged back to its last full sync minus this overlap (FIRST_LOOKBACK without one):
# calls last about 10 minutes at most, plus the extraction lag.
LOOKBACK_OVERLAP = timedelta(minutes=45)
FIRST_LOOKBACK = timedelta(hours=48)
# create_round writes a 'creating' batch just before it calls Bolna; one still 'creating' after this is stuck.
STUCK_CREATING_AFTER = timedelta(minutes=10)
# Bolna can keep saying 'scheduled' after the start time: past this the batch counts as running.
SCHEDULED_GRACE = timedelta(minutes=2)
# Bolna sometimes never flips a batch to completed: a running batch this long past its start, with no new call
# for BATCH_IDLE, counts as completed.
BATCH_MAX_RUNTIME = timedelta(hours=3)
BATCH_IDLE = timedelta(minutes=30)
# Bolna never confirmed a batch completed on that idle rule, so it may still be dialling. Its campaign's QUEUED
# leads are closed once Bolna reports a final status, or once Bolna's last status is running and the idle rule first
# completed the batch (IDLE_COMPLETED_AT) more than this long ago -- never on any other status (unknown, missing,
# paused, scheduled), however old; such a batch is polled every sync until Bolna reports a final status or resumes
# it.
IDLE_TRUST_AFTER = timedelta(hours=24)
# Bolna batch status -> ours. 'paused' stays active (a paused batch can resume and dial). Anything else is unknown.
BOLNA_BATCH_STATUS = {
    "created": "created",
    "scheduled": "scheduled", "queued": "scheduled", "paused": "scheduled",
    "running": "running", "in-progress": "running", "in_progress": "running", "processing": "running",
    "started": "running",
    "completed": "completed", "executed": "completed", "finished": "completed", "done": "completed",
    "stopped": "stopped", "cancelled": "stopped", "canceled": "stopped",
    "failed": "failed", "error": "failed",
}
# Bolna statuses that confirm a batch will not dial again, and those that mean it is dialling.
_BOLNA_FINAL = sorted(k for k, v in BOLNA_BATCH_STATUS.items() if v in ("completed", "stopped", "failed"))
_BOLNA_RUNNING = sorted(k for k, v in BOLNA_BATCH_STATUS.items() if v == "running")
# A batch paused at Bolna can resume and dial, so it stays active; once paused this long it is reported. When we
# first saw the pause is kept in the batch's stats under this key.
PAUSED_REPORT_AFTER = timedelta(hours=24)
PAUSED_SINCE = "console_paused_since"
# When the idle rule first completed the batch (ISO UTC), kept in its stats across polls (see IDLE_TRUST_AFTER).
IDLE_COMPLETED_AT = "console_idle_completed_at"
# A batch in an active status that Bolna still reports not running (scheduled, queued, created) this long past its
# start is reported. It never idle-completes, so its leads stay QUEUED; a paused batch has the report above instead.
NOT_RUNNING_REPORT_AFTER = timedelta(hours=24)
# A batch the console stopped -- at its window end, or on pause/stop (stats key, console_stop_stamp): close_stale_queued
# hands the leads it never reached back as NOT_CONNECTED without using an attempt, but only once the campaign agent's
# executions were fetched from a time after the stamp (before that, calls Bolna made up to the stop may be missing).
CONSOLE_STOP = "console_stopped"
# Bolna may still place a call for a moment after stop_batch returns: a console stop counts as this much later.
STOP_SETTLE = timedelta(minutes=2)
# retry_rules handles at most this many calls per run.
RULES_RETRY_LIMIT = 200
# A QUEUED lead held this long by a call Bolna never finished is reported.
STUCK_CALL_AFTER = timedelta(hours=24)
# Campaigns whose agents are synced, and whose leads a call without a known batch may be linked to.
LIVE_CAMPAIGN_STATUSES = ["RUNNING", "SCHEDULED", "PAUSED"]
ROLLUP_CAMPAIGN_STATUSES = ["RUNNING", "SCHEDULED"]
# Halted campaigns whose QUEUED leads (of a batch stopped on pause/stop) close_stale_queued still closes.
HALTED_CAMPAIGN_STATUSES = ["PAUSED", "STOPPED"]
# Lead statuses that become DONE when their campaign completes.
FINISHED_LEAD_STATUSES = ["NOT_CONNECTED", "BLOCKED", "VOICEMAIL", "FAILED", "CONNECTED"]
# The background thread refuses to start without these (an unset one would silently skip its step).
REQUIRED_CONFIG = ("BOLNA_API_KEY", "R2_BLOB_PHONE_CALLS_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                   "R2_PHONE_CALLS_PUBLIC_URL", "DI_AI_CALLS_URL", "DI_AI_CALLS_TOKEN")

_TERMINAL = sorted(TERMINAL_STATUSES)
_CALL_COLUMNS = ("agent_id", "campaign_id", "lead_id", "batch_id", "bolna_batch_id", "phone", "from_number",
                 "status", "outcome", "hangup_code", "hangup_reason", "duration_sec", "human_turns", "cost",
                 "started_at", "transcript", "recording_url", "extracted", "tag", "summary", "raw")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def console_stop_stamp(stopped_at: datetime) -> str:
    """The CONSOLE_STOP value of a batch whose stop_batch returned at `stopped_at`."""
    return _iso(stopped_at + STOP_SETTLE)


def _msg(e: Exception) -> str:
    return str(e).strip() or type(e).__name__


def _record(report: dict, entry: str) -> None:
    report["errors"].append(entry)
    log.warning("sync: %s", entry)


def _bolna_status(stats) -> str | None:
    """Our status for the last status Bolna reported (kept in batch.stats), or None: never polled, or unknown."""
    raw = stats.get("status") if isinstance(stats, dict) else None
    return BOLNA_BATCH_STATUS.get(raw.lower()) if isinstance(raw, str) else None


def norm_batch_status(s) -> str:
    """Our status for a Bolna batch status. Raises ValueError for one we do not know (missing included): an
    unknown status must never be read as 'not dialling'."""
    v = s.lower() if isinstance(s, str) else None
    if v not in BOLNA_BATCH_STATUS:
        raise ValueError(f"unknown Bolna status {s}")
    return BOLNA_BATCH_STATUS[v]


def new_report() -> dict:
    return {
        "started_at": _iso(_utcnow()), "finished_at": None, "full": False, "agents": 0, "batches_checked": 0,
        "executions_seen": 0, "calls_upserted": 0, "recordings_archived": 0, "redial_rounds": 0,
        "campaigns_completed": 0, "runs_started": 0, "triggers_fired": 0, "rules_applied": 0, "di_pushed": 0,
        "errors": [], "window_stops": [],
    }


# ---------------------------------------------------------------- 1. agents

def sync_agents(conn) -> int:
    """Upsert every Bolna agent (new ones default to SUPPLY; admins fix the vertical). Each agent is read in full
    and committed on its own; if any could not be read, the others are still saved and this raises naming them --
    an agent is never saved from the abbreviated list entry, which could wipe its template variables."""
    synced, failed = 0, []
    for a in bolna.list_agents():
        bolna_id = a.get("agent_id") or a.get("id")
        if not bolna_id:
            continue
        try:
            full = bolna.get_agent(bolna_id)
            task = (full.get("tasks") or [{}])[0] or {}
            tools = task.get("tools_config") or {}
            cfg = {
                "llm": (tools.get("llm_agent") or {}).get("llm_config"),
                "transcriber": tools.get("transcriber"),
                "synthesizer": tools.get("synthesizer"),
                "task": task.get("task_config"),
                "webhook_url": full.get("webhook_url"),
                "prompt": ((full.get("agent_prompts") or {}).get("task_1") or {}).get("system_prompt"),
            }
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent (bolna_agent_id, name, variables, welcome_message, config, last_synced_at) "
                    "VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT (bolna_agent_id) DO UPDATE SET "
                    "name = EXCLUDED.name, variables = EXCLUDED.variables, welcome_message = EXCLUDED.welcome_message, "
                    "config = EXCLUDED.config, last_synced_at = EXCLUDED.last_synced_at",
                    (bolna_id, full.get("agent_name") or a.get("agent_name") or bolna_id,
                     Json(bolna.agent_template_variables(full)), full.get("agent_welcome_message"), Json(cfg)))
            conn.commit()
            synced += 1
        except Exception as e:
            conn.rollback()
            failed.append(f"{bolna_id}: {_msg(e)}")
    if failed:
        raise RuntimeError(f"{len(failed)} agent(s) not synced: " + "; ".join(failed))
    return synced


# ---------------------------------------------------------------- 2. batches

def sync_batches(conn, report: dict, now: datetime) -> None:
    """Refresh from Bolna every batch that may still dial, and every batch completed on the idle rule that Bolna may
    still be dialling (`_unconfirmed_idle_batches`: while under IDLE_TRUST_AFTER old if Bolna's last status is
    running, at any age otherwise), so a resume or a final status is seen. Each poll stores Bolna's response in
    the batch's stats. Reports, on every sync:
    - batches stuck in 'creating' (they block auto-redial; their leads are left QUEUED because Bolna may hold the
      batch): "batch <id> stuck in creating since <ts>";
    - batches paused at Bolna for over PAUSED_REPORT_AFTER: "batch <id> paused at Bolna since <ts>";
    - other batches in an active status that Bolna still reports not running NOT_RUNNING_REPORT_AFTER past their
      start: "batch <id> still <Bolna status> at Bolna since <scheduled_at>";
    - a status Bolna reports that we do not know: "batch <bolna id>: unknown Bolna status <v>". The stored status
      is kept (an active batch keeps being polled) and the unknown status goes into stats, so the idle rule, which
      needs Bolna's last status to be running, never completes the batch.
    Bolna's answer replaces the stats; the idle rule's completion stamp (IDLE_COMPLETED_AT) is carried over, so a
    batch Bolna keeps reporting running is trusted a day after its first idle completion, not never."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, created_at FROM batch WHERE status = 'creating' AND created_at < %s "
                    "ORDER BY created_at, id", (now - STUCK_CREATING_AFTER,))
        for b in cur.fetchall():
            _record(report, f"batch {b['id']} stuck in creating since {_iso(b['created_at'])}")
        cur.execute("SELECT * FROM batch WHERE status = ANY(%s) AND bolna_batch_id IS NOT NULL "
                    "ORDER BY created_at, id", (redial.ACTIVE_BATCH_STATUSES,))
        rows = cur.fetchall()
        rows += [b for b in _unconfirmed_idle_batches(cur, None, now) if b["bolna_batch_id"]]
    conn.commit()
    for b in rows:
        report["batches_checked"] += 1
        try:
            remote = bolna.get_batch(b["bolna_batch_id"])
            raw = remote.get("status")
            stats = dict(remote)
            prev = b["stats"] if isinstance(b["stats"], dict) else {}
            if prev.get(IDLE_COMPLETED_AT):
                stats[IDLE_COMPLETED_AT] = prev[IDLE_COMPLETED_AT]
            try:
                bolna_status = norm_batch_status(raw)
            except ValueError as e:  # never read as 'not dialling': keep our status, store Bolna's
                _record(report, f"batch {b['bolna_batch_id']}: {_msg(e)}")
                status = b["status"]
            else:
                status = bolna_status
                if status == "scheduled" and b["scheduled_at"] and b["scheduled_at"] < now - SCHEDULED_GRACE:
                    status = "running"
                if _is_paused(stats):
                    since = prev.get(PAUSED_SINCE) if _is_paused(prev) and prev.get(PAUSED_SINCE) else _iso(now)
                    stats[PAUSED_SINCE] = since
                    if _parse_utc(since) < now - PAUSED_REPORT_AFTER:
                        _record(report, f"batch {b['id']} paused at Bolna since {since}")
                elif (bolna_status != "running" and status in redial.ACTIVE_BATCH_STATUSES and b["scheduled_at"]
                      and b["scheduled_at"] < now - NOT_RUNNING_REPORT_AFTER):
                    _record(report, f"batch {b['id']} still {raw} at Bolna since {_iso(b['scheduled_at'])}")
            with conn.cursor() as cur:
                # unless pause/stop changed the batch since it was read (its CONSOLE_STOP must stay)
                cur.execute("UPDATE batch SET status = %s, stats = %s, last_synced_at = %s WHERE id = %s "
                            "AND status = %s", (status, Json(stats), now, b["id"], b["status"]))
            conn.commit()
        except Exception as e:
            conn.rollback()
            _record(report, f"batch {b['bolna_batch_id']}: {_msg(e)}")


def _is_paused(stats: dict) -> bool:
    raw = stats.get("status")
    return isinstance(raw, str) and raw.lower() == "paused"


# ---------------------------------------------------------------- 3. executions

def lead_update_from_call(lead: dict, c: dict, *, first_link: bool, rules_applied: bool,
                          terminal: bool) -> dict | None:
    """The lead columns to write for call `c` (a classify_execution result), or None to leave the lead alone.

    Executions arrive newest first and are seen again on later syncs, so an older call never overwrites a newer
    one; it only counts its attempt the first time it is linked. The call the lead already shows rewrites it only
    until its rules have run (they may have changed the lead), and never takes a QUEUED lead out of the batch it
    was queued into after that call (create_round stamped next_dial_at; a lead QUEUED by this same call while it
    was in progress has next_dial_at cleared, and does move on once the call finishes). BALANCE_LOW never counts
    as an attempt and leaves the lead PENDING; DONE and DNC stay.
    """
    inc = 1 if first_link and c["outcome"] != "BALANCE_LOW" else 0
    attempts = lead["attempts"] + inc
    started, last = c["started_at"], lead["last_call_at"]
    newer = last is None or started is None or started > last
    same = started == last
    if not (newer or (same and not rules_applied)):
        return {"attempts": attempts} if inc else None
    status = lead_status_for(c["outcome"], c["tag"]) if terminal else "QUEUED"
    if c["outcome"] == "BALANCE_LOW":
        status = "PENDING"
    if lead["status"] in ("DONE", "DNC") or (
            not newer and lead["status"] == "QUEUED" and lead["next_dial_at"] is not None):
        status = lead["status"]
    return {"attempts": attempts, "last_call_at": started, "last_outcome": c["outcome"], "status": status,
            "final_tag": c["tag"] or lead["final_tag"], "next_dial_at": None if newer else lead["next_dial_at"]}


def _link(cur, e: dict, phone: str, agent: dict | None, started: datetime | None, existing: dict | None):
    """(batch_id, campaign_id, lead_id) for an execution.

    A call keeps the lead it was first linked to: re-resolving by phone on a later sync could move it to another
    campaign, or unlink it once its campaign finished. Otherwise: through its Bolna batch; failing that (a call
    without a batch, or a batch Bolna knows but we never recorded) the lead with this phone in the newest live
    campaign of the agent -- one that had started (started_at set) at or before the call's start, so history calls
    (backfill, full sync) are never pulled into a new or calendar-queued campaign.
    """
    batch = None
    if e.get("batch_id"):
        cur.execute("SELECT id, campaign_id FROM batch WHERE bolna_batch_id = %s", (e["batch_id"],))
        batch = cur.fetchone()
    batch_id = batch["id"] if batch else (existing or {}).get("batch_id")
    if existing and existing["lead_id"]:
        return batch_id, existing["campaign_id"], existing["lead_id"]
    if batch:
        cur.execute("SELECT id FROM lead WHERE campaign_id = %s AND phone = %s", (batch["campaign_id"], phone))
        lead = cur.fetchone()
        return batch_id, batch["campaign_id"], lead["id"] if lead else None
    if agent:
        cur.execute(
            "SELECT l.id, l.campaign_id FROM lead l JOIN campaign c ON c.id = l.campaign_id "
            "WHERE c.agent_id = %s AND l.phone = %s AND c.status::text = ANY(%s) "
            "AND c.started_at IS NOT NULL AND %s::timestamptz IS NOT NULL AND c.started_at <= %s "
            "ORDER BY c.created_at DESC, c.id DESC LIMIT 1",
            (agent["id"], phone, LIVE_CAMPAIGN_STATUSES, started, started))
        lead = cur.fetchone()
        if lead:
            return batch_id, lead["campaign_id"], lead["id"]
    return batch_id, None, None


def upsert_execution(conn, e: dict, agent: dict | None = None, now=None, *, report: dict | None = None,
                     attempted: set[str] | None = None) -> str:
    """Save one Bolna execution as a call, roll it up to its lead and apply the result rules. Returns "inserted" or
    "updated".

    A finished call linked to no campaign (a test call, a call of no live campaign) whose tag is in DNC_TAGS puts
    the phone on the DNC list (source 'call_tag'); a linked one gets that from its rules.

    The call and lead writes commit first; then `apply_result_rules` stamps `rules_applied_at` and commits (it
    waits for extraction on untagged connected calls, so a later sync applies them). A call older than the lead's
    last call is applied `stale`: its redial / stop rules never undo what the newer call decided. A rule failure is
    rolled back so the next sync retries it. With `report`, the failure and every ":error:" entry the rules return
    are recorded there (and a call with rules applied is counted); without one they raise. With `attempted`, the
    call's id is added to it once its rules are tried (retry_rules skips those for the rest of the run).
    """
    now = now or _utcnow()
    c = classify_execution(e)
    phone = normalize_phone(c["phone"]) or c["phone"] or "unknown"
    with conn.cursor() as cur:
        if agent is None and e.get("agent_id"):
            cur.execute("SELECT * FROM agent WHERE bolna_agent_id = %s", (e["agent_id"],))
            agent = cur.fetchone()
        cur.execute("SELECT id, campaign_id, lead_id, batch_id, rules_applied_at FROM call "
                    "WHERE bolna_execution_id = %s FOR UPDATE", (e["id"],))
        existing = cur.fetchone()
        batch_id, campaign_id, lead_id = _link(cur, e, phone, agent, c["started_at"], existing)
        values = {
            "agent_id": agent["id"] if agent else None, "campaign_id": campaign_id, "lead_id": lead_id,
            "batch_id": batch_id, "bolna_batch_id": e.get("batch_id"), "phone": phone,
            "from_number": c["from_number"], "status": e.get("status"), "outcome": c["outcome"],
            "hangup_code": c["hangup_code"], "hangup_reason": c["hangup_reason"], "duration_sec": c["duration_sec"],
            "human_turns": c["human_turns"], "cost": c["cost"], "started_at": c["started_at"],
            "transcript": c["transcript"], "recording_url": c["recording_url"],
            "extracted": Json(c["extracted"]) if c["extracted"] is not None else None, "tag": c["tag"],
            "summary": c["summary"], "raw": Json(e),
        }
        params = [values[k] for k in _CALL_COLUMNS]
        if existing:
            cur.execute(f"UPDATE call SET {', '.join(f'{k} = %s' for k in _CALL_COLUMNS)}, updated_at = now() "
                        "WHERE id = %s", [*params, existing["id"]])
            result = "updated"
        else:
            cur.execute(f"INSERT INTO call (bolna_execution_id, {', '.join(_CALL_COLUMNS)}) "
                        f"VALUES (%s, {', '.join(['%s'] * len(_CALL_COLUMNS))})", [e["id"], *params])
            result = "inserted"
        lead = None
        if lead_id:
            cur.execute("SELECT * FROM lead WHERE id = %s FOR UPDATE", (lead_id,))
            lead = cur.fetchone()
            upd = lead_update_from_call(lead, c, first_link=not (existing and existing["lead_id"]),
                                        rules_applied=bool(existing and existing["rules_applied_at"]),
                                        terminal=is_terminal_status(e.get("status")))
            if upd:
                cur.execute(f"UPDATE lead SET {', '.join(f'{k} = %s' for k in upd)} WHERE id = %s RETURNING *",
                            [*upd.values(), lead_id])
                lead = cur.fetchone()
    # Every finished call (a late tag too: rules may have run before it arrived), not only unlinked ones.
    if (phone != "unknown" and is_terminal_status(e.get("status"))
            and (c["tag"] or "").lower() in DNC_TAGS):
        mark_dnc(conn, phone, "call_tag", reason=c["tag"])
    conn.commit()

    if campaign_id:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM call WHERE bolna_execution_id = %s", (e["id"],))
            call = cur.fetchone()
            cur.execute("SELECT * FROM campaign WHERE id = %s", (campaign_id,))
            campaign = cur.fetchone()
        _apply_rules(conn, call, campaign, lead, now, report, attempted)
    return result


def _apply_rules(conn, call: dict, campaign: dict, lead: dict | None, now: datetime, report: dict | None,
                 attempted: set[str] | None) -> None:
    """apply_result_rules for one call -- `stale` when it started before the lead's last call, so its redial / stop
    rules never undo the newer call. A failure is rolled back (the next sync retries). With `report` the failure,
    every ":error:" entry and every "dnc:queued_in:" entry (a phone just put on the DNC list that a live batch still
    holds: someone must pause that campaign) are recorded there and a call with rules applied is counted; without
    one they raise.
    The call's id is added to `attempted` (when given) before the rules run."""
    started, last = call["started_at"], (lead or {}).get("last_call_at")
    stale = bool(started and last and started < last)
    if attempted is not None:
        attempted.add(call["id"])
    try:
        done = workflows.apply_result_rules(conn, call, campaign, lead, now=now, stale=stale)
    except Exception as err:
        conn.rollback()
        if report is None:
            raise
        _record(report, f"rules {call['bolna_execution_id']}: {_msg(err)}")
        return
    errors = [d for d in done if ":error:" in d or d.startswith("dnc:queued_in:")]  # a DNC phone a batch still holds
    if report is None:
        if errors:
            raise RuntimeError(f"rules {call['bolna_execution_id']}: " + "; ".join(errors))
        return
    if done:
        report["rules_applied"] += 1
    for d in errors:
        _record(report, f"rules {call['bolna_execution_id']}: {d}")


def retry_rules(conn, report: dict, now: datetime, limit: int = RULES_RETRY_LIMIT, *, attempted: set[str]) -> None:
    """Apply the rules of finished campaign calls that still have none (oldest first, up to `limit`): calls whose
    rules failed, or that left the executions window during the extraction wait. Only calls past that wait; one
    without a start time counts as past it (and as the oldest). `attempted` holds the calls whose rules were tried
    earlier in this run (the executions step adds them): those are skipped, so a lasting failure is reported once
    per run. The calls tried here are added to it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT k.* FROM call k JOIN campaign cm ON cm.id = k.campaign_id "
            "WHERE k.rules_applied_at IS NULL AND lower(k.status) = ANY(%s) "
            "AND coalesce(k.started_at, '-infinity') < %s AND k.id <> ALL(%s::text[]) "
            "ORDER BY k.started_at NULLS FIRST, k.id LIMIT %s",
            (_TERMINAL, now - workflows.EXTRACTION_WAIT, sorted(attempted), limit))
        rows = cur.fetchall()
    conn.commit()
    for call in rows:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM campaign WHERE id = %s", (call["campaign_id"],))
            campaign = cur.fetchone()
            lead = None
            if call["lead_id"]:
                cur.execute("SELECT * FROM lead WHERE id = %s", (call["lead_id"],))
                lead = cur.fetchone()
        _apply_rules(conn, call, campaign, lead, now, report, attempted)


def cursor_key(agent_id: str) -> str:
    """The setting holding when an agent's executions last synced fully (ISO UTC)."""
    return f"lastExecutionSyncAt:{agent_id}"


def sync_executions(conn, report: dict, *, full: bool = False, max_pages: int | None = None,
                    now=None, attempted: set[str] | None = None) -> set[str]:
    """Page every agent with a live campaign (every agent on a full sync) newest first and upsert each execution.

    Each agent pages back to its own lookback: its cursor (`cursor_key`) minus LOOKBACK_OVERLAP, or FIRST_LOOKBACK
    when it has none. A normal sync stops there; a full sync keeps paging (still upserting) to backfill, up to
    `max_pages`. `max_pages=None` means no cap (the backfill); run_sync passes MAX_PAGES.

    Returns the ids of the agents synced fully -- no agent or execution error, and paging passed the lookback or
    reached the last page -- and moves only their cursors to `now`. Only for those may rollup take "no call in the
    database" to mean "never dialled". An agent that hits the cap before its lookback is reported ("executions
    <name> (<bolna id>): capped at <N> pages before reaching lookback") and keeps its cursor; so does one whose
    cursor could not be saved (the others carry on). `attempted` is passed to upsert_execution.
    """
    now = now or _utcnow()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM agent ORDER BY created_at, id")
        agents = cur.fetchall()
        cur.execute("SELECT DISTINCT agent_id FROM campaign c WHERE c.status::text = ANY(%s) "
                    "OR EXISTS (SELECT 1 FROM lead l WHERE l.campaign_id = c.id AND l.status = 'QUEUED')",
                    (LIVE_CAMPAIGN_STATUSES,))  # a stopped campaign's agent until its QUEUED leads are closed
        live = {r["agent_id"] for r in cur.fetchall()}
    conn.commit()
    synced: set[str] = set()
    for agent in agents:
        if not full and agent["id"] not in live:
            continue
        report["agents"] += 1
        label = f"{agent['name']} ({agent['bolna_agent_id']})"
        ok, passed = True, False
        try:
            last = get_setting(conn, cursor_key(agent["id"]))
            since = _parse_utc(last) - LOOKBACK_OVERLAP if last else now - FIRST_LOOKBACK
            page = 1
            while max_pages is None or page <= max_pages:
                items, has_more = bolna.list_agent_executions(agent["bolna_agent_id"], page, PAGE_SIZE)
                if not items:
                    passed = True
                    break
                for e in items:
                    report["executions_seen"] += 1
                    created = _parse_utc(e.get("created_at"))
                    if created is not None and created < since:
                        passed = True
                        if not full:
                            continue
                    try:
                        upsert_execution(conn, e, agent, now, report=report, attempted=attempted)
                        report["calls_upserted"] += 1
                    except Exception as err:
                        conn.rollback()
                        ok = False
                        _record(report, f"execution {e.get('id')}: {_msg(err)}")
                if not has_more:
                    passed = True
                    break
                if passed and not full:
                    break
                page += 1
            if ok and passed:
                set_setting(conn, cursor_key(agent["id"]), _iso(now))
                conn.commit()
                synced.add(agent["id"])
            elif ok:
                _record(report, f"executions {label}: capped at {max_pages} pages before reaching lookback")
        except Exception as err:
            conn.rollback()
            _record(report, f"executions {label}: {_msg(err)}")
    return synced


# ---------------------------------------------------------------- 4. rollup

def _unconfirmed_idle_batches(cur, campaign_id: str | None, now: datetime) -> list[dict]:
    """Batches (of one campaign, or all with None) completed on the idle rule that Bolna may still be dialling:
    close_stale_queued waits for them and sync_batches keeps polling them.

    Completed on the idle rule: status 'completed', Bolna's last status (stats) is not final (completed, stopped,
    failed) and not every contact (phone) has a finished call. Of those, only a batch whose last Bolna status is
    running and that the idle rule first completed (IDLE_COMPLETED_AT) more than IDLE_TRUST_AFTER ago is left out
    (trusted); every other status -- unknown, missing, paused, scheduled -- is kept, however old the batch, and so is
    a batch without the stamp."""
    cur.execute(
        "SELECT b.* FROM batch b WHERE (%s::text IS NULL OR b.campaign_id = %s) AND b.status = 'completed' "
        "AND lower(coalesce(b.stats->>'status', '')) <> ALL(%s) "
        "AND NOT (lower(coalesce(b.stats->>'status', '')) = ANY(%s) "
        f"         AND coalesce((b.stats->>'{IDLE_COMPLETED_AT}')::timestamptz < %s, false)) "
        "AND (SELECT count(DISTINCT k.phone) FROM call k WHERE k.batch_id = b.id AND lower(k.status) = ANY(%s)) "
        "    < b.total "
        "ORDER BY b.scheduled_at, b.id",
        (campaign_id, campaign_id, _BOLNA_FINAL, _BOLNA_RUNNING, now - IDLE_TRUST_AFTER, _TERMINAL))
    return cur.fetchall()


def close_stale_queued(conn, campaign_id: str, now: datetime) -> int:
    """QUEUED leads Bolna never dialled -> FAILED, a used attempt; those of a batch the console stopped (CONSOLE_STOP) ->
    NOT_CONNECTED with last_outcome NULL and no attempt used (auto-redial takes them in the next window). Returns how
    many. Does not commit. The caller must have synced the campaign agent's executions fully this run (else "no call"
    proves nothing) -- and since _rollup_campaign stops batches at the window end only after calling this, a
    batch stopped at the window end is closed on a later sync, whose executions include every call it made.

    Only when no batch of the campaign may still dial: none creating/created/scheduled/running (a stuck 'creating'
    batch keeps its leads QUEUED), none the console stopped (CONSOLE_STOP) at or after the agent's cursor, and none completed on the idle rule unless Bolna has since reported a final
    status, or its last Bolna status is running and its idle completion is more than IDLE_TRUST_AFTER ago
    (`_unconfirmed_idle_batches`). A lead is
    stale once its batch start (create_round stamps it in next_dial_at) has passed and no call reached it since; a
    lead without that stamp, only when it has no call at all. A newer call clears next_dial_at, so a lead whose
    call is still in progress is left alone.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM batch WHERE campaign_id = %s AND status = ANY(%s) LIMIT 1",
                    (campaign_id, redial.ACTIVE_BATCH_STATUSES))
        if cur.fetchone() or _unconfirmed_idle_batches(cur, campaign_id, now):
            return 0
        cur.execute(f"SELECT max((stats->>'{CONSOLE_STOP}')::timestamptz) AS t FROM batch "
                    "WHERE campaign_id = %s AND status = 'stopped'", (campaign_id,))
        last_stop = cur.fetchone()["t"]
        if last_stop is not None:  # the agent's cursor: when this run began fetching its executions
            cur.execute("SELECT agent_id FROM campaign WHERE id = %s", (campaign_id,))
            fetched = get_setting(conn, cursor_key(cur.fetchone()["agent_id"]))
            if fetched is None:
                raise RuntimeError(f"campaign {campaign_id}: its agent has no executions cursor")
            if last_stop >= datetime.fromisoformat(fetched):
                return 0
        cur.execute(
            "UPDATE lead l SET status = 'NOT_CONNECTED', last_outcome = NULL, next_dial_at = NULL "
            "WHERE l.campaign_id = %s AND l.status = 'QUEUED' AND l.next_dial_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM batch b WHERE b.campaign_id = l.campaign_id AND b.status = 'stopped' "
            f"           AND b.stats ? '{CONSOLE_STOP}' AND b.scheduled_at = l.next_dial_at) "
            "AND NOT EXISTS (SELECT 1 FROM call k WHERE k.lead_id = l.id AND k.started_at >= l.next_dial_at) "
            "RETURNING l.id", (campaign_id,))
        window_stopped = len(cur.fetchall())
        cur.execute(
            "UPDATE lead l SET status = 'FAILED', last_outcome = 'FAILED', attempts = l.attempts + 1, "
            "next_dial_at = NULL "
            "WHERE l.campaign_id = %s AND l.status = 'QUEUED' AND (l.next_dial_at IS NULL OR l.next_dial_at <= %s) "
            "AND NOT EXISTS (SELECT 1 FROM call k WHERE k.lead_id = l.id "
            "                AND (l.next_dial_at IS NULL OR k.started_at >= l.next_dial_at)) RETURNING l.id",
            (campaign_id, now))
        return window_stopped + len(cur.fetchall())


def _stop_at_window_end(conn, campaign: dict, now: datetime, report: dict) -> None:
    """Stop every live batch (running or scheduled, known to Bolna) of `campaign` that has started (scheduled_at at or
    before now) while its calling window is closed -- a REDIAL batch's window is the campaign's redial policy, any other
    the department's -- so nobody is called outside the window. Left alone: a batch scheduled for later (the next
    window start), and any batch while the window opens within scheduler.DISPATCH_LEAD (a calendar run is started
    that early). Each stop commits, and is listed in report["window_stops"] as "batch <id>: stopped at window end";
    the batch becomes 'stopped' with stats CONSOLE_STOP, and close_stale_queued hands its untouched leads back on a
    later sync. A stop Bolna refuses is an error and the batch stays live (tried again next sync)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM batch WHERE campaign_id = %s AND status IN ('running', 'scheduled') "
                    "AND bolna_batch_id IS NOT NULL ORDER BY created_at, id", (campaign["id"],))
        live = cur.fetchall()
    conn.commit()
    if not live:
        return
    policy, dept = redial.policy_of(campaign), scheduler.dept_config(conn, campaign["vertical"])
    for b in live:
        w = policy if b["kind"] == "REDIAL" else dept
        start, end = w["window_start"], w["window_end"]
        if (in_window(now, start, end) or next_window_start(now, start, end) - now <= scheduler.DISPATCH_LEAD
                or (b["scheduled_at"] is not None and b["scheduled_at"] > now)):
            continue
        try:
            bolna.stop_batch(b["bolna_batch_id"])
        except Exception as e:
            _record(report, f"batch {b['id']}: stop at window end failed: {_msg(e)}")
            continue
        stats = {**(b["stats"] if isinstance(b["stats"], dict) else {}), CONSOLE_STOP: console_stop_stamp(now)}
        with conn.cursor() as cur:
            cur.execute("UPDATE batch SET status = 'stopped', stats = %s WHERE id = %s AND status = %s",
                        (Json(stats), b["id"], b["status"]))  # a pause/stop meanwhile keeps its own stamp
        conn.commit()
        report["window_stops"].append(f"batch {b['id']}: stopped at window end")
        log.info("sync: batch %s (Bolna %s) stopped at window end", b["id"], b["bolna_batch_id"])


def _rollup_batches(cur, campaign_id: str, now: datetime) -> None:
    """Batch counters from calls, and completion of batches Bolna never flips: every contact (phone) has a finished
    call, or (the idle rule) a batch Bolna last reported running that is BATCH_MAX_RUNTIME past its start with no
    call for BATCH_IDLE. A batch Bolna reports paused or scheduled, or with a status we do not know, or that it never
    reported, is never idle-completed. The idle rule stamps IDLE_COMPLETED_AT in the stats the first time it
    completes a batch, and again only if the batch has dialled since its stamp (a new idle period)."""
    cur.execute(
        "SELECT b.id, b.status, b.total, b.scheduled_at, b.stats, count(k.id) AS dialed, "
        "count(k.id) FILTER (WHERE k.outcome = 'CONNECTED') AS connected, "
        "count(DISTINCT k.phone) FILTER (WHERE lower(k.status) = ANY(%s)) AS finished, "
        "max(k.started_at) AS last_call "
        "FROM batch b LEFT JOIN call k ON k.batch_id = b.id WHERE b.campaign_id = %s GROUP BY b.id",
        (_TERMINAL, campaign_id))
    for b in cur.fetchall():
        status, stats = b["status"], b["stats"]
        if status in ("created", "running") and b["total"] > 0 and b["finished"] >= b["total"]:
            status = "completed"
        if (b["status"] == "running" and _bolna_status(b["stats"]) == "running" and b["scheduled_at"]
                and now - b["scheduled_at"] > BATCH_MAX_RUNTIME
                and (b["last_call"] is None or now - b["last_call"] > BATCH_IDLE)):
            status = "completed"
            stamp = _parse_utc(stats.get(IDLE_COMPLETED_AT))
            if stamp is None or (b["last_call"] is not None and b["last_call"] > stamp):
                stats = {**stats, IDLE_COMPLETED_AT: _iso(now)}
        cur.execute("UPDATE batch SET dialed = %s, connected = %s, status = %s, stats = %s WHERE id = %s "
                    "AND status = %s",  # unless pause/stop changed it since the read
                    (b["dialed"], b["connected"], status, Json(stats) if stats is not None else None, b["id"],
                     b["status"]))


def _complete_if_finished(conn, campaign: dict, report: dict, now: datetime) -> None:
    """COMPLETED once the campaign has batches, none may still dial, no lead is PENDING or QUEUED, no finished call
    still waits for its rules (extraction; this keeps the agent synced until the tag lands) and no lead could ever
    be redialled. Its finished leads become DONE."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE status = ANY(%s)) AS active FROM batch "
                    "WHERE campaign_id = %s", (redial.ACTIVE_BATCH_STATUSES, campaign["id"]))
        b = cur.fetchone()
        if b["n"] == 0 or b["active"]:
            return
        cur.execute("SELECT 1 FROM lead WHERE campaign_id = %s AND status IN ('PENDING', 'QUEUED') LIMIT 1",
                    (campaign["id"],))
        if cur.fetchone():
            return
        cur.execute("SELECT 1 FROM call WHERE campaign_id = %s AND rules_applied_at IS NULL "
                    "AND lower(status) = ANY(%s) LIMIT 1", (campaign["id"], _TERMINAL))
        if cur.fetchone():
            return
    if redial.eligible_redial_leads(conn, campaign, now + timedelta(days=365)):  # ignoring the gap
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE campaign SET status = 'COMPLETED', completed_at = %s, updated_at = now() "
                    "WHERE id = %s AND status::text = ANY(%s) RETURNING id",
                    (now, campaign["id"], ROLLUP_CAMPAIGN_STATUSES))
        if cur.fetchone() is None:
            return
        cur.execute("UPDATE lead SET status = 'DONE' WHERE campaign_id = %s AND status::text = ANY(%s)",
                    (campaign["id"], FINISHED_LEAD_STATUSES))
    report["campaigns_completed"] += 1


def _rollup_campaign(conn, campaign_id: str, report: dict, now: datetime, agent_synced: bool) -> None:
    with conn.cursor() as cur:
        _rollup_batches(cur, campaign_id, now)
        # A campaign scheduled outside the window is RUNNING once Bolna starts dialling (else no auto-redial).
        cur.execute("UPDATE campaign SET status = 'RUNNING', updated_at = now() WHERE id = %s AND status = 'SCHEDULED' "
                    "AND EXISTS (SELECT 1 FROM batch WHERE campaign_id = %s AND status IN ('running', 'completed'))",
                    (campaign_id, campaign_id))
        idle = _unconfirmed_idle_batches(cur, campaign_id, now)
        if idle:
            cur.execute("SELECT count(*) AS n FROM lead WHERE campaign_id = %s AND status = 'QUEUED'", (campaign_id,))
            queued = cur.fetchone()["n"]
            for b in idle if queued else ():
                stamp = _parse_utc((b["stats"] or {}).get(IDLE_COMPLETED_AT))
                if _bolna_status(b["stats"]) == "running" and stamp:
                    until = f"not closed before {_iso(stamp + IDLE_TRUST_AFTER)}"
                else:
                    raw = b["stats"].get("status") if isinstance(b["stats"], dict) else None
                    until = f"not closed while Bolna reports {raw}"
                _record(report, f"batch {b['id']}: completed on the idle rule with {queued} QUEUED lead(s) in its "
                                f"campaign; {until}")
        # A lead held QUEUED by a call Bolna never finished (its latest call, not terminal, STUCK_CALL_AFTER old; a
        # call without a start time counts as the latest and as old).
        cur.execute(
            "SELECT l.id, k.bolna_execution_id FROM lead l JOIN LATERAL ("
            "  SELECT bolna_execution_id, status, started_at FROM call WHERE lead_id = l.id "
            "  ORDER BY started_at DESC NULLS FIRST, id DESC LIMIT 1) k ON true "
            "WHERE l.campaign_id = %s AND l.status = 'QUEUED' AND lower(k.status) <> ALL(%s) "
            "AND coalesce(k.started_at, '-infinity') < %s "
            "ORDER BY l.created_at, l.id", (campaign_id, _TERMINAL, now - STUCK_CALL_AFTER))
        for r in cur.fetchall():
            _record(report, f"lead {r['id']} stuck QUEUED behind unfinished call {r['bolna_execution_id']}")
        if agent_synced:  # else "no call in the database" may only mean "not fetched this run"
            close_stale_queued(conn, campaign_id, now)
        cur.execute("SELECT * FROM campaign WHERE id = %s", (campaign_id,))
        campaign = cur.fetchone()
    conn.commit()
    _stop_at_window_end(conn, campaign, now, report)  # after close_stale_queued: see its docstring
    round_ = redial.maybe_auto_redial(conn, campaign, now)
    if round_ and round_["batch_ids"]:
        report["redial_rounds"] += 1
    _complete_if_finished(conn, campaign, report, now)
    conn.commit()


def rollup_campaigns(conn, report: dict, now: datetime, *, synced_agents: set[str]) -> None:
    """Per RUNNING / SCHEDULED campaign: batch counters, SCHEDULED -> RUNNING, close_stale_queued (only when the
    campaign's agent is in `synced_agents`, what sync_executions returned this run), the window-end stop, auto-redial,
    completion. Each campaign commits on its own; a failure is rolled back and recorded."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, agent_id FROM campaign WHERE status::text = ANY(%s) ORDER BY created_at, id",
                    (ROLLUP_CAMPAIGN_STATUSES,))
        rows = cur.fetchall()
    conn.commit()
    for row in rows:
        campaign_id = row["id"]
        try:
            _rollup_campaign(conn, campaign_id, report, now, row["agent_id"] in synced_agents)
        except Exception as e:
            conn.rollback()  # create_round can raise with row locks held
            _record(report, f"rollup {campaign_id}: {_msg(e)}")
    with conn.cursor() as cur:
        cur.execute("SELECT id, agent_id FROM campaign c WHERE c.status::text = ANY(%s) AND EXISTS "
                    "(SELECT 1 FROM lead l WHERE l.campaign_id = c.id AND l.status = 'QUEUED') ORDER BY created_at, id",
                    (HALTED_CAMPAIGN_STATUSES,))
        halted = [r for r in cur.fetchall() if r["agent_id"] in synced_agents]
    conn.commit()
    for row in halted:  # pause and stop leave the leads of a started batch QUEUED for this
        try:
            close_stale_queued(conn, row["id"], now)
            conn.commit()
        except Exception as e:
            conn.rollback()
            _record(report, f"rollup {row['id']}: {_msg(e)}")


# ---------------------------------------------------------------- 7. recordings

def archive_recordings(conn, report: dict, limit: int = 40) -> None:
    """Copy up to `limit` recordings (newest first) into R2. Nothing when storage is not configured. A timeout or
    connection error (recording host or R2) stops the run with one error: "recordings: unreachable — stopping this
    run"; any other failure is recorded per recording."""
    if not storage.storage_enabled():
        return
    with conn.cursor() as cur:
        cur.execute("SELECT id, bolna_execution_id, recording_url, started_at, created_at FROM call "
                    "WHERE recording_url IS NOT NULL AND recording_key IS NULL "
                    "ORDER BY started_at DESC NULLS LAST, id LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.commit()
    for r in rows:
        try:
            when = (r["started_at"] or r["created_at"]).astimezone(timezone.utc)
            key = storage.archive_recording(r["bolna_execution_id"], r["recording_url"], when)
            with conn.cursor() as cur:
                cur.execute("UPDATE call SET recording_key = %s WHERE id = %s", (key, r["id"]))
            conn.commit()
            report["recordings_archived"] += 1
        except storage.UNREACHABLE as e:
            conn.rollback()
            log.warning("sync: recording %s: %s", r["bolna_execution_id"], _msg(e))
            _record(report, "recordings: unreachable — stopping this run")
            break
        except Exception as e:
            conn.rollback()
            _record(report, f"recording {r['bolna_execution_id']}: {_msg(e)}")


# ---------------------------------------------------------------- run_sync

def _run_steps(conn, report: dict, full: bool, max_pages: int | None) -> None:
    def triggers():
        t = workflows.fire_triggers(conn, now=_utcnow())
        report["triggers_fired"] = len(t["fired"])
        for x in t["errors"]:
            _record(report, f"trigger {x}")

    synced: set[str] = set()  # stays empty when the executions step fails: no campaign closes stale leads
    attempted: set[str] = set()  # calls whose rules were tried this run: the rules step does not retry them

    def executions():
        synced.update(sync_executions(conn, report, full=full, max_pages=max_pages, now=_utcnow(),
                                      attempted=attempted))

    def runs():
        report["runs_started"] = len(scheduler.dispatch_due_runs(conn, now=_utcnow(), errors=report["errors"]))

    def di():
        report["di_pushed"] = di_push.push_calls_to_di(conn, report)

    steps = [("agents", lambda: sync_agents(conn))] if full else []
    steps += [
        ("batches", lambda: sync_batches(conn, report, _utcnow())),
        ("executions", executions),
        ("rules", lambda: retry_rules(conn, report, _utcnow(), attempted=attempted)),
        ("rollup", lambda: rollup_campaigns(conn, report, _utcnow(), synced_agents=synced)),
        ("triggers", triggers),
        ("scheduler", runs),
        ("recordings", lambda: archive_recordings(conn, report)),
        ("di", di),
    ]
    for name, step in steps:
        try:
            step()
        except Exception as e:
            conn.rollback()
            log.exception("sync: step %s failed", name)
            report["errors"].append(f"{name}: {_msg(e)}")


@contextmanager
def sync_lock():
    """Take the sync lock without waiting: yields True when this caller holds it for the whole `with` block, False
    when another sync does. Every syncer goes through this (run_sync, so the thread, the CLI and "Sync now"; the
    backfill).

    The lock is `pg_try_advisory_xact_lock` in a transaction held open on a dedicated connection (the work
    connection commits many times). An open transaction pins its server connection, so this also holds behind
    Neon's pooler (PgBouncer transaction mode), where a session lock could be taken and released on different
    backends. On exit the transaction is rolled back (releasing the lock) and the connection closed.
    """
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    # keepalives: the lock connection sits idle for the whole sync; if the network drops it, the lock goes too
    lock = psycopg2.connect(config.DATABASE_URL, keepalives=1, keepalives_idle=30, keepalives_interval=10,
                            keepalives_count=3)
    try:
        with lock.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_KEY,))
            got = cur.fetchone()[0]
        yield got
    finally:
        try:
            lock.rollback()
        finally:
            lock.close()


def run_sync(*, full: bool = False, max_pages: int | None = None) -> dict | None:
    """One sync under `sync_lock`. Returns the report (also saved as the lastSyncReport setting, and the IST date as
    lastFullSyncDate on a full sync), or None when another sync holds the lock. `max_pages` per agent defaults to
    MAX_PAGES (40); 0 means no cap (manual recovery of an agent stuck behind the cap: run_sync.py --max-pages 0)."""
    if max_pages is not None and max_pages < 0:
        raise ValueError("max_pages must be 0 (no cap) or more")
    started = _utcnow()
    with sync_lock() as got:
        if not got:
            return None
        report = new_report()
        report["started_at"], report["full"] = _iso(started), full
        conn = get_conn()
        try:
            _run_steps(conn, report, full, MAX_PAGES if max_pages is None else (max_pages or None))
            report["finished_at"] = _iso(_utcnow())
            set_setting(conn, "lastSyncReport", report)
            if full:
                set_setting(conn, "lastFullSyncDate", started.astimezone(IST).date().isoformat())
            conn.commit()
        finally:
            conn.close()
        return report


def is_full_due(now: datetime, last_full_date: str | None) -> bool:
    """True from 03:00 IST until a full sync has run that IST day."""
    ist = now.astimezone(IST)
    return ist.hour >= FULL_SYNC_HOUR_IST and last_full_date != ist.date().isoformat()


# ---------------------------------------------------------------- in-process loop (runs inside the web service)
#
# Copied from DI's dialer: every gunicorn worker starts this loop from wsgi.py; the advisory lock in run_sync makes
# exactly one of them sync at a time (the others get None and skip the tick).

def _loop():
    log.info("in-process sync loop started (advisory-locked; every %ds)", SYNC_SECONDS)
    while True:
        try:
            conn = get_conn()
            try:
                last_full = get_setting(conn, "lastFullSyncDate")
            finally:
                conn.close()
            report = run_sync(full=is_full_due(_utcnow(), last_full))
            if report is None:
                log.info("sync: skipped, another worker is syncing")
            elif report["errors"]:
                log.warning("sync: finished with %d error(s)", len(report["errors"]))
        except Exception:  # noqa: BLE001 -- the loop must survive anything
            log.exception("sync: tick failed")
        sleep(SYNC_SECONDS)


_started = False


def start_background_sync() -> None:
    """Start the sync loop as a daemon thread, once per process. Called from wsgi.py. SYNC_ENABLED=0 turns it off.
    Raises when an integration the sync needs is not configured."""
    global _started
    if config.SYNC_ENABLED != "1":
        log.info("sync: not started (SYNC_ENABLED=%s)", config.SYNC_ENABLED)
        return
    missing = [k for k in REQUIRED_CONFIG if not getattr(config, k)]
    if missing:
        raise RuntimeError(f"background sync needs {', '.join(missing)} (or set SYNC_ENABLED=0)")
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="sync", daemon=True).start()
