# Original task spec: selectable data-domains + gradual upgrade flow

Verbatim copy of the task prompt this plan (`2026-08-24-garmin-domains-upgrade.md`)
implements. Kept alongside the plan so executors can check the plan's
interpretation against the original wording. Where the plan deviates, it
says so explicitly in its "Two deliberate deviations" section — the plan
is authoritative where the two disagree.

---

You are working in the `garmin-data` repo (pulls Garmin Connect data into a
private Neon Postgres DB via the unofficial `garminconnect` library, scheduled
on GitHub Actions). Read the whole prompt before touching anything, then work
in the ordered parts below. Prefer many small, verifiable commits over one big
change.

## 0. Orient yourself first (do this before writing code)

Read these files fully and match their existing style exactly — do not
reinvent patterns that already exist:

- `install.py` — the wizard. Note `run_setup()`, `run_reauth()`, `main()`'s
  argparse, and that it already depends on `questionary` and `rich`. **The new
  UI must reuse `questionary`/`rich`, not add a new dependency.**
- `apply_schema.py` — currently applies `schema/init.sql` in one shot.
- `schema/init.sql` — the current monolithic, idempotent schema.
- `ingestion/pull.py` — incremental sync (`LOOKBACK_DAYS = 5`), the per-date
  loop, and the snapshot/range pulls.
- `backfill.py` — full-history backfill, `already_backfilled()` skip logic,
  `backfill_range_endpoint()` chunk-shrinking, `BACKFILL_START_DATE`.
- `bootstrap/garmin_auth.py` — `TOKEN_DIR`, `load_token_from_db`,
  `save_token_to_db`, `interactive_login`.
- `workflow_tools.py` — `trigger_backfill`, `check_backfill_status`,
  `trigger_workflow` (refuses if a run is already in progress).
- `.github/workflows/sync.yml` (`garmin-sync`) and `backfill.yml`
  (`garmin-backfill`) — both `pip install -r ingestion/requirements.txt` and
  read `DATABASE_URL` from `secrets.NEON_DATABASE_URL`.
- `docs/garmin_api_coverage.md` — the coverage audit and its conventions.

### Invariants you must NOT break (hard constraints)

1. **Read-only against Garmin.** No write endpoints, ever (no `set_*`,
   `add_*`, `delete_*`, `upload_*`, `schedule_*`).
2. **No redundant columns; verify live before writing schema.** Every new
   endpoint must be probed against a real account first (see Part 1) to
   confirm its shape and natural key and to rule out duplication of data
   already stored. This is a repo convention, not optional.
3. **`pull.py` and `backfill.py` upsert helpers stay in lockstep.** They
   currently share identical `upsert_daily_metrics` / `upsert_training_insight`
   bodies by design. Keep any shared helper identical in both, or (preferred)
   factor it into one importable module and have both call it.
4. **Idempotent and resumable everywhere.** Safe to re-run schema, sync, and
   backfill any number of times.
5. **Do not break existing installs.** There are users on the current version
   with populated DBs and no migrations table. The upgrade path must adopt
   those DBs without re-applying or double-creating anything (Part 3).
6. **Per-person model stays intact.** Each person has their own repo copy, own
   Neon DB, own token. Nothing here shares data across accounts.

## 1. Probe the genuinely-new endpoints FIRST (verify-live-before-schema)

Do **not** write schema for the new endpoints from assumptions. Create a
throwaway `scripts/probe_new_endpoints.py` (uv inline-deps script, same header
style as `backfill.py`) that logs in with the existing token and prints pretty
JSON for each new endpoint, so the human can run it and paste back real shapes.

Cover:

- **Challenges (run on the primary account):** `get_in_progress_badges`,
  `get_inprogress_virtual_challenges`, `get_non_completed_badge_challenges`.
  For each, determine: the natural key to upsert on (`badgeId`? `uuid`? `id`?),
  and whether it is a genuinely new list or just a filtered slice of
  `get_available_badge_challenges` / `get_available_badges` / `get_earned_badges`
  already stored. If one is a strict duplicate, record it in the coverage doc's
  "Investigated, deliberately not used (duplicates)" section and drop it —
  exactly like the existing `get_daily_steps` / `get_rhr_day` entries.
- **Reproductive (run on an account that actually tracks it — a partner's
  install, not the primary):** `get_menstrual_calendar_data(startdate, enddate)`,
  `get_menstrual_data_for_date(date)`, `get_pregnancy_summary()`. Determine:
  does the calendar range endpoint already carry per-date entries (making
  `get_menstrual_data_for_date` redundant, like `get_daily_steps` vs
  `get_steps_data`)? What is the per-date natural key? Is pregnancy a single
  snapshot object with no natural list key (store like `performance_snapshots`)?

