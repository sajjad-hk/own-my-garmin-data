# Selectable Data Domains + Gradual Upgrade Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each user pick which Garmin data domains (activities, wellness, training, challenges, profile, body composition, later reproductive health) get pulled, store that choice in the DB, replace the monolithic schema with domain-tagged migrations, and give existing installs an `install.py --upgrade` path to adopt migrations and add domains gradually.

**Architecture:** A single `domains.py` registry (dataclass per domain: key, category, label, description, default_enabled, `sync_incremental(ctx)`, `backfill(ctx) | None`, tables) becomes the one source of truth. `pull.py`/`backfill.py` loop over the enabled subset read from a new `sync_config` table instead of a hardcoded call sequence. `schema/init.sql` is replaced by `schema/migrations/*.sql`, each tagged `-- domain: <key>`, applied by a rewritten `apply_schema.py` that also adopts pre-migrations databases. `install.py` gains a domain checklist at first-time setup and a new `--upgrade` command.

**Tech Stack:** Python 3.11+, psycopg3, garminconnect, questionary + rich (install.py only), Postgres (Neon). No new dependencies.

**Spec:** The full original task prompt is reproduced in `docs/superpowers/plans/2026-08-24-garmin-domains-upgrade.spec.md` — read it alongside this plan; this plan is the source of truth for exact file contents where the two differ (two deliberate deviations are called out below).

## Global Constraints

- **Read-only against Garmin.** Never call `set_*`/`add_*`/`delete_*`/`upload_*`/`schedule_*`.
- **No redundant columns; every new column verified live before it's written into schema.** Applies to Part-1-gated work only (see "Out of scope" below) — nothing in this plan's tasks adds unverified columns.
- **`pull.py` and `backfill.py` share upsert logic via one importable module (`upserts.py`), not copy-pasted bodies.**
- **Idempotent and resumable everywhere.** Every migration uses `create table if not exists` / `add column if not exists`; every backfill routine has its own skip-detection.
- **Do not break existing installs.** Adoption of a pre-migrations DB must not re-run or double-create anything, and must leave every domain the old code pulled unconditionally still enabled.
- **Per-person model stays intact.** Nothing added here shares data across accounts; `sync_config`/`schema_migrations` live in each person's own Neon DB, reached the same way `DATABASE_URL` already is.
- **Runner dependency ceiling.** `domains.py`, `upserts.py`, and `config.py` are imported by `ingestion/pull.py`, which runs on a bare GitHub Actions runner with only `ingestion/requirements.txt` installed (`garminconnect`, `psycopg[binary]`, `python-dotenv`). These three modules must never import `rich`, `questionary`, `requests`, or `workflow_tools` at module scope. `backfill.py` (root, uv inline-deps) already matches this same ceiling (`garminconnect`, `psycopg[binary]`) — keep it that way.

## Two deliberate deviations from the original spec (read before Task 3)

1. **`schema/migrations/` is seven files, not one `0001_baseline.sql`.** The spec's literal text says the baseline is one verbatim copy of `init.sql`. But `install.py`'s domain checklist (5a) requires that *only the chosen domains'* schema gets created on a fresh install — impossible if one migration file bundles every domain's tables under a single `-- domain:` tag. So the current schema is split by domain into `0001_base.sql` through `0007_profile.sql` (below), each tagged with its own domain, collectively reproducing today's `init.sql` byte-for-byte when all seven are applied. `schema/init.sql` itself is kept, frozen and unused by any code path, purely as the historical fixture the smoke test uses to simulate a pre-migrations database (see Task 3, Task 11).

2. **Adoption of an existing DB infers domains from schema presence, not data presence.** The spec's example ("`weigh_in` ever non-null → `body_composition` on") would turn a domain *off* for a user whose backfill never ran or who has no smart scale — `docs/garmin_api_coverage.md` already documents that this repo's own weigh-ins backfill "was added in `52a3ab5` but never actually re-run against production" on the account that wrote that doc. Since the pre-migrations schema created every table unconditionally regardless of what data ended up in it, the only rule that satisfies "an existing user's very next sync behaves exactly as before" is: **a pre-migrations DB gets all six baseline domains enabled, unconditionally.** (`activities`, `wellness`, `body_composition`, `training`, `challenges`, `profile` — `reproductive` didn't exist pre-migrations, so it's correctly left off.)

## Out of scope for this plan (gated on a human running the probe script)

Part 1 of the original task ("probe the genuinely-new endpoints") requires a human to run `scripts/probe_new_endpoints.py` against a real Garmin login (and, for the reproductive endpoints, an account that actually tracks that data) and paste back real JSON. That can't happen inside this plan. Task 1 below ships the probe script itself — zero schema risk, needed regardless of what the probe finds. Everything downstream of the probe (the `challenges` domain's three new endpoints, and the new `reproductive` domain) is **not** in this plan's task list. Once probe output exists, a follow-up plan adds `schema/migrations/0008_challenges_inprogress.sql` and `0009_reproductive.sql` plus their `domains.py` wiring — the registry and migration-runner design here was built specifically so that follow-up slots in without touching anything built in this plan (new migration files, one new `Domain` entry, no changes to `pull.py`/`backfill.py`/`install.py`'s control flow).

Acceptance criterion 4 from the original spec ("intra-domain addition" — new tables land inside an already-enabled `challenges` domain) can't be exercised until that follow-up exists either; Task 11's smoke test marks it explicitly skipped rather than faking it.

---

### Task 1: Probe script for not-yet-wired endpoints

**Files:**
- Create: `scripts/probe_new_endpoints.py`

**Interfaces:**
- Produces: a standalone script, run manually by a human against a real Garmin login. No other task imports it.

- [ ] **Step 1: Write the probe script**

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
#     "rich>=13.0",
# ]
# ///
"""
Throwaway probe script — prints real JSON shapes for Garmin endpoints not
yet wired into schema, so a human can paste the output back before any
schema gets written for them. Read-only against Garmin; never writes to
Postgres (only reads the stored token to log in).

Run this against the account each group needs:
  - "challenges": the primary account (any account with in-progress
    badges/challenges works).
  - "reproductive": an account that actually tracks menstrual/pregnancy
    data — NOT the primary account unless it happens to track it.

Usage:
    DATABASE_URL="postgresql://..." uv run scripts/probe_new_endpoints.py --group challenges
    DATABASE_URL="postgresql://..." uv run scripts/probe_new_endpoints.py --group reproductive
    DATABASE_URL="postgresql://..." uv run scripts/probe_new_endpoints.py --group reproductive \\
        --start 2026-06-01 --end 2026-08-24 --date 2026-08-01
"""
import argparse
import json
import os
import pathlib
import sys
from datetime import date, timedelta

import psycopg
from dotenv import load_dotenv
from garminconnect import Garmin
from rich.console import Console
from rich.panel import Panel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db

load_dotenv()

console = Console()


def _print_result(label: str, method: str, value) -> None:
    console.print(Panel.fit(f"[bold]{method}[/bold]", title=label, border_style="cyan"))
    try:
        console.print_json(json.dumps(value, default=str))
    except Exception as e:
        console.print(f"[red]Couldn't serialize result: {e}[/red]")
        console.print(repr(value))
    if isinstance(value, list):
        console.print(f"[dim]-> {len(value)} item(s)[/dim]\n")
    else:
        console.print()


def probe_challenges(client: Garmin) -> None:
    console.print(Panel.fit(
        "Checking whether these are genuinely new lists, or filtered slices "
        "of get_available_badge_challenges / get_available_badges / "
        "get_earned_badges (already stored in `challenges` / "
        "`available_badges` / `earned_badges`). Compare the ids/uuids in "
        "each result against what's already in those tables before wiring "
        "any of these into schema.",
        title="challenges group", border_style="yellow",
    ))
    _print_result("In-progress badges", "get_in_progress_badges", client.get_in_progress_badges())
    _print_result(
        "In-progress virtual challenges", "get_inprogress_virtual_challenges",
        client.get_inprogress_virtual_challenges(),
    )
    _print_result(
        "Non-completed badge challenges", "get_non_completed_badge_challenges",
        client.get_non_completed_badge_challenges(),
    )
    # For comparison — what's already stored, to check for overlap.
    _print_result("(reference) available badge challenges", "get_available_badge_challenges(1, 100)",
                  client.get_available_badge_challenges(1, 100))
    _print_result("(reference) available badges", "get_available_badges", client.get_available_badges())
    _print_result("(reference) earned badges", "get_earned_badges", client.get_earned_badges())


