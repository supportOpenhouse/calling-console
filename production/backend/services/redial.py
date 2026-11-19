"""The dial guard. Every Bolna dial goes through `create_round` (batches) or `assert_dialable` (single calls).

Like the other services this module takes `conn` and does not commit -- except `create_round`, which commits
at fixed points around the Bolna calls so that a failed or crashed request can never leave a dial the
database does not know about (see its docstring).
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone

from . import bolna
from .classify import normalize_phone
from .datasets import dnc_phones, recently_spoken_phones
from .errors import ApiError
from .ist import in_window, validate_window

DEFAULT_REDIAL_POLICY = {
    "max_attempts": 3, "gap_minutes": 180, "window_start": "10:00", "window_end": "19:00",
    "retry_outcomes": ["NOT_PICKED", "CARRIER_BLOCKED", "VOICEMAIL", "FAILED", "BALANCE_LOW"],
    "retry_tags": ["not_picked", "callback_requested", "unclear", "yes_unverified"],
    "rotate_numbers": [], "auto_redial": True,
}

KINDS = ("FIRST", "REDIAL")
NO_ELIGIBLE = "no eligible leads"
SKIPPED_COOLING = "skipped_cooling"
SKIPPED_IN_FLIGHT = "skipped_in_flight"
CAMPAIGN_NOT_ACTIVE = "campaign not active"
# A campaign in one of these dials nobody, and create_round never moves it.
INACTIVE_CAMPAIGN_STATUSES = ["PAUSED", "STOPPED", "COMPLETED"]
# Lead statuses a redial round looks at.
REDIAL_STATUSES = ["NOT_CONNECTED", "BLOCKED", "VOICEMAIL", "FAILED", "CONNECTED", "PENDING"]
# A lead in one of these is never put into a new batch: it is in flight or finished.
UNDIALABLE_STATUSES = ("QUEUED", "DONE", "DNC")
# Batches that may still dial. 'creating' = create_round is between its DB write and Bolna (or died there).
ACTIVE_BATCH_STATUSES = ["creating", "created", "scheduled", "running"]
# Advisory lock namespace for claiming a phone: pg_advisory_xact_lock(PHONE_LOCK, hashtext(phone)). Two int4 keys, so
# it never meets the sync's single-key lock.
PHONE_LOCK = 42100002
# A lead of another campaign holds this phone in a live batch.
_IN_FLIGHT = ("EXISTS (SELECT 1 FROM lead o WHERE o.phone = lead.phone AND o.status = 'QUEUED' "
              "AND o.campaign_id <> lead.campaign_id)")


# ---------------------------------------------------------------- policy

def policy_of(campaign: dict) -> dict:
    return {**copy.deepcopy(DEFAULT_REDIAL_POLICY), **(campaign.get("redial_policy") or {})}


def _whole_number(key: str, v, lo: int, hi: int) -> int:
    if isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
        v = int(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{key} must be a whole number")
    if not lo <= v <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return v


def validate_policy(p: dict) -> dict:
    """Merge with the default and validate. Raises ValueError on bad input."""
    if not isinstance(p, dict):
        raise ValueError("redial policy must be an object")
    out = {k: copy.deepcopy(p[k] if p.get(k) is not None else v) for k, v in DEFAULT_REDIAL_POLICY.items()}
    out["max_attempts"] = _whole_number("max_attempts", out["max_attempts"], 1, 8)
    out["gap_minutes"] = _whole_number("gap_minutes", out["gap_minutes"], 15, 10080)
    validate_window(out["window_start"], out["window_end"])
    for k in ("retry_outcomes", "retry_tags", "rotate_numbers"):
        if not isinstance(out[k], list) or not all(isinstance(x, str) for x in out[k]):
            raise ValueError(f"{k} must be a list of strings")
    if not isinstance(out["auto_redial"], bool):
        raise ValueError("auto_redial must be true or false")
    return out


# ---------------------------------------------------------------- eligibility

def eligible_redial_leads(conn, campaign: dict, now: datetime) -> list[dict]:
    """Leads that have earned another attempt at `now` (port of lib/redial.ts), with two changes:

    - A PENDING lead is eligible only when its last outcome was BALANCE_LOW. Any other PENDING lead (never
      dialled, or returned to PENDING by a pause, which can leave its old batch's next_dial_at) belongs to a
      FIRST round, whatever its next_dial_at says.
    - A lead whose next_dial_at is due (set by a result rule's "redial after") is eligible whatever its last
      outcome and the gap.
    Always: attempts < max_attempts, never QUEUED, DONE or DNC, and a next_dial_at in the future means not yet.
    """
    p = policy_of(campaign)
    gap_cutoff = now - timedelta(minutes=int(p["gap_minutes"]))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM lead WHERE campaign_id = %s AND attempts < %s AND status::text = ANY(%s) "
            "AND (next_dial_at IS NULL OR next_dial_at <= %s) "
            "ORDER BY created_at, id",
            (campaign["id"], int(p["max_attempts"]), REDIAL_STATUSES, now))
        rows = cur.fetchall()
    retry_tags = {str(t).lower() for t in p["retry_tags"] or []}
    retry_outcomes = set(p["retry_outcomes"] or [])

    def earned(lead: dict) -> bool:
        past_gap = lead["last_call_at"] is None or lead["last_call_at"] < gap_cutoff
        if lead["status"] == "PENDING":
            return lead["last_outcome"] == "BALANCE_LOW" and past_gap
        if lead["next_dial_at"] is not None:  # due: later ones were left out above
            return True
        if not past_gap:
            return False
        if lead["status"] == "CONNECTED":
            return (lead["final_tag"] or "").lower() in retry_tags
        return lead["last_outcome"] in retry_outcomes if lead["last_outcome"] else True

    return [lead for lead in rows if earned(lead)]


def from_number_for(lead: dict, campaign: dict, policy: dict) -> str:
    """Which number to dial a lead from on its next attempt (carrier-blocked leads rotate)."""
    rotate = policy.get("rotate_numbers") or []
    if lead["status"] == "BLOCKED" and rotate:
        return rotate[(lead["attempts"] - 1) % len(rotate)]
    return campaign["from_number"]


# ---------------------------------------------------------------- the guard

def _cooling_days(conn, vertical: str) -> int:
    # A BOTH campaign has no department row of its own: use the longest cooling of any department.
    with conn.cursor() as cur:
        cur.execute("SELECT max(cooling_days) AS d FROM department WHERE key = %s OR %s = 'BOTH'",
                    (vertical, vertical))
        d = cur.fetchone()["d"]
    if d is None:
        raise RuntimeError(f"no department row for {vertical!r}: cannot apply cooling")
    return int(d)


def guard_leads(conn, campaign: dict, leads: list[dict], kind: str, now: datetime) -> tuple[list, list, list]:
    """Split `leads` into (dialable, dnc, cooled). Reads only; create_round records the statuses.

    - dnc: the phone is in `dnc_phone`. Always checked.
    - cooled: FIRST rounds only. Each is a copy of the lead with `skip_tag`, the final tag create_round
      records:
      - "skipped_cooling" (campaigns without `skip_cooling`): the phone had a CONNECTED or VOICEMAIL call in
        another campaign within the department's cooling days;
      - "skipped_in_flight" (every FIRST round, `skip_cooling` or not -- it waives past conversations, never
        a simultaneous dial): the phone is QUEUED in another campaign right now.
      A phone with both reasons gets "skipped_cooling", the lasting one.

    Redial rounds skip cooling. A phone QUEUED in another campaign is left out of a redial round but is in
    none of the three lists: the lead is not touched and stays eligible for a later round.
    """
    if kind not in KINDS:
        raise ValueError(f"round kind must be one of {KINDS}, got {kind!r}")
    blocked = dnc_phones(conn, [lead["phone"] for lead in leads])
    dnc = [lead for lead in leads if lead["phone"] in blocked]
    dialable = [lead for lead in leads if lead["phone"] not in blocked]
    cooled: list[dict] = []
    if not dialable:
        return dialable, dnc, cooled
    phones = [lead["phone"] for lead in dialable]
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT phone FROM lead WHERE phone = ANY(%s) AND status = 'QUEUED' "
                    "AND campaign_id <> %s", (phones, campaign["id"]))
        in_flight = {r["phone"] for r in cur.fetchall()}
    if kind == "REDIAL":
        return [lead for lead in dialable if lead["phone"] not in in_flight], dnc, cooled
    recent: set[str] = set()
    if not campaign.get("skip_cooling"):
        since = now - timedelta(days=_cooling_days(conn, campaign["vertical"]))
        recent = recently_spoken_phones(conn, phones, since, exclude_campaign_id=campaign["id"])
    tag_of = {**{p: SKIPPED_IN_FLIGHT for p in in_flight}, **{p: SKIPPED_COOLING for p in recent}}
    cooled = [{**lead, "skip_tag": tag_of[lead["phone"]]} for lead in dialable if lead["phone"] in tag_of]
    dialable = [lead for lead in dialable if lead["phone"] not in tag_of]
    return dialable, dnc, cooled


def assert_dialable(conn, phone: str) -> None:
    """409 if the number is on the do-not-call list (checked as given and in its +91 form)."""
    forms = {phone, normalize_phone(phone)} - {None}
    if dnc_phones(conn, forms):
        raise ApiError(409, "number is on the do-not-call list")


def place_test_call(conn, agent: dict, to: str, from_number: str, variables: dict) -> dict:
    assert_dialable(conn, to)
    return bolna.place_call(agent["bolna_agent_id"], to, from_number, variables or {})


# ---------------------------------------------------------------- rounds

def _may_still_dial(bolna_batch_id: str, scheduled_at: datetime, err: Exception) -> bool:
    """Bolna created this batch but did not confirm it as scheduled. Stop it, then say whether it may still
    dial or already has: True if the stop failed, or if its start time has passed (calls may have begun
    before the stop). The reasons go on `err` as notes, so the original error is the one raised."""
    stopped = True
    try:
        bolna.stop_batch(bolna_batch_id)
    except Exception as stop_err:
        err.add_note(f"stop_batch({bolna_batch_id}) also failed: {stop_err}")
        stopped = False
    if datetime.now(timezone.utc) >= scheduled_at:
        err.add_note(f"bolna batch {bolna_batch_id} was due at {scheduled_at.isoformat()}; "
                     f"it may already have dialled")
        return True
    return not stopped


def _send_group(conn, campaign: dict, agent: dict, kind: str, round_no: int, from_number: str,
                group: list[dict], scheduled_at: datetime, result: dict) -> int:
    """Step 4 of create_round for one from-number. Returns how many leads were queued (0 if none could be
    claimed). Adds the batch to result["batch_ids"] once Bolna may dial it -- also when it then raises --
    phones put on the DNC list since guard_leads ran to result["dnc"], and phones a FIRST round found QUEUED in
    another campaign since then to result["cooled"]."""
    ids = [lead["id"] for lead in group]
    with conn.cursor() as cur:
        # One claim per phone at a time, across campaigns, until this transaction commits: a concurrent round that
        # holds a phone has committed its QUEUED lead before this one reads it. Locks are taken in hash order, so
        # two rounds over the same phones never deadlock.
        cur.execute("SELECT pg_advisory_xact_lock(%s, h) FROM (SELECT DISTINCT hashtext(p) AS h "
                    "FROM unnest(%s::text[]) AS p ORDER BY h) s", (PHONE_LOCK, [lead["phone"] for lead in group]))
        cur.fetchall()
        # A phone put on the DNC list since guard_leads ran is marked and never sent.
        cur.execute("UPDATE lead SET status = 'DNC' WHERE id = ANY(%s) AND campaign_id = %s "
                    "AND EXISTS (SELECT 1 FROM dnc_phone d WHERE d.phone = lead.phone) RETURNING phone",
                    (ids, campaign["id"]))
        result["dnc"] += [r["phone"] for r in cur.fetchall()]
        # A phone QUEUED in another campaign since guard_leads ran is never sent: a FIRST round marks its lead DONE
        # (skipped_in_flight, as guard_leads does), a REDIAL round leaves it for a later round.
        if kind == "FIRST":
            cur.execute(f"UPDATE lead SET status = 'DONE', final_tag = %s WHERE id = ANY(%s) AND campaign_id = %s "
                        f"AND status::text <> ALL(%s) AND {_IN_FLIGHT} RETURNING phone",
                        (SKIPPED_IN_FLIGHT, ids, campaign["id"], list(UNDIALABLE_STATUSES)))
            result["cooled"] += [r["phone"] for r in cur.fetchall()]
        # Claim the leads: lock them, and keep only those still exactly as the caller read them (status and
        # attempts) and not in flight or finished. A second round over the same (stale) rows, or a
        # concurrent one, finds them QUEUED or re-attempted and dials nobody twice.
        cur.execute("SELECT id, phone, variables, status, attempts, next_dial_at FROM lead "
                    "WHERE id = ANY(%s) AND campaign_id = %s "
                    f"AND NOT EXISTS (SELECT 1 FROM dnc_phone d WHERE d.phone = lead.phone) AND NOT {_IN_FLIGHT} "
                    "ORDER BY id FOR UPDATE", (ids, campaign["id"]))
        read_as = {lead["id"]: (lead["status"], lead["attempts"]) for lead in group}
        claimed = [r for r in cur.fetchall()
                   if (r["status"], r["attempts"]) == read_as[r["id"]] and r["status"] not in UNDIALABLE_STATUSES]
        if not claimed:
            conn.commit()  # keeps the DNC marks and releases the row locks
            return 0
        cur.execute("INSERT INTO batch (campaign_id, round, kind, from_number, scheduled_at, status, total) "
                    "VALUES (%s, %s, %s, %s, %s, 'creating', %s) RETURNING id",
                    (campaign["id"], round_no, kind, from_number, scheduled_at, len(claimed)))
        batch_id = cur.fetchone()["id"]
        cur.execute("UPDATE lead SET status = 'QUEUED', next_dial_at = %s WHERE id = ANY(%s)",
                    (scheduled_at, [r["id"] for r in claimed]))
    conn.commit()  # recorded before Bolna hears of it: a crash from here on leaves the leads QUEUED

    bolna_batch_id = None
    try:
        csv_text = bolna.build_batch_csv([{"phone": r["phone"], "variables": r["variables"]} for r in claimed],
                                         agent["variables"] or [])
        filename = f"{re.sub(r'[^a-z0-9]+', '_', campaign['name'], flags=re.I)}_r{round_no}.csv"
        created = bolna.create_batch(agent["bolna_agent_id"], from_number, csv_text, filename)
        bolna_batch_id = created["batch_id"]
        bolna.schedule_batch(bolna_batch_id, scheduled_at)
    except Exception as err:
        conn.rollback()
        if bolna_batch_id is not None and _may_still_dial(bolna_batch_id, scheduled_at, err):
            # Bolna may still dial this batch (or already has). Track it like any scheduled batch (the sync
            # polls its real state) and keep its leads QUEUED, so no other round dials them again.
            with conn.cursor() as cur:
                cur.execute("UPDATE batch SET status = 'scheduled', bolna_batch_id = %s WHERE id = %s",
                            (bolna_batch_id, batch_id))
            conn.commit()
            result["batch_ids"].append(batch_id)
            raise
        with conn.cursor() as cur:
            # No batch on Bolna can dial these leads (never created, or stopped before its start time): hand
            # them back -- only those still QUEUED, so a change made meanwhile is not overwritten. Keep
            # Bolna's id of a stopped batch so it can be traced.
            cur.execute("UPDATE batch SET status = 'failed', bolna_batch_id = %s WHERE id = %s",
                        (bolna_batch_id, batch_id))
            cur.execute("UPDATE lead l SET status = x.status::lead_status, next_dial_at = x.next_dial_at "
                        "FROM unnest(%s::text[], %s::text[], %s::timestamptz[]) AS x(id, status, next_dial_at) "
                        "WHERE l.id = x.id AND l.status = 'QUEUED'",
                        ([r["id"] for r in claimed], [r["status"] for r in claimed],
                         [r["next_dial_at"] for r in claimed]))
        conn.commit()
        raise
    result["batch_ids"].append(batch_id)  # scheduled on Bolna: it will dial
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE batch SET bolna_batch_id = %s, status = 'scheduled' WHERE id = %s",
                        (bolna_batch_id, batch_id))
        conn.commit()
    except Exception as err:
        err.add_note(f"bolna batch {bolna_batch_id} is live")
        raise
    return len(claimed)


def _mark_campaign_live(conn, campaign_id: str, when: datetime | None, now: datetime) -> None:
    """Step 5. Never moves a paused, stopped or finished campaign."""
    status = "SCHEDULED" if when is not None and when > now + timedelta(minutes=2) else "RUNNING"
    with conn.cursor() as cur:
        cur.execute("UPDATE campaign SET status = %s, started_at = coalesce(started_at, %s), "
                    "scheduled_at = coalesce(%s, scheduled_at), updated_at = now() "
                    "WHERE id = %s AND status::text <> ALL(%s)",
                    (status, now, when, campaign_id, INACTIVE_CAMPAIGN_STATUSES))
    conn.commit()


def create_round(conn, campaign: dict, leads: list[dict], kind: str, when: datetime | None,
                 now: datetime | None = None) -> dict:
    """Create and schedule Bolna batches for `leads`, one batch per from-number. `when=None` means now.

    `leads` are lead rows as read from the database. A lead whose status or attempts have changed since,
    that is QUEUED, DONE or DNC, or that belongs to another campaign, is not dialled.

    Commits, in this order (this is what prevents untracked and double dials):
      0. Re-read the campaign's status (FOR UPDATE): PAUSED, STOPPED or COMPLETED -> return with skipped.
      1. guard_leads; DNC leads -> DNC, cooled leads -> DONE with their skip_tag (skipped_cooling or
         skipped_in_flight). Commit.
      2. Nothing dialable -> return with skipped.
      3. Group by from-number; round numbers continue from max(batch.round). A missing agent raises here.
      4. Per group: lock the group's phones (PHONE_LOCK); phones newly on the DNC list -> DNC; phones newly
         QUEUED in another campaign -> DONE/skipped_in_flight on a FIRST round, untouched on a REDIAL round;
         claim the leads; batch row 'creating' + leads QUEUED (commit, releasing the phone locks); Bolna create +
         schedule; then the batch is 'scheduled' (commit). On a Bolna error
         (commit, re-raise):
         - create failed, or create succeeded and the batch was stopped before its start time: batch
           'failed', leads still QUEUED restored;
         - create succeeded but the stop failed or the start time had passed: Bolna may dial it, so the
           batch is 'scheduled' with its Bolna id (the sync polls its real state) and the leads stay QUEUED.
      5. Campaign SCHEDULED (when > now+2min) or RUNNING, started_at set, unless it is paused, stopped or
         finished. Commit. This also runs when a group fails after any batch of this round may dial.
    """
    now = now or datetime.now(timezone.utc)
    for name, t in (("now", now), ("when", when)):
        if t is not None and t.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
    result = {"batch_ids": [], "leads": 0, "scheduled_at": None, "skipped": None, "dnc": [], "cooled": []}

    # 0. The campaign as it is now, not as the caller read it. The lock holds until step 1 commits.
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM campaign WHERE id = %s FOR UPDATE", (campaign["id"],))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"campaign {campaign['id']} not found")
    if row["status"] in INACTIVE_CAMPAIGN_STATUSES:
        conn.commit()
        return {**result, "skipped": CAMPAIGN_NOT_ACTIVE}

    # 1.
    dialable, dnc, cooled = guard_leads(conn, campaign, leads, kind, now)
    with conn.cursor() as cur:
        if dnc:
            cur.execute("UPDATE lead SET status = 'DNC' WHERE id = ANY(%s) AND campaign_id = %s",
                        ([lead["id"] for lead in dnc], campaign["id"]))
        for tag in (SKIPPED_COOLING, SKIPPED_IN_FLIGHT):
            ids = [lead["id"] for lead in cooled if lead["skip_tag"] == tag]
            if ids:
                cur.execute("UPDATE lead SET status = 'DONE', final_tag = %s "
                            "WHERE id = ANY(%s) AND campaign_id = %s AND status::text <> ALL(%s)",
                            (tag, ids, campaign["id"], list(UNDIALABLE_STATUSES)))
    conn.commit()
    result["dnc"] = [lead["phone"] for lead in dnc]
    result["cooled"] = [lead["phone"] for lead in cooled]

    # 2.
    if not dialable:
        return {**result, "skipped": NO_ELIGIBLE}

    # 3.
    policy = policy_of(campaign)
    groups: dict[str, list[dict]] = {}
    for lead in dialable:
        from_number = from_number_for(lead, campaign, policy) if kind == "REDIAL" else campaign["from_number"]
        groups.setdefault(from_number, []).append(lead)
    with conn.cursor() as cur:
        cur.execute("SELECT bolna_agent_id, variables FROM agent WHERE id = %s", (campaign["agent_id"],))
        agent = cur.fetchone()
        if agent is None:
            raise RuntimeError(f"agent {campaign['agent_id']} of campaign {campaign['id']} not found")
        cur.execute("SELECT coalesce(max(round), 0) AS n FROM batch WHERE campaign_id = %s", (campaign["id"],))
        round_no = int(cur.fetchone()["n"])
    scheduled_at = when or now + timedelta(seconds=60)

    # 4.
    queued = 0
    try:
        for from_number, group in groups.items():
            n = _send_group(conn, campaign, agent, kind, round_no + 1, from_number, group, scheduled_at, result)
            if n:
                round_no += 1
                queued += n
    except Exception:
        conn.rollback()  # every intended write is already committed
        if result["batch_ids"]:
            _mark_campaign_live(conn, campaign["id"], when, now)
        raise
    if not result["batch_ids"]:
        return {**result, "skipped": NO_ELIGIBLE}

    # 5.
    _mark_campaign_live(conn, campaign["id"], when, now)
    return {**result, "leads": queued, "scheduled_at": scheduled_at}


def maybe_auto_redial(conn, campaign: dict, now: datetime) -> dict | None:
    """Sync hook: open the next REDIAL round of a RUNNING campaign that has no batch that may still dial, inside the
    policy's calling window only. Outside it nothing is pre-scheduled at Bolna (a phone put on the DNC list meanwhile
    could not be taken out of such a batch); a sync inside the window opens the round."""
    p = policy_of(campaign)
    if not p["auto_redial"] or campaign["status"] != "RUNNING":
        return None
    if not in_window(now, p["window_start"], p["window_end"]):
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM batch WHERE campaign_id = %s AND status = ANY(%s) LIMIT 1",
                    (campaign["id"], ACTIVE_BATCH_STATUSES))
        if cur.fetchone():
            return None
    eligible = eligible_redial_leads(conn, campaign, now)
    if not eligible:
        return None
    return create_round(conn, campaign, eligible, "REDIAL", None, now=now)
