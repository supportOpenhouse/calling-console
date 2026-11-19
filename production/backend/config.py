"""Runtime config loaded from environment variables (see `.env.example`).

JWT_SECRET and DATABASE_URL are required — the app refuses to start without
them (see backend/app.py).
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# Load backend/.env explicitly so secrets resolve regardless of the working
# directory; a root .env is honoured too but never overrides real env values.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))
load_dotenv()

# DB
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Auth
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24 * 7
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
# "1" enables POST /api/auth/dev (local development only).
DEV_LOGIN = os.environ.get("DEV_LOGIN", "")
ALLOWED_EMAIL_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "openhouse.in")

# CORS
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")

# Bolna
BOLNA_API_KEY = os.environ.get("BOLNA_API_KEY", "")
BOLNA_BASE_URL = os.environ.get("BOLNA_BASE_URL", "https://api.bolna.ai")

# S3-compatible recording storage
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_REGION = os.environ.get("S3_REGION", "auto")

# Direct_Inventory integration
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
DI_AI_CALLS_URL = os.environ.get("DI_AI_CALLS_URL", "")
DI_AI_CALLS_TOKEN = os.environ.get("DI_AI_CALLS_TOKEN", "")

# Interakt (WhatsApp)
INTERAKT_API_KEY = os.environ.get("INTERAKT_API_KEY", "")
INTERAKT_FROM = os.environ.get("INTERAKT_FROM", "")

# Misc
SYNC_ENABLED = os.environ.get("SYNC_ENABLED", "1")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
