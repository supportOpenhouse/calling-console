"""Run one sync from the command line, through the same advisory lock as the background thread.

Usage (from production/):
    backend/.venv/bin/python -m backend.scripts.run_sync [--full] [--max-pages N]

`--max-pages 0` pages every agent with no cap: the manual recovery for an agent reported as "capped at 40 pages
before reaching lookback". Prints the report as JSON. Exits 1 when the report has errors or another sync holds the
lock.
"""
from __future__ import annotations

import argparse
import json
import logging

from .. import config
from ..services import sync


def _pages(v: str) -> int:
    n = int(v)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 (no cap) or more")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one Calling Console sync.")
    ap.add_argument("--full", action="store_true",
                    help="also sync agents and keep paging every agent's executions past its lookback (backfill)")
    ap.add_argument("--max-pages", type=_pages, default=None,
                    help=f"pages per agent (default {sync.MAX_PAGES}); 0 = no cap")
    args = ap.parse_args(argv)
    report = sync.run_sync(full=args.full, max_pages=args.max_pages)
    if report is None:
        print(json.dumps({"error": "a sync is already running"}))
        return 1
    print(json.dumps(report, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO))
    raise SystemExit(main())
