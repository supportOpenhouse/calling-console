from io import BytesIO

import pytest
from openpyxl import Workbook

from backend.services.lead_import import build_leads, guess_column_map, parse_lead_file, spoken_bhk


def test_parse_xlsx_numeric_phone():
    wb = Workbook()
    ws = wb.active
    ws.append(["Phone", "Name"])
    ws.append([9810043058, "Ram"])
    ws.append([9810043059.0, "Sam"])
    buf = BytesIO()
    wb.save(buf)
    r = parse_lead_file(buf.getvalue(), "x.xlsx")
    assert r["rows"][0]["Phone"] == "9810043058"
    assert r["rows"][1]["Phone"] == "9810043059"
    assert r["total"] == 2 and r["columns"] == ["Phone", "Name"]


def test_parse_csv_utf8_bom():
    r = parse_lead_file(b"\xef\xbb\xbfPhone,Name\n9810043058,\xe0\xa4\xb0\xe0\xa4\xbe\xe0\xa4\xae", "a.csv")
    assert r["columns"] == ["Phone", "Name"]
    assert r["rows"][0]["Name"] == "राम"


def test_parse_rejects_pdf_and_empty():
    with pytest.raises(ValueError, match="Upload a .csv or .xlsx file"):
        parse_lead_file(b"x", "a.pdf")
    with pytest.raises(ValueError, match="The file has no rows"):
        parse_lead_file(b"Phone,Name\n", "a.csv")


def test_guess_column_map():
    m = guess_column_map(["Mobile No", "Owner Name", "Society"], ["owner_name", "society_name"])
    assert m == {"phone": "Mobile No", "owner_name": "Owner Name", "society_name": "Society"}


def test_build_leads_dedup_and_invalid():
    rows = [{"P": "9810043058", "N": "A"}, {"P": "+91 98100 43058", "N": "B"}, {"P": "abc", "N": "C"}]
    r = build_leads(rows, {"phone": "P", "owner_name": "N"}, ["owner_name"])
    assert len(r["leads"]) == 1 and r["duplicates"] == 1 and r["invalid"] == 1
    assert r["leads"][0]["name"] == "A"


def test_spoken_bhk():
    assert spoken_bhk("3 BHK") == "थ्री बी एच के"
    assert spoken_bhk("2.5") == "टू पॉइंट फ़ाइव बी एच के"
    assert spoken_bhk("studio") == "studio"
