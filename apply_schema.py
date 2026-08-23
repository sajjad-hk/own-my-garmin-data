# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.3"]
# ///
"""
Applies schema/init.sql to a Postgres database — replaces
`psql "$DATABASE_URL" -f schema/init.sql`, so no local psql install is needed.

Usage:
    DATABASE_URL="postgresql://..." uv run apply_schema.py
"""

import os
import pathlib

import psycopg

SCHEMA_FILE = pathlib.Path(__file__).resolve().parent / "schema" / "init.sql"


def apply_schema(database_url: str) -> None:
    sql = SCHEMA_FILE.read_text()
    with psycopg.connect(database_url) as conn:
        conn.execute(sql)
        conn.commit()


def main() -> None:
    apply_schema(os.environ["DATABASE_URL"])
    print(f"Schema applied from {SCHEMA_FILE}.")


if __name__ == "__main__":
    main()
