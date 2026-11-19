# Calling Console (Python backend + Vite frontend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Openhouse Calling Console as `production/backend` (Flask on Render) plus `production/frontend` (Vite + React on Vercel), copying Direct_Inventory's patterns, with every product-spec feature working and every review fix in.

**Architecture:** The Flask API (a package named `backend`, raw SQL through psycopg2, numbered SQL migrations) owns all data and all Bolna/R2/Direct_Inventory traffic, and runs the 5-minute sync in a background thread guarded by an advisory lock. The Vite single-page app talks only to `/api/*`, which Vercel rewrites to Render. Every Bolna dial goes through one guard (`create_round` / `assert_dialable`).

**Tech Stack:** Python 3.12.7, Flask 3.0.3, psycopg2, PyJWT, google-auth, gunicorn, boto3, openpyxl, pytest · Vite 5, React 18, react-router-dom 6, @react-oauth/google · Neon Postgres · Cloudflare R2.

**Spec:** `docs/superpowers/specs/2026-11-18-calling-console-split-design.md`. Read it before starting. The product behaviour lives in `initial zip/Openhouse_Calling_Console.md`, and the old TypeScript code under `initial zip/` is the behavioural reference wherever a task says "port".

## Global Constraints

- **Folder isolation.** All backend files go under `production/backend/` and all frontend files under `production/frontend/`. Nothing imports across them.
- **Where commands run.** The backend is the Python package `backend` (relative imports). Run every backend command from `production/`.
- **Python version.** 3.12.7 in `runtime.txt` (`python-3.12.7`) and `.python-version` (`3.12.7`). Any local 3.12.x is fine for development.
- **`backend/requirements.txt`, exactly these pins:**
  - Flask==3.0.3
  - flask-cors==4.0.1
  - flask-compress==1.24
  - psycopg2-binary==2.9.9
  - PyJWT==2.9.0
  - gunicorn==22.0.0
  - google-auth==2.34.0
  - requests==2.32.3
  - python-dotenv==1.0.1
  - boto3==1.35.36
  - openpyxl==3.1.5
