"""Flask entry point for the Openhouse Calling Console API.

Local dev: `python -m backend.app` from `production/` (port 5060 by default).
Production: gunicorn 'backend.wsgi:app' --bind 0.0.0.0:$PORT
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal

from flask import Flask, jsonify
from flask.json.provider import DefaultJSONProvider
from flask_compress import Compress
from flask_cors import CORS
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from . import config
from .db import get_conn
from .services.bolna import BolnaError
from .services.errors import ApiError

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO))
log = logging.getLogger(__name__)


class _JSONProvider(DefaultJSONProvider):
    """Emit NUMERIC (Decimal) as JSON numbers and datetimes as ISO-8601."""

    @staticmethod
    def default(o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        if hasattr(o, "isoformat") and callable(o.isoformat) and not isinstance(o, str):
            return o.isoformat()
        return DefaultJSONProvider.default(o)


def _blueprints() -> list:
    """Blueprints to register. Later tasks append to this list."""
    from .api import (agents, auth, calls, campaigns, datasets, departments, handoffs, ingest, knowledgebases, schedule,
                      settings, sync, use_cases, users, workflows)
    return [auth.bp, departments.bp, use_cases.bp, datasets.bp, ingest.bp, workflows.bp, schedule.bp, sync.bp, campaigns.bp, calls.bp, handoffs.bp,
            users.bp, agents.bp, knowledgebases.bp, settings.bp]


def create_app() -> Flask:
    if not config.JWT_SECRET:
        raise RuntimeError("JWT_SECRET is not set. Set it in the environment (backend/.env).")
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Set it in the environment (backend/.env).")

    app = Flask(__name__)
    app.json = _JSONProvider(app)
    CORS(app, origins=[config.FRONTEND_ORIGIN], supports_credentials=False)
    Compress(app)
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # bounds every upload held in memory

    @app.get("/api/health")
    def health():
        out = {"status": "ok", "db": "unknown"}
        try:
            c = get_conn()
            try:
                with c, c.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                c.close()
            out["db"] = "connected"
        except Exception as e:
            out["db"] = f"error: {e}"
        return jsonify(out)

    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        return jsonify({"error": e.message}), e.status

    @app.errorhandler(BolnaError)
    def _bolna_error(e: BolnaError):
        log.exception("Bolna call failed")
        return jsonify({"error": f"Bolna {e.status}: {str(e.body)[:200]}"}), 502

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(e):
        return jsonify({"error": "file too large (max 20 MB)"}), 413

    @app.errorhandler(HTTPException)
    def _http_error(e: HTTPException):
        return jsonify({"error": e.description}), e.code

    for bp in _blueprints():
        app.register_blueprint(bp)
    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5060))
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    app.run(host="0.0.0.0", port=port, debug=debug)
