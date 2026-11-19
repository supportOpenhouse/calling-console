# Calling Console: frontend / backend split (design)

Date: 2026-11-18 · Status: for approval
Product spec (what the console does): `initial zip/Openhouse_Calling_Console.md`
Previous code (TypeScript, reference only, gitignored): `initial zip/`
House reference for **both** halves: `/Users/oh-sahaj/Documents/GitHub/Direct_Inventory` (DI). The frontend copies `DI/frontend`; the backend copies `DI/backend`.

## 1. Intent

The console Supply and Demand use to run Bolna AI voice campaigns is being rebuilt on Openhouse's standard setup, the exact one DI uses: a Vite + React single-page app on Vercel and a Python Flask API on Render. Each half is deployed from its own folder only. Data lives in Neon, recordings in Cloudflare R2. The point is to keep things **universal**: anyone who knows DI can open this repo and find the same structure, libraries, conventions and deploy setup.

The product itself doesn't change: everything in the product spec, sections 2–6. The exceptions are the decisions recorded below. The rebuild is also the moment to fix the problems found in the review of the old code (section 9).

Delivery order (from the user): write the code → connect services → deploy to production → test in production → share with the team.

## 2. Goals, non-goals, anti-goals

**Goals**
- Everything in product spec §2–§6 works end to end: roles, departments, use cases, datasets, workflows, follow-up steps, calendar scheduler, campaigns, calls, hand-offs, team, agents, integrations, the background job.
- `production/frontend` deploys on Vercel and `production/backend` on Render, each with no file outside its folder.
- Same stack, layout and conventions as DI on both halves (§5.2, §5.9). Where this design departs from DI, it says so and why.
- Supply calls reach DI's AI Calls page. Meta landing-page leads are dialled within minutes.
- Every fix in §9 is in.

**Non-goals**
- Editing agent voice or transcriber settings (product spec non-goal). Prompt editing stays.
- Replacing DI. DI decides which AI calls become inventory leads (its own cron and "Add as lead").
- The product spec's "next steps": best-time-per-lead scheduling, Google Sheets source, timed CRM re-pull.
- Owning Bolna agents' webhook URLs. They stay as they are; the console gets results by polling.
- A mobile layout. It's a desktop console, but it must not break on narrow screens.
- Reusing the old TypeScript code. It's the reference for behaviour and Bolna quirks; the backend is written fresh in Python.

**Anti-goals** (failure even if everything else works)
- Anyone dialled twice by accident, or a DNC number dialled at all.
- Silent fallbacks. Missing config or a failed call to Bolna, R2 or DI fails loudly and is shown to the user. No mock data in production builds.
- Any dial (batch or single call) that skips the dial guard (§5.4).

## 3. Constraints

- Vercel's root directory is `production/frontend` and Render's is `production/backend`. No imports across folders and no shared package.
- Database: Neon Postgres. Storage: Cloudflare R2 through the S3 API.
- Bolna: one account with 10 concurrent lines shared by both departments. The API quirks handled in `initial zip/lib/bolna.ts` must carry over: the read-only agent fields on PUT, batch create then schedule, executions paged newest first, `balance-low`. The call classifier rules in `initial zip/lib/classify.ts` (hangup codes, echo detection, the voicemail and not-picked heuristics) carry over exactly.
- Sign-in: Google, `@openhouse.in` only.
- DI's ingest contract (`DI/backend/api/ai_calls.py` and `services/ai_calls.py`):
  - `POST /api/ai-calls/ingest`, with the token in an `X-AI-Calls-Token` header.
  - It accepts DI's own "normalised" format: `call_id`, `source`, `campaign`, `batch_id`, `called_at`, `phone`, `caller_id`, `contact{name, society, city}`, `outcome`, `outcome_detail`, `talk_seconds`, `carrier_blocked`, `call_tag`, `tag_confidence`, `property{configuration, size_sqft, asking_price_raw, facing}`, `timeline`, `callback_at`, `summary`, `transcript`, `recording_url`, `cost_usd`.
  - It stores one row per `call_id` and merges repeat deliveries, so pushing the same call twice is harmless.
- Claude never commits. The user commits, in this repo and in DI.

## 4. Approaches

