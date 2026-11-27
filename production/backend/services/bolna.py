"""Server-side Bolna API client (ported from the old lib/bolna.ts)."""
from __future__ import annotations

import copy
import csv
import io
import re
from datetime import datetime, timezone

import requests

from .. import config

TIMEOUT = 30

# Fields Bolna rejects on PUT.
READONLY_AGENT_FIELDS = [
    "id", "agent_id", "created_at", "updated_at", "created_by", "parent_agent_id", "source_agent_id",
    "agent_status", "creation_source", "restricted", "ingest_lookup_key", "agent_prompts", "dispositions",
    "inbound_phone_number", "ingest_source_config",
]


class BolnaError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Bolna {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


def _req(method: str, path: str, *, json=None, data=None, files=None):
    key = config.BOLNA_API_KEY
    if not key:
        raise RuntimeError("BOLNA_API_KEY is not set")
    url = config.BOLNA_BASE_URL.rstrip("/") + path
    res = requests.request(
        method, url, headers={"Authorization": f"Bearer {key}"},
        json=json, data=data, files=files, timeout=TIMEOUT,
    )
    if not 200 <= res.status_code < 300:
        raise BolnaError(res.status_code, res.text)
    try:
        return res.json()
    except ValueError:
        raise BolnaError(res.status_code, res.text) from None


def list_agents() -> list[dict]:
    return _req("GET", "/v2/agent/all")


def get_agent(agent_id: str) -> dict:
    return _req("GET", f"/v2/agent/{agent_id}")


def update_agent(agent_id: str, agent: dict, system_prompt: str | None = None) -> dict:
    cfg = copy.deepcopy(agent)
    for k in READONLY_AGENT_FIELDS:
        cfg.pop(k, None)
    prompt = system_prompt if system_prompt is not None else (
        ((agent.get("agent_prompts") or {}).get("task_1") or {}).get("system_prompt") or ""
    )
    try:
        cfg["tasks"][0]["tools_config"]["llm_agent"]["llm_config"]["prompt"] = prompt
    except (KeyError, IndexError, TypeError):
        pass  # shape differs; Bolna takes agent_prompts anyway
    return _req("PUT", f"/v2/agent/{agent_id}", json={
        "agent_config": cfg, "agent_prompts": {"task_1": {"system_prompt": prompt}},
    })


def agent_template_variables(agent: dict) -> list[str]:
    text = "\n".join([
        agent.get("agent_welcome_message") or "",
        ((agent.get("agent_prompts") or {}).get("task_1") or {}).get("system_prompt") or "",
    ])
    return list(dict.fromkeys(re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", text)))


def list_knowledgebases() -> list[dict]:
    return _req("GET", "/knowledgebase/all")


def get_knowledgebase(rag_id: str) -> dict:
    return _req("GET", f"/knowledgebase/{rag_id}")


def create_knowledgebase(fields: dict, pdf: tuple[str, bytes] | None = None) -> dict:
    """POST /knowledgebase as multipart/form-data: `fields` (url, chunk_size, similarity_top_k, overlapping,
    language_support -- whichever are set) and/or a PDF as (filename, bytes). Every part goes in `files`, so the body
    is multipart even without a file."""
    parts = {k: (None, str(v)) for k, v in fields.items()}
    if pdf:
        parts["file"] = (pdf[0], pdf[1], "application/pdf")
    return _req("POST", "/knowledgebase", files=parts)


def delete_knowledgebase(rag_id: str) -> dict:
    return _req("DELETE", f"/knowledgebase/{rag_id}")


def list_agent_executions(agent_id: str, page: int = 1, page_size: int = 50) -> tuple[list[dict], bool]:
    r = _req("GET", f"/v2/agent/{agent_id}/executions?page_number={page}&page_size={page_size}")
    if isinstance(r, list):
        return r, len(r) == page_size
    data = r.get("data") or []
    has_more = r.get("has_more")
    return data, (len(data) == page_size) if has_more is None else bool(has_more)


def get_batch(batch_id: str) -> dict:
    return _req("GET", f"/batches/{batch_id}")


def create_batch(agent_id: str, from_number: str, csv_text: str, filename: str = "contacts.csv") -> dict:
    return _req("POST", "/batches",
                data={"agent_id": agent_id, "from_phone_number": from_number},
                files={"file": (filename, csv_text, "text/csv")})


def schedule_batch(batch_id: str, when: datetime) -> dict:
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    iso = when.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return _req("POST", f"/batches/{batch_id}/schedule", data={"scheduled_at": iso})


def stop_batch(batch_id: str) -> dict:
    return _req("POST", f"/batches/{batch_id}/stop")


def place_call(agent_id: str, to: str, from_number: str, user_data: dict) -> dict:
    return _req("POST", "/call", json={
        "agent_id": agent_id, "recipient_phone_number": to,
        "from_phone_number": from_number, "user_data": user_data,
    })


def list_phone_numbers() -> list[dict]:
    return _req("GET", "/phone-numbers/all")


def build_batch_csv(rows: list[dict], variables: list[str]) -> str:
    """contact_number first, then one column per template variable."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["contact_number", *variables])
    for r in rows:
        w.writerow([r["phone"], *[(r.get("variables") or {}).get(v) or "" for v in variables]])
    return buf.getvalue().rstrip("\n")