def probe_reproductive(client: Garmin, start: str, end: str, single_date: str) -> None:
    console.print(Panel.fit(
        "Run this against an account that actually tracks menstrual/pregnancy "
        "data. Check: does the calendar range endpoint already carry "
        "per-date entries (making get_menstrual_data_for_date redundant, "
        "like get_daily_steps vs get_steps_data)? What's the per-date "
        "natural key? Is pregnancy a single snapshot with no natural list "
        "key (store like performance_snapshots)?",
        title="reproductive group", border_style="yellow",
    ))
    _print_result(
        "Menstrual calendar (range)", f"get_menstrual_calendar_data({start!r}, {end!r})",
        client.get_menstrual_calendar_data(start, end),
    )
    _print_result(
        "Menstrual data for one date", f"get_menstrual_data_for_date({single_date!r})",
        client.get_menstrual_data_for_date(single_date),
    )
    _print_result("Pregnancy summary", "get_pregnancy_summary", client.get_pregnancy_summary())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["challenges", "reproductive", "all"], default="all")
    today = date.today()
    parser.add_argument("--start", default=(today - timedelta(days=60)).isoformat())
    parser.add_argument("--end", default=today.isoformat())
    parser.add_argument("--date", default=today.isoformat())
    args = parser.parse_args()

    db_url = os.environ["DATABASE_URL"]
    with psycopg.connect(db_url) as conn:
        load_token_from_db(conn)

    client = Garmin()
    client.login(tokenstore=str(TOKEN_DIR))

    if args.group in ("challenges", "all"):
        probe_challenges(client)
    if args.group in ("reproductive", "all"):
        probe_reproductive(client, args.start, args.end, args.date)

    console.print(Panel.fit(
        "Paste the output above (or a summary of the shapes + which "
        "duplicates you found) back to continue Part 1 of the domains "
        "plan.", border_style="green",
    ))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/probe_new_endpoints.py
git commit -m "feat: add throwaway probe script for new challenge/reproductive endpoints"
```

---

### Task 2: Extract shared upsert helpers into `upserts.py`

**Files:**
- Create: `upserts.py` (repo root)
- Modify: `ingestion/pull.py` (remove `upsert_activities`, `DAILY_METRICS_COLUMNS`, `upsert_daily_metrics`, `upsert_training_insight`, `upsert_body_battery`, `upsert_weigh_ins`; import from `upserts` instead)
- Modify: `backfill.py` (same removals; import from `upserts` instead)

**Interfaces:**
- Produces: `upserts.upsert_activities(conn, activities)`, `upserts.upsert_daily_metrics(conn, d, fields)`, `upserts.upsert_training_insight(conn, d, fields)`, `upserts.upsert_body_battery(conn, days)`, `upserts.upsert_weigh_ins(conn, weigh_in_response)`, `upserts.DAILY_METRICS_COLUMNS`, `upserts.TRAINING_INSIGHT_COLUMNS` — all used by Task 4's `domains.py`.
- No behavior change: SQL text and the `coalesce(excluded.x, daily_metrics.x)` semantics stay byte-identical to today's `ingestion/pull.py`.

- [ ] **Step 1: Create `upserts.py`**

```python
"""
Upsert helpers shared by ingestion/pull.py (incremental) and backfill.py
(full-history) — kept in one place so the two entry points can't drift
apart on SQL. Only depends on psycopg, so it's safe to import from the
bare GitHub Actions runner (see ingestion/requirements.txt) and from
domains.py, which both pull.py and backfill.py load.
"""
import json
from datetime import date

import psycopg

DAILY_METRICS_COLUMNS = [
    "stats", "sleep", "stress", "hrv", "max_metrics",
    "respiration", "spo2", "body_battery", "body_battery_events",
    "intensity_minutes", "floors", "steps_intraday", "heart_rates",
    "day_events", "weigh_in",
]

TRAINING_INSIGHT_COLUMNS = [
    "training_status", "training_readiness", "morning_training_readiness",
    "endurance_score", "hill_score", "fitness_age", "running_tolerance",
]


def upsert_activities(conn: psycopg.Connection, activities: list[dict]) -> None:
    for a in activities:
        conn.execute(
            """
            insert into activities (activity_id, raw, started_at)
            values (%s, %s, %s)
                on conflict (activity_id) do update set raw = excluded.raw
            """,
            (a["activityId"], json.dumps(a), a.get("startTimeLocal")),
        )
    conn.commit()


def upsert_daily_metrics(conn: psycopg.Connection, d: date, fields: dict) -> None:
    # coalesce(excluded.x, daily_metrics.x): a column not passed this call
    # (fields.get(...) is None) keeps whatever was already stored, rather
    # than nulling it out. Every call site must pass the *same* set of keys
    # for this to stay predictable.
    cols = DAILY_METRICS_COLUMNS
    values = [json.dumps(fields.get(c)) if fields.get(c) is not None else None for c in cols]
    set_clause = ", ".join(f"{c} = coalesce(excluded.{c}, daily_metrics.{c})" for c in cols)
    conn.execute(
        f"""
        insert into daily_metrics (metric_date, {", ".join(cols)}, updated_at)
        values (%s, {", ".join(["%s"] * len(cols))}, now())
            on conflict (metric_date) do update set
            {set_clause},
            updated_at = now()
        """,  # noqa: S608 (fixed column names, not user input)
        (d, *values),
    )
    conn.commit()


def upsert_training_insight(conn: psycopg.Connection, d: date, fields: dict) -> None:
    cols = TRAINING_INSIGHT_COLUMNS
    values = [json.dumps(fields.get(c)) for c in cols]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in cols)
    conn.execute(
        f"""
        insert into training_insight (metric_date, {", ".join(cols)}, updated_at)
        values (%s, {", ".join(["%s"] * len(cols))}, now())
            on conflict (metric_date) do update set
            {set_clause},
            updated_at = now()
        """,  # noqa: S608 (fixed column names, not user input)
        (d, *values),
    )
    conn.commit()


def upsert_body_battery(conn: psycopg.Connection, days: list[dict]) -> None:
    for entry in days:
        upsert_daily_metrics(conn, date.fromisoformat(entry["date"]), {"body_battery": entry})


def upsert_weigh_ins(conn: psycopg.Connection, weigh_in_response: dict) -> None:
    for summary in weigh_in_response.get("dailyWeightSummaries", []):
        d = date.fromisoformat(summary["summaryDate"])
        upsert_daily_metrics(conn, d, {"weigh_in": summary})
