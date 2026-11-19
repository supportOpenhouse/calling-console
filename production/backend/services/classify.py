"""Classify a Bolna execution into an outcome. Rule-for-rule port of the old classify.ts."""
import json
import math
import re
from datetime import datetime, timezone

HANGUP_CODE_LABEL = {
    3010: "Carrier blocked", 3080: "Carrier error", 3020: "No answer",
    3040: "Declined / busy", 4010: "Voicemail", 4000: "Normal",
}

TERMINAL_STATUSES = {
    "completed", "call-disconnected", "no-answer", "busy", "failed", "error",
    "balance-low", "canceled", "cancelled",
}

TERMINAL_TAGS = [
    "qualified", "qualified_no_price", "want_to_sell", "want_to_sell_later", "sell_later", "not_selling",
    "already_sold", "wrong_person", "buyer_lead", "not_eligible", "dnc", "do_not_call", "angry",
    "hot_buyer", "no_buyer", "not_interested", "booked",
]
DNC_TAGS = ["dnc", "do_not_call", "angry", "wrong_person"]

_TAG_KEYS = ["call_tag", "tag", "lead_tag", "disposition", "owner_intent", "intent", "cp_intent"]
_TURN = re.compile(r"^\s*(assistant|agent|user)\s*:\s*(.*)$", re.I)
_CODE_IN_REASON = re.compile(r"\b(3010|3080|3020|3040|4010|4000)\b")
_VOICEMAIL_REASON = re.compile(r"voicemail|answering machine", re.I)


def is_terminal_status(status) -> bool:
    return (status or "").lower() in TERMINAL_STATUSES


def _num(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _is_echo(user_text: str, agent_text: str) -> bool:
    u = user_text.split()
    a = set(agent_text.split())
    if len(u) < 4 or not a:
        return False
    return sum(1 for w in u if w in a) / len(u) >= 0.5


def count_human_turns(transcript) -> int:
    if not transcript:
        return 0
    prev_agent, turns = "", 0
    for line in re.split(r"\r?\n", transcript):
        m = _TURN.match(line)
        if not m:
            continue
        who, text = m.group(1).lower(), m.group(2).strip()
        if who == "user":
            if text and not _is_echo(text, prev_agent):
                turns += 1
        else:
            prev_agent = text
    return turns


def _objective(v):
    if isinstance(v, dict):
        for k in ("objective", "subjective", "value"):
            if v.get(k) is not None:
                return v[k]
        return None
    return v


def extract_tag(extracted) -> str | None:
    def walk(obj, depth=0):
        if depth > 4:
            return None
        if isinstance(obj, list):
            for v in obj:
                r = walk(v, depth + 1)
                if r:
                    return r
            return None
        if not isinstance(obj, dict):
            return None
        for k in _TAG_KEYS:
            if k in obj:
                v = obj[k]
                s = v if isinstance(v, str) else (_objective(v) if isinstance(v, dict) else None)
                if isinstance(s, str) and s:
                    return s
        for v in obj.values():
            r = walk(v, depth + 1)
            if r:
                return r
        return None

    return walk(extracted) if extracted else None


def _js_str(v) -> str:
    """Stringify like JS String(v ?? "")."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


def flatten_extracted(extracted) -> dict[str, str]:
    out: dict[str, str] = {}
    if not extracted:
        return out
    for cat, fields in extracted.items():
        if not isinstance(fields, dict):
            out[cat] = _js_str(fields)
            continue
        for name, v in fields.items():
            if isinstance(v, dict):
                val = _objective(v)
                if val is None:
                    val = ""
                out[name] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, separators=(",", ":"))
            else:
                out[name] = _js_str(v)
    return out


def _parse_utc(ts) -> datetime | None:
    if not ts:
        return None
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def classify_execution(e: dict) -> dict:
    t = e.get("telephony_data") or {}
    status = (e.get("status") or "").lower()
    reason = t.get("hangup_reason") or ""
    code = _num(t.get("hangup_provider_code"))
    if code is None:
        code = _num(t.get("provider_hangup_code"))
    if code is None:
        code = _num(e.get("hangup_code"))
    if code is None:
        m = _CODE_IN_REASON.search(reason)
        if m:
            code = float(m.group(1))
    code = int(code) if code is not None else None
    dur = _num(e.get("conversation_duration"))
    if dur is None:
        dur = _num(t.get("duration")) or 0
    duration_sec = int(math.floor(dur + 0.5))
    transcript = e.get("transcript")
    human_turns = count_human_turns(transcript)
    extracted = e.get("extracted_data") or None
    tag = extract_tag(extracted)

    outcome = "UNKNOWN"
    if status == "balance-low":
        outcome = "BALANCE_LOW"
    elif code == 3010:
        outcome = "CARRIER_BLOCKED"
    elif code == 4010 or _VOICEMAIL_REASON.search(reason) or tag == "voicemail":
        outcome = "VOICEMAIL"
    elif code == 3080 or status in ("failed", "error"):
        outcome = "FAILED"
    elif code in (3020, 3040) or status in ("no-answer", "busy", "canceled", "cancelled"):
        outcome = "NOT_PICKED"
    elif status in ("completed", "call-disconnected") or code == 4000:
        outcome = "CONNECTED" if human_turns > 0 else "NOT_PICKED"
        if human_turns == 0 and duration_sec >= 25:
            outcome = "VOICEMAIL"  # long silent connects are almost always voicemail
        if outcome == "CONNECTED" and not tag and human_turns <= 1 and duration_sec < 20:
            outcome = "NOT_PICKED"  # hung up on the opener

    recipient = (e.get("context_details") or {}).get("recipient_data") or {}
    started = _parse_utc(e.get("created_at"))
    return {
        "outcome": outcome,
        "hangup_code": code,
        "hangup_reason": reason or None,
        "duration_sec": duration_sec,
        "human_turns": human_turns,
        "tag": tag,
        "summary": e.get("summary"),
        "phone": str(t.get("to_number") or recipient.get("contact_number") or ""),
        "from_number": t.get("from_number"),
        "started_at": started,
        "cost": _num(e.get("total_cost")),
        "recording_url": t.get("recording_url"),
        "transcript": transcript,
        "extracted": extracted,
    }


def lead_status_for(outcome: str, tag: str | None) -> str:
    t = (tag or "").lower()
    if t in DNC_TAGS:
        return "DNC"
    if outcome == "CONNECTED":
        return "DONE" if t in TERMINAL_TAGS else "CONNECTED"
    return {
        "CARRIER_BLOCKED": "BLOCKED", "VOICEMAIL": "VOICEMAIL", "FAILED": "FAILED",
        "BALANCE_LOW": "PENDING",
    }.get(outcome, "NOT_CONNECTED")


def normalize_phone(raw) -> str | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float):
        if not math.isfinite(raw) or raw != int(raw):
            return None
        s = str(int(raw))
    elif isinstance(raw, int):
        s = str(raw)
    else:
        s = str(raw).strip()
        if "e" in s.lower():
            return None
        if re.fullmatch(r"\d+\.0", s):
            s = s[:-2]
    s = re.sub(r"[^\d+]", "", s)
    if not s:
        return None
    if s.startswith("+"):
        s = s[1:]
    s = s.lstrip("0")
    if len(s) == 10:
        s = "91" + s
    if len(s) == 12 and s.startswith("91"):
        return "+" + s
    if 11 <= len(s) <= 15:
        return "+" + s
    return None