**Gate the rest of the schema work on these probe results.** Where you can't
verify a natural key against real data (e.g. the primary account has zero
in-progress virtual challenges), follow the repo's existing precedent for
`goals`: store via delete-then-insert or a surrogate key and leave a schema
comment saying the natural key is unverified, rather than guessing one.

Update `docs/garmin_api_coverage.md` as you go: move each newly-wired method
from "Not used yet" into "Currently ingested" with its storage mapping, or into
the duplicates section with the reason.

## 2. Introduce the domain registry (`domains.py`, single source of truth)

Create `domains.py` at repo root. It is the one place that defines what data
domains exist; the checklist, `pull.py`, `backfill.py`, and the docs all derive
from it. Do not hardcode the domain list anywhere else.

Each domain declares:

- `key` — machine name (e.g. `"wellness"`).
- `category` — checklist grouping header (e.g. `"Daily wellness"`).
- `label` + `description` — human text for the checklist.
- `default_enabled: bool` — pre-checked in the checklist.
- `sync_incremental(ctx)` — pulls this domain's data for the incremental
  lookback window and upserts it. `ctx` bundles `client`, `conn`,
  `window_start`, `today`.
- `backfill(ctx) | None` — historical backfill for this domain, with its own
  "already done" detection so re-runs skip. `None` for snapshot-only domains
  (they have no history; the next incremental sync populates them).
- `tables` — the table/column names this domain owns (for docs + the
  "what's new" upgrade summary; not used to run schema).

Define exactly these domains, mapping the CURRENT code into them so behavior is
preserved (do not change what's pulled for existing domains beyond the new
challenge endpoints):

| Category | key | default | endpoints it owns |
|---|---|---|---|
| Activities | `activities` | on | `get_activities_by_date` |
| Daily wellness | `wellness` | on | stats, sleep, stress, hrv, max_metrics, respiration, spo2, body_battery (+events), intensity_minutes, floors, steps_intraday, heart_rates, day_events |
| Daily wellness | `body_composition` | on | `get_weigh_ins` (→ `daily_metrics.weigh_in`) |
| Training & performance | `training` | on | training status/readiness/morning readiness, endurance_score, hill_score, fitnessage, running_tolerance, race_predictions, cycling_ftp, lactate_threshold, personal_records |
| Challenges & badges | `challenges` | on | available badge challenges, earned + available badges, **+ in-progress badges, in-progress virtual challenges, non-completed badge challenges** (whichever survive the Part 1 probe) |
| Profile & records | `profile` | on | user profile + settings, goals |
| Reproductive health | `reproductive` | **off** | menstrual calendar (+ per-date if not redundant), pregnancy summary |

Notes:
- Group the current per-date loop's endpoints into `wellness` vs `training`
  cleanly. `race_predictions` / `cycling_ftp` / `lactate_threshold` are current-
  state snapshots pulled during incremental sync; `training`'s `backfill` covers
  only the per-date `training_insight` history, not the snapshots.
- `challenges` and `profile` are snapshot-only: `backfill = None`.
- `body_composition` and `reproductive`'s menstrual calendar are range
  endpoints: they DO have a `backfill` (reuse `backfill_range_endpoint`'s
  chunk-shrinking pattern).
- Keep `activities` recommended-on. Do not force it always-on with special
  casing; just default it on.

## 3. Config + migrations infrastructure

### 3a. `sync_config` table (enabled domains live in the DB)

Add a `sync_config(key text primary key, value jsonb not null, updated_at
timestamptz default now())` table. Store the enabled-domain set under key
`enabled_domains` as a JSON array of domain keys. Add small helpers (in a new
`config.py` or in `domains.py`): `get_enabled_domains(conn) -> set[str]` and
`set_enabled_domains(conn, domains)`.

Because domains live in the DB that both the wizard and the Actions runners
already reach via `DATABASE_URL`, the workflows need **no** new secret for this.

### 3b. Migrations replace monolithic `init.sql`

- Create `schema/migrations/` with zero-padded, ordered, idempotent SQL files:
  `0001_baseline.sql`, `0002_...`, etc.
