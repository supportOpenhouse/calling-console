"""One-off backfill before the first production dial: pull every agent and its whole execution history, and mark
every number a call tagged as do-not-call.

Usage (from production/):
    backend/.venv/bin/python -m backend.scripts.backfill

Takes the sync lock (exit 1 if a sync is running), syncs the agents, pages every agent's executions with no cap
through the normal upsert path (which already blocks the phone of a finished DNC-tagged call: source "call_tag"),
then marks DNC (source "backfill", reason = the tag) every phone with a call whose lower-cased tag is in DNC_TAGS
that is not blocked yet (calls saved earlier). Safe to re-run: calls upsert by execution id and DNC marking is
insert-if-absent.
Exits 1 too when the executions step reported errors (the DNC marking still runs on what was saved).
"""
from __future__ import annotations

import logging

from .. import config
from ..db import get_conn
from ..services import datasets, sync
from ..services.classify import DNC_TAGS


def main() -> int:
    with sync.sync_lock() as got:
        if not got:
            print("a sync is already running; try again when it finishes")
            return 1
        conn = get_conn()
        try:
            report = sync.new_report()
            agents = sync.sync_agents(conn)
            sync.sync_executions(conn, report, full=True, max_pages=None)
            with conn.cursor() as cur:
                # the newest call per phone supplies the reason
                cur.execute("SELECT DISTINCT ON (phone) phone, tag FROM call "
                            "WHERE lower(tag) = ANY(%s) AND phone <> 'unknown' "
                            "ORDER BY phone, started_at DESC NULLS LAST", (DNC_TAGS,))
                dnc = cur.fetchall()
            for row in dnc:
                datasets.mark_dnc(conn, row["phone"], "backfill", reason=row["tag"])
            conn.commit()
        finally:
            conn.close()
    print(f"{agents} agents, {report['executions_seen']} executions seen, {report['calls_upserted']} calls upserted, "
          f"{len(dnc)} DNC phones marked")
    for e in report["errors"]:
        print(f"error: {e}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO))
    raise SystemExit(main())
