"""WSGI entry point for gunicorn.

Adds the parent folder to sys.path so the `backend` package (which uses
relative imports) is importable when the service runs from `backend/`.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from backend.app import app  # noqa: E402

# The 5-minute sync runs in-process as a daemon thread (one per gunicorn worker); a Postgres
# advisory lock elects a single syncer. Started here (not in create_app) so it only runs under
# gunicorn — never during tests/CLI that import the app. SYNC_ENABLED=0 turns it off.
from backend.services.sync import start_background_sync  # noqa: E402
start_background_sync()

__all__ = ["app"]
