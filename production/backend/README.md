# Calling Console — Backend

Flask API for the Openhouse Calling Console: Bolna calling campaigns, workflows, the department calendar, hand-offs
and the sync job. Same stack and patterns as `Direct_Inventory/backend` (psycopg2 raw SQL, numbered idempotent SQL
migrations, PyJWT + Google sign-in, gunicorn, a sync thread elected by a Postgres advisory lock).

## Layout

```
app.py              Flask app factory + /api/health
wsgi.py             gunicorn entry; checks the sync config
gunicorn.conf.py    post_fork: starts the background sync thread in each worker
config.py           env-driven config
db.py               pooled psycopg2 connections
api/                blueprints: auth, users, agents, departments, use_cases, datasets, workflows, schedule,
                    campaigns, calls, handoffs, settings, sync, ingest, knowledgebases (Bolna, read-through)
services/           bolna, sync, redial, scheduler, workflows, datasets, classify, di_push, whatsapp, storage, rbac ...
migrations/         001..NNN SQL, applied in filename order (idempotent)
scripts/            migrate, seed, backfill, run_sync
tests/              pytest (database tests; Bolna and DI are stubbed)
render.yaml         Render service definition
```

## Setup

Run everything from `production/` (the package is `backend`, with relative imports).

```bash
brew install postgresql@17 && brew services start postgresql@17
createdb calling_console_dev && createdb calling_console_test

cd production
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cp backend/.env.example backend/.env      # then fill it in
```

## Environment

| var | purpose |
| --- | --- |
| `DATABASE_URL` | app Postgres (required) |
| `JWT_SECRET` | signs JWTs (required) |
| `GOOGLE_OAUTH_CLIENT_ID` | Google sign-in |
| `DEV_LOGIN` | `1` enables `POST /api/auth/dev` and only works while `GOOGLE_OAUTH_CLIENT_ID` is empty. Local only; never set it in production |
| `ALLOWED_EMAIL_DOMAIN` | login domain gate (default `openhouse.in`) |
| `FRONTEND_ORIGIN` | CORS allow-origin |
| `BOLNA_API_KEY`, `BOLNA_BASE_URL` | Bolna; unset key reports `configured: false` |
| `R2_BLOB_PHONE_CALLS_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_PHONE_CALLS_PUBLIC_URL`, `R2_KEY_PREFIX` | recording archive: the shared phone-calls bucket (same names as Openhouse_Meetings). Bucket = `https://<account>.r2.cloudflarestorage.com/<bucket>`; recordings go under `<R2_KEY_PREFIX>calling-console/recordings/`; links use the first public base. `R2_KEY_PREFIX` empty in production, `dev/` locally |
| `INGEST_TOKEN` | bearer token for the ingest webhooks (see Ingest webhooks below); unset = they answer 503 |
| `DI_AI_CALLS_URL`, `DI_AI_CALLS_TOKEN` | Direct_Inventory ai-calls ingest (Supply calls are pushed there) |
| `INTERAKT_API_KEY`, `INTERAKT_FROM` | WhatsApp via Interakt |
| `SYNC_ENABLED` | `0` disables the background sync thread (default `1`) |
| `LOG_LEVEL` | logging level |

The background sync thread refuses to start unless the Bolna, S3 and DI variables are all set.

## Commands

```bash
cd production
PY=backend/.venv/bin/python

$PY -m backend.scripts.migrate                       # apply migrations/*.sql
ALLOW_SEED=1 $PY -m backend.scripts.seed             # demo users, agents, datasets, workflows (never calls Bolna)
$PY -m backend.scripts.backfill                      # full agent + execution history, marks DNC (see below)
$PY -m backend.scripts.run_sync                      # one sync; add --full, and/or --max-pages N (0 = no cap)

$PY -m backend.app                                   # dev server on :5060 (DEV_LOGIN=1 for POST /api/auth/dev)
backend/.venv/bin/python -m pytest backend/tests -q  # TEST_DATABASE_URL defaults to calling_console_test
```

`seed` exits 2 unless `ALLOW_SEED=1`. It is re-runnable.

