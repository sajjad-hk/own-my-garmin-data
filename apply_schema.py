# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.3"]
# ///
"""
Schema migration runner — replaces the old one-shot apply_schema.py that
executed schema/init.sql in full. Applies schema/migrations/*.sql files,
gated by each file's `-- domain: <key>` tag, and tracks what's applied in
schema_migrations. Also adopts a pre-migrations database (one that has
tables but no schema_migrations row) without re-running or double-creating
anything — see PRE_MIGRATIONS_DOMAINS below.

Usage:
    DATABASE_URL="postgresql://..." uv run apply_schema.py
"""
import json
import os
import pathlib
import re

import psycopg

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / "schema" / "migrations"

_DOMAIN_TAG_RE = re.compile(r"^--\s*domain:\s*(\S+)", re.MULTILINE)

# Domains whose tables were created unconditionally by the schema that
# existed before migrations were introduced (schema/init.sql, frozen —
# see its header comment). Used only to adopt a pre-migrations database:
# every domain here gets enabled, regardless of how much data ended up in
# its tables, because the old code pulled all of them on every run
# regardless of data sparsity (see the plan's "deliberate deviations"
# note for why this must not be inferred from data presence).
PRE_MIGRATIONS_DOMAINS = {"activities", "wellness", "body_composition", "training", "challenges", "profile"}

# The exact migration versions that reproduced schema/init.sql (the
# pre-migrations schema) when this adoption logic was written. This set
# must NEVER grow — a future migration for an already-adopted domain
# (e.g. new challenges-in-progress tables) must NOT be silently stamped
# as applied without running; it needs to actually execute so the new
# tables get created. Adding to this set defeats that on purpose.
PRE_MIGRATIONS_VERSIONS = {1, 2, 3, 4, 5, 6, 7}


def _migration_files() -> list[pathlib.Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _version_of(path: pathlib.Path) -> int:
    return int(path.name.split("_", 1)[0])


def _domain_of(path: pathlib.Path) -> str:
    match = _DOMAIN_TAG_RE.search(path.read_text())
    if not match:
        raise ValueError(f"{path} is missing a '-- domain: <key>' header line")
    return match.group(1)


def list_migrations() -> list[tuple[int, str, str]]:
    """Return (version, domain, filename) for every migration file, in
    ascending version order. Used by install.py's --upgrade to compute
    what's pending without duplicating the file-parsing logic."""
    return [(_version_of(p), _domain_of(p), p.name) for p in _migration_files()]


def applied_versions(url: str) -> set[int]:
    """Versions already recorded in schema_migrations. Creates the
    tracker table first if missing, same as apply_migrations does."""
    with psycopg.connect(url) as conn:
        _ensure_tracker_tables(conn)
        conn.commit()
        return {row[0] for row in conn.execute("select version from schema_migrations").fetchall()}


def _ensure_tracker_tables(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        create table if not exists schema_migrations (
            version    int primary key,
            name       text not null,
            applied_at timestamptz not null default now()
        )
        """
    )
    conn.execute(
        """
        create table if not exists sync_config (
            key        text primary key,
            value      jsonb not null,
            updated_at timestamptz default now()
        )
        """
    )


def _needs_adoption(conn: psycopg.Connection) -> bool:
    has_rows = conn.execute("select count(*) from schema_migrations").fetchone()[0] > 0
    if has_rows:
        return False
    exists = conn.execute(
        "select exists (select 1 from information_schema.tables where table_name = 'activities')"
    ).fetchone()[0]
    return bool(exists)


def _adopt_existing_db(conn: psycopg.Connection) -> set[str]:
    """Stamp every pre-migrations table's version as applied AND write the
    adopted enabled_domains to sync_config, both uncommitted on the
    caller's connection. Deliberately does not call config.set_enabled_domains
    (which commits internally) or commit here itself — the caller must
    commit both writes together in one transaction. If a crash split these
    into two commits, a resumed adoption run would see schema_migrations
    already populated (_needs_adoption() -> False) and skip adoption
    forever, leaving sync_config.enabled_domains empty and silently
    disabling every domain for that install."""
    adopted_domains = set(PRE_MIGRATIONS_DOMAINS)
    for version, _domain, name in list_migrations():
        if version not in PRE_MIGRATIONS_VERSIONS:
            continue  # a migration added after adoption logic was written — must actually run, not be stamped
        conn.execute(
            "insert into schema_migrations (version, name) values (%s, %s) on conflict (version) do nothing",
            (version, name),
        )
    conn.execute(
        """
        insert into sync_config (key, value, updated_at)
        values ('enabled_domains', %s, now())
            on conflict (key) do update set value = excluded.value, updated_at = now()
        """,
        (json.dumps(sorted(adopted_domains)),),
    )
    return adopted_domains


def apply_migrations(url: str, enabled_domains: set[str] | None = None) -> list[str]:
    """Apply every unapplied migration whose '-- domain:' tag is 'base', or
    is in `enabled_domains`. `enabled_domains=None` applies base only —
    the very first bootstrap step, before a user has chosen domains.
    Adopts a pre-migrations database before applying anything else, so an
    existing install's next sync behaves exactly as before. Returns the
    list of migration filenames actually applied this call (empty if
    nothing was pending)."""
    with psycopg.connect(url) as conn:
        _ensure_tracker_tables(conn)
        conn.commit()

        if _needs_adoption(conn):
            _adopt_existing_db(conn)
            conn.commit()

        applied_now = {row[0] for row in conn.execute("select version from schema_migrations").fetchall()}

        applied_names: list[str] = []
        for version, domain, name in list_migrations():
            if version in applied_now:
                continue
            if domain != "base" and (enabled_domains is None or domain not in enabled_domains):
                continue
            path = MIGRATIONS_DIR / name
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute(
                    "insert into schema_migrations (version, name) values (%s, %s)",
                    (version, name),
                )
            applied_names.append(name)

        conn.commit()
        return applied_names


def apply_schema(url: str) -> None:
    """Backwards-compatible entry point — applies base migrations only
    (auth_tokens, sync_config, schema_migrations). Anything doing a full
    schema setup should call apply_migrations(url, enabled_domains=...)
    once domains are chosen instead."""
    apply_migrations(url, enabled_domains=None)


def main() -> None:
    applied = apply_migrations(os.environ["DATABASE_URL"])
    print(f"Applied: {', '.join(applied)}" if applied else "No pending migrations.")


if __name__ == "__main__":
    main()
