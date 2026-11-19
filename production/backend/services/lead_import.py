"""Lead-file import: parse CSV/XLSX, map columns to agent variables, build leads."""
from __future__ import annotations

import csv
import io
import re

from .classify import normalize_phone


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def parse_lead_file(data: bytes, filename: str) -> dict:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        it = wb.worksheets[0].iter_rows(values_only=True)
        header = next(it, None) or ()
        idx = [(i, _cell(h)) for i, h in enumerate(header)]
        idx = [(i, h) for i, h in idx if h]
        raw = [{h: _cell(row[i]) if i < len(row) else "" for i, h in idx} for row in it]
        wb.close()
        columns = [h for _, h in idx]
    elif name.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        columns = [c.strip() for c in (reader.fieldnames or []) if c and c.strip()]
        raw = [{c.strip(): _cell(v) for c, v in r.items() if c and c.strip()} for r in reader]
    else:
        raise ValueError("Upload a .csv or .xlsx file")
    rows = [r for r in raw if any(r.values())]
    if not rows:
        raise ValueError("The file has no rows")
    return {"columns": columns, "rows": rows, "total": len(rows)}


_HINTS = {
    "owner_name": ["ownername", "name", "owner", "fullname", "customername", "cpname"],
    "society_name": ["societyname", "society", "project", "projectname", "building"],
    "city": ["city", "location", "area"],
    "bhk": ["bhk", "configuration", "config", "unittype", "typology"],
    "size_sqft": ["sizesqft", "size", "area", "sqft", "superarea", "carpetarea"],
    "expected_price": ["expectedprice", "price", "askprice", "budget"],
    "timeline": ["timeline", "whentosell", "when"],
    "firm_name": ["firmname", "firm", "company", "agency"],
    "cp_status": ["cpstatus", "status", "activitycategory", "category", "segment"],
    "area": ["area", "locality", "sector"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def guess_column_map(columns: list[str], variables: list[str]) -> dict:
    cols = [(c, _norm(c)) for c in columns]

    def pick(*needles: str) -> str:
        for c, n in cols:
            if n in needles:
                return c
        for c, n in cols:
            if any(nd in n for nd in needles):
                return c
        return ""

    m = {"phone": pick("phone", "mobile", "contactnumber", "contact", "number", "phonenumber", "mobileno", "whatsapp")}
    for v in variables:
        direct = next((c for c, n in cols if n == _norm(v)), None)
        if direct is not None:
            m[v] = direct
        else:
            m[v] = pick(*_HINTS[v]) if v in _HINTS else pick(_norm(v))
    return m


def build_leads(rows: list[dict], column_map: dict, variables: list[str]) -> dict:
    seen: set[str] = set()
    invalid = duplicates = 0
    leads = []
    for r in rows:
        phone = normalize_phone(r.get(column_map.get("phone") or ""))
        if not phone:
            invalid += 1
            continue
        if phone in seen:
            duplicates += 1
            continue
        seen.add(phone)
        vars_ = {v: (r.get(column_map[v], "") if column_map.get(v) else "") for v in variables}
        name = vars_.get("owner_name") or vars_.get("name") or vars_.get("cp_name") or None
        leads.append({"phone": phone, "name": name, "variables": vars_})
    return {"leads": leads, "invalid": invalid, "duplicates": duplicates}


_BHK_WORDS = {
    "1": "वन", "2": "टू", "3": "थ्री", "4": "फ़ोर", "5": "फ़ाइव", "6": "सिक्स",
    "1.5": "वन पॉइंट फ़ाइव", "2.5": "टू पॉइंट फ़ाइव", "3.5": "थ्री पॉइंट फ़ाइव", "4.5": "फ़ोर पॉइंट फ़ाइव",
}


def spoken_bhk(v: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)", v)
    if not m:
        return v
    return _BHK_WORDS.get(m.group(1), m.group(1)) + " बी एच के"