- `0001_baseline.sql` = the CURRENT contents of `schema/init.sql` verbatim
  (so a fresh DB ends up identical to today's schema), **plus** the new
  `sync_config` and `schema_migrations` tables.
- Each migration file starts with a header line `-- domain: <key>` (use
  `-- domain: base` for infra/shared tables like `auth_tokens`, `sync_config`,
  `activities` if you treat activities as base — but per the registry,
  `activities` is its own domain, so tag its objects `-- domain: activities`).
  New per-domain tables get that domain's tag (e.g. the reproductive tables get
  `-- domain: reproductive`).
- Add a `schema_migrations(version int primary key, name text, applied_at
  timestamptz default now())` tracker.
- Rewrite `apply_schema.py` into a migration runner. Public API:
  - `apply_migrations(url, enabled_domains: set[str] | None = None)` — creates
    the tracker if missing, then applies every unapplied migration whose
    `-- domain:` is `base` OR in `enabled_domains` (if `enabled_domains` is
    `None`, apply base only — used for the very first bootstrap before the
    user has chosen domains). Record each applied migration.
  - Keep a thin `apply_schema(url)` wrapper so nothing that imports it breaks;
    have it call `apply_migrations`.
- **Adoption of existing DBs (critical):** before applying anything, if
  `schema_migrations` does not exist but a known baseline table does (e.g.
  `activities`), create `schema_migrations` and mark `0001_baseline` as applied
  **without running it** (the objects already exist). Then infer the user's
  current `enabled_domains` from which tables/data are already present (e.g.
  `training_insight` populated → `training` on; `weigh_in` ever non-null →
  `body_composition` on; `challenges`/badges tables present → `challenges` on;
  `daily_metrics` wellness columns present → `wellness` on; `activities` present
  → `activities` on; `user_profile` present → `profile` on; `reproductive`
  tables absent → off) and write that to `sync_config`. This guarantees an
  existing user's very next sync behaves exactly as before.

Update the README's "Prefer to do it by hand?" section: `apply_schema.py` now
runs migrations; document `schema/migrations/`.

## 4. Make `pull.py` and `backfill.py` domain-aware

### `ingestion/pull.py`
- At startup, read `enabled_domains` from `sync_config`.
- Replace the flat sequence of pulls with a loop over enabled domains calling
  each domain's `sync_incremental(ctx)`. Move the existing pull bodies into the
  corresponding domain's `sync_incremental` (in `domains.py` or per-domain
  modules under `ingestion/domains/`), keeping the exact same upsert SQL.
- Keep the end-of-run summary table, but build its rows from the enabled
  domains / the tables they touched, so a disabled domain doesn't show up.

### `backfill.py`
- Read `enabled_domains` from `sync_config`. Also read a new optional env var
  `BACKFILL_DOMAINS` (comma-separated); if set, restrict this run to those
  domains intersected with the enabled set; if empty, backfill all enabled
  domains that have a `backfill` routine.
- Replace the inline activity/weigh-in/body-battery/per-date logic with a loop
  over the selected domains calling each domain's `backfill(ctx)`. Each
  domain's backfill keeps its own resumable skip logic — the current
  `already_backfilled()` becomes the `wellness`+`training` detector; snapshot
  domains have no backfill; range domains reuse `backfill_range_endpoint`.
- Keep `BACKFILL_START_DATE` behavior for the per-date domains.

Keep any shared upsert helper identical across both entry points, or factor it
into one module both import (preferred — see invariant 3).

## 5. `install.py`: checklist in setup + new `--upgrade` command

### 5a. Domain checklist during first-time setup
In `run_setup()`, after the schema step is reworked, present a
`questionary.checkbox()` grouped by `category`, pre-checked from each domain's
`default_enabled`. Persist the selection to `sync_config`, then call
`apply_migrations(url, enabled_domains=selected)` so only chosen domains' schema
is created. Add a non-interactive escape hatch: a `--domains a,b,c` flag and/or
`GARMIN_DOMAINS` env var that bypasses the prompt (for CI / unattended installs).
The initial backfill trigger should pass the selected domains through (see 5c).

### 5b. New `uv run install.py --upgrade`
Mirror `run_reauth()`'s shape (env handling, `_check_connection`, gh-optional
fallback with manual instructions). It must:
1. Load `DATABASE_URL` from `.env` (prompt if absent); check the connection.
2. Run the adoption/bootstrap from Part 3b if needed (so a pre-migrations DB is
   safely brought under management).
3. Read current `enabled_domains` and applied migrations from the DB.
4. Compute and show, in a `rich` panel, **what's new**:
   - domains available in `domains.py` but not enabled, and
   - domains already enabled that have pending (unapplied) migrations — i.e.
     new data types added *within* a domain the user already has (e.g. the new
     in-progress challenge tables for someone already on `challenges`).
