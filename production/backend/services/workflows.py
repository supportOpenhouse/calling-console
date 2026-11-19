"""Workflow engine: runs, triggers, result rules and hand-offs (port of the old lib/workflows.ts).

Takes `conn` and does not commit, except:
- `run_workflow` on the immediate path (inside the department window) commits the campaign and leads before
  `create_round` (which commits on its own) and, if the start fails, commits the fallback calendar run before
  re-raising. On the calendar path (also an immediate run outside the window) it does not commit: campaign, leads,
  run and `last_run_at` stamp go in the caller's one commit.
- `fire_triggers` commits the caller's open transaction, then after each workflow; a workflow that raises is
  rolled back and reported, and the others still run.
- `apply_result_rules` commits its writes together with the `rules_applied_at` stamp, then sends WhatsApp.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import Json, execute_values

from . import whatsapp
from .classify import DNC_TAGS, flatten_extracted, is_terminal_status
from .datasets import mark_dnc, queued_in, select_leads_for_run, touch_dataset_lead
from .ist import IST, in_window, next_window_start, parse_hhmm
from .lead_import import spoken_bhk
from .redial import NO_ELIGIBLE, create_round
from .scheduler import dept_config, plan_run

# A phone in a lead of one of these campaigns of the workflow is not added to a new run of it.
LIVE_CAMPAIGN_STATUSES = ["DRAFT", "SCHEDULED", "RUNNING", "PAUSED"]
# How far back a new-lead workflow that has never run looks for rows.
NEW_LEAD_LOOKBACK = timedelta(hours=24)
# Later new-lead runs look back to the last run minus this, so a row whose insert committed after that run with
# an earlier created_at (concurrent ingests, app/database clock skew) is still picked up. Phones the workflow
# already took are excluded, so the overlap never dials anyone twice.
NEW_LEAD_OVERLAP = timedelta(minutes=10)
AFTER_STEP_SCAN_LIMIT = 2000
# Extraction can land after the call: an untagged connected call waits this long before its rules run.
EXTRACTION_WAIT = timedelta(minutes=10)
HANDOFF_DUE = timedelta(hours=24)


# ---------------------------------------------------------------- runs

def _key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def map_variables(row: dict, variables: list[str], variable_map: dict) -> dict:
    """Map a dataset row onto the agent's template variables.

    A column named in `variable_map` wins; otherwise the same-name column (ignoring case and punctuation). An
    empty name / society / city falls back to the row's own field. `bhk` is spelled out; names keep the first word.
    """
    data = row.get("data") or {}
    by_key = {_key(k): v for k, v in data.items()}
    out = {}
    for v in variables:
        col = variable_map.get(v)
        if col:
            val = data.get(col)
        else:
            val = data.get(v)
            if val is None:
                val = by_key.get(_key(v))
        val = "" if val is None else str(val)
        if not val:
            if v in ("owner_name", "name", "cp_name"):
                val = row.get("name") or ""
            elif v in ("society_name", "society"):
                val = row.get("society") or ""
            elif v == "city":
                val = row.get("city") or ""
        if v == "bhk" and val:
            val = spoken_bhk(val)
        if v in ("owner_name", "name"):
            val = next(iter(val.split()), "")
        out[v] = val
    return out


def run_workflow(conn, wf: dict, *, requested_start=None, user_id=None, explicit_leads=None, name_suffix=None,
                 ignore_cooling=False, immediate=False, now=None) -> dict | None:
    """Create a campaign from a workflow and start it. Returns None when nobody is left to call.

    `explicit_leads` (dataset rows) replace the segment query (new leads, follow-up steps). A phone already in a
    live campaign of this workflow is never added. `ignore_cooling` skips the cooling guard here and, through
    `campaign.skip_cooling`, at dial time.

    The campaign goes on the department calendar (`plan_run`, at `requested_start` or as soon as possible), or with
    `immediate` is dialled through `create_round` straight away inside the department window; outside it, it goes
    on the calendar for the next window start instead (nothing is pre-scheduled at Bolna: a phone put on the DNC list
    overnight could not be taken out of such a batch) and dispatch starts it then. If an immediate start raises, the
    campaign is put on the calendar instead (committed) and the error is re-raised. See the module docstring for
    what commits when.
    """
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        # Serialises runs of one workflow (a sync tick and an ingest): the second waits here, then sees the
        # first one's committed leads as live.
        cur.execute("SELECT id FROM workflow WHERE id = %s FOR UPDATE", (wf["id"],))
        if cur.fetchone() is None:
            raise LookupError(f"workflow {wf['id']} not found")
        cur.execute("SELECT variables FROM agent WHERE id = %s", (wf["agent_id"],))
        agent = cur.fetchone()
    if agent is None:
        raise RuntimeError(f"agent {wf['agent_id']} of workflow {wf['name']} not found")

    cooled = 0
    if explicit_leads is not None:
        picked = list(explicit_leads)
    else:
        if not wf["dataset_id"]:
            raise ValueError(f"workflow {wf['name']} has no dataset")
        picked, cooled = select_leads_for_run(conn, wf["department"], wf["dataset_id"], wf["segment"] or {},
                                              ignore_cooling=ignore_cooling)
    if picked:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT l.phone FROM lead l JOIN campaign c ON c.id = l.campaign_id "
                        "WHERE c.workflow_id = %s AND c.status::text = ANY(%s) AND l.phone = ANY(%s)",
                        (wf["id"], LIVE_CAMPAIGN_STATUSES, [p["phone"] for p in picked]))
            live = {r["phone"] for r in cur.fetchall()}
        picked = [p for p in picked if p["phone"] not in live]
    if not picked:
        return None

    suffix = now.astimezone(IST).strftime("%d %b %H:%M") if name_suffix is None else name_suffix
    variable_map = wf["variable_map"] or {}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO campaign (name, agent_id, vertical, from_number, status, column_map, redial_policy, "
            "use_case_id, workflow_id, dataset_id, purpose, priority, step_order, skip_cooling, created_by_id, notes) "
            "VALUES (%s,%s,%s,%s,'DRAFT',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (f"{wf['name']} · {suffix}", wf["agent_id"], wf["department"], wf["from_number"], Json(variable_map),
             Json(wf["redial_policy"]), wf["use_case_id"], wf["id"], wf["dataset_id"], wf["purpose"], wf["priority"],
             wf["step_order"], ignore_cooling, user_id or wf["created_by_id"],
             f"{cooled} lead(s) skipped by the cooling guard" if cooled else None))
        campaign = cur.fetchone()
        inserted = execute_values(
            cur, "INSERT INTO lead (campaign_id, phone, name, variables, dataset_lead_id) VALUES %s "
                 "ON CONFLICT (campaign_id, phone) DO NOTHING RETURNING id",
            [(campaign["id"], p["phone"], p.get("name"), Json(map_variables(p, agent["variables"] or [], variable_map)),
              p.get("id")) for p in picked],
            page_size=500, fetch=True)
        cur.execute("UPDATE workflow SET last_run_at = %s WHERE id = %s", (now, wf["id"]))
    out = {"campaign_id": campaign["id"], "leads": len(inserted), "cooled": cooled}

    if immediate:
        cfg = dept_config(conn, wf["department"])
        if not in_window(now, cfg["window_start"], cfg["window_end"]):
            requested_start = next_window_start(now, cfg["window_start"], cfg["window_end"])
            immediate = False
    if not immediate:
        # Campaign, leads, run and stamp in one transaction: the caller's commit. A failure leaves nothing behind.
        run = plan_run(conn, dept=wf["department"], campaign_id=campaign["id"], use_case_id=wf["use_case_id"],
                       workflow_id=wf["id"], purpose=wf["purpose"], priority=wf["priority"],
                       requested_start=requested_start, user_id=user_id, now=now)
        return {**out, "run_id": run["id"], "planned_start": run["planned_start"]}

    conn.commit()  # create_round commits on its own; from here on every failure goes to the calendar fallback
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM lead WHERE campaign_id = %s AND status = 'PENDING' ORDER BY created_at, id",
                        (campaign["id"],))
            pending = cur.fetchall()
        result = create_round(conn, campaign, pending, "FIRST", None, now=now)
    except Exception as err:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM campaign WHERE id = %s", (campaign["id"],))
            status = cur.fetchone()["status"]
        # Still DRAFT = no batch of it can dial. Hand it to the calendar, which retries it every tick; otherwise
        # its leads stay live for this workflow and nobody calls them. (Not DRAFT = create_round marked it
        # live because Bolna may still dial a batch: leave it to the sync.)
        if status == "DRAFT":
            plan_run(conn, dept=wf["department"], campaign_id=campaign["id"], use_case_id=wf["use_case_id"],
                     workflow_id=wf["id"], purpose=wf["purpose"], priority=wf["priority"], requested_start=None,
                     note=f"immediate start failed: {err}", now=now)
            conn.commit()
        raise
    if result["skipped"] == NO_ELIGIBLE:  # everyone turned out DNC: nothing will ever dial this campaign
        with conn.cursor() as cur:
            cur.execute("UPDATE campaign SET status = 'COMPLETED', completed_at = %s "
                        "WHERE id = %s AND status = 'DRAFT'", (now, campaign["id"]))
    return {**out, "run_id": None, "planned_start": result["scheduled_at"]}


# ---------------------------------------------------------------- triggers (sync job and ingest)

def schedule_due(trigger: dict, last_run_at, now) -> bool:
    """SCHEDULE: every N minutes, or once a day at `time` (IST) on `days` (0 = Sunday; empty = every day)."""
    every = trigger.get("every_minutes")
    if every:
        return last_run_at is None or now - last_run_at >= timedelta(minutes=every)
    if not trigger.get("time"):
        raise ValueError("SCHEDULE trigger needs time or every_minutes")
    ist = now.astimezone(IST)
    days = trigger.get("days")
    if days and (ist.weekday() + 1) % 7 not in days:
        return False
    hh, mm = parse_hhmm(trigger["time"])
    due_today = ist.hour * 60 + ist.minute >= hh * 60 + mm
    ran_today = last_run_at is not None and last_run_at.astimezone(IST).date() == ist.date()
    return due_today and not ran_today


def _fire_schedule(conn, wf: dict, now) -> dict | None:
    if not schedule_due(wf["trigger"], wf["last_run_at"], now):
        return None
    r = run_workflow(conn, wf, now=now)
    if r is None:  # nothing to dial: don't retry every tick
        with conn.cursor() as cur:
            cur.execute("UPDATE workflow SET last_run_at = %s WHERE id = %s", (now, wf["id"]))
    return r


def _fire_new_leads(conn, wf: dict, now) -> dict | None:
    """Rows added since the last run, never called, not DNC, and not yet taken by this workflow: dial now."""
    if not wf["dataset_id"]:
        raise ValueError("ON_NEW_LEAD workflow has no dataset")
    since = wf["last_run_at"] - NEW_LEAD_OVERLAP if wf["last_run_at"] else now - NEW_LEAD_LOOKBACK
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dl.* FROM dataset_lead dl WHERE dl.dataset_id = %s AND dl.created_at >= %s "
            "AND dl.last_called_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM dnc_phone d WHERE d.phone = dl.phone) "
            "AND NOT EXISTS (SELECT 1 FROM lead l JOIN campaign c ON c.id = l.campaign_id "
            "                WHERE c.workflow_id = %s AND l.phone = dl.phone) "
            "ORDER BY dl.created_at, dl.id", (wf["dataset_id"], since, wf["id"]))
        fresh = cur.fetchall()
    if not fresh:
        return None
    return run_workflow(conn, wf, explicit_leads=fresh, name_suffix=f"new leads {len(fresh)}", ignore_cooling=True,
                        immediate=True, now=now)


def _row_from_lead(lead: dict) -> dict:
    """The dataset-row fields run_workflow reads, from a parent step's lead (a follow-up step without a dataset)."""
    variables = lead["variables"] or {}
    return {"id": lead["dataset_lead_id"], "phone": lead["phone"], "name": lead["name"], "data": variables,
            "city": None, "society": variables.get("society_name")}


