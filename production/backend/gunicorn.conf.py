"""gunicorn reads this file from its working directory (production/backend on Render).

Render runs gunicorn with --preload (GUNICORN_CMD_ARGS), so wsgi.py is imported once in the master and the workers are
forked from it. The sync thread must start after that fork, in each worker: a thread (with its DB sockets and held
locks) alive in the master at fork time deadlocked a worker in production -- every request it accepted hung.
"""
import os
import sys

# post_fork runs before the app is imported when --preload is off: make the `backend` package importable (as wsgi.py does).
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


def post_fork(server, worker):
    from backend.services.sync import start_background_sync
    start_background_sync()
