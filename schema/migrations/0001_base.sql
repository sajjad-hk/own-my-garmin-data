-- domain: base
-- Token storage: lets the ephemeral GitHub Actions runner resume a Garmin
-- session without ever storing a password.
create table if not exists auth_tokens (
    provider   text primary key,
    payload    jsonb not null,
    updated_at timestamptz not null default now()
);

-- Tracks which migrations have been applied. apply_schema.py also creates
-- this table directly (before checking for adoption), so this copy is a
-- harmless no-op on a fresh install — it only matters for an adopted DB,
-- where this file is stamped applied without being run.
create table if not exists schema_migrations (
    version    int primary key,
    name       text not null,
    applied_at timestamptz not null default now()
);

-- Enabled-domain set and any other small config, keyed by name. Reachable
-- by both install.py and the GitHub Actions runners via DATABASE_URL, so
-- no new secret is needed to carry it.
create table if not exists sync_config (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz default now()
);
