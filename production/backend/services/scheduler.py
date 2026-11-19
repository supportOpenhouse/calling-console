"""Department calendar: lays campaign runs back-to-back inside the calling window and starts them when due.

Port of lib/scheduler.ts. Takes `conn` and does not commit, except `dispatch_due_runs`, which calls
`create_round` (that commits) and commits its own run-status updates.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from . import redial
from .ist import minutes_left_in_window, next_window_start, validate_window

# Seconds of line time one dialled contact costs on average (ring-out ~30 s, connected ~110 s, ~35% connect).
AVG_CALL_SECONDS = 55
DEPARTMENTS = ("SUPPLY", "DEMAND")
# A run is started this long before its planned start (create_round schedules the batch for the planned time).
DISPATCH_LEAD = timedelta(minutes=2)


def estimate_minutes(lead_count: int, lines: int) -> int:
    return max(3, math.ceil(lead_count * AVG_CALL_SECONDS / max(1, lines) / 60) + 2)


def dept_config(conn, dept: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM department WHERE key = %s", (dept,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"department {dept} not found")
    return row


def place_runs(runs: list[dict], *, now: datetime, busy_until: datetime | None, window_start: str,
               window_end: str, line_share: int) -> list[tuple[str, datetime, datetime]]:
    """Pure. Lay `runs` (already ordered) back-to-back inside the IST window, spilling to the next day."""
    validate_window(window_start, window_end)
    cursor = max(now, busy_until) if busy_until else now
    out = []
    for r in runs:
        start = next_window_start(max(cursor, r["requested_start"]), window_start, window_end)
        remaining = r["est_minutes"] or estimate_minutes(r["est_leads"], line_share)
        end = start
        while remaining > 0:
            left = minutes_left_in_window(end, window_start, window_end)
            if left <= 0:
                end = next_window_start(end + timedelta(minutes=1), window_start, window_end)
                continue
            take = min(left, remaining)
            end += timedelta(minutes=take)
            remaining -= take
        out.append((r["id"], start, end))
        cursor = end
    return out


def replan_department(conn, dept: str, now: datetime | None = None) -> list[dict]:
    """Re-plan every PLANNED run of `dept`: priority (1 first), then requested start, then creation."""
    now = now or datetime.now(timezone.utc)
    cfg = dept_config(conn, dept)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM scheduled_run WHERE department = %s AND status = 'PLANNED' "
                    "ORDER BY priority, requested_start, created_at", (dept,))
        runs = cur.fetchall()
        cur.execute("SELECT max(planned_end) AS busy FROM scheduled_run WHERE department = %s AND status = 'RUNNING'",
                    (dept,))
        busy_until = cur.fetchone()["busy"]
    placed = place_runs(runs, now=now, busy_until=busy_until, window_start=cfg["window_start"],
                        window_end=cfg["window_end"], line_share=cfg["line_share"])
    out = []
    with conn.cursor() as cur:
        for r, (_, start, end) in zip(runs, placed):
            cur.execute("UPDATE scheduled_run SET planned_start = %s, planned_end = %s, lines = %s WHERE id = %s",
                        (start, end, cfg["line_share"], r["id"]))
            out.append({**r, "planned_start": start, "planned_end": end, "lines": cfg["line_share"]})
    return out


def plan_run(conn, *, dept: str, campaign_id: str, use_case_id: str | None = None, workflow_id: str | None = None,
             purpose: str | None = None, priority: int = 5, requested_start: datetime | None = None,
             user_id: str | None = None, note: str | None = None, now: datetime | None = None) -> dict:
    """Register a campaign on the department calendar. `requested_start` None = as soon as possible."""
    now = now or datetime.now(timezone.utc)
    requested_start = requested_start or now
    cfg = dept_config(conn, dept)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM lead WHERE campaign_id = %s AND status = 'PENDING'", (campaign_id,))
        est_leads = cur.fetchone()["n"]
        cur.execute(
            "INSERT INTO scheduled_run (department, campaign_id, use_case_id, workflow_id, purpose, priority, "
            "requested_start, est_leads, est_minutes, lines, created_by_id, note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (dept, campaign_id, use_case_id, workflow_id, purpose, priority, requested_start, est_leads,
             estimate_minutes(est_leads, cfg["line_share"]), cfg["line_share"], user_id, note))
        run = cur.fetchone()
        cur.execute("UPDATE campaign SET status = 'SCHEDULED', scheduled_at = %s WHERE id = %s",
                    (requested_start, campaign_id))
    plan = replan_department(conn, dept, now)
    return next((r for r in plan if r["id"] == run["id"]), run)


def _set_run(conn, run_id: str, status: str, note: str | None = None, planned_end: datetime | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE scheduled_run SET status = %s, note = coalesce(%s, note), "
                    "planned_end = coalesce(%s, planned_end) WHERE id = %s", (status, note, planned_end, run_id))


def _has_active_batch(conn, campaign_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM batch WHERE campaign_id = %s AND status = ANY(%s) LIMIT 1",
                    (campaign_id, redial.ACTIVE_BATCH_STATUSES))
        return cur.fetchone() is not None


def _live_campaign_has_active_batch(conn, campaign_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM campaign WHERE id = %s", (campaign_id,))
        c = cur.fetchone()
    return c is not None and c["status"] in ("RUNNING", "SCHEDULED") and _has_active_batch(conn, campaign_id)


def dispatch_due_runs(conn, now: datetime | None = None, errors: list | None = None) -> list[str]:
    """Called by the sync job: start every PLANNED run whose planned start is at most 2 minutes away. A run that
    fails to start gets the error in its note and, with `errors` (the sync report's), also
    "run <id>: start failed: <msg>"."""
    now = now or datetime.now(timezone.utc)
    started: list[str] = []
    for dept in DEPARTMENTS:
        replan_department(conn, dept, now)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM scheduled_run WHERE status = 'PLANNED' AND planned_start <= %s "
                    "ORDER BY planned_start, priority", (now + DISPATCH_LEAD,))
        due = cur.fetchall()
    conn.commit()
    for r in due:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM campaign WHERE id = %s", (r["campaign_id"],))
            campaign = cur.fetchone()
        if campaign is None or campaign["status"] in ("STOPPED", "COMPLETED"):
            _set_run(conn, r["id"], "CANCELLED")
            conn.commit()
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM lead WHERE campaign_id = %s AND status = 'PENDING'", (campaign["id"],))
            pending = cur.fetchall()
        try:
            result = redial.create_round(conn, campaign, pending, "FIRST", None, now=now)
        except Exception as e:
            conn.rollback()
            # A batch may still dial: the run effectively started, so it must not be retried.
            live = _live_campaign_has_active_batch(conn, campaign["id"])
            _set_run(conn, r["id"], "RUNNING" if live else "PLANNED", note=f"start failed: {e}")
            conn.commit()
            if errors is not None:
                errors.append(f"run {r['id']}: start failed: {e}")
            continue
        if result["skipped"] == redial.NO_ELIGIBLE:
            if _has_active_batch(conn, campaign["id"]):
                _set_run(conn, r["id"], "RUNNING")
            else:
                _set_run(conn, r["id"], "DONE", note=f"skipped: {result['skipped']}")
                with conn.cursor() as cur:
                    cur.execute("UPDATE campaign SET status = 'COMPLETED', completed_at = %s WHERE id = %s",
                                (now, campaign["id"]))
        elif result["skipped"]:
            _set_run(conn, r["id"], "PLANNED", note=f"skipped: {result['skipped']}")
        else:
            _set_run(conn, r["id"], "RUNNING")
            started.append(r["id"])
        conn.commit()
    # close runs whose campaign finished
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduled_run r SET status = CASE WHEN c.status = 'STOPPED' THEN 'CANCELLED'::run_status "
            "ELSE 'DONE'::run_status END, planned_end = %s FROM campaign c "
            "WHERE c.id = r.campaign_id AND r.status = 'RUNNING' AND c.status IN ('COMPLETED', 'STOPPED')", (now,))
    conn.commit()
    return started


def cancel_run(conn, run_id: str, now: datetime | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE scheduled_run SET status = 'CANCELLED' WHERE id = %s RETURNING department, campaign_id",
                    (run_id,))
        r = cur.fetchone()
        if r is None:
            return
        if r["campaign_id"]:
            cur.execute("UPDATE campaign SET status = 'DRAFT', scheduled_at = NULL WHERE id = %s AND status = 'SCHEDULED'",
                        (r["campaign_id"],))
    replan_department(conn, r["department"], now)


def set_run_priority(conn, run_id: str, priority: int, requested_start: datetime | None = None,
                     now: datetime | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("UPDATE scheduled_run SET priority = %s, requested_start = coalesce(%s, requested_start) "
                    "WHERE id = %s RETURNING department", (priority, requested_start, run_id))
        r = cur.fetchone()
    if r is None:
        raise LookupError(f"run {run_id} not found")
    return replan_department(conn, r["department"], now)


def runs_between(conn, start: datetime, end: datetime, dept: str | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.*, c.name AS campaign_name, c.status AS campaign_status FROM scheduled_run r "
            "LEFT JOIN campaign c ON c.id = r.campaign_id "
            "WHERE (%s::vertical IS NULL OR r.department = %s::vertical) "
            "AND coalesce(r.planned_start, r.requested_start) BETWEEN %s AND %s ORDER BY r.planned_start",
            (dept, dept, start, end))
        return cur.fetchall()