def _fire_after_step(conn, wf: dict, now) -> dict | None:
    """Phones whose parent-step call matched `when_tags` / `when_outcomes` at least `delay_hours` ago, once each."""
    tr = wf["trigger"]
    if not wf["parent_id"]:
        raise ValueError("AFTER_STEP workflow has no parent step")
    if tr.get("delay_hours") is None:
        raise ValueError("AFTER_STEP trigger needs delay_hours")
    if not (tr.get("when_tags") or tr.get("when_outcomes")):  # never "every call of the parent"
        raise ValueError("AFTER_STEP trigger needs when_tags or when_outcomes")
    where = ["c.workflow_id = %s", "k.started_at <= %s"]
    params: list = [wf["parent_id"], now - timedelta(hours=tr["delay_hours"])]
    match = []
    if tr.get("when_tags"):
        match.append("k.tag = ANY(%s)")
        params.append(list(tr["when_tags"]))
    if tr.get("when_outcomes"):
        match.append("k.outcome::text = ANY(%s)")
        params.append(list(tr["when_outcomes"]))
    where.append(f"({' OR '.join(match)})")
    with conn.cursor() as cur:
        cur.execute(f"SELECT k.phone FROM call k JOIN campaign c ON c.id = k.campaign_id WHERE {' AND '.join(where)} "
                    "ORDER BY k.started_at DESC LIMIT %s", [*params, AFTER_STEP_SCAN_LIMIT])
        phones = list(dict.fromkeys(r["phone"] for r in cur.fetchall()))
        if not phones:
            return None
        cur.execute("SELECT DISTINCT l.phone FROM lead l JOIN campaign c ON c.id = l.campaign_id "
                    "WHERE c.workflow_id = %s AND l.phone = ANY(%s)", (wf["id"], phones))
        taken = {r["phone"] for r in cur.fetchall()}
        todo = [p for p in phones if p not in taken]
        if not todo:
            return None
        # source rows: the step's dataset row if there is one, else the parent step's lead
        by_phone: dict[str, dict] = {}
        if wf["dataset_id"]:
            cur.execute("SELECT * FROM dataset_lead WHERE dataset_id = %s AND phone = ANY(%s)",
                        (wf["dataset_id"], todo))
            by_phone = {r["phone"]: r for r in cur.fetchall()}
        cur.execute("SELECT l.* FROM lead l JOIN campaign c ON c.id = l.campaign_id "
                    "WHERE c.workflow_id = %s AND l.phone = ANY(%s) ORDER BY l.created_at DESC, l.id",
                    (wf["parent_id"], todo))
        for lead in cur.fetchall():
            by_phone.setdefault(lead["phone"], _row_from_lead(lead))
    return run_workflow(conn, wf, explicit_leads=list(by_phone.values()),
                        name_suffix=f"step {wf['step_order']} · {len(todo)}", ignore_cooling=True, now=now)