5. Re-show the domain checklist (`questionary.checkbox`), pre-checked = current
   selection, so the user can add (or remove) domains.
6. Persist the new selection to `sync_config`.
7. `apply_migrations(url, enabled_domains=new_selection)` — applies only pending
   base + enabled-domain migrations. Idempotent; safe if nothing is pending.
8. Re-push GitHub secrets only if needed (NEON_DATABASE_URL unchanged → skip;
   no domain secret exists to push).
9. Trigger a **targeted backfill** for newly-enabled domains that have a
   `backfill` routine, by calling the backfill workflow with the new `domains`
   input (Part 6) set to just those domains. If the only newly-enabled domains
   are snapshot-only (e.g. added a challenge sub-type), skip the backfill and
   print that the next scheduled sync will populate them. Reuse
   `workflow_tools.trigger_backfill` (extend it to accept a `domains` arg) and
   its "already running" guard. If gh/PAT isn't configured, print manual
   Actions-tab instructions, same style as `_print_manual_secret_instructions`.
10. Print next steps in the existing panel style.

Add `--upgrade` (and `--domains`) to `argparse` and `main()`. Update the module
docstring's usage block to list all three modes (`setup`, `--reauth`,
`--upgrade`).

## 6. Workflow changes (minimal)

- `sync.yml`: no change needed — `pull.py` reads domains from the DB.
- `backfill.yml`: add an optional `domains` workflow_dispatch input
  (comma-separated, blank = all enabled), passed to `backfill.py` as
  `BACKFILL_DOMAINS`. Keep the existing `backfill_start_date` input.
- `workflow_tools.py`: extend `trigger_backfill` to accept an optional
  `domains` argument and pass it through as the workflow input alongside
  `backfill_start_date`.

## 7. Docs

- `docs/garmin_api_coverage.md`: add a short "Data domains" section explaining
  the registry and which methods belong to which domain; keep the
  ingested/duplicates/not-used tables accurate after the new endpoints land.
- `README.md`: document (a) the domain checklist at install, (b) the new
  `--upgrade` flow for existing users pulling in new functionality gradually,
  (c) that `apply_schema.py` now runs migrations, (d) `reproductive` being
  default-off and meant for accounts that track it.

## 8. Acceptance criteria (test these before you're done)

Write a short `scripts/smoke_test.py` (or a section in the README) exercising,
against a scratch Neon DB, that:

1. **Fresh install**, all defaults → base + default-on domains' tables exist;
   `reproductive` tables do NOT; `sync_config.enabled_domains` matches;
   `schema_migrations` lists the applied set.
2. **Existing-install adoption**: seed a DB with the OLD `init.sql` and some
   rows, no `schema_migrations`. Running `--upgrade` stamps the baseline
   without re-creating anything, infers enabled domains from existing data, and
   a subsequent `pull.py` run pulls the same domains as before (no behavior
   change, no errors).
3. **Upgrade adding a domain**: enable `reproductive` via `--upgrade` → its
   migrations apply, its tables appear, `enabled_domains` updates, and a
   targeted backfill is triggered only for it (mock the workflow trigger).
4. **Intra-domain addition**: a DB already on `challenges` gets the new
   in-progress tables via pending migrations on `--upgrade`, and `pull.py` then
   populates them.
5. **Idempotency**: running `apply_migrations` and `--upgrade` twice is a
   no-op the second time.
6. **Disabled domain is not pulled**: with `wellness` disabled, `pull.py` makes
   no wellness calls and writes no wellness columns.

## 9. Suggested commit sequence

1. Probe script + coverage-doc updates from real shapes (Part 1).
2. Migrations infra: `schema/migrations/0001_baseline.sql`, `schema_migrations`,
   `sync_config`, rewritten `apply_schema.py`, adoption logic (Part 3).
3. `domains.py` registry mapping current pulls into domains, no behavior change
   (Part 2 + move existing pull bodies).
4. Domain-aware `pull.py` and `backfill.py` (Part 4).
5. New endpoints as migrations + domain wiring (challenges in-progress,
   reproductive), gated on probe results (Parts 1–2).
6. `install.py` checklist + `--upgrade`; `workflow_tools`/`backfill.yml`
   `domains` input (Parts 5–6).
7. Docs + smoke test (Parts 7–8).

Ask before doing anything destructive to a real database. Keep every step
re-runnable.