**Backend language (user's choice, changed from Node + TypeScript): Python + Flask, copied from DI.**
1. *Python + Flask, DI's exact stack (picked).* One backend stack across Openhouse, and DI's database, auth, config, migration and background-job code are copied as they are. The cost is writing about 2,500 lines of logic fresh in Python. The risk on Bolna edge cases is covered by porting the old behaviour rule by rule and testing it (§6).
2. *Node + TypeScript (dropped).* Would have reused the old code, but it's a second backend language at Openhouse.

**Frontend: Vite + React single-page app in JSX, DI's exact stack.** Next.js would only server-render pages that have no database to read.

**How the browser reaches the API: DI's way.** Vercel rewrites `/api/*` to the Render service, so the browser sees a single origin, and `VITE_API_BASE` is left blank. DI's client already supports pointing `VITE_API_BASE` at the Render URL, with CORS already allowing `FRONTEND_ORIGIN`. If Vercel's proxy ever rejects a large lead upload or a long "Sync now", that's the switch, with no code change.

## 5. Design

### 5.1 Shape

```
Browser ── SPA served by Vercel (production/frontend)
   │  /api/*  (Vercel rewrite → Render)   Authorization: Bearer <app JWT>
   ▼
Render web service (production/backend): Flask under gunicorn + background sync thread
   ├─▶ Neon Postgres (psycopg2 pool, raw SQL)
   ├─▶ Bolna API (agents, batches, executions, single calls, numbers)
   ├─▶ R2 (recording archive, signed playback URLs)
   ├─▶ DI  POST /api/ai-calls/ingest   (every finished Supply call)
   └─▶ Interakt WhatsApp · per-dataset CRM pull URLs
Inbound to Render: Google ID-token sign-in · lead ingest from the OH-Supply_meta landing page
```

### 5.2 Backend: `production/backend` (copies DI/backend)

- **Runtime:** Python 3.12.7, recorded in both `runtime.txt` (`python-3.12.7`) and `.python-version`.
- **`requirements.txt`:** the same pins as DI: `Flask==3.0.3`, `flask-cors==4.0.1`, `flask-compress==1.24`, `psycopg2-binary==2.9.9`, `PyJWT==2.9.0`, `gunicorn==22.0.0`, `google-auth==2.34.0`, `requests==2.32.3`, `python-dotenv==1.0.1`. The console needs two more, pinned:
  - `boto3`, for R2;
  - `openpyxl`, for reading XLSX. CSV uses the standard library.
  Time-zone handling uses the standard library's `zoneinfo` with `Asia/Kolkata`.
- **Package layout** (DI's): the folder is the Python package `backend`, with relative imports (`from . import config`).
  - `wsgi.py` puts the parent folder on `sys.path`, imports `backend.app:app` and starts the background thread.
  - `app.py` holds `create_app()`:
    - DI's JSON provider, so `Decimal` comes out as a number;
    - `CORS(app, origins=[FRONTEND_ORIGIN])` and `Compress(app)`;
    - `GET /api/health`, which pings the database;
    - one blueprint registered per area;
    - it refuses to start without `JWT_SECRET`.

  ```
  backend/
    app.py  wsgi.py  config.py  db.py  __init__.py
    api/        auth.py, departments.py, use_cases.py, datasets.py, workflows.py, schedule.py,
                campaigns.py, calls.py, handoffs.py, users.py, agents.py, settings.py, ingest.py, sync.py
    services/   bolna.py, classify.py, sync.py, redial.py, scheduler.py, workflows.py, datasets.py,
                lead_import.py, di_push.py, storage.py, whatsapp.py, ist.py, rbac.py, audit.py
    migrations/ 001_init.sql …   (numbered, idempotent)
    scripts/    migrate.py, run_sync.py, backfill.py, seed.py
    tests/      test_*.py
    render.yaml  runtime.txt  .python-version  requirements.txt  .env.example  .gitignore  README.md
  ```
- **`config.py`:** DI's style. Module-level constants read from the environment, and `backend/.env` loaded through python-dotenv. Variables:
  - `DATABASE_URL`, `JWT_SECRET` (required, no default), `GOOGLE_OAUTH_CLIENT_ID`, `ALLOWED_EMAIL_DOMAIN` (default `openhouse.in`), `FRONTEND_ORIGIN`;
  - `BOLNA_API_KEY`, `BOLNA_BASE_URL`;
  - `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`;
  - `INGEST_TOKEN`, `DI_AI_CALLS_URL`, `DI_AI_CALLS_TOKEN`;
  - `INTERAKT_API_KEY`, `INTERAKT_FROM`;
  - `SYNC_ENABLED`, `LOG_LEVEL`.

  Integrations follow DI's style: each reports "not configured" when its variables are absent, and the ingest endpoint answers 503 without `INGEST_TOKEN` (it fails closed, the way DI's AI-calls ingest does).
- **`db.py`:** copied from DI as-is. A lazily-created psycopg2 `ThreadedConnectionPool` with `RealDictCursor`, TCP keepalives, the `SELECT 1` ping that drops connections Neon cut while idle, and `get_conn()`. Only the app-database pool is kept; DI's properties and CP pools aren't needed.
- **Data access:** raw SQL through psycopg2, as in DI. No ORM.
- **Migrations:** numbered, idempotent SQL files in `migrations/`, applied in filename order by `scripts/migrate.py` (DI's runner, copied as-is).
  - `001_init.sql` creates the whole schema: the old Drizzle schema with the changes in §5.8, rewritten as plain SQL.
  - Later changes are new numbered files.
  - Migrations are run by hand (`python -m backend.scripts.migrate`), as in DI. They aren't part of the build.
- **Request handling:** blueprints under `/api/...`. Request bodies are read with `request.get_json(silent=True)` and checked by hand, as in DI. Errors are returned as `jsonify({"error": ...})` with the right status code.
- **Local dev:** `python -m backend.app` from `production/` serves on port 5060, as in DI. Under that command the background thread doesn't start (wsgi only).

### 5.3 Sign-in and permissions

- **Sign-in** is DI's `api/auth.py`, copied:
  1. `POST /api/auth/google {id_token}` → verified with `google.oauth2.id_token.verify_oauth2_token` against `GOOGLE_OAUTH_CLIENT_ID`.
  2. The email must be verified and end in `@ALLOWED_EMAIL_DOMAIN`.
  3. The user is created on first sign-in as VIEWER with department BOTH; an existing row keeps its role. Product owners (ADMIN) are set in the database (no env list; changed 2026-11-19).
  4. It returns an HS256 JWT signed with `JWT_SECRET` that lasts 7 days (DI's `JWT_EXPIRY_HOURS`).
- `GET /api/auth/me` returns the user.
- `POST /api/auth/dev` (DI's passwordless sign-in for seeded demo accounts) is active only while `GOOGLE_OAUTH_CLIENT_ID` is unset, which means local development only.
- **One departure from DI, for safety:** `require_auth(*roles)` sets `g.user` from the **users table on every request**, not from the token's claims. Role changes and deactivation then apply immediately, which matters with 7-day tokens. An inactive user gets a 401.
- **Permission checks** are helpers in `services/rbac.py`, used by every route. They load the record being touched (campaign, dataset, workflow, run, call, hand-off, user, and so on) and check against *that record's* department, never against a department sent by the client, plus the user's use-case restriction and the capability needed.
- **Who can do what** follows product spec §2. In addition:
  - A head can create or edit MANAGER and VIEWER users in their own department. A head can also take a VIEWER whose department is BOTH (anyone newly signed in) into their own department. A head can never edit an ADMIN or HEAD. Nobody can change their own role.
  - Agents (department, caller name, default number, prompt) and phone numbers: owner only.
  - Test call: managers and above.

### 5.4 Dialling: one guard every call goes through

- **Bulk dialling.** Every Bolna batch is created in one function, `services/redial.create_round`. The ad-hoc wizard, workflow runs, new-lead runs, follow-up steps, redials and resume all go through it. Right before building the batch, it removes:
  - **Do-not-call numbers:** any phone in the global `dnc_phone` table. The lead is marked `DNC`.
  - **Cooled-off numbers:** on first rounds, unless the campaign has `skip_cooling`, any phone that had a connected or voicemail call in another campaign within the department's cooling days. Backfilled history counts. The lead is marked `DONE` with final tag `skipped_cooling`.

  Redial rounds of the same campaign skip the cooling check.
- **Test call.** The Agents page's test call (Bolna's single-call API) goes through the same DNC check before dialling.

### 5.5 Background sync (copies DI's dialer thread)

- `wsgi.py` calls `start_background_sync()`, which starts one daemon thread per gunicorn worker. This is DI's `start_background_dialer` pattern.
- Each tick runs inside `pg_try_advisory_xact_lock(<key>)`, so exactly one worker syncs at a time. A worker that doesn't get the lock skips the tick.
- The tick runs every 5 minutes. The first tick after 03:00 IST each night is a full sync.
- `SYNC_ENABLED=0` turns it off, like DI's `DIALER_ENABLED`.
- The thread starts only under gunicorn, never in tests, scripts or `python -m backend.app`.
- `scripts/run_sync.py [--full]` runs one sync from the command line through the same lock.
- **"Sync now"** (`POST /api/sync`) takes the same lock without waiting and returns 409 if a sync is already running.
- The steps are product spec §5 with the §9 fixes, plus one new step: push to DI (§5.6).
- Results come from polling only. The console doesn't change agents' webhook URLs, and there's no webhook endpoint or webhook toggle.

### 5.6 Direct_Inventory: Supply calls in, Meta leads out

- **Calls → DI.**
  - Every finished call in a Supply-department campaign is sent once to DI's ingest in DI's normalised format, with header `X-AI-Calls-Token`. "Finished" means a final Bolna status, and for connected calls, after the extraction wait.
  - `call_id` is the Bolna execution ID, so a call DI also receives some other way merges into the same row.
  - `contact{name, society, city}` comes from the lead's own list data, which matches DI's master society list better than the names the AI hears, and falls back to the extracted fields.
  - `call.pushed_to_di_at` marks a call as sent. A failed send is retried on the next sync and shown on the Integrations page.
  - DI's own cron decides which calls become inventory leads. The old "push to CRM" rule action, manual push buttons and `CRM_PUSH_TAGS` are removed.
- **Meta leads → console.**
  - `POST /api/ingest/datasets/<id>/leads` (Bearer `INGEST_TOKEN`) accepts one lead object or an array of them and adds them to the dataset.
  - It then immediately fires that dataset's active "new leads" workflows.
  - A "new leads" run dials straight away through `create_round`, without the calendar. Outside the calling window, the batch is scheduled for the next window start.
  - **Change in the DI repo:** after `OH-Supply_meta/api/lead.js` saves a verified lead, it POSTs the lead to this endpoint, configured by two new env vars on that Vercel app (`CONSOLE_INGEST_URL` including the dataset ID, and `CONSOLE_INGEST_TOKEN`).
  - The landing page has already saved the lead in its own database, so a failed POST is logged there and doesn't fail the visitor's submission.

### 5.7 History backfill (before the first production dial)

- `python -m backend.scripts.backfill` is a one-off script run during setup. It pulls every past execution of every Bolna agent with no page cap, saves them as calls (not linked to any campaign), and adds every phone tagged `dnc`, `do_not_call`, `angry` or `wrong_person` to `dnc_phone` with source `backfill`.
- Cooling then covers people already reached in the November campaigns.
- The script can be re-run safely.

### 5.8 Data model (`migrations/001_init.sql`)

- Same tables as `initial zip/db/schema.ts` (product spec §7), written as plain SQL with Postgres enums, text UUID primary keys, `timestamptz` and `jsonb`, keeping the same indexes and unique constraints. The changes:
- Drop the Auth.js tables (`account`, `session`, `verification_token`) and `user.email_verified`.
- Add `dnc_phone`: `phone` as primary key, `reason`, `source` (one of `use_case_tag`, `rule`, `manual`, `backfill`), `created_by_id`, `created_at`. This is the only place DNC is stored. Drop `dataset_lead.dnc`. The dataset screen shows and toggles DNC through this table.
- Add `campaign.skip_cooling` (boolean, default false). It's set by "new leads" and follow-up runs, and by a manager's "ignore cooling" run.
- `call.pushed_to_crm_at` becomes `call.pushed_to_di_at`.

### 5.9 Frontend: `production/frontend` (copies DI/frontend)

- **Stack:** DI's `package.json` stack and versions: Vite 5, `@vitejs/plugin-react`, React 18, react-router-dom 6, `@react-oauth/google`, `motion`, in JSX with `"type": "module"`, and scripts `dev`/`build`/`preview`. DI's map libraries are left out. No CSS framework.
- **Styles:** copy DI's `src/styles.css`, keeping the design tokens (light and dark), fonts (Fraunces and Inter), base styles, buttons, pills and chips, sidebar and topbar shell, page header, stat tiles, toolbar, tables, modals, toasts and login page. Leave out the DI-only sections (inventory board, dialer, maps, festive). Copy the Openhouse logo and favicon from DI's `public/`.
- **Shared code copied from DI and adapted:**
  - `src/api/client.js`: Bearer token, a 401 triggers the expiry event, identical GETs are de-duplicated with short caches, the cache is wiped on any write, and downloads come back as a Blob. Remove the mock layer.
  - `AuthContext`: token kept in localStorage under `cc_token`.
  - `ThemeContext`.
  - Toaster.
  - Layout shell, with a sidebar built for the console's navigation.
- **`vite.config.js`:** DI's, with the dev server proxying `/api` to `http://localhost:5060`.
- **`vercel.json`:** DI's. `/api/(.*)` is rewritten to the Render service URL, everything else falls back to `/index.html`, and static assets get the cache headers.
- **Screens** (product spec §4):

  | Route | Screen |
  |---|---|
  | `/login` | Sign in |
  | `/` | Home. Department picker for the owner and both-department users; single-department users are sent straight to their department |
  | `/d/:dept` | Department dashboard |
  | `/d/:dept/use-cases` | Use cases |
  | `/d/:dept/datasets`, `/new`, `/:id` | Datasets list, create, detail |
  | `/d/:dept/workflows`, `/new`, `/:id` | Workflows list, builder, detail and edit |
  | `/d/:dept/schedule` | Schedule (calendar) |
  | `/d/:dept/campaigns`, `/new` | Campaigns list, ad-hoc wizard |
  | `/campaigns/:id` | Campaign detail |
  | `/d/:dept/calls` | Calls list |
  | `/calls/:id` | Call detail |
  | `/d/:dept/handoffs` | Hand-offs |
  | `/d/:dept/team` | Department team |
  | `/d/:dept/settings` | Department settings |
  | `/agents`, `/agents/:id` | Agents list and detail |
  | `/team` | All users |
  | `/settings` | Integrations |

- The interactive parts of the old client components (campaign wizard, workflow builder, redial-policy fields, use-case editor, schedule controls) are converted from TSX to JSX. Server actions become API calls, and the look is rebuilt with DI's classes.
- **Recordings:** playback fetches a signed URL first (`GET /api/calls/<id>/recording` returns `{url}`), because `<audio src>` can't send a Bearer header.
- **CSV export:** downloaded through the client's Blob download.
- **Env vars:** `VITE_GOOGLE_OAUTH_CLIENT_ID`, `VITE_API_BASE` (blank in production).

### 5.10 API surface (grouped; the exact list is in the plan)

All endpoints are REST under `/api` with JSON bodies, and errors come back as `{error}` with the right status code.

| Area | Covers |
|---|---|
| auth | Google sign-in, dev sign-in, me |
| departments | Settings |
| use-cases | |
| datasets | Create, upload, append, pull, rows, facets, segment preview, DNC toggle |
| workflows | Create, update, toggle, delete, run |
| schedule | Runs, cancel, priority, re-plan |
| campaigns | Wizard preview and create, plan, start now, pause, resume, stop, redial, policy, delete |
| calls | List with filters, detail, retag, DNC, recording, export |
| hand-offs | |
| users / team | |
| agents | List, refresh, meta, prompt, test call |
| numbers | |
| settings | Integrations status, last sync report, DI push failures, audit log |
| other | sync, lead ingest, health |

## 6. Tests

- **Backend unit tests:** pytest-style `test_*.py` in `backend/tests`, pure where possible (DI's convention), run with `pytest backend/tests` from `production/`.
  - **Classifier:** the old `classify.ts` behaviour, case by case, on fixed sample executions.
  - redial eligibility
  - newest-call roll-up ordering
  - campaign completion
  - scheduler placement
  - IST window maths
  - permission helpers
  - the dial guard (DNC and cooling) in `create_round` and test call
  - DI payload mapping against DI's own `normalize()` expectations
  - The Bolna client and DI are stubbed.
- **Backend smoke test against a local Postgres** (Homebrew Postgres is installed): migrate, seed, then dev sign-in → create dataset → ingest a lead → run workflow → sync a stubbed execution → hand-off created and DI push recorded.
- **Frontend:** a production build, then a click-through of every screen against the local backend.
- **Production test** (a later phase): the product spec's first run of about 20 leads.

## 7. Deployment shape (set up in later phases)

- **Render:** `production/backend/render.yaml`, following DI's:
  - a Python web service with root directory `production/backend`, region Singapore;
  - build `pip install -r requirements.txt`;
  - start `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --worker-class gthread --threads 4 --timeout 180 --keep-alive 65`;
  - health check `/api/health`, and `PYTHON_VERSION` 3.12.7.
  - Plan: always-on, because the sync thread lives in it. DI uses `standard`; the exact plan is decided in the deploy phase.
  - There's no separate cron service; the thread replaces it.
- **Vercel:** project root `production/frontend`, Vite preset, output `dist`. `vercel.json` rewrites `/api` to the Render URL.
- **Neon:** a database for the console in the region closest to Render Singapore. `001_init.sql` onwards is applied with `scripts/migrate.py`.
- **R2:** a bucket for recordings.
- **Google OAuth client:** authorised JavaScript origins are the Vercel domain and localhost.
- **DI:**
  - The console's `DI_AI_CALLS_TOKEN` equals DI's `AI_CALLS_TOKEN`.
  - The OH-Supply_meta Vercel app gets `CONSOLE_INGEST_URL` and `CONSOLE_INGEST_TOKEN`.
- **First production dial** happens only after the backfill script.

## 8. Calls Claude made (check these)

1. **One departure from DI:** the logged-in user is read from the database on every request instead of trusted from the 7-day token, so role changes and deactivation apply immediately.
2. `boto3` and `openpyxl` are added to DI's requirements (R2 and XLSX); everything else is pinned the same as DI.
3. The sync runs as a background thread inside the web service (DI's dialer pattern), not as a Render cron service (DI's other pattern), so there's one service to pay for and no separate cron.
4. Every finished Supply call goes to DI, and Demand calls don't (DI is supply inventory). DI's cron decides conversion, so the console's "push to CRM" rule, buttons and tag list are removed.
5. Polling only. The console never touches Bolna webhook URLs.
6. Test call stays, behind the same DNC guard; managers and above.
7. Agents and phone numbers: owner only (matches product spec §2).
8. Heads can take a newly signed-in VIEWER (department BOTH) into their department.
9. The backfill is a script run once during setup, not something that happens automatically on boot.
10. A failed POST from the landing page to the console is logged and never fails the visitor's submission.

## 9. Fixes carried in from the review

1. Only one sync runs at a time (advisory lock). This also keeps rule side effects (WhatsApp, DI push) to once.
2. A lead's status, outcome, tag and last-call time come only from its newest call. Older executions update their own call row only.
3. A rule's "redial after N minutes" survives later syncs. Only a newer call resets it.
4. DNC is a global per-phone table, and it plus cooling are checked at dial time (§5.4). This covers follow-up steps, the ad-hoc wizard and test calls.
5. Every campaign finishes. A lead still QUEUED after its batch has ended and with no call becomes a FAILED attempt, which normal redial rules then handle. A balance-low call doesn't count as an attempt, and that lead stays eligible to be dialled again.
6. Permissions are checked against the record's own department and use case on every route. Heads can't touch owners or heads. The export and recordings endpoints are covered too.
7. The schema lives in committed, numbered SQL migrations. Nothing is gitignored.
8. No fixed login token. Dev sign-in works only without a Google client ID, and the seed script refuses to run unless `ALLOW_SEED=1`.
9. No vulnerable `xlsx` package: XLSX is read with `openpyxl`.
10. New leads are dialled within minutes (§5.6).
11. The push to DI uses DI's real contract (header and payload). The old push was rejected every time.

## 10. Left to the builder

- The exact endpoint paths and payloads, within §5.10's grouping and conventions.
- How the frontend is split into components, and which DI CSS sections are copied as-is or trimmed.
- The dashboard's SQL and response shapes.
- The exact pins for `boto3` and `openpyxl`.