_FIRE = {"SCHEDULE": _fire_schedule, "ON_NEW_LEAD": _fire_new_leads, "AFTER_STEP": _fire_after_step}


def fire_triggers(conn, now=None, dataset_id=None) -> dict:
    """Fire every due SCHEDULE, ON_NEW_LEAD and AFTER_STEP workflow (the sync job), or with `dataset_id` only the
    active ON_NEW_LEAD workflows of that dataset (the ingest endpoint). Returns {fired, errors}."""
    now = now or datetime.now(timezone.utc)
    conn.commit()  # a failing workflow is rolled back; that must never take the caller's writes with it
    with conn.cursor() as cur:
        if dataset_id:
            cur.execute("SELECT * FROM workflow WHERE active AND dataset_id = %s AND trigger->>'type' = 'ON_NEW_LEAD' "
                        "ORDER BY created_at, id", (dataset_id,))
        else:
            cur.execute("SELECT * FROM workflow WHERE active ORDER BY created_at, id")
        workflows = cur.fetchall()
    fired, errors = [], []
    for wf in workflows:
        kind = wf["trigger"].get("type")
        if kind == "MANUAL":
            continue
        fire = _FIRE.get(kind)
        if fire is None:
            errors.append(f"{wf['name']}: unknown trigger type {kind}")
            continue
        try:
            r = fire(conn, wf, now)
            conn.commit()
        except Exception as e:
            conn.rollback()
            errors.append(f"{wf['name']}: {e}")
            continue
        if r:
            fired.append({"workflow": wf["name"], "type": kind, "leads": r["leads"]})
    return {"fired": fired, "errors": errors}