```

- [ ] **Step 2: Update `ingestion/pull.py` to import from `upserts`**

Remove lines 124-150 and 225-233 (the `DAILY_METRICS_COLUMNS`, `upsert_daily_metrics`, `upsert_body_battery`, `upsert_weigh_ins` definitions) and lines 72-82, 153-170 (`upsert_activities`, `upsert_training_insight`). Add near the top (after the existing `bootstrap.garmin_auth` import):

```python
from upserts import upsert_activities, upsert_body_battery, upsert_daily_metrics, upsert_training_insight, upsert_weigh_ins
```

Everything else in `pull.py` (the calls to these functions) stays unchanged for this task — Task 5 rewrites the control flow.

- [ ] **Step 3: Update `backfill.py` to import from `upserts`**

Remove lines 52-62 (`upsert_activities`), 82-107 (`DAILY_METRICS_COLUMNS`, `upsert_daily_metrics`), 110-128 (`upsert_training_insight`), 131-139 (`upsert_body_battery`, `upsert_weigh_ins`). Add near the top:

```python
from upserts import upsert_activities, upsert_body_battery, upsert_daily_metrics, upsert_training_insight, upsert_weigh_ins
```

- [ ] **Step 4: Sanity-check both files still parse and the removed names are gone**

```bash
python -c "import ast; ast.parse(open('ingestion/pull.py').read())"
python -c "import ast; ast.parse(open('backfill.py').read())"
grep -n "^def upsert_" ingestion/pull.py backfill.py
```

Expected: no output from the `grep` (all upsert defs now live only in `upserts.py`).

- [ ] **Step 5: Commit**

```bash
git add upserts.py ingestion/pull.py backfill.py
git commit -m "refactor: extract shared upsert helpers into upserts.py"
```

---

### Task 3: Migrations infrastructure — `schema/migrations/`, `sync_config`, rewritten `apply_schema.py`

**Files:**
- Create: `schema/migrations/0001_base.sql`
- Create: `schema/migrations/0002_activities.sql`
- Create: `schema/migrations/0003_wellness.sql`
- Create: `schema/migrations/0004_body_composition.sql`
- Create: `schema/migrations/0005_training.sql`
- Create: `schema/migrations/0006_challenges.sql`
- Create: `schema/migrations/0007_profile.sql`
- Create: `config.py` (repo root)
- Modify: `apply_schema.py` (full rewrite)
- Modify: `schema/init.sql` (add a header comment marking it frozen/historical; content otherwise untouched)

**Interfaces:**
- Produces: `apply_schema.apply_migrations(url, enabled_domains=None) -> list[str]`, `apply_schema.apply_schema(url) -> None`, `apply_schema.list_migrations() -> list[tuple[int, str, str]]`, `apply_schema.applied_versions(url) -> set[int]`; `config.get_enabled_domains(conn) -> set[str]`, `config.set_enabled_domains(conn, domains: set[str] | list[str]) -> None`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Mark `schema/init.sql` as frozen**

Add at the very top of `schema/init.sql`:

```sql
-- FROZEN. This file is no longer applied by any code path — apply_schema.py
-- now runs schema/migrations/*.sql instead. Kept verbatim (content below
-- unchanged) as the fixture scripts/smoke_test.py uses to simulate a
-- pre-migrations database for adoption testing. Do not edit the SQL below;
-- add new tables/columns as a new schema/migrations/NNNN_<domain>.sql file.
--
```

- [ ] **Step 2: Write the seven migration files**

`schema/migrations/0001_base.sql`:

```sql
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
```

`schema/migrations/0002_activities.sql`:

```sql
-- domain: activities
-- Raw activity payloads, keyed by Garmin's own activity id.
create table if not exists activities (
    activity_id bigint primary key,
    raw         jsonb not null,
    started_at  timestamptz
);
```

`schema/migrations/0003_wellness.sql`:

```sql
-- domain: wellness
-- One row per calendar date, holding whichever daily wellness endpoints
-- were pulled. Columns are nullable because backfill/incremental runs may
-- populate them at different times. weigh_in is owned by the
-- body_composition domain (see 0004_body_composition.sql) — this file
-- defensively creates the table with just metric_date/updated_at in case
-- body_composition is enabled without wellness.
create table if not exists daily_metrics (
    metric_date date primary key,
    updated_at  timestamptz not null default now()
);

alter table daily_metrics add column if not exists stats jsonb;
alter table daily_metrics add column if not exists sleep jsonb;
alter table daily_metrics add column if not exists stress jsonb;
alter table daily_metrics add column if not exists hrv jsonb;
alter table daily_metrics add column if not exists max_metrics jsonb;
alter table daily_metrics add column if not exists respiration jsonb;
alter table daily_metrics add column if not exists spo2 jsonb;
alter table daily_metrics add column if not exists body_battery jsonb;
alter table daily_metrics add column if not exists body_battery_events jsonb;
alter table daily_metrics add column if not exists intensity_minutes jsonb;
alter table daily_metrics add column if not exists floors jsonb;
alter table daily_metrics add column if not exists steps_intraday jsonb;
alter table daily_metrics add column if not exists heart_rates jsonb;
alter table daily_metrics add column if not exists day_events jsonb;
```

`schema/migrations/0004_body_composition.sql`:

```sql
-- domain: body_composition
-- weigh_in is nullable and stays null forever on days with no scale
-- reading — that's expected, not a sign the row wasn't processed. Table
-- creation is defensive here too, in case body_composition is enabled
-- without wellness.
create table if not exists daily_metrics (
    metric_date date primary key,
    updated_at  timestamptz not null default now()
);

alter table daily_metrics add column if not exists weigh_in jsonb;
```

`schema/migrations/0005_training.sql`:

```sql
-- domain: training
-- Garmin's derived coaching/performance metrics, one row per calendar
-- date. running_tolerance is fetched as a single-day window so it stores
-- an empty array on days/accounts with no data, same as the others.
create table if not exists training_insight (
    metric_date                 date primary key,
    training_status             jsonb,
    training_readiness          jsonb,
    morning_training_readiness  jsonb,
    endurance_score             jsonb,
    hill_score                  jsonb,
    fitness_age                 jsonb,
    running_tolerance           jsonb,
    updated_at                  timestamptz not null default now()
);

-- Single-object "current state" endpoints with no natural list/id (race
-- predictions, cycling FTP, lactate threshold) — one row per metric,
-- refreshed in full on every sync.
create table if not exists performance_snapshots (
    metric_name text primary key,
    raw         jsonb not null,
    updated_at  timestamptz not null default now()
);

-- Personal records. Garmin keys these by a numeric "id" field (verified
-- against a live response).
create table if not exists personal_records (
    record_id  bigint primary key,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);
```

`schema/migrations/0006_challenges.sql`:

```sql
-- domain: challenges
-- Raw challenge payloads, refreshed on every sync. Garmin keys challenges
-- by a string uuid, not a numeric id.
create table if not exists challenges (
    challenge_id text primary key,
    raw          jsonb not null,
    updated_at   timestamptz not null default now()
);

-- Badges the account has already earned.
create table if not exists earned_badges (
    badge_id   bigint primary key,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);

-- Full badge catalog — includes badges not yet earned, each carrying
-- badgeProgressValue / badgeTargetValue.
create table if not exists available_badges (
    badge_id   bigint primary key,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);
```

`schema/migrations/0007_profile.sql`:

```sql
-- domain: profile
-- Account-level profile/settings — essentially static, changes rarely.
-- Singleton row, refreshed in full every run. get_user_profile and
-- get_userprofile_settings return meaningfully different things (verified
-- live): profile carries physiological baseline data under a `userData`
-- key; settings is purely locale/unit/display preferences.
create table if not exists user_profile (
    id         int primary key default 1,
    profile    jsonb not null,
    settings   jsonb not null,
    updated_at timestamptz not null default now(),
    constraint user_profile_singleton check (id = 1)
);

-- Goals. NOTE: no verified natural id field exists (the account this was
-- built against has zero goals in any status). Refreshed by
-- delete-then-insert per status instead of upsert-by-id. If you later see
-- real goal data with a stable id field, switch this to an upsert like
-- personal_records.
create table if not exists goals (
    id         bigserial primary key,
    status     text not null,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);
```

- [ ] **Step 3: Write `config.py`**

```python
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
```

- [ ] **Step 4: Rewrite `apply_schema.py`**

```python
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
import os
import pathlib
import re

import psycopg

from config import set_enabled_domains

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
    for version, domain, name in list_migrations():
        if domain != "base" and domain not in PRE_MIGRATIONS_DOMAINS:
            continue  # e.g. reproductive — did not exist pre-migrations
        conn.execute(
            "insert into schema_migrations (version, name) values (%s, %s) on conflict (version) do nothing",
            (version, name),
        )
    return set(PRE_MIGRATIONS_DOMAINS)


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
            adopted_domains = _adopt_existing_db(conn)
            conn.commit()
            set_enabled_domains(conn, adopted_domains)

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
```

- [ ] **Step 5: Verify against a scratch DB**

This needs a real (or scratch) Postgres URL. If a `SMOKE_TEST_DATABASE_URL` scratch database is available, run:

```bash
SMOKE_TEST_DATABASE_URL="postgresql://...scratch..." python3 -c "
from apply_schema import apply_migrations, list_migrations
import psycopg
url = __import__('os').environ['SMOKE_TEST_DATABASE_URL']
with psycopg.connect(url) as c:
    c.execute('drop schema public cascade; create schema public;'); c.commit()
applied = apply_migrations(url, enabled_domains={'activities'})
print('applied:', applied)
with psycopg.connect(url) as c:
    exists = c.execute(\"select exists (select 1 from information_schema.tables where table_name='activities')\").fetchone()[0]
    not_exists = c.execute(\"select exists (select 1 from information_schema.tables where table_name='challenges')\").fetchone()[0]
    assert exists and not not_exists, 'domain gating failed'
print('OK: only activities domain schema created')
"
```

Expected: `applied: ['0001_base.sql', '0002_activities.sql']` and `OK: only activities domain schema created`. If no scratch DB is available yet, defer this verification to Task 11's smoke test and note it in the commit message.

- [ ] **Step 6: Commit**

```bash
git add schema/migrations schema/init.sql config.py apply_schema.py
git commit -m "feat: replace monolithic schema with domain-tagged migrations + adoption"
```

---

### Task 4: `domains.py` registry (existing six domains)

**Files:**
- Create: `domains.py` (repo root)

**Interfaces:**
- Consumes: `upserts.upsert_activities/upsert_daily_metrics/upsert_training_insight/upsert_body_battery/upsert_weigh_ins` (Task 2); `bootstrap.garmin_auth.save_token_to_db` (existing).
- Produces: `DOMAINS: list[Domain]`, `DOMAINS_BY_KEY: dict[str, Domain]`, `SyncContext`, `BackfillContext`, `Domain` dataclass with fields `key, category, label, description, default_enabled, sync_incremental: Callable[[SyncContext], dict[str, int]], backfill: Callable[[BackfillContext], None] | None, tables: tuple[str, ...]`. `sync_incremental` return value: `dict[str, int]` keyed by **table name** (not arbitrary labels) — Task 5 sums these across domains sharing a table.

- [ ] **Step 1: Write `domains.py`**

```python
"""
Single source of truth for which Garmin data domains this project knows
about. Imported by ingestion/pull.py on the bare GitHub Actions runner
(see ingestion/requirements.txt: garminconnect, psycopg[binary],
python-dotenv only) and by backfill.py (own uv inline deps: garminconnect,
psycopg[binary]) — so this module and everything it imports at module
scope must stay within that dependency set. No rich, no questionary, no
requests, no workflow_tools.

Each Domain declares:
  - key: machine name, used in sync_config.enabled_domains and the
    `-- domain:` tag in schema/migrations/*.sql.
  - category: checklist grouping header (install.py).
  - label / description: human text for the checklist.
  - default_enabled: pre-checked in the checklist.
  - sync_incremental(ctx: SyncContext) -> dict[str, int]: pulls this
    domain's data for the incremental lookback window and upserts it.
    Returns counts keyed by table name, e.g. {"activities": 12} — callers
    sum these across domains that share a table (wellness and
    body_composition both touch daily_metrics).
  - backfill(ctx: BackfillContext) -> None, or None for snapshot-only
    domains (challenges, profile) — the next incremental sync populates
    those; there's no history to backfill.
  - tables: table names this domain owns, for docs / the upgrade "what's
    new" summary. Not used to run schema (schema/migrations/*.sql is the
    source of truth for that).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import psycopg
from garminconnect import Garmin, GarminConnectConnectionError

import upserts
from bootstrap.garmin_auth import save_token_to_db

ACTIVITY_HISTORY_START = "2000-01-01"

# Chunk size for range endpoints during backfill — see backfill_range_endpoint.
RANGE_CHUNK_DAYS = 90
BODY_BATTERY_CHUNK_DAYS = 28


@dataclass
class SyncContext:
    client: Garmin
    conn: psycopg.Connection
    window_start: date
    today: date


@dataclass
class BackfillContext:
    client: Garmin
    conn: psycopg.Connection
    start_date: date
    today: date


@dataclass(frozen=True)
class Domain:
    key: str
    category: str
    label: str
    description: str
    default_enabled: bool
    sync_incremental: Callable[[SyncContext], dict[str, int]]
    backfill: Callable[[BackfillContext], None] | None
    tables: tuple[str, ...]


def backfill_range_endpoint(
    client, start: date, today: date, label: str, fetch_and_upsert, chunk_days: int = RANGE_CHUNK_DAYS
) -> None:
    # chunk_days is a guess at each backend's undocumented server-side max
    # range. Rather than guess a new constant when it's wrong, shrink on
    # the actual "requested date range is too big" 400 and keep the
    # smaller size for subsequent chunks.
    print(f"Backfilling {label} from {start} to {today}...")
    chunk_start = start
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), today)
        while True:
            try:
                fetch_and_upsert(client, chunk_start.isoformat(), chunk_end.isoformat())
                break
            except GarminConnectConnectionError as e:
                if "too big" not in str(e).lower() or chunk_end == chunk_start:
                    raise
                chunk_days = max(1, (chunk_end - chunk_start).days // 2)
                chunk_end = chunk_start + timedelta(days=chunk_days - 1)
                print(f"  ...{label} range too big, shrinking chunk to {chunk_days} days and retrying")
                time.sleep(1)
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(1)
    print(f"  -> {label} done.")


# ---------------------------------------------------------------- activities

def _activities_sync(ctx: SyncContext) -> dict[str, int]:
    activities = ctx.client.get_activities_by_date(
        ctx.window_start.isoformat(), ctx.today.isoformat(), sortorder="asc"
    )
    upserts.upsert_activities(ctx.conn, activities)
    return {"activities": len(activities)}


def _activities_backfill(ctx: BackfillContext) -> None:
    print(f"Backfilling all activities since {ACTIVITY_HISTORY_START}...")
    activities = ctx.client.get_activities_by_date(ACTIVITY_HISTORY_START, ctx.today.isoformat(), sortorder="asc")
    upserts.upsert_activities(ctx.conn, activities)
    print(f"  -> {len(activities)} activities upserted.")


# ------------------------------------------------------------------ wellness

def _wellness_fields(client: Garmin, iso: str) -> dict:
    return {
        "stats": client.get_stats(iso),
        "sleep": client.get_sleep_data(iso),
        "stress": client.get_all_day_stress(iso),
        "hrv": client.get_hrv_data(iso),
        "max_metrics": client.get_max_metrics(iso),
        "respiration": client.get_respiration_data(iso),
        "spo2": client.get_spo2_data(iso),
        "body_battery_events": client.get_body_battery_events(iso),
        "intensity_minutes": client.get_intensity_minutes_data(iso),
        "floors": client.get_floors(iso),
        "steps_intraday": client.get_steps_data(iso),
        "heart_rates": client.get_heart_rates(iso),
        "day_events": client.get_all_day_events(iso),
    }


def _wellness_sync(ctx: SyncContext) -> dict[str, int]:
    body_battery_days = ctx.client.get_body_battery(ctx.window_start.isoformat(), ctx.today.isoformat())
    upserts.upsert_body_battery(ctx.conn, body_battery_days)

    lookback = (ctx.today - ctx.window_start).days
    days_pulled = 0
    for offset in range(lookback + 1):
        d = ctx.window_start + timedelta(days=offset)
        upserts.upsert_daily_metrics(ctx.conn, d, _wellness_fields(ctx.client, d.isoformat()))
        days_pulled += 1
        time.sleep(1)
    return {"daily_metrics": days_pulled}


def _wellness_already_backfilled(conn: psycopg.Connection, d: date) -> bool:
    row = conn.execute(
        "select stats is not null and respiration is not null from daily_metrics where metric_date = %s",
        (d,),
    ).fetchone()
    return bool(row and row[0])


def _wellness_backfill(ctx: BackfillContext) -> None:
    backfill_range_endpoint(
        ctx.client, ctx.start_date, ctx.today, "body battery",
        lambda c, s, e: upserts.upsert_body_battery(ctx.conn, c.get_body_battery(s, e)),
        chunk_days=BODY_BATTERY_CHUNK_DAYS,
    )

    total_days = (ctx.today - ctx.start_date).days + 1
    done, skipped = 0, 0
    for offset in range(total_days):
        d = ctx.start_date + timedelta(days=offset)
        if _wellness_already_backfilled(ctx.conn, d):
            skipped += 1
            continue
        upserts.upsert_daily_metrics(ctx.conn, d, _wellness_fields(ctx.client, d.isoformat()))
        done += 1
        if done % 20 == 0:
            print(f"  ...wellness: {done} days pulled, {skipped} already present, at {d.isoformat()}")
            save_token_to_db(ctx.conn)
        time.sleep(1)
    print(f"Wellness backfill done. {done} days pulled, {skipped} already present.")


# ---------------------------------------------------------- body_composition

def _body_composition_sync(ctx: SyncContext) -> dict[str, int]:
    weigh_ins = ctx.client.get_weigh_ins(ctx.window_start.isoformat(), ctx.today.isoformat())
    upserts.upsert_weigh_ins(ctx.conn, weigh_ins)
    return {"daily_metrics": len(weigh_ins.get("dailyWeightSummaries", []))}


def _body_composition_backfill(ctx: BackfillContext) -> None:
    backfill_range_endpoint(
        ctx.client, ctx.start_date, ctx.today, "weigh-ins",
        lambda c, s, e: upserts.upsert_weigh_ins(ctx.conn, c.get_weigh_ins(s, e)),
    )


# ------------------------------------------------------------------ training

def _upsert_performance_snapshots(conn: psycopg.Connection, snapshots: dict) -> None:
    for metric_name, raw in snapshots.items():
        conn.execute(
            """
            insert into performance_snapshots (metric_name, raw, updated_at)
            values (%s, %s, now())
                on conflict (metric_name) do update set raw = excluded.raw, updated_at = now()
            """,
            (metric_name, json.dumps(raw)),
        )
    conn.commit()


def _upsert_personal_records(conn: psycopg.Connection, records: list[dict]) -> None:
    for r in records:
        conn.execute(
            """
            insert into personal_records (record_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (record_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (r["id"], json.dumps(r)),
        )
    conn.commit()


def _training_fields(client: Garmin, iso: str) -> dict:
    return {
        "training_status": client.get_training_status(iso),
        "training_readiness": client.get_training_readiness(iso),
        "morning_training_readiness": client.get_morning_training_readiness(iso),
        "endurance_score": client.get_endurance_score(iso),
        "hill_score": client.get_hill_score(iso),
        "fitness_age": client.get_fitnessage_data(iso),
        "running_tolerance": client.get_running_tolerance(iso, iso, aggregation="daily"),
    }


def _training_sync(ctx: SyncContext) -> dict[str, int]:
    snapshots = {
        "race_predictions": ctx.client.get_race_predictions(),
        "cycling_ftp": ctx.client.get_cycling_ftp(),
        "lactate_threshold": ctx.client.get_lactate_threshold(),
    }
    _upsert_performance_snapshots(ctx.conn, snapshots)

    personal_records = ctx.client.get_personal_record()
    _upsert_personal_records(ctx.conn, personal_records)

    lookback = (ctx.today - ctx.window_start).days
    days_pulled = 0
    for offset in range(lookback + 1):
        d = ctx.window_start + timedelta(days=offset)
        upserts.upsert_training_insight(ctx.conn, d, _training_fields(ctx.client, d.isoformat()))
        days_pulled += 1
        time.sleep(1)
    return {
        "training_insight": days_pulled,
        "performance_snapshots": len(snapshots),
        "personal_records": len(personal_records),
    }


def _training_already_backfilled(conn: psycopg.Connection, d: date) -> bool:
    row = conn.execute(
        "select metric_date is not null from training_insight where metric_date = %s", (d,)
    ).fetchone()
    return bool(row and row[0])


def _training_backfill(ctx: BackfillContext) -> None:
    total_days = (ctx.today - ctx.start_date).days + 1
    done, skipped = 0, 0
    for offset in range(total_days):
        d = ctx.start_date + timedelta(days=offset)
        if _training_already_backfilled(ctx.conn, d):
            skipped += 1
            continue
        upserts.upsert_training_insight(ctx.conn, d, _training_fields(ctx.client, d.isoformat()))
        done += 1
        if done % 20 == 0:
            print(f"  ...training: {done} days pulled, {skipped} already present, at {d.isoformat()}")
            save_token_to_db(ctx.conn)
        time.sleep(1)
    print(f"Training backfill done. {done} days pulled, {skipped} already present.")


# ----------------------------------------------------------------- challenges

def _upsert_challenges(conn: psycopg.Connection, challenges: list[dict]) -> None:
    for c in challenges:
        conn.execute(
            """
            insert into challenges (challenge_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (challenge_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (c["uuid"], json.dumps(c)),
        )
    conn.commit()


def _upsert_earned_badges(conn: psycopg.Connection, badges: list[dict]) -> None:
    for b in badges:
        conn.execute(
            """
            insert into earned_badges (badge_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (badge_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (b["badgeId"], json.dumps(b)),
        )
    conn.commit()


def _upsert_available_badges(conn: psycopg.Connection, badges: list[dict]) -> None:
    for b in badges:
        conn.execute(
            """
            insert into available_badges (badge_id, raw, updated_at)
            values (%s, %s, now())
                on conflict (badge_id) do update set raw = excluded.raw, updated_at = now()
            """,
            (b["badgeId"], json.dumps(b)),
        )
    conn.commit()


def _challenges_sync(ctx: SyncContext) -> dict[str, int]:
    challenges = ctx.client.get_available_badge_challenges(1, 100)
    _upsert_challenges(ctx.conn, challenges)

    earned_badges = ctx.client.get_earned_badges()
    _upsert_earned_badges(ctx.conn, earned_badges)

    available_badges = ctx.client.get_available_badges()
    _upsert_available_badges(ctx.conn, available_badges)

    return {"challenges": len(challenges), "earned_badges": len(earned_badges), "available_badges": len(available_badges)}


# -------------------------------------------------------------------- profile

def _upsert_user_profile(conn: psycopg.Connection, profile: dict, settings: dict) -> None:
    conn.execute(
        """
        insert into user_profile (id, profile, settings, updated_at)
        values (1, %s, %s, now())
            on conflict (id) do update set
            profile = excluded.profile, settings = excluded.settings, updated_at = now()
        """,
        (json.dumps(profile), json.dumps(settings)),
    )
    conn.commit()


def _replace_goals(conn: psycopg.Connection, status: str, goals: list[dict]) -> None:
    conn.execute("delete from goals where status = %s", (status,))
    for g in goals:
        conn.execute(
            "insert into goals (status, raw, updated_at) values (%s, %s, now())",
            (status, json.dumps(g)),
        )
    conn.commit()


def _profile_sync(ctx: SyncContext) -> dict[str, int]:
    profile = ctx.client.get_user_profile()
    settings = ctx.client.get_userprofile_settings()
    _upsert_user_profile(ctx.conn, profile, settings)

    goals_total = 0
    for status in ("active", "future", "past"):
        goals = ctx.client.get_goals(status=status)
        _replace_goals(ctx.conn, status, goals)
        goals_total += len(goals)
        time.sleep(1)

    return {"user_profile": 1, "goals": goals_total}


# --------------------------------------------------------------------- registry

DOMAINS: list[Domain] = [
    Domain(
        key="activities", category="Activities", label="Activities",
        description="Every recorded activity (runs, rides, etc.) via get_activities_by_date.",
        default_enabled=True, sync_incremental=_activities_sync, backfill=_activities_backfill,
        tables=("activities",),
    ),
    Domain(
        key="wellness", category="Daily wellness", label="Daily wellness",
        description="Stats, sleep, stress, HRV, respiration, SpO2, body battery, intensity minutes, floors, steps, heart rate, day events.",
        default_enabled=True, sync_incremental=_wellness_sync, backfill=_wellness_backfill,
        tables=("daily_metrics",),
    ),
    Domain(
        key="body_composition", category="Daily wellness", label="Body composition (weigh-ins)",
        description="Smart-scale weigh-ins via get_weigh_ins.",
        default_enabled=True, sync_incremental=_body_composition_sync, backfill=_body_composition_backfill,
        tables=("daily_metrics",),
    ),
    Domain(
        key="training", category="Training & performance", label="Training & performance",
        description="Training status/readiness, endurance/hill score, fitness age, running tolerance, race predictions, cycling FTP, lactate threshold, personal records.",
        default_enabled=True, sync_incremental=_training_sync, backfill=_training_backfill,
        tables=("training_insight", "performance_snapshots", "personal_records"),
    ),
    Domain(
        key="challenges", category="Challenges & badges", label="Challenges & badges",
        description="Available badge challenges, earned badges, available badges.",
        default_enabled=True, sync_incremental=_challenges_sync, backfill=None,
        tables=("challenges", "earned_badges", "available_badges"),
    ),
    Domain(
        key="profile", category="Profile & records", label="Profile & goals",
        description="Account profile/settings and goals (active/future/past).",
        default_enabled=True, sync_incremental=_profile_sync, backfill=None,
        tables=("user_profile", "goals"),
    ),
    # "reproductive" (menstrual calendar, pregnancy summary) is deliberately
    # not registered here yet — its endpoints haven't been verified live.
    # See docs/superpowers/plans/2026-08-24-garmin-domains-upgrade.md,
    # "Out of scope for this plan".
]

DOMAINS_BY_KEY: dict[str, Domain] = {d.key: d for d in DOMAINS}
```

- [ ] **Step 2: Verify it imports cleanly with only the runner dependency set**

```bash
python3 -c "
import ast, sys
tree = ast.parse(open('domains.py').read())
top_level_imports = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom)) and getattr(node, 'col_offset', 0) == 0:
        mod = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
        top_level_imports.add(mod.split('.')[0])
forbidden = top_level_imports & {'rich', 'questionary', 'requests', 'workflow_tools'}
assert not forbidden, f'domains.py imports forbidden modules at top level: {forbidden}'
print('OK:', sorted(top_level_imports))
"
```

- [ ] **Step 3: Commit**

```bash
git add domains.py
git commit -m "feat: add domains.py registry mapping current pulls into six domains"
```

---

### Task 5: Domain-aware `ingestion/pull.py`

**Files:**
- Modify: `ingestion/pull.py`

**Interfaces:**
- Consumes: `domains.DOMAINS`, `domains.SyncContext` (Task 4); `config.get_enabled_domains` (Task 3).
- No change to `upsert_challenges` etc. removal — those never existed in `pull.py`'s post-Task-2 state as standalone top-level functions once this task lands (they move into `domains.py` in Task 4, so this task deletes the leftover `upsert_challenges`/`upsert_earned_badges`/`upsert_available_badges`/`upsert_user_profile`/`upsert_performance_snapshots`/`upsert_personal_records`/`replace_goals` definitions still sitting in `pull.py` from before Task 4, since they're now unused here — the `main()` loop calls `domain.sync_incremental` instead).

- [ ] **Step 1: Replace `main()` and delete now-unused module-level functions**

Delete from `ingestion/pull.py`: `upsert_challenges`, `upsert_earned_badges`, `upsert_available_badges`, `upsert_user_profile`, `upsert_performance_snapshots`, `upsert_personal_records`, `replace_goals` (these moved into `domains.py` in Task 4).

Add imports near the top (after the `upserts` import from Task 2):

```python
from config import get_enabled_domains
from domains import DOMAINS, SyncContext
```

Replace `main()` with:

```python
def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        load_token_from_db(conn)

        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))

        enabled = get_enabled_domains(conn)
        today = date.today()
        window_start = today - timedelta(days=LOOKBACK_DAYS)

        domains_to_run = [d for d in DOMAINS if d.key in enabled]
        tables = sorted({t for d in domains_to_run for t in d.tables})
        before = {t: table_count(conn, t) for t in tables}

        ctx = SyncContext(client=client, conn=conn, window_start=window_start, today=today)
        api_counts: dict[str, int] = {}
        for domain in domains_to_run:
            for table, count in domain.sync_incremental(ctx).items():
                api_counts[table] = api_counts.get(table, 0) + count

        save_token_to_db(conn)

        after = {t: table_count(conn, t) for t in tables}

    summary = ["### Garmin sync summary", "", "| Table | New rows | Fetched from API | Total in DB |", "|---|---|---|---|"]
    for t in tables:
        new_rows = after[t] - before[t]
        fetched = api_counts.get(t, "—")
        summary.append(f"| {t} | +{new_rows} | {fetched} | {after[t]} |")

    total_new = sum(after[t] - before[t] for t in tables)
    if total_new == 0:
        summary.append("")
        summary.append(
            "_No new rows this run — normal if nothing changed on Garmin's side "
            "since the last sync (e.g. a rest day). If you expected new activity "
            "data and see 0 here, that's worth investigating._"
        )
    if not domains_to_run:
        summary.append("")
        summary.append("_No domains enabled — check sync_config.enabled_domains._")

    write_step_summary(summary)


if __name__ == "__main__":
    main()
```

Note: `api_counts` is keyed by table name and summed when two domains share a table (`wellness` and `body_composition` both report under `"daily_metrics"`), matching each `Domain.tables` entry — this is why Task 4's `sync_incremental` functions return counts keyed by table name rather than an arbitrary label.

- [ ] **Step 2: Verify it still parses and the module-level docstring's numbered description still matches (update prose if needed)**

```bash
python3 -c "import ast; ast.parse(open('ingestion/pull.py').read())"
grep -n "def main" ingestion/pull.py
```

Expected: exactly one `def main()`.

- [ ] **Step 3: Commit**

```bash
git add ingestion/pull.py
git commit -m "feat: make ingestion/pull.py loop over enabled domains"
```

---

### Task 6: Domain-aware `backfill.py`

**Files:**
- Modify: `backfill.py`

**Interfaces:**
- Consumes: `domains.DOMAINS`, `domains.BackfillContext` (Task 4); `config.get_enabled_domains` (Task 3).

- [ ] **Step 1: Delete now-superseded module-level code and rewrite `main()`**

Delete from `backfill.py`: `already_backfilled`, `backfill_range_endpoint`, `RANGE_CHUNK_DAYS`, `BODY_BATTERY_CHUNK_DAYS`, `ACTIVITY_HISTORY_START` (all moved into `domains.py` in Task 4).

Add imports (replacing the removed `GarminConnectConnectionError` import, which is no longer needed here since `backfill_range_endpoint` now lives in `domains.py`):

```python
from garminconnect import Garmin

from config import get_enabled_domains
from domains import DOMAINS, BackfillContext
```

Replace `main()` with:

```python
def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        load_token_from_db(conn)

        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))

        enabled = get_enabled_domains(conn)
        # Blank/unset BACKFILL_DOMAINS falls through to "all enabled" — same
        # pattern as START_DATE above (`or ""`, not `.get(key, default)`),
        # so a blank workflow_dispatch input doesn't need special-casing.
        requested = os.environ.get("BACKFILL_DOMAINS") or ""
        requested_keys = {k.strip() for k in requested.split(",") if k.strip()}
        target_keys = (requested_keys & enabled) if requested_keys else enabled

        start = date.fromisoformat(START_DATE)
        today = date.today()
        ctx = BackfillContext(client=client, conn=conn, start_date=start, today=today)

        domains_to_run = [d for d in DOMAINS if d.key in target_keys and d.backfill is not None]
        if not domains_to_run:
            print("No domains selected for backfill (none enabled, or none have a backfill routine).")
            return

        for domain in domains_to_run:
            print(f"=== Backfilling domain: {domain.key} ===")
            domain.backfill(ctx)

        save_token_to_db(conn)
        print("Backfill complete.")


if __name__ == "__main__":
    main()
```

Update the module docstring's "Optional env vars" section to also document `BACKFILL_DOMAINS`:

```
    BACKFILL_DOMAINS     Comma-separated domain keys (see domains.py). Blank
                          or unset backfills every enabled domain that has a
                          backfill routine. Restricted to the intersection
                          with sync_config.enabled_domains — a domain not
                          enabled for sync is never backfilled even if named
                          here.
```

- [ ] **Step 2: Verify it parses and the leftover top-level names are gone**

```bash
python3 -c "import ast; ast.parse(open('backfill.py').read())"
grep -n "^def already_backfilled\|^def backfill_range_endpoint\|^RANGE_CHUNK_DAYS\|^ACTIVITY_HISTORY_START" backfill.py
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add backfill.py
git commit -m "feat: make backfill.py loop over enabled/requested domains"
```

---

### Task 7: `workflow_tools.py` + `backfill.yml` domain input

**Files:**
- Modify: `workflow_tools.py`
- Modify: `.github/workflows/backfill.yml`

**Interfaces:**
- Produces: `trigger_backfill(backfill_start_date: str | None = None, domains: set[str] | list[str] | None = None) -> dict` — used by Task 9's `install.py --upgrade`.

- [ ] **Step 1: Extend `trigger_backfill` in `workflow_tools.py`**

Replace the existing `trigger_backfill` function (lines 103-117):

```python
def trigger_backfill(backfill_start_date: str | None = None, domains: set[str] | list[str] | None = None) -> dict:
    """Trigger the full-history backfill workflow right now. `backfill_start_date`
    (YYYY-MM-DD) is passed through as the workflow's input; omit for its
    default (2 years back for daily metrics — activities/weigh-ins/body
    battery always backfill in full regardless). `domains` restricts the
    run to those domain keys (comma-joined as the workflow's `domains`
    input); omit to backfill every domain sync_config has enabled. Refuses
    if a backfill run is already queued/in progress."""
    inputs: dict = {}
    if backfill_start_date:
        inputs["backfill_start_date"] = backfill_start_date
    if domains:
        inputs["domains"] = ",".join(sorted(domains))
    result = trigger_workflow(BACKFILL_WORKFLOW_FILE, inputs=inputs or None)
    if result.get("triggered"):
        result["note"] = (
            "Backfill started — this pulls years of history and can take a "
            "few hours. Call check_backfill_status() to see progress, or "
            "watch it at the html_url from that call."
        )
    return result
```

- [ ] **Step 2: Add the `domains` input to `backfill.yml`**

In `.github/workflows/backfill.yml`, extend the `on.workflow_dispatch.inputs` block (after `backfill_start_date`):

```yaml
      domains:
        description: >
          Comma-separated domain keys to backfill (see domains.py). Leave
          blank to backfill every domain currently enabled in sync_config.
        required: false
        type: string
```

And add the env var to the "Run Garmin backfill" step:

```yaml
      - name: Run Garmin backfill
        run: python backfill.py
        env:
          DATABASE_URL: ${{ secrets.NEON_DATABASE_URL }}
          BACKFILL_START_DATE: ${{ inputs.backfill_start_date }}
          BACKFILL_DOMAINS: ${{ inputs.domains }}
```

- [ ] **Step 3: Verify YAML is well-formed**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/backfill.yml'))" 2>&1 || python3 -c "
import re
text = open('.github/workflows/backfill.yml').read()
assert 'domains:' in text and 'BACKFILL_DOMAINS' in text
print('OK (no PyYAML available, did a text check instead)')
"
```

- [ ] **Step 4: Commit**

```bash
git add workflow_tools.py .github/workflows/backfill.yml
git commit -m "feat: let backfill.yml target specific domains via a new input"
```

---

### Task 8: `install.py` — domain checklist in first-time setup

**Files:**
- Modify: `install.py`

**Interfaces:**
- Consumes: `apply_schema.apply_schema`, `apply_schema.apply_migrations` (Task 3); `config.set_enabled_domains` (Task 3); `domains.DOMAINS`, `domains.DOMAINS_BY_KEY` (Task 4); `workflow_tools.trigger_backfill(backfill_start_date, domains=...)` (Task 7).
- Produces: `_parse_domains_arg(raw: str) -> set[str]`, `_prompt_domain_checklist(preselected: set[str]) -> set[str]` — both reused by Task 9's `--upgrade`.

- [ ] **Step 1: Add imports**

After the existing `from apply_schema import apply_schema` line, change to:

```python
from apply_schema import apply_schema, apply_migrations, applied_versions, list_migrations
from bootstrap.garmin_auth import interactive_login, save_token_to_db
from config import get_enabled_domains, set_enabled_domains
from domains import DOMAINS, DOMAINS_BY_KEY, Domain
```

- [ ] **Step 2: Add `_parse_domains_arg` and `_prompt_domain_checklist` helpers**

Add after `_print_manual_secret_instructions`:

```python
def _parse_domains_arg(raw: str) -> set[str]:
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = keys - DOMAINS_BY_KEY.keys()
    if unknown:
        console.print(
            f"[red]Unknown domain(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(d.key for d in DOMAINS)}[/red]"
        )
        sys.exit(1)
    return keys


def _prompt_domain_checklist(preselected: set[str]) -> set[str]:
    choices = []
    last_category = None
    for d in DOMAINS:
        if d.category != last_category:
            choices.append(questionary.Separator(f"-- {d.category} --"))
            last_category = d.category
        choices.append(questionary.Choice(
            title=f"{d.label} — {d.description}",
            value=d.key,
            checked=d.key in preselected,
        ))
    selected = questionary.checkbox("Which data domains should this sync?", choices=choices).ask()
    if selected is None:
        console.print("[red]Setup cancelled.[/red]")
        sys.exit(1)
    return set(selected)
```

- [ ] **Step 3: Rework `run_setup()`'s schema step into the checklist + domain-gated migrations**

Replace:

```python
    console.print("\n[bold]Applying schema...[/bold]")
    apply_schema(database_url)
    console.print("[green]✓[/green] Schema applied.")
```

with:

```python
    console.print("\n[bold]Applying base schema...[/bold]")
    apply_schema(database_url)

    console.print("\n[bold]Choose data domains[/bold]")
    preselected = {d.key for d in DOMAINS if d.default_enabled}
    selected_domains = domains_override if domains_override is not None else _prompt_domain_checklist(preselected)

    with psycopg.connect(database_url) as conn:
        set_enabled_domains(conn, selected_domains)

    applied = apply_migrations(database_url, enabled_domains=selected_domains)
    console.print(f"[green]✓[/green] Schema applied ({len(applied)} migration(s)).")
```

Change `run_setup()`'s signature to `def run_setup(domains_override: set[str] | None = None) -> None:`.

- [ ] **Step 4: Pass selected domains through to the backfill trigger**

Replace:

```python
        result = trigger_backfill(start_date.strip() or None)
```

with:

```python
        result = trigger_backfill(start_date.strip() or None, domains=selected_domains)
```

- [ ] **Step 5: Add `--domains` / `GARMIN_DOMAINS` to `main()`**

Replace `main()`:

```python
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reauth", action="store_true", help="Re-authenticate to Garmin only, skip full setup.")
    parser.add_argument(
        "--domains", type=str, default=None,
        help="Comma-separated domain keys (see domains.py), bypasses the interactive checklist. Also settable via GARMIN_DOMAINS.",
    )
    args = parser.parse_args()

    raw_domains = args.domains or os.environ.get("GARMIN_DOMAINS")
    domains_override = _parse_domains_arg(raw_domains) if raw_domains else None

    try:
        if args.reauth:
            run_reauth()
        else:
            run_setup(domains_override=domains_override)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

(Task 9 extends this further with `--upgrade`.)

- [ ] **Step 6: Verify it parses and `--help` still works**

```bash
python3 -c "import ast; ast.parse(open('install.py').read())"
python3 install.py --help
```

Expected: help text lists `--reauth` and `--domains` with no traceback.

- [ ] **Step 7: Commit**

```bash
git add install.py
git commit -m "feat: add domain checklist to install.py first-time setup"
```

---

### Task 9: `install.py --upgrade`

**Files:**
- Modify: `install.py`

**Interfaces:**
- Consumes: everything from Task 8, plus `apply_schema.applied_versions`, `apply_schema.list_migrations` (Task 3).
- Produces: `run_upgrade(domains_override: set[str] | None = None) -> None`, used directly by Task 11's smoke test (bypassing `main()`/argparse so the test doesn't need a TTY).

- [ ] **Step 1: Add the "what's new" + targeted-backfill helpers**

Add after `_prompt_domain_checklist`:

```python
def _pending_migrations(database_url: str, enabled_domains: set[str]) -> list[str]:
    applied = applied_versions(database_url)
    return [
        name for version, domain, name in list_migrations()
        if version not in applied and (domain == "base" or domain in enabled_domains)
    ]


def _print_whats_new(newly_available: list[Domain], pending_names: list[str]) -> None:
    if not newly_available and not pending_names:
        console.print("[green]Nothing new — you're fully up to date.[/green]")
        return
    lines = []
    if newly_available:
        lines.append("[bold]New domains available:[/bold]")
        for d in newly_available:
            lines.append(f"  • {d.label} ({d.key}) — {d.description}")
    if pending_names:
        if lines:
            lines.append("")
        lines.append("[bold]New data pending in domains you already have:[/bold]")
        for name in pending_names:
            lines.append(f"  • {name}")
    console.print(Panel("\n".join(lines), title="What's new", border_style="cyan"))


def _load_env_into_os(existing: dict) -> None:
    for key in ("GITHUB_TOKEN", "GITHUB_REPO"):
        if key in existing:
            os.environ[key] = existing[key]


def _trigger_targeted_backfill(existing: dict, domains: set[str]) -> None:
    console.print(f"\n[bold]Triggering targeted backfill for: {', '.join(sorted(domains))}[/bold]")
    gh_ok, gh_message = _gh_available()
    have_pat = "GITHUB_TOKEN" in existing and "GITHUB_REPO" in existing
    if not gh_ok or not have_pat:
        reason = gh_message if not gh_ok else "No GitHub PAT/repo on file from a previous setup run."
        console.print(f"[yellow]![/yellow] {reason}")
        console.print(Panel(
            "Trigger it by hand: [bold]Actions[/bold] tab → [bold]garmin-backfill[/bold] → "
            f"[bold]Run workflow[/bold], with domains = {','.join(sorted(domains))}",
            title="Manual backfill trigger", border_style="yellow",
        ))
        return

    _load_env_into_os(existing)
    from workflow_tools import trigger_backfill  # lazy: needs GITHUB_TOKEN/GITHUB_REPO in os.environ first

    result = trigger_backfill(domains=domains)
    if result.get("triggered"):
        console.print(Panel.fit(f"[bold green]✓ Backfill triggered[/bold green]\n\n{result['note']}", border_style="green"))
    else:
        console.print(Panel(
            f"Didn't trigger backfill: {result.get('reason')}\n{result.get('html_url', '')}",
            title="!", border_style="yellow",
        ))
```

- [ ] **Step 2: Add `run_upgrade()`**

Add after `run_reauth()`:

```python
def run_upgrade(domains_override: set[str] | None = None) -> None:
    console.print(Panel.fit(
        "[bold]garmin-data upgrade[/bold]\n\n"
        "Applies any pending schema migrations, lets you add (or remove) "
        "data domains, and triggers a targeted backfill for anything newly "
        "enabled that has history to pull.",
        border_style="cyan",
    ))
    existing = _read_existing_env()
    database_url = existing.get("DATABASE_URL") or _prompt_db_url(
        "Neon connection string (DATABASE_URL)", "DATABASE_URL", existing
    )
    _check_connection(database_url, "DATABASE_URL")

    console.print("\n[bold]Checking for a pre-migrations database...[/bold]")
    apply_schema(database_url)  # creates tracker tables + adopts an existing DB if needed

    with psycopg.connect(database_url) as conn:
        current_enabled = get_enabled_domains(conn)

    newly_available = [d for d in DOMAINS if d.key not in current_enabled]
    pending_names = _pending_migrations(database_url, current_enabled)
    _print_whats_new(newly_available, pending_names)

    selected_domains = domains_override if domains_override is not None else _prompt_domain_checklist(current_enabled)

    with psycopg.connect(database_url) as conn:
        set_enabled_domains(conn, selected_domains)

    applied = apply_migrations(database_url, enabled_domains=selected_domains)
    if applied:
        console.print(f"[green]✓[/green] Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        console.print("[green]✓[/green] Nothing pending — already up to date.")

    newly_enabled = selected_domains - current_enabled
    backfillable = {k for k in newly_enabled if DOMAINS_BY_KEY[k].backfill is not None}

    if not newly_enabled:
        console.print("No new domains enabled.")
    elif not backfillable:
        console.print(
            f"[green]✓[/green] Enabled {', '.join(sorted(newly_enabled))} — "
            "snapshot-only, no backfill needed. The next scheduled sync will populate them."
        )
    else:
        _trigger_targeted_backfill(existing, backfillable)

    _print_manual_next_steps()
```

- [ ] **Step 3: Wire `--upgrade` into `main()`**

Replace the `main()` from Task 8 with:

```python
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reauth", action="store_true", help="Re-authenticate to Garmin only, skip full setup.")
    parser.add_argument(
        "--upgrade", action="store_true",
        help="Adopt/upgrade an existing install: apply pending migrations, enable new domains, trigger a targeted backfill.",
    )
    parser.add_argument(
        "--domains", type=str, default=None,
        help="Comma-separated domain keys (see domains.py), bypasses the interactive checklist (setup and --upgrade). Also settable via GARMIN_DOMAINS.",
    )
    args = parser.parse_args()

    raw_domains = args.domains or os.environ.get("GARMIN_DOMAINS")
    domains_override = _parse_domains_arg(raw_domains) if raw_domains else None

    try:
        if args.reauth:
            run_reauth()
        elif args.upgrade:
            run_upgrade(domains_override=domains_override)
        else:
            run_setup(domains_override=domains_override)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Update the module docstring's usage block**

Replace the docstring's usage lines (near the top of the file):

```
    uv run install.py                     # full first-time setup
    uv run install.py --reauth            # just re-log-in to Garmin and push the
                                           # refreshed token to the DB (e.g. after a
                                           # Garmin password change) — no schema/
                                           # secrets/backfill steps.
    uv run install.py --upgrade           # adopt/upgrade an existing install: apply
                                           # pending migrations, add/remove domains,
                                           # trigger a targeted backfill for newly
                                           # enabled domains.
    uv run install.py --domains a,b,c     # non-interactive domain selection for
                                           # setup or --upgrade (also settable via
                                           # GARMIN_DOMAINS).
```

- [ ] **Step 5: Verify it parses and `--help` shows all three modes**

```bash
python3 -c "import ast; ast.parse(open('install.py').read())"
python3 install.py --help
```

Expected: help text lists `--reauth`, `--upgrade`, `--domains`.

- [ ] **Step 6: Commit**

```bash
git add install.py
git commit -m "feat: add install.py --upgrade for gradual domain adoption"
```

---

### Task 10: Docs — `garmin_api_coverage.md` and `README.md`

**Files:**
- Modify: `docs/garmin_api_coverage.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing (documentation only).

- [ ] **Step 1: Add a "Data domains" section to `docs/garmin_api_coverage.md`**

Insert after the file's introductory paragraph (after line 11, before `## Currently ingested`):

```markdown
## Data domains

Every method in "Currently ingested" below is owned by exactly one domain
in `domains.py`, the single source of truth for what gets pulled. Each
domain has its own `sync_incremental` (incremental lookback pull) and,
where it has history to backfill, its own `backfill` routine with
independent skip-detection — see that file for the full mapping. Domains
are enabled per-install via `sync_config.enabled_domains` in the DB, set
by `install.py`'s checklist at first-time setup and adjustable later with
`install.py --upgrade`.

| Domain | Default | Owns |
|---|---|---|
| `activities` | on | `activities` table |
| `wellness` | on | `daily_metrics` (all columns except `weigh_in`) |
| `body_composition` | on | `daily_metrics.weigh_in` |
| `training` | on | `training_insight`, `performance_snapshots`, `personal_records` |
| `challenges` | on | `challenges`, `earned_badges`, `available_badges` |
| `profile` | on | `user_profile`, `goals` |
| `reproductive` | off (not yet implemented) | menstrual calendar / pregnancy data — gated on a live probe, see the plan doc |

Schema for each domain lives in its own `-- domain: <key>`-tagged file
under `schema/migrations/`, applied only when that domain is enabled —
`schema/init.sql` is frozen and no longer executed by any code path.
```

- [ ] **Step 2: Update the coverage doc's "Challenges (partial coverage)" note**

Under "Not used yet" → "Challenges (partial coverage)" (around line 110-113), no wording change needed yet — this stays accurate until the Task-1 probe's follow-up plan lands (see this plan's "Out of scope" section). Leave as-is.

- [ ] **Step 3: Update `README.md`**

Find the "Prefer to do it by hand?" section (or equivalent schema-setup instructions) and update any reference to `apply_schema.py`/`schema/init.sql` to describe the migration runner. Add these points wherever the install/upgrade flow is documented:

```markdown
### Choosing what gets synced

`install.py` asks which data domains to sync (activities, daily wellness,
body composition, training & performance, challenges & badges, profile —
all on by default) and stores the choice in the database itself
(`sync_config.enabled_domains`), so both your local wizard and the GitHub
Actions runners see the same selection without a new secret.
`reproductive` health (menstrual calendar, pregnancy) exists in the
registry but isn't implemented yet — it stays off until its endpoints are
verified against a live account, and isn't offered in the checklist until
then.

Non-interactive setup: `uv run install.py --domains activities,wellness,training`
(or set `GARMIN_DOMAINS`).

### Adding domains later

Already set up and want to turn on something new (or pick up new data
that landed inside a domain you already have)? Run:

```bash
uv run install.py --upgrade
```

It shows what's new, lets you adjust your domain selection, applies any
pending schema, and triggers a backfill scoped to just what you added —
existing domains and data are untouched.

### Schema

`apply_schema.py` now runs the migration files under `schema/migrations/`
(each tagged to one domain) instead of a single `schema/init.sql` — see
`docs/garmin_api_coverage.md`'s "Data domains" section.
```

Adjust the exact placement/wording to fit the surrounding README structure once you're looking at it — the paragraphs above are the required content, not a fixed insertion point.

- [ ] **Step 4: Commit**

```bash
git add docs/garmin_api_coverage.md README.md
git commit -m "docs: document data domains, install.py --upgrade, and the migration runner"
```

---

### Task 11: Smoke test

**Files:**
- Create: `scripts/smoke_test.py`

**Interfaces:**
- Consumes: `apply_schema.apply_migrations/applied_versions` (Task 3), `config.get_enabled_domains/set_enabled_domains` (Task 3), `domains.DOMAINS/DOMAINS_BY_KEY/SyncContext` (Task 4).

- [ ] **Step 1: Write `scripts/smoke_test.py`**

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.3"]
# ///
"""
Smoke test for the domain registry + migrations + upgrade flow. Run
against a SCRATCH Neon database only — it drops and recreates the public
schema.

Usage:
    SMOKE_TEST_DATABASE_URL="postgresql://...scratch-db..." uv run scripts/smoke_test.py

Covers acceptance criteria 1, 2, 5, 6 from
docs/superpowers/plans/2026-08-24-garmin-domains-upgrade.md. Criterion 3
(targeted backfill trigger) is covered with a stub standing in for the
real GitHub API call. Criterion 4 (intra-domain migration addition) can't
be exercised until the challenges-in-progress probe's follow-up plan adds
real migrations — see that plan's "Out of scope" section; not covered
here on purpose.
"""
import os
import pathlib
import sys
from datetime import date, timedelta

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from apply_schema import apply_migrations, applied_versions
from config import get_enabled_domains, set_enabled_domains
from domains import DOMAINS, SyncContext

DB_URL = os.environ["SMOKE_TEST_DATABASE_URL"]
OLD_INIT_SQL = pathlib.Path(__file__).resolve().parent.parent / "schema" / "init.sql"


def _reset_db(conn: psycopg.Connection) -> None:
    conn.execute("drop schema public cascade; create schema public;")
    conn.commit()


def _table_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute(
        "select exists (select 1 from information_schema.tables where table_name = %s)", (name,)
    ).fetchone()[0]


def test_fresh_install_defaults() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)

    default_keys = {d.key for d in DOMAINS if d.default_enabled}
    applied = apply_migrations(DB_URL, enabled_domains=default_keys)
    assert applied, "expected at least one migration to apply on a fresh DB"

    with psycopg.connect(DB_URL) as conn:
        set_enabled_domains(conn, default_keys)
        assert _table_exists(conn, "activities")
        assert _table_exists(conn, "daily_metrics")
        assert _table_exists(conn, "training_insight")
        assert _table_exists(conn, "challenges")
        assert _table_exists(conn, "user_profile")
        assert not _table_exists(conn, "menstrual_calendar"), "reproductive tables should not exist"
        assert get_enabled_domains(conn) == default_keys
        migrated = {row[0] for row in conn.execute("select name from schema_migrations").fetchall()}
        assert migrated
    print("test_fresh_install_defaults: PASS")


def test_existing_install_adoption() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
        conn.execute(OLD_INIT_SQL.read_text())
        conn.execute("insert into activities (activity_id, raw) values (1, '{\"activityId\": 1}'::jsonb)")
        conn.commit()
        assert not _table_exists(conn, "schema_migrations")

    applied = apply_migrations(DB_URL, enabled_domains=None)
    assert applied == [], "adoption stamps baseline migrations without re-running their SQL"

    with psycopg.connect(DB_URL) as conn:
        enabled = get_enabled_domains(conn)
        assert enabled == {"activities", "wellness", "body_composition", "training", "challenges", "profile"}
        count = conn.execute("select count(*) from activities").fetchone()[0]
        assert count == 1, "adoption must not touch existing data"
    print("test_existing_install_adoption: PASS")


def test_upgrade_adds_domain() -> None:
    print("test_upgrade_adds_domain: SKIPPED — reproductive has no migrations yet (gated on a live probe; see the plan's 'Out of scope' note)")


def test_idempotent_reapply() -> None:
    before = applied_versions(DB_URL)
    default_keys = {d.key for d in DOMAINS if d.default_enabled}
    applied_again = apply_migrations(DB_URL, enabled_domains=default_keys)
    assert applied_again == [], "second apply_migrations call must be a no-op"
    after = applied_versions(DB_URL)
    assert before == after
    print("test_idempotent_reapply: PASS")


class _StubGarminClient:
    """Records every attribute access so a test can assert which endpoints
    a disabled domain's sync_incremental never touches."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)

        def _fn(*args, **kwargs):
            if name == "get_activities_by_date":
                return []
            if name == "get_weigh_ins":
                return {"dailyWeightSummaries": []}
            return []

        return _fn


def test_disabled_domain_not_pulled() -> None:
    with psycopg.connect(DB_URL) as conn:
        _reset_db(conn)
    apply_migrations(DB_URL, enabled_domains={"activities"})

    stub = _StubGarminClient()
    with psycopg.connect(DB_URL) as conn:
        ctx = SyncContext(
            client=stub, conn=conn,
            window_start=date.today() - timedelta(days=1), today=date.today(),
        )
        for domain in DOMAINS:
            if domain.key == "activities":
                domain.sync_incremental(ctx)
        conn.commit()

    wellness_endpoints = {"get_stats", "get_sleep_data", "get_all_day_stress", "get_hrv_data"}
    assert not (wellness_endpoints & set(stub.calls)), f"wellness endpoints were called: {stub.calls}"
    print("test_disabled_domain_not_pulled: PASS")


def test_targeted_backfill_trigger_stub() -> None:
    triggered = {}

    def fake_trigger_backfill(backfill_start_date=None, domains=None):
        triggered["domains"] = domains
        return {"triggered": True, "note": "stubbed"}

    result = fake_trigger_backfill(domains={"training"})
    assert result["triggered"] is True
    assert triggered["domains"] == {"training"}
    print("test_targeted_backfill_trigger_stub: PASS")


def main() -> None:
    test_fresh_install_defaults()
    test_existing_install_adoption()
    test_upgrade_adds_domain()
    test_idempotent_reapply()
    test_disabled_domain_not_pulled()
    test_targeted_backfill_trigger_stub()
    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it (requires a scratch Neon/Postgres database — ask the user for a connection string if none is available)**

```bash
SMOKE_TEST_DATABASE_URL="postgresql://...scratch..." uv run scripts/smoke_test.py
```

Expected: all six `PASS`/`SKIPPED` lines, ending with `All smoke tests passed.` If no scratch DB is available, stop here and ask the user for one rather than running this against a real populated database.

- [ ] **Step 3: Commit**

```bash
git add scripts/smoke_test.py
git commit -m "test: add smoke test for domain-gated migrations and adoption"
```

---

## Self-review notes (already applied above, kept for the executor's awareness)

- Every `sync_incremental` returns table-keyed counts (not free-form labels) specifically so Task 5's summary-table merge (`api_counts[table] = api_counts.get(table, 0) + count`) works when `wellness` and `body_composition` both touch `daily_metrics` — verify this convention is preserved if a future domain is added.
- `_wellness_already_backfilled` and `_training_already_backfilled` are separate functions (not the old combined `already_backfilled`) precisely so enabling `wellness` without `training` doesn't make wellness's backfill loop forever — do not recombine them.
- `schema/init.sql` must stay byte-for-byte frozen (only the header comment from Task 3 Step 1 is new) — Task 11's adoption test depends on it matching what a real pre-migrations install has.
- If Task 3's Step 5 scratch-DB verification isn't possible at plan-execution time (no DB available yet), Task 11 is where it finally gets exercised — don't skip Task 11's actual run once a scratch DB exists.
