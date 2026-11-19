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

# The 5-minute sync runs in-process as a daemon thread, one per gunicorn worker (a Postgres advisory lock elects a
# single syncer). It is started by gunicorn.conf.py post_fork, NOT here: Render runs gunicorn with --preload, so this
# module is imported in the master, and a thread started here would be alive at fork and deadlock the workers.
# Missing integrations still fail the deploy here. SYNC_ENABLED=0 turns the sync off.
from backend.services.sync import check_sync_config  # noqa: E402
check_sync_config()

__all__ = ["app"]