# ---------------------------------------------------------------- result rules and hand-offs (per finished call)

def rule_matches(rule: dict, call: dict) -> bool:
    when = rule.get("when") or {}
    outcomes = when.get("outcomes") or []
    tags = [t.lower() for t in when.get("tags") or []]
    return (bool(outcomes) and call["outcome"] in outcomes) or (bool(tags) and (call.get("tag") or "").lower() in tags)


def _block(conn, call: dict, lead: dict | None, source: str, reason: str) -> None:
    """DNC everywhere: the global list, and this lead."""
    mark_dnc(conn, call["phone"], source, reason=reason)
    if lead:
        with conn.cursor() as cur:
            cur.execute("UPDATE lead SET status = 'DNC' WHERE id = %s", (lead["id"],))


def _set_lead_by_rule(conn, lead: dict | None, kind: str, assignments: str, params: tuple, stale: bool) -> str:
    """A `stop` / `redial` rule's lead update. A DNC lead is never changed. A QUEUED lead is in a live batch: changing
    it could dial it a second time, so it is skipped ("<kind>:skipped:queued"). For a `stale` call (older than the
    lead's last call) it is skipped too ("<kind>:skipped:stale"): the newer call already decided the lead."""
    if stale:
        return f"{kind}:skipped:stale"
    if lead:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM lead WHERE id = %s FOR UPDATE", (lead["id"],))
            row = cur.fetchone()
            if row and row["status"] == "QUEUED":
                return f"{kind}:skipped:queued"
            if row and row["status"] != "DNC":
                cur.execute(f"UPDATE lead SET {assignments} WHERE id = %s", (*params, lead["id"]))
    return kind


