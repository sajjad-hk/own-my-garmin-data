"""
sync_config helpers — the enabled-domain set lives in the DB (key
'enabled_domains'), reachable by both install.py and the GitHub Actions
runners via DATABASE_URL, so no new secret is needed to carry it. Only
depends on psycopg, matching the runner dependency ceiling documented in
domains.py.
"""
import json

import psycopg


def get_enabled_domains(conn: psycopg.Connection) -> set[str]:
    row = conn.execute("select value from sync_config where key = 'enabled_domains'").fetchone()
    if not row:
        return set()
    return set(row[0])


def set_enabled_domains(conn: psycopg.Connection, domains: set[str] | list[str]) -> None:
    conn.execute(
        """
        insert into sync_config (key, value, updated_at)
        values ('enabled_domains', %s, now())
            on conflict (key) do update set value = excluded.value, updated_at = now()
        """,
        (json.dumps(sorted(domains)),),
    )
    conn.commit()
