# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
# ]
# ///
"""
Run this ONCE, locally, to backfill history that pull.py's incremental
lookback window doesn't cover. Safe to re-run — each domain's backfill
routine (see domains.py) skips dates/records already populated, so an
interrupted run can just be restarted.

Usage:
    DATABASE_URL="postgresql://..." uv run backfill.py

Optional env vars:
    BACKFILL_START_DATE   YYYY-MM-DD. Defaults to 2 years ago.
                          Activities, weigh-ins, and body battery always
                          backfill fully regardless of this (Garmin returns
                          those as ranges, it's cheap). This setting only
                          controls how far back the per-day loop goes —
                          ~20 API calls per day now (wellness + training
                          insight, not just stats/sleep/stress/hrv/VO2max),
                          so 2 years means ~14,600 requests, assuming all
                          domains are enabled — fewer for a domain-scoped
                          run (see BACKFILL_DOMAINS below). Expect this to
                          take a few hours; it's safe to stop and resume.
    BACKFILL_DOMAINS     Comma-separated domain keys (see domains.py). Blank
                          or unset backfills every enabled domain that has a
                          backfill routine. Restricted to the intersection
                          with sync_config.enabled_domains — a domain not
                          enabled for sync is never backfilled even if named
                          here.
"""

import os
from datetime import date, timedelta

import psycopg
from garminconnect import Garmin

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db, save_token_to_db
from config import get_enabled_domains
from domains import DOMAINS, BackfillContext

DB_URL = os.environ["DATABASE_URL"]

DEFAULT_START = (date.today() - timedelta(days=365 * 2)).isoformat()
# `or` (not .get(key, default)) so an empty-string env var — e.g. a blank
# workflow_dispatch input from backfill.yml — also falls through to the
# default, instead of hitting date.fromisoformat("").
START_DATE = os.environ.get("BACKFILL_START_DATE") or DEFAULT_START


def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        enabled = get_enabled_domains(conn)
        # Blank/unset BACKFILL_DOMAINS falls through to "all enabled" — same
        # pattern as START_DATE above (`or ""`, not `.get(key, default)`),
        # so a blank workflow_dispatch input doesn't need special-casing.
        requested = os.environ.get("BACKFILL_DOMAINS") or ""
        requested_keys = {k.strip() for k in requested.split(",") if k.strip()}
        target_keys = (requested_keys & enabled) if requested_keys else enabled

        start = date.fromisoformat(START_DATE)
        today = date.today()

        domains_to_run = [d for d in DOMAINS if d.key in target_keys and d.backfill is not None]
        if not domains_to_run:
            print("No domains selected for backfill (none enabled, or none have a backfill routine).")
            return

        # Only touch the token/network once we know there's actual work to do —
        # load_token_from_db/login can refresh the on-disk token, and skipping
        # save_token_to_db below on a no-op run would silently discard that
        # refresh (load_token_from_db overwrites TOKEN_DIR from the DB on every
        # run, including the next one).
        load_token_from_db(conn)

        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))

        ctx = BackfillContext(client=client, conn=conn, start_date=start, today=today)

        for domain in domains_to_run:
            print(f"=== Backfilling domain: {domain.key} ===")
            domain.backfill(ctx)

        save_token_to_db(conn)
        print("Backfill complete.")


if __name__ == "__main__":
    main()