`backfill` takes the sync lock (exit 1 if a sync is running), syncs the agents, pages every agent's executions with
no cap, then marks DNC (source `backfill`, reason = the tag) every phone with a call tagged `dnc`, `do_not_call`,
`angry` or `wrong_person`. It is re-runnable and prints counts.

`run_sync` runs one sync under the same lock as the background thread and "Sync now". `--full` also syncs agents and
pages past each agent's lookback; `--max-pages 0` lifts the per-agent cap of 40 pages. It exits 1 when the report
has errors or another sync holds the lock.

## Ingest webhooks

All three take `Authorization: Bearer <INGEST_TOKEN>` and JSON; a wrong or missing token is 401, an unset one 503.
Each is recorded in the audit trail as `system`.

| Webhook | Does |
| --- | --- |
| `POST /api/ingest/use-cases` | create or update a use case, matched by `department` + `key` |
| `POST /api/ingest/datasets` | create or update a dataset, matched by `department` + `name`, and append `leads` |
| `POST /api/ingest/datasets/<id>/leads` | append leads to an existing dataset (the Meta landing page) |

Use case (a new one needs `tag_taxonomy` with exactly the lists `good, soft, terminal, dnc, retry`; an update changes
only the fields sent; the default agent is named by its Bolna agent id, null clears it):

```json
{"department": "SUPPLY", "key": "meta_sellers", "name": "Meta sellers", "description": "optional",
 "default_agent_bolna_id": "<bolna agent id>", "priority": 3, "color": "#12315C", "active": true,
 "tag_taxonomy": {"good": ["qualified"], "soft": ["callback_requested"], "terminal": ["not_selling"],
                  "dnc": ["do_not_call"], "retry": ["busy"]}}
```
→ `200 {"use_case": {...}, "created": true}`; 400 on a bad field, 409 when a renamed key is taken.

Dataset (`leads` optional, same handling as the leads webhook, including "new leads" workflow triggers;
`use_case_key` and `description` change only when sent, null clears them):

```json
{"department": "SUPPLY", "name": "Meta form leads - Nov", "use_case_key": "meta_sellers", "description": "optional",
 "leads": [{"phone": "+919810000000", "name": "Ravi", "city": "Noida", "society": "ATS", "any_other_field": "kept"}]}
```
→ `200 {"dataset_id": "...", "created": true, "stats": {"added": 1, "updated": 0, "invalid": 0, "duplicates": 0},
"fired": [], "errors": []}`; 409 when two datasets of the department already share the name (rename one in the app).

## Deployment (Render + Neon)

- Service: see `render.yaml` (root dir `production/backend`). The sync thread lives in the web service, so the plan
  must be always-on, not a free/sleeping tier.
- The sync thread starts in `gunicorn.conf.py` `post_fork`, in each worker, never at import: Render runs gunicorn with
  `--preload`, and a thread alive in the master at fork deadlocks the worker. The advisory lock lets one sync run at a
  time. The thread pulls every agent's last 15 minutes of Bolna executions every 10 s (`run_quick`) and runs the full
  sync every 5 minutes; the frontend polls `GET /api/sync/pulse?dept=` every 10 s for new calls and a stale pull.
- Migrations are run by hand, not on deploy: `DATABASE_URL=<neon> $PY -m backend.scripts.migrate`.
- Owners (ADMIN) come from the database, not the environment: a first Google sign-in becomes VIEWER. Make the first
  owner in Neon (before or after their first sign-in), then owners promote others from the Team page:
  `INSERT INTO "user" (email, name, role, vertical) VALUES ('<email>', '<name>', 'ADMIN', 'BOTH')
  ON CONFLICT (email) DO UPDATE SET role = 'ADMIN', vertical = 'BOTH';`
- Run the **backfill before creating any campaign and before the first production dial**, so numbers already marked
  DNC in Bolna history are blocked (and so no backfilled call links to a live campaign and fires its rules).
- The Neon pooled URL is fine: the sync lock is a transaction-level advisory lock, and the open transaction pins the
  server connection.
- That lock connection holds an open transaction for the whole sync. Check `SHOW idle_in_transaction_session_timeout`
  on Neon; if it is set below the longest sync, the lock connection is killed and a second sync can start.
- `DEV_LOGIN` is not in `render.yaml` and must stay unset in production.
