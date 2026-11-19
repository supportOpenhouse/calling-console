"""Row factories for tests. Each inserts a row and returns it as a dict."""
import uuid
from datetime import datetime, timezone

from psycopg2.extras import Json

from ..services.taxonomy import DEFAULT_TAXONOMY


def _insert(conn, table, **cols):
    names = ", ".join(f'"{k}"' for k in cols)
    marks = ", ".join(["%s"] * len(cols))
    with conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks}) RETURNING *', list(cols.values()))
        return cur.fetchone()


def _uid():
    return uuid.uuid4().hex[:12]


def make_user(conn, *, role="MANAGER", vertical="SUPPLY", email=None, use_case_ids=(), active=True):
    email = email or f"user-{_uid()}@example.com"
    return _insert(conn, "user", name=email.split("@")[0], email=email, role=role, vertical=vertical,
                   use_case_ids=Json(list(use_case_ids)), active=active)


def make_agent(conn, *, vertical="SUPPLY", variables=("owner_name", "society_name"), bolna_agent_id=None,
               default_from_number="+918000000001"):
    bid = bolna_agent_id or f"bolna-agent-{_uid()}"
    return _insert(conn, "agent", bolna_agent_id=bid, name=f"Agent {bid}", vertical=vertical,
                   variables=Json(list(variables)), default_from_number=default_from_number)


def make_use_case(conn, *, department="SUPPLY", key="seller_qualification", tag_taxonomy=None):
    taxonomy = DEFAULT_TAXONOMY if tag_taxonomy is None else tag_taxonomy
    return _insert(conn, "use_case", department=department, key=key, name=key.replace("_", " ").title(),
                   tag_taxonomy=Json(taxonomy))


def make_dataset(conn, *, department="SUPPLY", use_case_id=None, rows=()):
    ds = _insert(conn, "dataset", department=department, use_case_id=use_case_id, name=f"Dataset {_uid()}",
                 row_count=len(rows))
    for r in rows:
        _insert(conn, "dataset_lead", dataset_id=ds["id"], phone=r["phone"], name=r.get("name"),
                city=r.get("city"), society=r.get("society"), segment=r.get("segment"),
                data=Json(r.get("data") or {}))
    return ds


DEFAULT_REDIAL_POLICY = {
    "max_attempts": 3, "gap_minutes": 60, "window_start": "10:00", "window_end": "19:00",
    "retry_outcomes": ["NOT_PICKED", "VOICEMAIL"], "retry_tags": [], "rotate_numbers": False, "auto_redial": True,
}


def make_campaign(conn, *, agent, vertical="SUPPLY", status="RUNNING", redial_policy=None, skip_cooling=False,
                  workflow_id=None, use_case_id=None, from_number="+918000000001"):
    policy = dict(DEFAULT_REDIAL_POLICY) if redial_policy is None else redial_policy
    return _insert(conn, "campaign", name=f"Campaign {_uid()}", agent_id=agent["id"], vertical=vertical,
                   from_number=from_number, status=status, redial_policy=Json(policy), skip_cooling=skip_cooling,
                   workflow_id=workflow_id, use_case_id=use_case_id)


def make_lead(conn, campaign, *, phone, status="PENDING", attempts=0, last_call_at=None, last_outcome=None,
              final_tag=None, variables=None, dataset_lead_id=None):
    return _insert(conn, "lead", campaign_id=campaign["id"], phone=phone, status=status, attempts=attempts,
                   last_call_at=last_call_at, last_outcome=last_outcome, final_tag=final_tag,
                   variables=Json(variables or {}), dataset_lead_id=dataset_lead_id)


def make_batch(conn, campaign, *, status="scheduled", total=1, bolna_batch_id=None, scheduled_at=None, round=1):
    return _insert(conn, "batch", campaign_id=campaign["id"], status=status, total=total,
                   bolna_batch_id=bolna_batch_id, scheduled_at=scheduled_at, round=round,
                   from_number=campaign["from_number"])


def make_call(conn, *, phone, outcome="CONNECTED", started_at, status="completed", campaign=None, lead=None,
              batch=None, tag=None, agent=None, bolna_execution_id=None):
    agent_id = agent["id"] if agent else (campaign["agent_id"] if campaign else None)
    return _insert(conn, "call", bolna_execution_id=bolna_execution_id or f"exec-{_uid()}", agent_id=agent_id,
                   campaign_id=campaign["id"] if campaign else None, lead_id=lead["id"] if lead else None,
                   batch_id=batch["id"] if batch else None, phone=phone,
                   from_number=campaign["from_number"] if campaign else None, status=status, outcome=outcome,
                   started_at=started_at, tag=tag)


def bolna_execution(**overrides) -> dict:
    ex = {
        "id": f"exec-{_uid()}",
        "agent_id": "bolna-agent-1",
        "batch_id": None,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "conversation_duration": 60,
        "transcript": "assistant: नमस्ते\nuser: हां जी बोलिए",
        "telephony_data": {
            "to_number": "+919999900001",
            "from_number": "+918000000001",
            "hangup_provider_code": 4000,
            "recording_url": "https://example.com/rec.mp3",
        },
        "extracted_data": {"Seller Details": {"call_tag": {"objective": "qualified"}}},
        "total_cost": 4.1,
    }
    ex.update(overrides)
    return ex