def apply_result_rules(conn, call: dict, campaign: dict, lead: dict | None, *, now=None, stale=False) -> list[str]:
    """Run the DNC tags, the use-case taxonomy defaults and the workflow's result rules for one finished call, once.

    A tag in classify.DNC_TAGS always puts the phone on the DNC list (source 'call_tag'), whatever the campaign's use
    case or workflow; the taxonomy's dnc tags ('use_case_tag') and `dnc` rule actions ('rule') add to that.

    Returns what was done ("handoff", "dnc", "stop", "redial", "whatsapp:<template>"), plus "<action>:error:<msg>"
    for each action that could not run, "dnc:queued_in:<campaign name> (<campaign id>)" for each live batch that
    still holds a phone this call put on the DNC list, "stop/redial:skipped:queued" for a lead in a live batch and
    "stop/redial:skipped:stale" when `stale` (the call is older than the lead's last call; the sync passes it: DNC,
    hand-offs and WhatsApp still apply, the lead's status and next dial and the dataset row's last_* fields are left
    to the newer call; the dataset row still counts the attempt). Does nothing
    for a call that is unfinished, already has its rules applied, or is connected, untagged and under 10 minutes old
    (waiting for extraction).

    The writes commit together with the `rules_applied_at` stamp; WhatsApp messages are sent after that commit, so
    none is ever sent twice. A database error propagates with nothing committed: the caller rolls back and the
    next sync tries the call again.
    """
    now = now or datetime.now(timezone.utc)
    if not is_terminal_status(call["status"]) or call.get("rules_applied_at"):
        return []
    if (call["outcome"] == "CONNECTED" and not call.get("tag") and call.get("started_at")
            and now - call["started_at"] < EXTRACTION_WAIT):
        return []
    with conn.cursor() as cur:
        # Claim the call first: with a stale `call` row, or a second applier, the stamp is already there.
        cur.execute("UPDATE call SET rules_applied_at = %s WHERE id = %s AND rules_applied_at IS NULL RETURNING id",
                    (now, call["id"]))
        if cur.fetchone() is None:
            return []
        use_case = wf = None
        if campaign.get("use_case_id"):
            cur.execute("SELECT * FROM use_case WHERE id = %s", (campaign["use_case_id"],))
            use_case = cur.fetchone()
        if campaign.get("workflow_id"):
            cur.execute("SELECT * FROM workflow WHERE id = %s", (campaign["workflow_id"],))
            wf = cur.fetchone()
    fields = flatten_extracted(call.get("extracted"))
    tag = (call.get("tag") or "").lower()
    done: list[str] = []
    held: list[dict] | None = None  # live batches still holding the phone, read before the first block marks the lead

    def block(source: str, reason: str) -> None:
        nonlocal held
        if held is None:
            held = queued_in(conn, call["phone"])
        _block(conn, call, lead, source, reason)
        done.append("dnc")

    if call["outcome"] != "BALANCE_LOW":  # the dataset mirror
        touch_dataset_lead(conn, lead["dataset_lead_id"] if lead else None, call["phone"], call["outcome"],
                           call.get("tag"), call.get("started_at") or now, stale=stale)

    # the use case's taxonomy: good/soft tags -> hand-off, dnc tags -> DNC everywhere
    if use_case:
        tx = use_case["tag_taxonomy"]
        if tag in {t.lower() for t in [*tx["good"], *tx["soft"]]}:
            if create_handoff(conn, call, campaign, use_case, lead, fields, now=now):
                done.append("handoff")
        if tag in {t.lower() for t in tx["dnc"]}:
            block("use_case_tag", f"tag {call['tag']}")
    if tag in DNC_TAGS and "dnc" not in done:  # with or without a use case
        block("call_tag", call["tag"])

    # the workflow's own rules, on top
    templates: list[str] = []
    for rule in (wf["rules"] if wf else None) or []:
        if not rule_matches(rule, call):
            continue
        for action in rule.get("actions") or []:
            kind = action.get("type")
            try:
                if kind == "whatsapp":
                    if not action.get("template"):
                        raise ValueError("whatsapp needs a template")
                    templates.append(action["template"])  # sent after the commit
                elif kind == "handoff":
                    if create_handoff(conn, call, campaign, use_case, lead, fields, action, now=now):
                        done.append("handoff")
                elif kind == "dnc":
                    block("rule", f"result rule of workflow {wf['name']}")
                elif kind == "stop":
                    done.append(_set_lead_by_rule(conn, lead, "stop", "status = 'DONE'", (), stale))
                elif kind == "redial":
                    if action.get("after_minutes") is None:
                        raise ValueError("redial needs after_minutes")
                    due = now + timedelta(minutes=float(action["after_minutes"]))
                    done.append(_set_lead_by_rule(conn, lead, "redial", "status = 'NOT_CONNECTED', next_dial_at = %s",
                                                  (due,), stale))
                elif kind != "next_step":  # next_step: the AFTER_STEP child workflow picks the call up
                    raise ValueError(f"unknown action type {kind}")
            except psycopg2.Error:
                raise
            except Exception as e:
                done.append(f"{kind}:error:{str(e)[:80]}")
    done += [f"dnc:queued_in:{q['campaign_name']} ({q['campaign_id']})" for q in held or []]
    conn.commit()

    body_values = [(lead or {}).get("name") or "", ((lead or {}).get("variables") or {}).get("society_name") or ""]
    for template in templates:
        try:
            whatsapp.send_template(call["phone"], template, body_values)
            done.append(f"whatsapp:{template}")
        except Exception as e:
            done.append(f"whatsapp:error:{str(e)[:80]}")
    return done


def create_handoff(conn, call: dict, campaign: dict, use_case: dict | None, lead: dict | None, fields: dict,
                   action: dict | None = None, *, now=None) -> bool:
    """Hand the call to the use case's team (or the rule's `team`), due in 24 hours. One per phone per campaign:
    returns False when it already exists (unique index handoff_phone_campaign_uq, migration 002)."""
    now = now or datetime.now(timezone.utc)
    details = {**((lead or {}).get("variables") or {}), **fields}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO handoff (department, use_case_id, campaign_id, call_id, phone, name, tag, summary, details, "
            "team, due_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (phone, campaign_id) WHERE campaign_id IS NOT NULL DO NOTHING RETURNING id",
            (campaign["vertical"], campaign.get("use_case_id"), campaign["id"], call["id"], call["phone"],
             (lead or {}).get("name") or details.get("owner_name") or None, call.get("tag"),
             call.get("summary") or fields.get("owner_questions") or None, Json(details),
             (action or {}).get("team") or (use_case["name"] if use_case else None), now + HANDOFF_DUE))
        return cur.fetchone() is not None
