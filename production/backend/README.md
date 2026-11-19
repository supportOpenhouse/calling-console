# Calling Console — Backend

Flask API for the Openhouse Calling Console: Bolna calling campaigns, workflows, the department calendar, hand-offs
and the sync job. Same stack and patterns as `Direct_Inventory/backend` (psycopg2 raw SQL, numbered idempotent SQL
migrations, PyJWT + Google sign-in, gunicorn, a sync thread elected by a Postgres advisory lock).

## Layout

```
app.py              Flask app factory + /api/health
wsgi.py             gunicorn entry; starts the background sync thread
config.py           env-driven config
db.py               pooled psycopg2 connections
api/                blueprints: auth, users, agents, departments, use_cases, datasets, workflows, schedule,
                    campaigns, calls, handoffs, home, settings, sync, ingest
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
| `ADMIN_EMAILS` | comma-separated emails that get the ADMIN role |
| `FRONTEND_ORIGIN` | CORS allow-origin |
| `BOLNA_API_KEY`, `BOLNA_BASE_URL` | Bolna; unset key reports `configured: false` |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` | recording archive (R2) |
| `INGEST_TOKEN` | bearer token for `POST /api/ingest/datasets/<id>/leads` |
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

## Deployment (Render + Neon)

- Service: see `render.yaml` (root dir `production/backend`). The sync thread lives in the web service, so the plan
  must be always-on, not a free/sleeping tier.
- **Never use `--preload`** in the gunicorn command: `wsgi.py` starts the sync thread at import, and a thread started
  in the master dies at fork. Every worker runs its own thread; the advisory lock lets one sync at a time.
- Migrations are run by hand, not on deploy: `DATABASE_URL=<neon> $PY -m backend.scripts.migrate`.
- Run the **backfill before creating any campaign and before the first production dial**, so numbers already marked
  DNC in Bolna history are blocked (and so no backfilled call links to a live campaign and fires its rules).
- The Neon pooled URL is fine: the sync lock is a transaction-level advisory lock, and the open transaction pins the
  server connection.
- That lock connection holds an open transaction for the whole sync. Check `SHOW idle_in_transaction_session_timeout`
  on Neon; if it is set below the longest sync, the lock connection is killed and a second sync can start.
- `DEV_LOGIN` is not in `render.yaml` and must stay unset in production.