- **`backend/requirements-dev.txt`:** pytest==8.3.3.
- **`frontend/package.json` versions** (DI's): react ^18.3.1, react-dom ^18.3.1, react-router-dom ^6.26.2, @react-oauth/google ^0.12.1, motion ^13.1.1; dev: vite ^5.4.8, @vitejs/plugin-react ^4.3.1. Nothing else unless a task names it.
- **JSON conventions.** Keys are snake_case and equal to the database column names. Request bodies are snake_case too. Timestamps are ISO-8601 UTC. Errors come back as `{"error": "<message>"}` with the right HTTP status. Lists come back as `{"items": [...]}`; paged lists add `"total"` and take `?page=` (50 per page).
- **jsonb config keys are snake_case:**
  - redial policy: `max_attempts, gap_minutes, window_start, window_end, retry_outcomes, retry_tags, rotate_numbers, auto_redial`
  - trigger: `type, days, time, every_minutes, within_minutes, delay_hours, when_tags, when_outcomes`
  - segment: `cities, societies, segments, never_called, last_tag_in, last_outcome_in, max_attempts, limit`
  - rule: `{when:{outcomes,tags}, actions:[{type, after_minutes, template, team, note}]}`
  - tag taxonomy: `good, soft, terminal, dnc, retry`
- **Rule action types:** `redial`, `whatsapp`, `handoff`, `next_step`, `dnc`, `stop`. There is no `crm_push`.
- **Times.** All datetimes are timezone-aware UTC in code and `timestamptz` in the database. IST maths uses `zoneinfo.ZoneInfo("Asia/Kolkata")`.
- **No silent fallbacks.** An unconfigured integration reports `configured: false`. A failing external call raises, or records into the sync report's `errors`, and is visible on the Integrations page.
- **Every Bolna dial goes through `create_round` or `assert_dialable` (Task 7).** No other code calls `bolna.create_batch` or `bolna.place_call`.
- **Commits.** The executor never commits; the user commits. Every task ends with `git status --short production/ docs/` and a list of the changed files.
- **Backend tests:** `cd production && backend/.venv/bin/python -m pytest backend/tests -q`. Database tests use `TEST_DATABASE_URL=postgresql://localhost/calling_console_test`.

## Review Focus

1. **Lead-file phone formats.** Excel numeric phone cells (`9810043058`, `9810043058.0`) and `+91 98100 43058` / `09810043058` must normalise to `+919810043058`. Scientific notation (`9.81E+09`) must be counted invalid, never dialled as a wrong number. Test owners: Task 3 (`normalize_phone`) and Task 4 (XLSX cells).
2. **Bolna failing for one agent mid-sync** (5xx, HTML body, timeout). The error is recorded in `report["errors"]`, other agents still sync, and the lock is released. Test owner: Task 11.
3. **A bad calling window.** A department or policy window with `window_start >= window_end` or a malformed `HH:MM` is rejected with a 400, so the planner can never loop. Test owners: Tasks 8 and 12.
4. **The same phone posted to ingest twice within a minute** is dialled once. Test owner: Task 13.
5. **A user deactivated or re-roled while holding a valid 7-day token.** The next request returns 401, or applies the new role. Test owner: Task 5.

---

## Phase A: Backend (`production/backend`)

### Task 1: Scaffold, config, DB pool, app factory, health

**Files:**
- Create:
  - `production/backend/__init__.py` (empty), `app.py`, `wsgi.py`, `config.py`, `db.py`
  - `services/__init__.py`, `services/errors.py`
  - `api/__init__.py`
  - `tests/__init__.py`, `tests/conftest.py`, `tests/test_app.py`
  - `requirements.txt`, `requirements-dev.txt`, `runtime.txt`, `.python-version`, `.gitignore`, `.env.example`

**Interfaces:**
- Produces:
  - `backend.config` module constants:
    - `DATABASE_URL`
    - `JWT_SECRET`, `JWT_ALGORITHM="HS256"`, `JWT_EXPIRY_HOURS=168`
    - `GOOGLE_OAUTH_CLIENT_ID`, `ALLOWED_EMAIL_DOMAIN` (default `"openhouse.in"`)
    - `ADMIN_EMAILS: list[str]` (comma-split, stripped, lowercased)
    - `FRONTEND_ORIGIN` (default `"http://localhost:5173"`)
    - `BOLNA_API_KEY`, `BOLNA_BASE_URL` (default `"https://api.bolna.ai"`)
    - `S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY`, `S3_REGION` (default `"auto"`)
    - `INGEST_TOKEN`, `DI_AI_CALLS_URL`, `DI_AI_CALLS_TOKEN`
    - `INTERAKT_API_KEY, INTERAKT_FROM`
    - `SYNC_ENABLED` (default `"1"`), `LOG_LEVEL` (default `"INFO"`)
  - `backend.db.get_conn()`
  - `backend.services.errors`:
    - `ApiError(status: int, message: str)`
    - `bad_request(msg) -> ApiError` (400), `forbidden(msg="forbidden")` (403), `not_found(msg="not found")` (404), `conflict(msg)` (409)
  - `backend.app.create_app() -> Flask` and the module-level `app`.

- [ ] **Step 1: Local setup (one-time).** From `production/`:
  - `brew services start postgresql@17`
  - `createdb calling_console_test`
  - `createdb calling_console_dev`
  - `python3.12 -m venv backend/.venv`

  Write both requirements files with the exact pins from Global Constraints, then run `backend/.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt`.
  Expected: pip finishes with no errors.

- [ ] **Step 2: Write the failing tests.** `tests/conftest.py`, before any `backend` import, sets these environment variables:
  - `DATABASE_URL` = the value of `TEST_DATABASE_URL`, defaulting to `postgresql://localhost/calling_console_test`
  - `JWT_SECRET=test-secret`
  - `FRONTEND_ORIGIN=http://localhost:5173`
  - `BOLNA_API_KEY=test`
  - `GOOGLE_OAUTH_CLIENT_ID` = empty

  It also defines a `client` fixture: `create_app().test_client()`.
```python
# tests/test_app.py
def test_health_reports_db_connected(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json() == {"status": "ok", "db": "connected"}

def test_api_error_is_json(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and "error" in r.get_json()

def test_create_app_requires_jwt_secret(monkeypatch):
    from backend import config, app as appmod
    monkeypatch.setattr(config, "JWT_SECRET", "")
    import pytest
    with pytest.raises(RuntimeError):
        appmod.create_app()

def test_cors_allows_frontend_origin(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
```

- [ ] **Step 3: Run it.** `backend/.venv/bin/python -m pytest backend/tests/test_app.py -q`
  Expected: FAIL (`ModuleNotFoundError: backend.app`).

- [ ] **Step 4: Implement.**
  - **`config.py`:** DI's `config.py` structure (load `backend/.env` then the root `.env` with python-dotenv; module constants) with the variables above.
  - **`db.py`:** DI's `backend/db.py` copied verbatim, minus `get_props_conn` and `get_cp_conn`.
  - **`app.py`:** DI's `create_app` shape:
    - raise `RuntimeError` if `JWT_SECRET` or `DATABASE_URL` is empty;
    - the `_JSONProvider` for `Decimal` (and `datetime` → `.isoformat()`);
    - `CORS(app, origins=[config.FRONTEND_ORIGIN], supports_credentials=False)` and `Compress(app)`;
    - `GET /api/health` pinging `get_conn()`;
    - error handlers that turn `ApiError` into `{error}` with its status, and turn werkzeug `HTTPException` (404, 405) into `{error: e.description}`;
    - blueprints are registered from a list that later tasks append to.
  - **`wsgi.py`:** DI's, with `sys.path` set to the parent folder, importing `backend.app:app`. The `start_background_sync()` call is added in Task 11.
  - **`.gitignore`:** DI's backend `.gitignore`.
  - **`.env.example`:** every config variable with a one-line comment.

- [ ] **Step 5: Run it.** The same command. Expected: 4 passed.

- [ ] **Step 6: Report changed files** (`git status --short production/`). Don't commit.

### Task 2: Schema migration, migration runner, test fixtures

**Files:**
- Create: `production/backend/migrations/001_init.sql`, `scripts/__init__.py`, `scripts/migrate.py`, `tests/factories.py`, `tests/test_migrations.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `get_conn()` (Task 1).
- Produces:
  - `scripts.migrate.main() -> int` (DI's runner).
  - Fixture `conn`: a fresh, truncated database per test. Departments are re-seeded.
  - Factories in `tests/factories.py`. Each inserts a row and returns it as a dict:
    - `make_user(conn, *, role="MANAGER", vertical="SUPPLY", email=None, use_case_ids=(), active=True)`
    - `make_agent(conn, *, vertical="SUPPLY", variables=("owner_name","society_name"), bolna_agent_id=None, default_from_number="+918000000001")`
    - `make_use_case(conn, *, department="SUPPLY", key="seller_qualification", tag_taxonomy=None)` (`None` means `DEFAULT_TAXONOMY`)
    - `make_dataset(conn, *, department="SUPPLY", use_case_id=None, rows=())`, where rows are `dict(phone=..., name=..., city=..., society=..., segment=..., data=...)`
    - `make_campaign(conn, *, agent, vertical="SUPPLY", status="RUNNING", redial_policy=None, skip_cooling=False, workflow_id=None, use_case_id=None, from_number="+918000000001")`
    - `make_lead(conn, campaign, *, phone, status="PENDING", attempts=0, last_call_at=None, last_outcome=None, final_tag=None, variables=None, dataset_lead_id=None)`
    - `make_batch(conn, campaign, *, status="scheduled", total=1, bolna_batch_id=None, scheduled_at=None, round=1)`
    - `make_call(conn, *, phone, outcome="CONNECTED", started_at, status="completed", campaign=None, lead=None, batch=None, tag=None, agent=None, bolna_execution_id=None)`
    - `bolna_execution(**overrides) -> dict`: a realistic Bolna execution with `id`, `agent_id`, `batch_id`, `status="completed"`, `created_at`, `conversation_duration=60`, `transcript="assistant: नमस्ते\nuser: हां जी बोलिए"`, `telephony_data{to_number, from_number, hangup_provider_code: 4000, recording_url}`, `extracted_data{"Seller Details": {"call_tag": {"objective": "qualified"}}}`, `total_cost=4.1`.
  - Constant `DEFAULT_TAXONOMY`, defined in `backend/services/taxonomy.py` (create it here), with the values of `initial zip/lib/taxonomy.ts`.

- [ ] **Step 1: Write the failing tests.**
```python
# tests/test_migrations.py
EXPECTED = {"user","agent","phone_number","campaign","lead","batch","call","setting","audit_log","department",
            "use_case","dataset","dataset_lead","workflow","scheduled_run","handoff","dnc_phone"}

def test_tables(conn):
    with conn.cursor() as cur:
        cur.execute("select table_name from information_schema.tables where table_schema='public'")
        assert {r["table_name"] for r in cur.fetchall()} == EXPECTED

def test_migrate_is_idempotent():
    from backend.scripts import migrate
    assert migrate.main() == 0 and migrate.main() == 0

def test_departments_seeded_with_defaults(conn):
    with conn.cursor() as cur:
        cur.execute("select key, window_start, window_end, line_share, cooling_days from department order by key")
        assert [tuple(r.values()) for r in cur.fetchall()] == [("DEMAND","10:00","19:00",5,7), ("SUPPLY","10:00","19:00",5,7)]

def test_removed_and_added_columns(conn):
    with conn.cursor() as cur:
        cur.execute("select table_name||'.'||column_name c from information_schema.columns where table_schema='public'")
        cols = {r["c"] for r in cur.fetchall()}
    assert "campaign.skip_cooling" in cols and "call.pushed_to_di_at" in cols
    assert "dataset_lead.dnc" not in cols and "call.pushed_to_crm_at" not in cols and "user.email_verified" not in cols
```

- [ ] **Step 2: Run it.** `backend/.venv/bin/python -m pytest backend/tests/test_migrations.py -q`
  Expected: FAIL (missing `conn` fixture / tables).

- [ ] **Step 3: Write `001_init.sql`.** Translate `initial zip/drizzle/0000_nervous_namor.sql` and `0001_robust_rage.sql` into idempotent SQL:
  - Enums are created inside `DO $$ BEGIN CREATE TYPE ...; EXCEPTION WHEN duplicate_object THEN NULL; END $$;`.
  - Use `CREATE TABLE IF NOT EXISTS` with foreign keys inline, and `CREATE [UNIQUE] INDEX IF NOT EXISTS`.
  - `id` columns are `text PRIMARY KEY DEFAULT gen_random_uuid()::text`.
  - Apply the changes in spec §5.8:
    - Drop `account`, `session`, `verification_token` and `user.email_verified`.
    - Add `dnc_phone(phone text PRIMARY KEY, reason text, source text NOT NULL CHECK (source IN ('use_case_tag','rule','manual','backfill')), created_by_id text REFERENCES "user"(id), created_at timestamptz NOT NULL DEFAULT now())`.
    - Add `campaign.skip_cooling boolean NOT NULL DEFAULT false`.
    - Rename `call.pushed_to_crm_at` to `pushed_to_di_at`.
    - Drop `dataset_lead.dnc`.
  - End with `INSERT INTO department(key,name) VALUES ('SUPPLY','Supply'),('DEMAND','Demand') ON CONFLICT DO NOTHING;`.

  **`scripts/migrate.py`:** DI's `backend/scripts/migrate.py` verbatim.

  **`conftest.py`:**
  - a session-scoped autouse fixture runs `migrate.main()` once;
  - `conn` yields `get_conn()` after `TRUNCATE` of every table except `department` (`RESTART IDENTITY CASCADE`), resets the department rows to their defaults, then rolls back and closes the connection.

- [ ] **Step 4: Run it.** Expected: 4 passed. Then run the full suite. Expected: all pass.

- [ ] **Step 5: Report changed files.** Don't commit.

### Task 3: IST helpers and call classifier

**Files:**
- Create: `production/backend/services/ist.py`, `services/classify.py`, `tests/test_ist.py`, `tests/test_classify.py`

**Interfaces:**
- Produces:
  - **`ist.py`:**
    - `IST`
    - `parse_hhmm(s: str) -> tuple[int, int]`: raises `ValueError` on anything but `H:MM`/`HH:MM` with hours 0–23 and minutes 0–59.
    - `validate_window(start: str, end: str) -> None`: raises `ValueError` if either fails to parse or start ≥ end.
    - `in_window(now, start, end) -> bool`: `[start, end)` in IST.
    - `next_window_start(now, start, end) -> datetime`: returns `now` if inside the window, otherwise today's or tomorrow's start, as UTC.
    - `minutes_left_in_window(t, start, end) -> int`
    - `fmt_ist(dt, seconds=False) -> str`, in the format `"18 Nov, 03:05 PM"`; `"—"` for `None`.
  - **`classify.py`:**
    - Constants: `HANGUP_CODE_LABEL`, `TERMINAL_STATUSES`, `TERMINAL_TAGS`, `DNC_TAGS`.
    - `is_terminal_status(status) -> bool`
    - `count_human_turns(transcript) -> int`
    - `extract_tag(extracted) -> str | None`
    - `flatten_extracted(extracted) -> dict[str, str]`
    - `classify_execution(e: dict) -> dict`, returning the keys `outcome, hangup_code, hangup_reason, duration_sec, human_turns, tag, summary, phone, from_number, started_at, cost, recording_url, transcript, extracted`.
    - `lead_status_for(outcome, tag) -> str`
    - `normalize_phone(raw) -> str | None`

- [ ] **Step 1: Write the failing tests.** `test_classify.py` is parametrised over the rules in `initial zip/lib/classify.ts`, one case per branch, built with `factories.bolna_execution`.
```python
CASES = [  # (overrides, expected outcome)
    (dict(status="balance-low"), "BALANCE_LOW"),
    (dict(telephony_data={"hangup_provider_code": 3010}), "CARRIER_BLOCKED"),
    (dict(telephony_data={"hangup_provider_code": 4010}), "VOICEMAIL"),
    (dict(telephony_data={"hangup_reason": "answering machine detected"}), "VOICEMAIL"),
    (dict(telephony_data={"hangup_provider_code": 3080}), "FAILED"),
    (dict(status="no-answer", transcript=None), "NOT_PICKED"),
    (dict(telephony_data={"hangup_reason": "x 3040 y"}, transcript=None), "NOT_PICKED"),
    (dict(transcript="assistant: hello\nuser: haan ji kaun", conversation_duration=40), "CONNECTED"),
    (dict(transcript="assistant: hello sir", conversation_duration=30, extracted_data=None), "VOICEMAIL"),
    (dict(transcript="assistant: hello\nuser: ha", conversation_duration=12, extracted_data=None), "NOT_PICKED"),
    (dict(transcript="assistant: aap ghar bechna chahte hain kya\nuser: aap ghar bechna chahte hain kya", conversation_duration=10, extracted_data=None), "NOT_PICKED"),  # echo
]
def test_classify_outcomes(overrides, expected): ...   # classify_execution(bolna_execution(**overrides))["outcome"] == expected

def test_extract_tag_nested_objective():  # {"Seller Details":{"call_tag":{"objective":"qualified"}}} -> "qualified"
def test_flatten_extracted():  # {"G":{"ask_price":{"objective":"1.5 Cr"}}} -> {"ask_price":"1.5 Cr"}
def test_lead_status_for():  # ("CONNECTED","qualified")->"DONE"; ("CONNECTED","callback_requested")->"CONNECTED"; (any,"angry")->"DNC"; ("BALANCE_LOW",None)->"PENDING"; ("CARRIER_BLOCKED",None)->"BLOCKED"
def test_normalize_phone():
    for raw in ["9810043058", "+91 98100 43058", "09810043058", "919810043058", 9810043058, 9810043058.0, "9810043058.0"]:
        assert normalize_phone(raw) == "+919810043058"
    for raw in ["9.81E+09", "12345", "", None, "abc"]:
        assert normalize_phone(raw) is None
```
  `test_ist.py`:
  - `in_window` at 09:59 / 10:00 / 18:59 / 19:00 IST → False / True / True / False.
  - `next_window_start` at 20:00 IST equals tomorrow 10:00 IST.
  - `validate_window("19:00","10:00")` and `validate_window("25:00","26:00")` raise `ValueError`.
  - `fmt_ist(datetime(2026,11,18,9,35,tzinfo=utc)) == "18 Nov, 03:05 PM"`.

- [ ] **Step 2: Run it.** Expected: FAIL (import error).

- [ ] **Step 3: Implement.**
  - **Classifier:** a rule-for-rule port of `initial zip/lib/classify.ts`: same order of checks, same constants, the same echo rule (four or more user words with at least 50% overlap with the previous agent line).
  - **`normalize_phone`:**
    - an `int`, or a `float` with no fractional part, becomes digits;
    - a string matching `^\d+\.0$` loses the `.0`;
    - strings containing `e` or `E` return `None`;
    - then apply the old digit rules.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 4: Bolna client and lead-file import

**Files:**
- Create: `production/backend/services/bolna.py`, `services/lead_import.py`, `tests/test_bolna.py`, `tests/test_lead_import.py`
- Modify: `tests/conftest.py` (add the `bolna_stub` fixture)

**Interfaces:**
- Produces:
  - **`bolna.py`:**
    - `BolnaError(status: int, body: str)`
    - `READONLY_AGENT_FIELDS`, as in `initial zip/lib/bolna.ts`
    - `list_agents() -> list[dict]`, `get_agent(agent_id) -> dict`
    - `update_agent(agent_id, agent: dict, system_prompt: str | None = None) -> dict`
    - `agent_template_variables(agent) -> list[str]`: `{{var}}` names from the welcome message and system prompt, in first-seen order.
    - `list_agent_executions(agent_id, page=1, page_size=50) -> tuple[list[dict], bool]`
    - `get_batch(batch_id) -> dict`
    - `create_batch(agent_id, from_number, csv_text, filename) -> dict`: contains `"batch_id"`.
    - `schedule_batch(batch_id, when: datetime) -> dict`, `stop_batch(batch_id) -> dict`
    - `place_call(agent_id, to, from_number, user_data: dict) -> dict`
    - `list_phone_numbers() -> list[dict]`
    - `build_batch_csv(rows: list[dict], variables: list[str]) -> str`: each row is `{phone, variables}`.

    All calls use `requests` with `timeout=30`. A non-2xx response, or a 2xx response whose body isn't JSON, raises `BolnaError`. A missing `BOLNA_API_KEY` raises `RuntimeError`.
  - **`lead_import.py`:**
    - `parse_lead_file(data: bytes, filename: str) -> dict`: returns `{columns, rows, total}`. Raises `ValueError("Upload a .csv or .xlsx file")` for other extensions and `ValueError("The file has no rows")` for an empty file.
    - `guess_column_map(columns, variables) -> dict`
    - `build_leads(rows, column_map, variables) -> dict`: returns `{leads: [{phone, name, variables}], invalid, duplicates}`.
    - `spoken_bhk(v) -> str`
  - **Fixture `bolna_stub`:** monkeypatches every `bolna.*` network function. It exposes:
    - `.calls`, a list of `(name, args)`;
    - `.executions`, a dict mapping a Bolna agent ID to a list of pages;
    - `.agents`, a list;
    - `.fail_on`, a set of function names that raise `BolnaError(500, "<html>")`.

    `create_batch` returns `{"batch_id": "bb<n>"}`.

- [ ] **Step 1: Write the failing tests.**
```python
def test_build_batch_csv_quotes_and_keeps_hindi():
    csv = build_batch_csv([{"phone": "+919810043058", "variables": {"owner_name": "राम, जी"}}], ["owner_name"])
    assert csv == 'contact_number,owner_name\n+919810043058,"राम, जी"'
def test_update_agent_strips_readonly_and_sets_prompt(monkeypatch): ...  # captured PUT body: no "agent_id"/"created_at"; agent_prompts.task_1.system_prompt == "NEW"
def test_non_json_success_raises(monkeypatch): ...  # 200 with "<html>" body -> BolnaError
def test_template_variables(): ...  # "{{ owner_name }} … {{society_name}} … {{owner_name}}" -> ["owner_name","society_name"]

def test_parse_xlsx_numeric_phone(tmp_path): ...   # openpyxl workbook, phone cell 9810043058 (int) -> rows[0]["Phone"] == "9810043058"
def test_parse_csv_utf8_bom(): ...                 # b"\xef\xbb\xbfPhone,Name\n9810043058,राम" -> columns == ["Phone","Name"]
def test_parse_rejects_pdf_and_empty(): ...        # ValueError with the exact messages above
def test_guess_column_map(): ...                   # ["Mobile No","Owner Name","Society"] + ["owner_name","society_name"] -> {"phone":"Mobile No","owner_name":"Owner Name","society_name":"Society"}
def test_build_leads_dedup_and_invalid(): ...      # rows with two same phones + one "abc" -> len(leads)==1, duplicates==1, invalid==1
def test_spoken_bhk(): ...                         # "3 BHK" -> "थ्री बी एच के"; "2.5" -> "टू पॉइंट फ़ाइव बी एच के"
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement.**
  - Port the endpoints and payload shapes from `initial zip/lib/bolna.ts`. Batches and scheduling are multipart (`files={"file": (filename, csv_text, "text/csv")}`, `data=` fields). `scheduled_at` is ISO.
  - `parse_lead_file` reads XLSX with `openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)` (first sheet, first row as headers). Cells: an `int`, or a `float` that is an integer, becomes `str(int(v))`; everything else becomes `str(v).strip()`. CSV is decoded as `utf-8-sig` through `csv.DictReader`.
  - `guess_column_map` hints are those in `initial zip/lib/import.ts`.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 5: Auth, permissions, audit

**Files:**
- Create: `production/backend/api/auth.py`, `services/rbac.py`, `services/audit.py`, `tests/test_auth.py`, `tests/test_rbac.py`
- Modify: `app.py` (register `auth.bp`), `tests/conftest.py` (add `auth_headers(user) -> dict`)

**Interfaces:**
- Produces:
  - **`api/auth.py`:**
    - `bp`
    - `issue_jwt(user: dict) -> str`: claims are `sub`, `email` and `exp` (now + `JWT_EXPIRY_HOURS`).
    - `public_user(row) -> dict`: keys `id, email, name, image, role, vertical, use_case_ids, title, active`.
    - `require_auth(*roles)`: a decorator that decodes the Bearer JWT, then **loads the `user` row by `sub`**, and sets `g.user = public_user(row)`. A missing or invalid token, a missing user, or an inactive user gives 401. A role not in `roles` gives 403.
    - Routes:
      - `POST /api/auth/google {id_token}` → `{token, user}`.
      - `POST /api/auth/dev {email}` → `{token, user}`. Only while `GOOGLE_OAUTH_CLIENT_ID` is empty, and only for an existing active user. Otherwise 404.
      - `GET /api/auth/me` → `{user}`.
  - **`services/rbac.py`:**
    - `DEPTS = ("SUPPLY","DEMAND")`
    - `dept_from_slug(slug) -> str`: raises `not_found` for anything except `supply` or `demand`.
    - `dept_slug(d) -> str`
    - `user_depts(u) -> list[str]`
    - `can_access_dept(u, d)`, `is_owner(u)`, `can_manage_dept(u, d)`, `can_admin_dept(u, d)`
    - `can_see_use_case(u, use_case_id)`
    - `use_case_restriction(u) -> list[str] | None`
    - `require(u, level: str, dept: str, use_case_id: str | None = None) -> None`: the levels are `"view"`, `"manage"`, `"admin"`, `"owner"`, and a failure raises 403.
    - `load_scoped(conn, kind: str, id: str, u: dict, level: str) -> dict`:
      - `kind` is one of `campaign, dataset, workflow, run, handoff, use_case, call, dataset_lead`;
      - it raises 404 if the record is missing and 403 if the user lacks access;
      - the department is read from the record itself; a call uses its campaign's `vertical`, or its agent's `vertical` when it has no campaign, and a BOTH call needs `user_depts(u) == ["SUPPLY","DEMAND"]`;
      - the use-case check uses the record's `use_case_id` where it has one.
    - `check_user_edit(actor: dict, target: dict | None, role: str, vertical: str) -> None`: the head rules in spec §5.3; nobody may change their own role.
  - **`services/audit.py`:** `audit(conn, user_id, action, entity=None, entity_id=None, meta=None) -> None` (it doesn't commit).
  - The rules in `rbac` port `initial zip/lib/rbac.ts`, plus the spec §5.3 additions.

- [ ] **Step 1: Write the failing tests.**
```python
def test_google_login_creates_viewer_and_admin(client, conn, monkeypatch):
    # monkeypatch google id_token.verify_oauth2_token -> {"email": "a@openhouse.in", "email_verified": True, "name": "A"}
    # ADMIN_EMAILS=["boss@openhouse.in"]; a@ -> role VIEWER vertical BOTH; boss@ -> ADMIN BOTH; x@gmail.com -> 403
def test_dev_login_only_without_google_client(client, conn, monkeypatch): ...   # with GOOGLE_OAUTH_CLIENT_ID set -> 404
def test_deactivated_user_gets_401_with_valid_token(client, conn):              # Review Focus 5
    u = make_user(conn); h = auth_headers(u)
    assert client.get("/api/auth/me", headers=h).status_code == 200
    conn.cursor().execute('update "user" set active=false where id=%s', (u["id"],)); conn.commit()
    assert client.get("/api/auth/me", headers=h).status_code == 401
def test_role_change_applies_without_new_token(client, conn): ...               # update role -> /me shows new role

def test_load_scoped_uses_record_department(conn):  # DEMAND manager loading a SUPPLY campaign -> 403; missing id -> 404
def test_use_case_restricted_manager(conn): ...     # manager with use_case_ids=[uc1] -> campaign of uc2 -> 403
def test_head_cannot_edit_owner_or_head(): ...      # check_user_edit(head_supply, owner_row, "VIEWER","SUPPLY") raises 403
def test_head_can_claim_both_viewer(): ...          # target VIEWER/BOTH -> MANAGER/SUPPLY by SUPPLY head ok; -> DEMAND raises
def test_nobody_changes_own_role(): ...
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement.** Copy DI's `api/auth.py` flow (Google verify, domain check, user lookup, JWT issue), with the user creation from spec §5.3 and the per-request database load.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 6: Datasets and DNC

**Files:**
- Create: `production/backend/services/datasets.py`, `tests/test_datasets.py`

**Interfaces:**
- Consumes: `normalize_phone` (Task 3), `parse_lead_file` (Task 4).
- Produces:
  - `upsert_dataset_rows(conn, dataset_id, rows: list[dict], hints: dict | None = None) -> dict`: returns `{added, updated, invalid, duplicates}`.
  - `import_file_into_dataset(conn, dataset_id, data: bytes, filename: str) -> dict`
  - `pull_dataset_from_source(conn, dataset: dict) -> dict`
  - `dataset_facets(conn, dataset_id) -> dict`: returns `{cities, societies, segments, tags}`, each a list of `{v, n}`.
  - `segment_where(dataset_id, segment: dict) -> tuple[str, list]`: a SQL fragment over alias `dl`. It always excludes `EXISTS (select 1 from dnc_phone d where d.phone = dl.phone)`.
  - `select_leads_for_run(conn, dept, dataset_id, segment, *, ignore_cooling=False, limit=None) -> tuple[list[dict], int]`
  - `recently_spoken_phones(conn, phones: list[str], since: datetime, exclude_campaign_id: str | None = None) -> set[str]`: phones with a CONNECTED or VOICEMAIL call since `since`, ignoring calls in `exclude_campaign_id`.
  - `touch_dataset_lead(conn, dataset_lead_id, phone, outcome, tag, when) -> None`
  - `mark_dnc(conn, phone, source, reason=None, user_id=None) -> None` (idempotent), `unmark_dnc(conn, phone) -> None`
  - `dnc_phones(conn, phones) -> set[str]`

- [ ] **Step 1: Write the failing tests.**
```python
def test_upsert_counts_and_keeps_all_columns(conn): ...      # 2 new + 1 dup + 1 invalid, then re-upsert 1 -> {"added":0,"updated":1,...}; data keeps unknown columns
def test_upsert_requires_phone_column(conn): ...             # ValueError startswith "No phone column found"
def test_segment_filters_and_dnc_exclusion(conn): ...        # cities/segments/never_called/last_tag_in/max_attempts each narrow; a phone in dnc_phone never selected
def test_cooling_skips_spoken_not_ringing(conn): ...         # CONNECTED 2 days ago -> cooled; NOT_PICKED 2 days ago -> selected; CONNECTED 8 days ago (cooling 7) -> selected; returns (rows, cooled_count)
def test_mark_dnc_is_idempotent(conn): ...
def test_pull_dataset_from_source(conn, monkeypatch): ...    # requests.request -> {"data":{"leads":[{"mobile":"9810043058","nm":"A"}]}}, path "data.leads", phone_field "mobile" -> added 1
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement.** Port `initial zip/lib/datasets.ts`, with DNC read from `dnc_phone`. The column-guess needles are the same. A source pull sends `Authorization: Bearer <token>` when the config has a token, and raises `ValueError` with the first 200 characters of the response body on a non-2xx.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 7: The dial guard, create_round, redials

**Files:**
- Create: `production/backend/services/redial.py`, `tests/test_redial.py`

**Interfaces:**
- Consumes: `bolna.create_batch` / `schedule_batch` / `place_call` / `build_batch_csv` (Task 4); `dnc_phones` and `recently_spoken_phones` (Task 6); `ist` (Task 3).
- Produces:
  - `DEFAULT_REDIAL_POLICY = {"max_attempts": 3, "gap_minutes": 180, "window_start": "10:00", "window_end": "19:00", "retry_outcomes": ["NOT_PICKED","CARRIER_BLOCKED","VOICEMAIL","FAILED","BALANCE_LOW"], "retry_tags": ["not_picked","callback_requested","unclear","yes_unverified"], "rotate_numbers": [], "auto_redial": True}`
  - `policy_of(campaign) -> dict`
  - `validate_policy(p: dict) -> dict`: merges with the default, enforces `1 ≤ max_attempts ≤ 8` and `15 ≤ gap_minutes ≤ 10080`, and calls `validate_window`. Bad input raises `ValueError`.
  - `eligible_redial_leads(conn, campaign, now) -> list[dict]`
  - `from_number_for(lead, campaign, policy) -> str`
  - `guard_leads(conn, campaign, leads, kind, now) -> tuple[list, list, list]`: returns `(dialable, dnc, cooled)`.
  - `assert_dialable(conn, phone) -> None`: raises `ApiError(409, "number is on the do-not-call list")`.
  - `create_round(conn, campaign, leads, kind, when, now=None) -> dict`: `kind` is `"FIRST"` or `"REDIAL"`, `when` is a datetime or `None`. Returns `{batch_ids, leads, scheduled_at, skipped, dnc, cooled}`.
  - `maybe_auto_redial(conn, campaign, now) -> dict | None`
  - `place_test_call(conn, agent, to, from_number, variables) -> dict`: runs `assert_dialable`, then `bolna.place_call`.

- [ ] **Step 1: Write the failing tests.** Use `bolna_stub`.
```python
def test_first_round_skips_dnc_and_cooled(conn, bolna_stub):
    # leads A (clean), B (in dnc_phone), C (CONNECTED call in another campaign 2 days ago)
    r = create_round(conn, camp, [A, B, C], "FIRST", None)
    assert r["leads"] == 1 and len(r["dnc"]) == 1 and len(r["cooled"]) == 1
    # B.status == "DNC"; C.status == "DONE" and C.final_tag == "skipped_cooling"; A.status == "QUEUED"
    # bolna_stub csv contains only A's phone
def test_redial_round_ignores_cooling(conn, bolna_stub): ...       # same C but kind="REDIAL" -> dialled
def test_skip_cooling_campaign(conn, bolna_stub): ...              # campaign.skip_cooling=True, FIRST -> C dialled; B still blocked
def test_bolna_failure_restores_leads_and_marks_batch_failed(conn, bolna_stub):
    bolna_stub.fail_on = {"create_batch"}
    # pytest.raises(BolnaError); lead statuses back to their input values; batch row status == "failed"
def test_one_batch_per_from_number_with_rotation(conn, bolna_stub): ...  # BLOCKED leads with rotate_numbers -> 2 create_batch calls
def test_lead_marked_queued_before_bolna_call(conn, bolna_stub): ...     # inside the stubbed create_batch, the DB already shows status QUEUED and a batch row with status "creating"
def test_eligible_redial(conn): ...  # NOT_PICKED past gap -> yes; inside gap -> no; attempts==max -> no; PENDING+BALANCE_LOW -> yes; PENDING never dialled -> no; CONNECTED+retry tag -> yes; DONE -> no; next_dial_at in future -> no
def test_auto_redial_outside_window_schedules_next_start(conn, bolna_stub): ...  # now 20:00 IST -> scheduled_at == next day 10:00 IST, campaign stays RUNNING/SCHEDULED
def test_test_call_blocked_for_dnc(conn, bolna_stub): ...                 # ApiError 409, no place_call recorded
def test_validate_policy_rejects_bad_window(): ...                         # Review Focus 3: window_start "19:00", window_end "10:00" -> ValueError
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement `create_round` in this exact order** (this is what prevents double dials):
  1. Run `guard_leads`. Mark DNC leads `status='DNC'` and cooled leads `status='DONE', final_tag='skipped_cooling'`. Commit.
  2. If nothing is dialable, return with `skipped="no eligible leads"`.
  3. Group the dialable leads by from-number (`from_number_for` for REDIAL, `campaign["from_number"]` for FIRST). Round numbers continue from `max(batch.round)`.
  4. For each group:
     - insert a `batch` row with `status='creating'`, `kind`, `round`, `total` and `scheduled_at = when or now+60s`, and set the group's leads `status='QUEUED', next_dial_at=scheduled_at`; commit;
     - call `bolna.create_batch` and then `schedule_batch`;
     - on success, set `bolna_batch_id` and `status='scheduled'` and commit;
     - on any exception, set the batch `status='failed'`, restore each lead's previous `status` and `next_dial_at`, commit, and re-raise.
  5. Update the campaign: `status = 'SCHEDULED'` if `when` is more than 2 minutes in the future, otherwise `'RUNNING'`; `started_at = coalesce(started_at, now)`. Commit.

  Cooling (FIRST rounds without `skip_cooling` only) reads `department.cooling_days` for the campaign's `vertical` directly with SQL (the scheduler comes in Task 8) and uses `recently_spoken_phones(..., exclude_campaign_id=campaign["id"])`. Eligibility is a port of `initial zip/lib/redial.ts` with one change: `PENDING` leads are eligible only when `last_outcome = 'BALANCE_LOW'`.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 8: Scheduler

**Files:**
- Create: `production/backend/services/scheduler.py`, `services/settings_store.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `create_round` (Task 7); `ist` (Task 3).
- Produces:
  - **`settings_store.py`:** `get_setting(conn, key) -> Any | None`, `set_setting(conn, key, value) -> None`.
  - **`scheduler.py`:**
    - `AVG_CALL_SECONDS = 55`
    - `estimate_minutes(lead_count, lines) -> int`, defined as `max(3, ceil(lead_count*55/max(1,lines)/60) + 2)`
    - `dept_config(conn, dept) -> dict`
    - `place_runs(runs: list[dict], *, now, busy_until, window_start, window_end, line_share) -> list[tuple[str, datetime, datetime]]`: pure. `runs` carry `id, requested_start, est_minutes, est_leads`, already ordered.
    - `replan_department(conn, dept, now=None) -> list[dict]`
    - `plan_run(conn, *, dept, campaign_id, use_case_id=None, workflow_id=None, purpose=None, priority=5, requested_start=None, user_id=None, note=None) -> dict`
    - `dispatch_due_runs(conn, now=None) -> list[str]`
    - `cancel_run(conn, run_id) -> None`
    - `set_run_priority(conn, run_id, priority, requested_start=None) -> list[dict]`
    - `runs_between(conn, start, end, dept=None) -> list[dict]`: each row is `scheduled_run.*` plus `campaign_name` and `campaign_status`.

- [ ] **Step 1: Write the failing tests.**
```python
def test_place_runs_back_to_back_inside_window(): ...   # two runs of 60 and 30 min at 10:00 IST -> (10:00-11:00), (11:00-11:30)
def test_place_runs_spills_to_next_morning(): ...       # 120-min run requested 18:00 IST -> 18:00-19:00 then next day 10:00-11:00 (end = next day 11:00)
def test_place_runs_waits_for_running_end(): ...        # busy_until 12:00 -> first start 12:00
def test_place_runs_rejects_bad_window(): ...           # Review Focus 3: window_end <= window_start -> ValueError (never loops)
def test_replan_orders_priority_then_requested(conn): ...
def test_dispatch_starts_due_and_cancels_stopped(conn, bolna_stub): ...  # due PLANNED run -> create_round called, run RUNNING; run of STOPPED campaign -> CANCELLED
def test_dispatch_closes_runs_of_finished_campaigns(conn): ...           # RUNNING run + COMPLETED campaign -> DONE
def test_cancel_run_returns_campaign_to_draft(conn): ...
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement.** Port `initial zip/lib/scheduler.ts`. `place_runs` calls `validate_window` first. `plan_run` counts the campaign's PENDING leads to get `est_leads`.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 9: Workflow engine: runs, triggers, result rules, hand-offs

**Files:**
- Create: `production/backend/services/workflows.py`, `services/whatsapp.py`, `tests/test_workflows.py`

**Interfaces:**
- Consumes: `select_leads_for_run`, `touch_dataset_lead`, `mark_dnc` (Task 6); `create_round` (Task 7); `plan_run` (Task 8); `spoken_bhk` (Task 4); `flatten_extracted`, `TERMINAL_STATUSES` (Task 3).
- Produces:
  - **`whatsapp.py`:** `whatsapp_enabled() -> bool`, `send_template(phone_e164, template, body_values, language_code="en") -> str` (port of `initial zip/lib/whatsapp.ts`; it raises on a non-2xx).
  - `map_variables(row: dict, variables: list[str], variable_map: dict) -> dict`
  - `run_workflow(conn, wf, *, requested_start=None, user_id=None, explicit_leads=None, name_suffix=None, ignore_cooling=False, immediate=False, now=None) -> dict | None`: returns `{campaign_id, leads, cooled, run_id, planned_start}`.
  - `schedule_due(trigger: dict, last_run_at, now) -> bool`
  - `fire_triggers(conn, now=None, dataset_id=None) -> dict`: returns `{fired: [{workflow, type, leads}], errors: [str]}`.
  - `rule_matches(rule, call) -> bool`
  - `apply_result_rules(conn, call, campaign, lead) -> list[str]`
  - `create_handoff(conn, call, campaign, use_case, lead, fields, action=None) -> bool`

- [ ] **Step 1: Write the failing tests.**
```python
def test_map_variables(): ...                      # first name only; bhk spoken; society/city fall back to row fields; variable_map wins
def test_schedule_due(): ...                       # every_minutes; days+time before/after; already ran today (IST) -> False
def test_run_workflow_excludes_live_phones(conn): ...   # phone already in a live campaign of the same workflow -> not added
def test_run_workflow_sets_skip_cooling(conn): ...      # ignore_cooling=True -> campaign.skip_cooling True
def test_on_new_lead_dials_immediately_without_calendar(conn, bolna_stub):
    # ON_NEW_LEAD workflow; new dataset row; now inside window
    r = fire_triggers(conn, now=in_window_now, dataset_id=ds["id"])
    # one create_batch call; no scheduled_run row; campaign.skip_cooling True
def test_on_new_lead_outside_window_schedules_next_start(conn, bolna_stub): ...
def test_after_step_fires_once_after_delay(conn, bolna_stub): ...  # parent call tagged qualified 49h ago, delay 48 -> child campaign with that phone; second fire_triggers -> nothing new
def test_after_step_never_dials_dnc(conn, bolna_stub): ...        # same, but phone in dnc_phone -> lead DNC, no csv row
def test_rules_good_tag_creates_one_handoff(conn): ...            # apply twice -> 1 handoff (rules_applied_at stops the second)
def test_rules_dnc_tag_blocks_everywhere(conn): ...               # tag "angry" -> dnc_phone row source "use_case_tag", lead DNC
def test_rules_redial_after(conn): ...                            # action redial after_minutes 60 -> lead NOT_CONNECTED, next_dial_at ≈ now+60m
def test_rules_wait_for_extraction(conn): ...                     # CONNECTED, no tag, started 5 min ago -> [] and rules_applied_at stays null
def test_rules_whatsapp_error_recorded(conn, monkeypatch): ...    # send_template raises -> result contains "whatsapp:error:..."
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement.** Port `initial zip/lib/workflows.ts`, with these changes:
  - `immediate=True` (used by ON_NEW_LEAD) skips `plan_run` and calls `create_round(conn, campaign, pending, "FIRST", when)`, where `when` is `None` inside the department window and `next_window_start` outside it.
  - ON_NEW_LEAD and AFTER_STEP pass `ignore_cooling=True`, which sets `campaign.skip_cooling`.
  - `crm_push` is gone.
  - The taxonomy DNC writes go to `mark_dnc(..., source="use_case_tag")`, and the rule DNC to `source="rule"`.
  - `fire_triggers(dataset_id=...)` only considers ON_NEW_LEAD workflows of that dataset.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 10: DI push and R2 storage

**Files:**
- Create: `production/backend/services/di_push.py`, `services/storage.py`, `tests/test_di_push.py`, `tests/test_storage.py`

**Interfaces:**
- Consumes: `flatten_extracted`, `is_terminal_status` (Task 3).
- Produces:
  - **`di_push.py`:**
    - `di_enabled() -> bool`
    - `build_di_payload(call: dict, campaign: dict, lead: dict | None) -> dict`
    - `push_calls_to_di(conn, report: dict, limit=200, now=None) -> int`
  - **`storage.py`:**
    - `storage_enabled() -> bool`
    - `archive_recording(execution_id, url, when) -> str`: the key is `recordings/<yyyy>/<mm>/<execution_id>.<mp3|wav|ogg>`.
    - `signed_recording_url(key, expires=3600) -> str`

- [ ] **Step 1: Write the failing tests.**
```python
DI_KEYS = {"call_id","source","campaign","batch_id","called_at","phone","caller_id","contact","outcome","outcome_detail",
           "talk_seconds","carrier_blocked","call_tag","property","timeline","callback_at","summary","transcript","recording_url","cost_usd"}
def test_build_di_payload_shape(conn):
    p = build_di_payload(call, campaign, lead)   # lead.variables {"owner_name":"Rajendra Kumar","society_name":"Gaur City 1st Avenue","city":"Noida"}
    assert set(p) == DI_KEYS and p["call_id"] == call["bolna_execution_id"] and p["source"] == "calling-console"
    assert p["contact"] == {"name": "Rajendra Kumar", "society": "Gaur City 1st Avenue", "city": "Noida"}
    assert p["outcome"] == "connected" and p["carrier_blocked"] is False and p["campaign"] == campaign["name"]
    assert set(p["property"]) == {"configuration","size_sqft","asking_price_raw","facing"}
def test_contact_falls_back_to_extracted(): ...     # no lead -> name from owner_name, society from society_and_location
def test_push_only_finished_supply_calls_once(conn, monkeypatch):
    # requests.post stub records calls; calls: SUPPLY completed (push), DEMAND completed (skip), SUPPLY in-progress (skip),
    # SUPPLY CONNECTED untagged 5 min old (skip), backfilled call with no campaign (skip)
    # header X-AI-Calls-Token == config.DI_AI_CALLS_TOKEN; pushed_to_di_at set; second run pushes 0
def test_push_failure_recorded_and_retried(conn, monkeypatch): ...  # 500 -> report["errors"] has "di <call_id>: 500", pushed_to_di_at null, setting "diPushFailures" has it
def test_signed_url_without_network(monkeypatch): ...               # S3_* set -> URL contains the bucket and "X-Amz-Signature"
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **Payload mapping:**
    - `contact` comes from `lead.variables` (`owner_name` or `name`, `society_name` or `society`, `city`), falling back to the flattened extracted fields (`owner_name`, `society_and_location` or `society`).
    - `property`:
      - `configuration` = `fields.configuration`
      - `size_sqft` = `fields.size_sqft`
      - `asking_price_raw` = `ask_price` or `asking_price`
      - `facing` = `exit_facing` or `facing`
    - `timeline` = `sell_later_period` or `timeline`; `callback_at` = `callback_time`.
    - `outcome` is the lower-cased outcome; `carrier_blocked` is `outcome == "CARRIER_BLOCKED"`; `outcome_detail` is `hangup_reason`.
    - `talk_seconds` = `duration_sec`, `called_at` = `started_at` ISO, `caller_id` = `from_number`, `batch_id` = `bolna_batch_id`, `cost_usd` = `cost`.
  - **What gets pushed:**
    - The call's campaign `vertical='SUPPLY'`, it has a terminal status, and `pushed_to_di_at` is null.
    - It's not a `CONNECTED` call that has no tag and is under 10 minutes old.
  - **`diPushFailures`:** keep the last 50 as `{call_id, error, at}`.
  - **R2:** a boto3 client with `endpoint_url=S3_ENDPOINT` and `region_name=S3_REGION`. Download recordings with `requests.get(url, timeout=60)`.

- [ ] **Step 4: Run it.** Expected: PASS.

- [ ] **Step 5: Report changed files.**

### Task 11: Sync job, lock, background thread

**Files:**
- Create: `production/backend/services/sync.py`, `scripts/run_sync.py`, `tests/test_sync.py`
- Modify: `wsgi.py` (call `start_background_sync()`, as DI calls `start_background_dialer()`)

**Interfaces:**
- Consumes: Tasks 3–10.
- Produces:
  - `LOCK_KEY = 42100001`
  - `norm_batch_status(s) -> str`
  - `new_report() -> dict`, with the keys `started_at, finished_at, full, agents, batches_checked, executions_seen, calls_upserted, recordings_archived, redial_rounds, campaigns_completed, runs_started, triggers_fired, rules_applied, di_pushed, errors`
  - `lead_update_from_call(lead: dict, c: dict, *, first_link: bool, rules_applied: bool, terminal: bool) -> dict | None`: pure.
  - `upsert_execution(conn, e: dict, agent: dict | None = None, now=None) -> str`: returns `"inserted"` or `"updated"`.
  - `sync_agents(conn) -> int`
  - `sync_batches(conn, report, now)`
  - `sync_executions(conn, report, *, full=False, max_pages=None, now=None)`
  - `close_stale_queued(conn, campaign_id, now) -> int`
  - `rollup_campaigns(conn, report, now)`
  - `archive_recordings(conn, report, limit=40)`
  - `run_sync(*, full=False, max_pages=None) -> dict | None`: returns `None` when the lock is held elsewhere.
  - `is_full_due(now, last_full_date: str | None) -> bool`
  - `start_background_sync() -> None`

- [ ] **Step 1: Write the failing tests.**
```python
def test_older_call_does_not_overwrite_newer():
    lead = dict(status="CONNECTED", attempts=2, last_call_at=T(12), last_outcome="CONNECTED", final_tag="callback_requested", next_dial_at=T(15))
    assert lead_update_from_call(lead, call(started_at=T(9), outcome="NOT_PICKED"), first_link=False, rules_applied=False, terminal=True) is None
def test_first_link_counts_attempt_even_if_older(): ...   # first_link=True, older -> {"attempts": 3} only
def test_newer_call_overwrites_and_resets_next_dial(): ... # status from lead_status_for, next_dial_at None, last_call_at = new started_at
def test_same_call_after_rules_leaves_lead_alone(): ...    # started_at == last_call_at and rules_applied=True -> None
def test_balance_low_not_counted_and_pending(): ...        # outcome BALANCE_LOW, first_link -> attempts unchanged, status "PENDING"
def test_done_and_dnc_are_sticky(): ...
def test_non_terminal_call_sets_queued(): ...

def test_upsert_links_by_batch_and_applies_rules(conn): ...  # execution with batch_id of a campaign batch -> call row linked; lead updated; rules_applied_at set
def test_close_stale_queued_becomes_failed_attempt(conn): ...  # batch completed, QUEUED lead with no call -> status FAILED, last_outcome FAILED, attempts+1
def test_campaign_completes(conn, bolna_stub): ...             # all leads final, no active batch -> COMPLETED, leaves DONE
def test_balance_low_lead_keeps_campaign_alive_until_redialled(conn, bolna_stub): ...
def test_run_sync_returns_none_when_locked(conn):
    import psycopg2; other = psycopg2.connect(os.environ["DATABASE_URL"]); other.cursor().execute("select pg_advisory_lock(42100001)")
    assert run_sync() is None
    other.close()
    assert run_sync() is not None
def test_one_agent_failing_does_not_stop_others(conn, bolna_stub):  # Review Focus 2
    # two agents with live campaigns; bolna_stub raises BolnaError(502, "<html>") for agent A's executions
    r = run_sync()
    assert any("executions" in e and "502" in e for e in r["errors"]) and r["calls_upserted"] >= 1  # agent B synced
    # the lock was released: a second run_sync() is not None
def test_sync_pushes_to_di_and_saves_report(conn, bolna_stub, monkeypatch): ...  # setting lastSyncReport saved; di_pushed counted
def test_is_full_due(): ...  # 02:59 IST -> False; 03:01 IST with last_full_date yesterday (IST) -> True; same day -> False
def test_background_thread_needs_integrations(monkeypatch): ...  # SYNC_ENABLED=1 and S3_BUCKET empty -> RuntimeError naming S3_BUCKET
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **`lead_update_from_call` rules:**
    - `inc = 1` if `first_link` and the outcome isn't `BALANCE_LOW`, otherwise 0.
    - The call is **newer** if `lead.last_call_at` is None, or `c.started_at` is None, or `c.started_at > lead.last_call_at`.
    - The call is the **same** call if `c.started_at == lead.last_call_at`.
    - If the call is neither newer nor (the same and `not rules_applied`): return `{"attempts": attempts+inc}` when `inc`, otherwise `None`.
    - Otherwise, `status`:
      - `lead_status_for(outcome, tag)` if `terminal`, else `"QUEUED"`;
      - `"PENDING"` for `BALANCE_LOW`;
      - the lead's existing status if it is DONE or DNC.
    - Return `{attempts, last_call_at, last_outcome, status, final_tag: tag or lead.final_tag, next_dial_at: None if newer else lead.next_dial_at}`.
  - **`upsert_execution`:** the linking logic and the extraction wait are ported from `initial zip/lib/sync.ts`. It commits the call and lead writes before `apply_result_rules`, then stamps `rules_applied_at` (inside `apply_result_rules`) and commits.
  - **`run_sync`:**
    - It opens a dedicated `psycopg2.connect(config.DATABASE_URL)`, not the pool, for `select pg_try_advisory_lock(LOCK_KEY)`, and does the work on `get_conn()`.
    - It always calls `pg_advisory_unlock` and closes the lock connection in `finally`.
    - The steps run in this order:
      1. `sync_agents` (full only)
      2. `sync_batches`
      3. `sync_executions`
      4. `rollup_campaigns` (which calls `close_stale_queued` and then `maybe_auto_redial`)
      5. `fire_triggers`
      6. `dispatch_due_runs`
      7. `archive_recordings`
      8. `push_calls_to_di`
    - Each step is wrapped in `try/except Exception` that appends `"<step>: <msg>"` to `report["errors"]` and logs with `log.exception`. Per-agent and per-campaign errors are caught inside their loops.
    - Finally, save `lastSyncReport`, and `lastFullSyncDate` (IST date) when the run was full.
  - **Paging:** the lookback is `lastExecutionSyncAt - 3h`, or 48h if unset. `max_pages` is 6 normally and 40 when full, and `None` means no cap (used by the backfill).
  - **`start_background_sync`:** DI's `start_background_dialer` shape, a daemon thread started once.
    - It does nothing unless `SYNC_ENABLED == "1"`.
    - On start it raises `RuntimeError` listing any of these that are empty: `BOLNA_API_KEY, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, DI_AI_CALLS_URL, DI_AI_CALLS_TOKEN`.
    - The loop runs `run_sync(full=is_full_due(...))` and then sleeps 300 seconds.
  - **`scripts/run_sync.py [--full]`:** prints the report JSON and exits 1 if `errors` is non-empty or the lock was busy.

- [ ] **Step 4: Run it.** Expected: PASS. Then run the full suite. Expected: all pass.

- [ ] **Step 5: Report changed files.**

### Task 12: API: home, departments, dashboard, use cases

**Files:**
- Create: `production/backend/api/home.py`, `api/departments.py`, `api/use_cases.py`, `tests/test_api_departments.py`
- Modify: `app.py` (register the blueprints)

**Interfaces:**
- Consumes: `require_auth`, `rbac.*`, `audit`, `replan_department`, `validate_window`.
- Produces:

| Method + path | Who | Returns |
|---|---|---|
| `GET /api/home` | any | `{items: [{key, name, dialed_7d, connected_7d, live_campaigns, open_handoffs}]}` for `user_depts` |
| `GET /api/departments/<dept>` | view | department row |
| `PUT /api/departments/<dept>` `{name, window_start, window_end, line_share, cooling_days}` | admin | the row. `validate_window` failing gives 400; `1 ≤ line_share ≤ 10`; `0 ≤ cooling_days ≤ 90`; then replans |
| `GET /api/departments/<dept>/dashboard?days=1\|7\|30&use_case_id=` | view | `{days, stats:{dialed, connected, not_picked, blocked, voicemail, failed, minutes, cost, unique_phones}, funnel:[{use_case_id, name, color, leads, dialed, connected, good, soft, handoffs, open}], outcomes:[{key,n}], tags:[{tag,n}], agents:[{id,name,dialed,connected}], up_next:[run+campaign_name+use_case_name], open_handoffs:[handoff+use_case_name], last_sync}`, with the queries ported from `initial zip/app/(app)/d/[dept]/page.tsx` and respecting `use_case_restriction` |
| `GET /api/departments/<dept>/use-cases` | view | `{items}` restricted by `use_case_restriction` |
| `POST /api/departments/<dept>/use-cases`, `PUT /api/use-cases/<id>` `{key, name, description, default_agent_id, tag_taxonomy, priority, color, active}` | admin | the row. `key` is slugged to `[a-z0-9_]` |

- [ ] **Step 1: Write the failing tests.**
```python
def test_bad_window_rejected(client, conn): ...       # Review Focus 3: PUT window_start "19:00" window_end "10:00" -> 400; "9am" -> 400
def test_head_of_other_dept_forbidden(client, conn): ...  # DEMAND head PUT /api/departments/supply -> 403
def test_dashboard_counts(client, conn): ...          # 3 SUPPLY calls (2 CONNECTED), 1 DEMAND call -> supply stats dialed 3 connected 2
def test_dashboard_use_case_restriction(client, conn): ...
def test_home_lists_only_user_depts(client, conn): ...
def test_use_case_key_slugged_and_scoped(client, conn): ...  # "Seller Qualification!" -> "seller_qualification"; PUT on other dept's use case -> 403
```

- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** the routes in the table.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Report changed files.**

### Task 13: API: datasets, DNC, lead ingest

**Files:**
- Create: `production/backend/api/datasets.py`, `api/ingest.py`, `tests/test_api_datasets.py`, `tests/test_api_ingest.py`
- Modify: `app.py`

**Interfaces:**
- Consumes: Task 6, `fire_triggers` (Task 9), `load_scoped`.
- Produces:

| Method + path | Who | Notes |
|---|---|---|
| `GET /api/departments/<dept>/datasets` | view | `{items}`, each with `never_called` and `dnc` counts |
| `POST /api/departments/<dept>/datasets` (multipart: `name, use_case_id, source, description, file?, url, token, path, method, phone_field, name_field`) | manage | `{dataset, stats}`. A failed pull returns the dataset with `stats: {error}` |
| `GET /api/datasets/<id>` | view | `{dataset, stats:{total, never, connected, blocked, dnc, attempts}}` |
| `GET /api/datasets/<id>/rows?f=never\|connected\|dnc&q=&page=` | view | `{items, total}`. Each row has a `dnc` boolean |
| `GET /api/datasets/<id>/facets` | view | `dataset_facets` |
| `POST /api/datasets/<id>/append` (multipart `file`) | manage | stats |
| `POST /api/datasets/<id>/pull` | manage | stats |
| `POST /api/datasets/<id>/preview {segment}` | manage | `{selected, cooled, sample:[5 rows]}` |
| `DELETE /api/datasets/<id>` | admin | 204 |
| `PUT /api/dnc/<phone> {reason}` / `DELETE /api/dnc/<phone>` | manage in any dept | 204, audited. The phone is normalised first; invalid gives 400 |
| `POST /api/ingest/datasets/<id>/leads` (Bearer `INGEST_TOKEN`; body is a lead object or an array) | token | 503 if `INGEST_TOKEN` is unset; 401 on a wrong token (`hmac.compare_digest`); 400 if no lead has a valid phone. Upserts with hints `phone, name, city, society, segment`, commits, then runs `fire_triggers(conn, dataset_id=id)`. Returns `{stats, fired}` |

- [ ] **Step 1: Write the failing tests.**
```python
def test_create_dataset_upload_and_rows(client, conn): ...     # multipart CSV -> stats.added 2; rows endpoint total 2
def test_dataset_of_other_dept_403(client, conn): ...          # append/preview/delete with a DEMAND manager -> 403
def test_dnc_toggle_global(client, conn): ...                  # PUT /api/dnc/+919810043058 -> row dnc True in every dataset holding it; DELETE -> False
def test_ingest_requires_token(client, conn, monkeypatch): ... # unset -> 503; wrong -> 401
def test_ingest_dials_new_lead_once(client, conn, bolna_stub, monkeypatch):  # Review Focus 4
    # ON_NEW_LEAD workflow on dataset, inside window
    client.post(url, json={"phone": "9810043058", "name": "A", "society": "X"}, headers=tok)
    client.post(url, json={"phone": "+91 98100 43058", "name": "A"}, headers=tok)
    assert [c for c in bolna_stub.calls if c[0] == "create_batch"].__len__() == 1
def test_ingest_invalid_phone_400(client, conn, monkeypatch): ...
```

- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** the routes in the table.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Report changed files.**

### Task 14: API: workflows, schedule, sync

**Files:**
- Create: `production/backend/api/workflows.py`, `api/schedule.py`, `api/sync.py`, `tests/test_api_workflows.py`
- Modify: `app.py`

**Interfaces:**
- Consumes: Tasks 8, 9, 11.
- Produces:

| Method + path | Who | Notes |
|---|---|---|
| `GET /api/departments/<dept>/builder-data` | manage | `{use_cases, agents (vertical dept or BOTH), datasets, facets:{dataset_id: facets}, numbers (active), parents (top-level workflows), default_policy}` |
| `GET /api/departments/<dept>/workflows` | view | `{items}`, each with `use_case_name, agent_name, dataset_name, runs, live` |
| `POST /api/departments/<dept>/workflows`, `PUT /api/workflows/<id>` (WorkflowInput) | manage | validated (below). Returns the row |
| `GET /api/workflows/<id>` | view | `{workflow, use_case, agent, dataset, children, runs:[campaign rows with dialed/connected], stats}` |
| `POST /api/workflows/<id>/toggle {active}` | manage | row |
| `DELETE /api/workflows/<id>` | admin | 204 |
| `POST /api/workflows/<id>/run {requested_start?, ignore_cooling?}` | manage | the `run_workflow` result, or `{campaign_id: null, leads: 0}` |
| `GET /api/departments/<dept>/schedule?from=&to=` | view | `{runs (both depts, each with department), queue (this dept's PLANNED, ordered), window:{start,end}}` |
| `POST /api/runs/<id>/cancel` | manage | 204 |
| `POST /api/runs/<id>/priority {priority, requested_start?}` | admin | the new plan |
| `POST /api/departments/<dept>/replan` | manage | the plan |
| `POST /api/sync {full?}` | manage | the report, or 409 `{error:"a sync is already running"}` |
| `GET /api/sync/status` | any | `{last_report, last_full_sync_date}` |

  WorkflowInput: `{use_case_id, name, purpose, agent_id, dataset_id, segment, from_number, variable_map, trigger, redial_policy, rules, parent_id, step_order, priority, active}`.

  Validation (400 on failure):
  - the use case is in the department and the agent is in the department or BOTH;
  - the dataset is in the department, or null only for AFTER_STEP;
  - `trigger.type` is one of `MANUAL|SCHEDULE|ON_NEW_LEAD|AFTER_STEP`;
  - AFTER_STEP needs a `parent_id` in the same department and `delay_hours > 0`;
  - ON_NEW_LEAD needs a dataset;
  - action types come from the Global Constraints list;
  - the policy passes `validate_policy`.

- [ ] **Step 1: Write the failing tests.**
```python
def test_workflow_validation(client, conn): ...         # DEMAND dataset on a SUPPLY workflow -> 400; action "crm_push" -> 400; AFTER_STEP without parent -> 400
def test_workflow_cross_dept_403(client, conn): ...
def test_run_now_plans_on_calendar(client, conn): ...   # MANUAL run -> scheduled_run PLANNED with planned_start
def test_reprioritise_requires_head(client, conn): ...  # manager -> 403
def test_sync_409_when_locked(client, conn): ...        # hold pg_advisory_lock(42100001) on another connection -> 409
```

- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** the routes in the table.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Report changed files.**

### Task 15: API: campaigns

**Files:**
- Create: `production/backend/api/campaigns.py`, `tests/test_api_campaigns.py`
- Modify: `app.py`

**Interfaces:**
- Consumes: `parse_lead_file`, `guess_column_map`, `build_leads`, `spoken_bhk` (Task 4); `create_round`, `eligible_redial_leads`, `validate_policy` (Task 7); `plan_run` (Task 8); `bolna.stop_batch`.
- Produces:

| Method + path | Who | Notes |
|---|---|---|
| `GET /api/departments/<dept>/campaigns?status=&use_case_id=` | view | `{items}`, each with `use_case_name, agent_name, leads, dialed, connected, good, pending` |
| `POST /api/departments/<dept>/campaigns/preview` (multipart `file, agent_id`) | manage | `{columns, sample (5), total, column_map, variables}` |
| `POST /api/departments/<dept>/campaigns` (multipart `file` + `payload` JSON) | manage | payload `{name (≥2), agent_id (dept or BOTH), from_number (≥8), notes, column_map, redial_policy, start_mode: draft\|now\|schedule, scheduled_at, speak_bhk, use_case_id, purpose}`. Returns `{id, created, invalid, duplicates}`; `now`/`schedule` call `plan_run` |
| `GET /api/campaigns/<id>` | view | `{campaign, agent, use_case, counters:{by_status:{...}, dialed, connected, good}, batches, run}` |
| `GET /api/campaigns/<id>/leads?status=&page=` | view | `{items, total}` |
| `POST /api/campaigns/<id>/plan {scheduled_at?}` | manage | the run |
| `POST /api/campaigns/<id>/start-now` | admin | the `create_round` result |
| `POST /api/campaigns/<id>/pause` | manage | `{stopped, errors}`: stops active Bolna batches (marking them stopped), QUEUED → PENDING, campaign PAUSED |
| `POST /api/campaigns/<id>/resume` | manage | the `create_round` result (status RUNNING, then a FIRST round of PENDING leads) |
| `POST /api/campaigns/<id>/stop` | manage | pause, then STOPPED, `completed_at`, and cancel its PLANNED runs |
| `POST /api/campaigns/<id>/redial {ignore_gap}` | manage | the `create_round` REDIAL result (`ignore_gap` = eligibility at now+365d) |
| `PUT /api/campaigns/<id>/policy {redial_policy}` | manage | row (`validate_policy`) |
| `DELETE /api/campaigns/<id>` | owner | 409 unless the status is DRAFT, COMPLETED or STOPPED |

  Every mutation writes `audit(...)` with the same action names as `initial zip/actions/campaigns.ts`.

- [ ] **Step 1: Write the failing tests.**
```python
def test_create_campaign_from_upload(client, conn): ...       # CSV 3 rows (1 dup) -> created 2 duplicates 1; start_mode now -> scheduled_run exists
def test_create_rejects_agent_of_other_dept(client, conn): ...  # 400
def test_controls_cross_dept_403(client, conn): ...           # DEMAND manager pause on SUPPLY campaign -> 403
def test_pause_resume(client, conn, bolna_stub): ...          # pause -> stop_batch called, QUEUED->PENDING; resume -> create_batch called
def test_start_now_requires_head(client, conn, bolna_stub): ...
def test_delete_running_campaign_409(client, conn): ...
```

- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** the routes in the table.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Report changed files.**

### Task 16: API: calls, export, recordings, hand-offs

**Files:**
- Create: `production/backend/api/calls.py`, `api/handoffs.py`, `tests/test_api_calls.py`
- Modify: `app.py`

**Interfaces:**
- Consumes: `load_scoped`, `mark_dnc`, `signed_recording_url`, `storage_enabled`, `flatten_extracted`, `fmt_ist`.
- Produces:

| Method + path | Who | Notes |
|---|---|---|
| `GET /api/departments/<dept>/calls?agent_id&campaign_id&outcome&tag&from&to&q&page` | view | `{items, total}`. Restricted to calls whose campaign is in the department (and use-case restriction). `q` matches phone, lead name or transcript (`ILIKE`) |
| `GET /api/calls/<id>` | view | `{call, campaign, agent, lead, fields (flattened), history (calls of the same lead, newest first)}` |
| `POST /api/calls/<id>/tag {tag}` | manage | sets `call.tag` and `lead.final_tag`, audited |
| `POST /api/calls/<id>/dnc` | manage | `mark_dnc(source="manual")` and lead DNC |
| `GET /api/calls/<id>/recording` | view | `{url}`: a signed R2 URL if `recording_key` is set and storage is enabled, else `recording_url`; 404 if neither |
| `GET /api/departments/<dept>/calls/export.csv` (same filters) | view | CSV with the column set from `initial zip/app/api/export/calls/route.ts` (no `limit` change: 20000), UTF-8, `called_at` via `fmt_ist(seconds=True)`, filename `calls_<epoch>.csv` |
| `GET /api/departments/<dept>/handoffs?status&use_case_id` | view | `{items}`, each with `use_case_name` and `call_id` |
| `PATCH /api/handoffs/<id> {status?, assigned_to_id?, rm_notes?}` | manage | row; `status` must be in the handoff enum |

- [ ] **Step 1: Write the failing tests.**
```python
def test_calls_list_scoped_to_dept(client, conn): ...       # DEMAND manager sees no SUPPLY calls; q search finds by transcript
def test_call_detail_cross_dept_403(client, conn): ...
def test_recording_returns_signed_or_bolna(client, conn, monkeypatch): ...
def test_export_csv_scoped_and_utf8(client, conn): ...      # header row as specified; Hindi transcript intact; DEMAND rows absent
def test_retag_updates_lead(client, conn): ...
def test_handoff_patch_validates_status(client, conn): ...  # "WHATEVER" -> 400
```

- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** the routes in the table.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Report changed files.**

### Task 17: API: users/team, agents, numbers, settings

**Files:**
- Create: `production/backend/api/users.py`, `api/agents.py`, `api/settings.py`, `tests/test_api_admin.py`
- Modify: `app.py`

**Interfaces:**
- Consumes: `check_user_edit`, `sync_agents`, `bolna.*`, `place_test_call`, `get_setting`, `storage_enabled`, `di_enabled`, `whatsapp_enabled`.
- Produces:

| Method + path | Who | Notes |
|---|---|---|
| `GET /api/users` | owner | `{items}` of all users |
| `GET /api/departments/<dept>/team` | admin | users whose vertical is the department or BOTH |
| `POST /api/users {email, role, vertical, use_case_ids, title, active}` | owner or head | pre-creates or updates by email (`@ALLOWED_EMAIL_DOMAIN` only), with `check_user_edit` |
| `PUT /api/users/<id> {role, vertical, use_case_ids, title, active}` | owner or head | `check_user_edit(actor, target, ...)` |
| `GET /api/agents`, `GET /api/agents/<id>` | any (filtered to `user_depts` + BOTH) | the agent with `config` (prompt included) |
| `POST /api/agents/refresh` | owner | `{count}` |
| `PUT /api/agents/<id> {vertical, description, caller_name, default_from_number}` | owner | row |
| `PUT /api/agents/<id>/prompt {system_prompt, welcome_message?}` | owner | `get_agent`, then `update_agent`, then `sync_agents`; the audit keeps `before` |
| `POST /api/agents/<id>/test-call {to, from_number, variables}` | manage in the agent's department (owner if BOTH) | `place_test_call` (DNC gives 409) |
| `GET /api/numbers` | any | `{items}` |
| `POST /api/numbers/refresh`, `PUT /api/numbers/<id> {label, active}` | owner | |
| `GET /api/settings/integrations` | owner | `{bolna:{configured}, r2:{configured}, di:{configured, failures}, interakt:{configured}, ingest:{configured, path:"/api/ingest/datasets/<dataset_id>/leads"}, sync:{enabled, last_report, last_full_sync_date}}` |
| `GET /api/settings/audit?page` | owner | `{items, total}`, each with `user_email` |

- [ ] **Step 1: Write the failing tests.**
```python
def test_head_cannot_demote_owner_via_api(client, conn): ...   # 403 on both POST-by-email and PUT
def test_head_claims_new_viewer(client, conn): ...             # VIEWER/BOTH -> MANAGER/SUPPLY by SUPPLY head -> 200
def test_manager_cannot_edit_agents_or_numbers(client, conn): ...  # 403
def test_test_call_dnc_409(client, conn, bolna_stub): ...
def test_integrations_reports_unconfigured(client, conn, monkeypatch): ...  # S3_BUCKET "" -> r2.configured False
```

- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** the routes in the table.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Report changed files.**

### Task 18: Backfill, seed, render.yaml, README, end-to-end smoke test

**Files:**
- Create: `production/backend/scripts/backfill.py`, `scripts/seed.py`, `render.yaml`, `README.md`, `tests/test_smoke.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - **`scripts.backfill.main() -> int`:** under the sync lock, runs `sync_agents`, then pages every agent's executions with no cap through `upsert_execution`, then runs `mark_dnc(conn, phone, "backfill", reason=tag)` for every call whose lower-cased tag is in `DNC_TAGS`. It prints counts. Safe to re-run.
  - **`scripts.seed.main() -> int`:** refuses (exit 2, message) unless `ALLOW_SEED=1`. It creates:
    - users `owner@openhouse.in` (ADMIN/BOTH), `supply.head@`, `demand.head@` (HEAD), `supply.manager@`, `demand.manager@` (MANAGER) and `viewer@openhouse.in` (VIEWER/BOTH);
    - one agent per department, a use case per department with `DEFAULT_TAXONOMY`, one dataset with 20 rows per department, and one MANUAL workflow per department.

    It never calls Bolna.

- [ ] **Step 1: Write the failing test.**
```python
def test_end_to_end(client, conn, bolna_stub, monkeypatch):
    # seed-like setup via factories; manager signs in via issue_jwt
    # 1 POST dataset (CSV, 2 rows) 2 POST ingest one more lead to an ON_NEW_LEAD workflow -> create_batch called
    # 3 POST workflow MANUAL + /run -> scheduled_run PLANNED; dispatch_due_runs(now=planned_start) -> create_batch called
    # 4 bolna_stub.executions = executions for those batches (one tagged "qualified", one "angry")
    # 5 run_sync() with requests.post stubbed for DI
    # assert: handoff exists for the qualified phone; dnc_phone has the angry phone; DI got 2 posts; lastSyncReport saved; report["errors"] == []
def test_backfill_marks_dnc_and_is_rerunnable(conn, bolna_stub): ...
def test_seed_refuses_without_flag(monkeypatch): ...  # main() == 2
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Implement the scripts.** Then write `render.yaml`:
```yaml
services:
  - type: web
    name: calling-console-api
    env: python
    region: singapore
    plan: standard   # always-on; the sync thread lives here (confirm plan in the deploy phase)
    rootDir: production/backend
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --worker-class gthread --threads 4 --timeout 180 --keep-alive 65
    healthCheckPath: /api/health
    envVars:
      - { key: PYTHON_VERSION, value: 3.12.7 }
      - { key: ALLOWED_EMAIL_DOMAIN, value: openhouse.in }
      - { key: SYNC_ENABLED, value: "1" }
      - { key: JWT_SECRET, generateValue: true }
      - { key: INGEST_TOKEN, generateValue: true }
      # sync: false (set in dashboard)
      - { key: DATABASE_URL, sync: false }
      - { key: GOOGLE_OAUTH_CLIENT_ID, sync: false }
      - { key: ADMIN_EMAILS, sync: false }
      - { key: FRONTEND_ORIGIN, sync: false }
      - { key: BOLNA_API_KEY, sync: false }
      - { key: S3_ENDPOINT, sync: false }
      - { key: S3_BUCKET, sync: false }
      - { key: S3_ACCESS_KEY_ID, sync: false }
      - { key: S3_SECRET_ACCESS_KEY, sync: false }
      - { key: DI_AI_CALLS_URL, sync: false }
      - { key: DI_AI_CALLS_TOKEN, sync: false }
      - { key: INTERAKT_API_KEY, sync: false }
      - { key: INTERAKT_FROM, sync: false }
```
  Write `README.md` in DI's backend README format, covering layout, setup, env vars, migrate/seed/backfill/run_sync commands and tests.

- [ ] **Step 4: Run it.** First the smoke test file. Expected: PASS. Then the full backend suite. Expected: all pass, 0 failures.
- [ ] **Step 5: Manual check.**
  - From `production/`, run `ALLOW_SEED=1 DATABASE_URL=postgresql://localhost/calling_console_dev backend/.venv/bin/python -m backend.scripts.migrate`, then the same command with `backend.scripts.seed`.
  - Then start the server with `DATABASE_URL=... JWT_SECRET=dev BOLNA_API_KEY=x backend/.venv/bin/python -m backend.app`.
  - Expected: `curl localhost:5060/api/health` returns `{"db":"connected","status":"ok"}`, and `curl -XPOST localhost:5060/api/auth/dev -H 'Content-Type: application/json' -d '{"email":"owner@openhouse.in"}'` returns a token.
- [ ] **Step 6: Report changed files.**

## Phase B: Direct_Inventory repo change

### Task 19: OH-Supply_meta landing page → console ingest

**Files:**
- Modify: `/Users/oh-sahaj/Documents/GitHub/Direct_Inventory/OH-Supply_meta/api/lead.js`, `.env.example`, `README.md`

**Interfaces:**
- Consumes: `POST /api/ingest/datasets/<id>/leads` (Task 13).

- [ ] **Step 1: Write the failing test.** Add the case to `OH-Supply_meta/test/` in that folder's existing test style (see `test/otp-flow.mjs`). After a successful lead with `CONSOLE_INGEST_URL` and `CONSOLE_INGEST_TOKEN` set:
  - a stubbed `fetch` receives a POST to that URL;
  - it carries `Authorization: Bearer <token>`;
  - the JSON body is `{phone, name, city, society, configuration, expected_price_lacs, source: "meta_landing", submitted_at}`.

  A stubbed fetch that rejects must still give a 201 response.
- [ ] **Step 2: Run it** with the command that folder already uses for `otp-flow.mjs`. Expected: FAIL.
- [ ] **Step 3: Implement.** After `COMMIT` and before the 201 response, if both env vars are set, `await fetch(url, {method: "POST", headers, body, signal: AbortSignal.timeout(3000)})` inside a `try/catch` that only does `console.error("console ingest failed", err)`. The lead is already saved in that app's own database, so the visitor's submission must not fail (spec §5.6). Document the two env vars in `.env.example` and `README.md`.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Report changed files** in the DI repo. Don't commit.

## Phase C: Frontend (`production/frontend`)

Frontend tasks have no unit-test framework (DI has none), so verification is `npm run build` plus a scripted click-through. Use the `webapp-testing` skill (Playwright) against the seeded local backend from Task 18: backend on 5060, then `npm run dev` on 5174.

### Task 20: Scaffold copied from DI

**Files:**
- Create in `production/frontend/`:
  - `package.json`, `vite.config.js`, `vercel.json`, `index.html`, `.gitignore`, `.env.example`, `README.md`
  - `public/` (logos and favicon from DI)
  - `src/main.jsx`, `src/App.jsx`, `src/styles.css`
  - `src/api/client.js`
  - `src/contexts/AuthContext.jsx`, `src/contexts/ThemeContext.jsx`
  - `src/components/Toaster.jsx`, `src/utils/toast.js`, `src/components/Layout.jsx`, `src/pages/Login.jsx`
  - `src/utils/format.js`

**Interfaces:**
- Produces:
  - **`api` from `src/api/client.js`:**
    - `api.get(path)`, `api.post(path, body)`, `api.put`, `api.patch`, `api.del`
    - `download(path) -> Blob`
    - `upload(path, formData)`: POST multipart with no JSON Content-Type, the same 401 handling, and it wipes the cache
    - `setAuthToken(token)`
  - **`useAuth()`:** `{user, loading, loginWithGoogle(idToken), devLogin(email), logout()}`.
  - **`useTheme()`:** `{theme, toggle}`.
  - **`toast(msg, kind)`**
  - **`<Layout>`:** a sidebar plus topbar shell around an `<Outlet>`.
  - **`format.js`:** `fmtIST(iso, seconds=false)`, `fmtDur(sec)`, `pct(a,b)`, `OUTCOME_LABEL`, `describeTrigger(trigger)` (port of `initial zip/lib/describe.ts`).
  - **Route table in `App.jsx`:** every route in spec §5.9, wrapped in `RequireAuth`. Routes whose page doesn't exist yet render a placeholder `<div className="page-head"><h1>…</h1></div>`.

- [ ] **Step 1: Copy and adapt.**
  - **Package setup:** `package.json` uses the Global Constraints versions and DI's scripts. `vite.config.js` is DI's (port 5174, `/api` proxied to `http://localhost:5060`). `vercel.json` is DI's, with the `/api/(.*)` destination `https://calling-console-api.onrender.com/api/$1` (the final URL is set in the deploy phase).
  - **`styles.css`:** DI's file, trimmed to the sections listed in spec §5.9.
  - **`client.js`:** DI's, minus the mock layer, plus `upload`. TTL rules: `/api/departments/` dashboards 60s and `/api/agents` 300s.
  - **Auth:** `AuthContext` uses localStorage key `cc_token` and calls `/api/auth/google`, `/api/auth/dev` and `/api/auth/me`.
  - **`Login.jsx`:** DI's login look, a Google button (`VITE_GOOGLE_OAUTH_CLIENT_ID`), and a dev email box shown only when that variable is empty.
  - **Sidebar:** department switcher (`user` depts), then Dashboard, Use cases, Datasets, Workflows, Schedule, Campaigns, Calls, Hand-offs, Team and Settings (the last two for heads and the owner), then a global section with Agents, All users and Integrations (owner).
- [ ] **Step 2: Build.** `cd production/frontend && npm install && npm run build`. Expected: the build succeeds.
- [ ] **Step 3: Click-through.** Dev-login as `owner@openhouse.in`.
  - Expected: you land on `/`.
  - The sidebar shows both departments.
  - The theme toggle flips `data-theme`.
  - Signing out returns to `/login`.
  - An expired or invalid token gives the "Session expired" toast and `/login`.
- [ ] **Step 4: Report changed files.**

### Task 21: Shared UI pieces, Home, department dashboard

**Files:**
- Create: `production/frontend/src/components/ui.jsx`, `src/pages/Home.jsx`, `src/pages/Dashboard.jsx`

**Interfaces:**
- Consumes: `GET /api/home`, `GET /api/departments/<dept>/dashboard` (Task 12).
- Produces, in `ui.jsx`:
  - `PageHead({title, sub, actions})`
  - `Card({title, children})`
  - `Stat({label, value, tone})`
  - `Bars({items:[{label,n,href}]})`
  - `OutcomeBadge({o})`
  - `TagBadge({tag})`, with the colour families from `initial zip/components/ui.tsx` mapped to DI tokens: good uses `--green`, soft `--amber`, dnc `--red`, the rest neutral
  - `Empty({children})`
  - `Modal({open, onClose, title, children})`
  - `Table({cols, rows})`

- [ ] **Step 1: Implement.**
  - **Home:** department cards. Single-department users redirect to `/d/<slug>`.
  - **Dashboard:** matches `initial zip/app/(app)/d/[dept]/page.tsx`:
    - range buttons Today/7d/30d and the use-case filter;
    - six headline stats, the funnel table, the outcome and tag bars (linking to `/d/<dept>/calls?...`), agents, "Up next" and open hand-offs;
    - the last-sync line.
- [ ] **Step 2: Build.** `npm run build`. Expected: OK.
- [ ] **Step 3: Click-through.** As `supply.manager@`, you land on `/d/supply`, see the stats from seed data (zeros are fine), and switching range updates the URL and the data. As `viewer@`, Home shows both departments.
- [ ] **Step 4: Report changed files.**

### Task 22: Use cases, department settings, team, all users

**Files:**
- Create: `production/frontend/src/pages/UseCases.jsx`, `DeptSettings.jsx`, `Team.jsx` (used for both `/d/:dept/team` and `/team`), `src/components/UseCaseEditor.jsx`

**Interfaces:**
- Consumes: Task 12 use-case and department routes; Task 17 users routes.

- [ ] **Step 1: Implement.** Port `initial zip/components/use-case-editor.tsx`, `dept-settings-form.tsx`, `team-table.tsx` and the pages that use them. Show the backend's `{error}` in a toast on 400 or 403. Heads see role options MANAGER and VIEWER only.
- [ ] **Step 2: Build.** Expected: OK.
- [ ] **Step 3: Click-through.**
  - As `supply.head@`: edit the window to 19:00–10:00, which shows the backend error toast and saves nothing.
  - Add a use case.
  - Promote `viewer@` to MANAGER/SUPPLY.
  - The `/team` route redirects non-owners to `/`.
- [ ] **Step 4: Report changed files.**

### Task 23: Datasets

**Files:**
- Create: `production/frontend/src/pages/Datasets.jsx`, `DatasetNew.jsx`, `DatasetDetail.jsx`

**Interfaces:**
- Consumes: the Task 13 dataset and DNC routes.

- [ ] **Step 1: Implement.** Port `initial zip/app/(app)/d/[dept]/datasets/*` and `components/dataset-actions.tsx`:
  - **List:** rows, never-called and DNC counts.
  - **Create:** upload or CRM pull.
  - **Detail:** stats, a facets sidebar, row filters (never / connected / dnc), search, a DNC toggle per row (`PUT`/`DELETE /api/dnc/<phone>`), append file, pull again, and "New workflow on this data" (`/d/:dept/workflows/new?dataset_id=`).
- [ ] **Step 2: Build.** Expected: OK.
- [ ] **Step 3: Click-through.** Upload a 3-row CSV, which shows the counts. Toggle DNC on one row; it fades, and the DNC count is 1.
- [ ] **Step 4: Report changed files.**

### Task 24: Workflows

**Files:**
- Create: `production/frontend/src/pages/Workflows.jsx`, `WorkflowDetail.jsx`, `src/components/WorkflowBuilder.jsx`, `src/components/RedialPolicyFields.jsx`

**Interfaces:**
- Consumes: the Task 14 workflow routes and `builder-data`.

- [ ] **Step 1: Implement.** Port `initial zip/components/workflow-builder.tsx` (the six sections), `redial-policy-fields.tsx` and `workflow-controls.tsx`, using snake_case keys. "Preview selection" calls `POST /api/datasets/<id>/preview`. There is no CRM-push action option. The list groups workflows by use case, with child steps shown under their parents. The detail page shows stats, run/plan/activate/delete, rules, child steps and run history.
- [ ] **Step 2: Build.** Expected: OK.
- [ ] **Step 3: Click-through.** Create a MANUAL workflow on the seeded dataset. Preview shows a count. "Run now" creates a run that appears in run history and on the schedule.
- [ ] **Step 4: Report changed files.**

### Task 25: Schedule

**Files:**
- Create: `production/frontend/src/pages/Schedule.jsx`

**Interfaces:**
- Consumes: Task 14 `GET schedule`, cancel, priority and replan.

- [ ] **Step 1: Implement.** Port `initial zip/app/(app)/d/[dept]/schedule/page.tsx` and `components/schedule-controls.tsx`:
  - a week grid inside the department window, with the other department's runs faded;
  - a queue with priority controls (heads) and cancel;
  - a Re-plan button.
- [ ] **Step 2: Build.** Expected: OK.
- [ ] **Step 3: Click-through.** The run from Task 24 is visible. Changing its priority as a head re-orders the queue. Cancelling removes it.
- [ ] **Step 4: Report changed files.**

### Task 26: Campaigns

**Files:**
- Create: `production/frontend/src/pages/Campaigns.jsx`, `CampaignNew.jsx`, `CampaignDetail.jsx`, `src/components/CampaignWizard.jsx`, `src/components/CampaignControls.jsx`

**Interfaces:**
- Consumes: the Task 15 routes.

- [ ] **Step 1: Implement.**
  - **Wizard:** a port of `components/campaign-wizard.tsx` (agent → upload → map columns → redial policy → queue / plan / draft). It uses `upload()` for preview and create.
  - **Detail:** a port of `app/(app)/campaigns/[id]/page.tsx` plus `campaign-controls.tsx`: counters, batches and a paged leads table. Controls are shown by role: start-now for heads and the owner, delete for the owner on DRAFT, COMPLETED or STOPPED. There are no CRM push buttons.
- [ ] **Step 2: Build.** Expected: OK.
- [ ] **Step 3: Click-through.** Run the wizard with a 3-row CSV as a draft: the detail page shows 3 PENDING leads. "Plan" puts the campaign on the schedule. Delete is hidden for the manager.
- [ ] **Step 4: Report changed files.**

### Task 27: Calls and hand-offs

**Files:**
- Create: `production/frontend/src/pages/Calls.jsx`, `CallDetail.jsx`, `Handoffs.jsx`, `src/components/RecordingPlayer.jsx`

**Interfaces:**
- Consumes: the Task 16 routes.

- [ ] **Step 1: Implement.**
  - **Calls list:** a port of `components/calls-explorer.tsx` with filters held in the URL query and a CSV export through `download()`, saved as `calls_<ts>.csv`.
  - **Call detail:**
    - a port of `app/(app)/calls/[id]/page.tsx`;
    - `RecordingPlayer` fetches `GET /api/calls/<id>/recording` and then sets `<audio src>`;
    - a chat-style transcript, the extracted fields, the hangup label and the attempt history;
    - QA re-tag and mark DNC.
  - **Hand-offs:** a port of `handoff-row.tsx` with status tabs, a use-case filter and inline status, assignee and notes.
- [ ] **Step 2: Build.** Expected: OK.
- [ ] **Step 3: Click-through.** Use the calls from the Task 18 dev seed. To create some, run `python -m backend.scripts.run_sync` with stubbed data, or insert a call with SQL. Filter by outcome, open a call, re-tag it (the lead's final tag changes), and export the CSV. On hand-offs, move one to CONTACTED.
- [ ] **Step 4: Report changed files.**

### Task 28: Agents and Integrations

**Files:**
- Create: `production/frontend/src/pages/Agents.jsx`, `AgentDetail.jsx`, `Integrations.jsx`

**Interfaces:**
- Consumes: the Task 17 routes, `POST /api/sync`, `GET /api/sync/status`.

- [ ] **Step 1: Implement.**
  - **Agents:** port `app/(app)/agents/*` and `components/agent-forms.tsx`. Meta and prompt editing are for the owner only. There is no webhook toggle. Test call (manager and above) shows the 409 DNC message in a toast.
  - **Integrations:** a port of `app/(app)/settings/page.tsx` and `settings-forms.tsx`:
    - configured flags for Bolna, R2, DI and Interakt;
    - the ingest path;
    - the last sync report and its errors, the DI push failures, and the last full sync;
    - a "Sync now" button (a 409 shows "a sync is already running");
    - numbers refresh and labelling;
    - the audit trail, paged.
- [ ] **Step 2: Build.** Expected: OK.
- [ ] **Step 3: Click-through.** As the owner, edit an agent's caller name (it saves). Integrations shows R2/DI "not configured" locally. "Sync now" returns a report. As a manager, the agent edit controls are hidden and `/settings` redirects.
- [ ] **Step 4: Report changed files.**

### Task 29: Final verification

**Files:** none new.

- [ ] **Step 1: Backend suite.** `cd production && backend/.venv/bin/python -m pytest backend/tests -q`. Expected: all pass, 0 failures.
- [ ] **Step 2: Frontend build.** `cd production/frontend && npm run build`. Expected: OK, with no warnings about missing imports.
- [ ] **Step 3: Folder isolation.** `grep -rn "\.\./\.\./" production/frontend/src production/backend --include=*.py --include=*.js --include=*.jsx | grep -v node_modules`. Expected: no import reaches outside its own folder.
- [ ] **Step 4: Full click-through.** One Playwright pass over every route in spec §5.9 as owner, supply head, supply manager and viewer. Expected: each role sees only what product spec §2 allows, and nothing shows a blank page or a console error.
- [ ] **Step 5: Hand-over.** Report the changed files for both repos and the commands the user needs for the deploy phase (Neon `migrate`, `backfill`, the env var list). Don't commit.
