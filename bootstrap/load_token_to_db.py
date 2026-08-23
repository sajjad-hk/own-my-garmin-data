# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
# ]
# ///
"""
Run this ONCE, locally, right after generate_token.py.
Reads the token files from ~/.garminconnect and pushes them into the
auth_tokens table in Neon, so the GitHub Actions job can pick them up.

Usage:
    DATABASE_URL="postgresql://..." uv run bootstrap/load_token_to_db.py
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bootstrap.garmin_auth import TOKEN_DIR, save_token_to_db

import psycopg

DB_URL = os.environ["DATABASE_URL"]


def main() -> None:
    if not list(TOKEN_DIR.glob("*.json")):
        raise RuntimeError(f"No token files found in {TOKEN_DIR} — run generate_token.py first.")

    with psycopg.connect(DB_URL) as conn:
        conn.execute(
            """
            create table if not exists auth_tokens (
                provider   text primary key,
                payload    jsonb not null,
                updated_at timestamptz not null default now()
            )
            """
        )
        save_token_to_db(conn)

    print("Token pushed to Neon. Ingestion job (pull.py) can now run unattended.")


if __name__ == "__main__":
    main()
