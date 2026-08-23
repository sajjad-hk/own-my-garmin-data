-- Token storage: lets the ephemeral GitHub Actions runner resume
-- a Garmin session without ever storing a password.
create table if not exists auth_tokens (
    provider   text primary key,
    payload    jsonb not null,
    updated_at timestamptz not null default now()
);

-- Raw activity payloads, keyed by Garmin's own activity id.
create table if not exists activities (
    activity_id bigint primary key,
    raw         jsonb not null,
    started_at  timestamptz
);

-- Raw challenge payloads, refreshed on every sync.
-- Garmin keys challenges by a string uuid, not a numeric id.
create table if not exists challenges (
    challenge_id text primary key,
    raw          jsonb not null,
    updated_at   timestamptz not null default now()
);

-- One row per calendar date, holding whichever daily endpoints we pulled.
-- Columns are nullable because backfill/incremental runs may populate them
-- at different times (e.g. stats today, sleep backfilled later). weigh_in
-- stays null forever on days with no scale reading — that's expected, not
-- a sign the row wasn't processed.
create table if not exists daily_metrics (
    metric_date          date primary key,
    stats                jsonb,
    sleep                jsonb,
    stress               jsonb,
    hrv                  jsonb,
    max_metrics          jsonb,
    respiration          jsonb,
    spo2                 jsonb,
    body_battery         jsonb,
    body_battery_events  jsonb,
    intensity_minutes    jsonb,
    floors               jsonb,
    steps_intraday       jsonb,
    heart_rates          jsonb,
    day_events           jsonb,
    weigh_in             jsonb,
    updated_at           timestamptz not null default now()
);

-- The columns above cover a fresh installation. Existing (already-provisioned)
-- databases need these added explicitly, since `create table if not exists`
-- is a no-op once the table exists.
alter table daily_metrics add column if not exists respiration jsonb;
alter table daily_metrics add column if not exists spo2 jsonb;
alter table daily_metrics add column if not exists body_battery jsonb;
alter table daily_metrics add column if not exists body_battery_events jsonb;
alter table daily_metrics add column if not exists intensity_minutes jsonb;
alter table daily_metrics add column if not exists floors jsonb;
alter table daily_metrics add column if not exists steps_intraday jsonb;
alter table daily_metrics add column if not exists heart_rates jsonb;
alter table daily_metrics add column if not exists day_events jsonb;
alter table daily_metrics add column if not exists weigh_in jsonb;

-- Garmin's derived coaching/performance metrics, one row per calendar date.
-- Kept separate from daily_metrics (wellness) since it's a distinct
-- analytical domain — training load/readiness rather than raw health data.
-- running_tolerance is fetched as a single-day window so it stores an
-- empty array on days/accounts with no data, same as the others below.
create table if not exists training_insight (
    metric_date                 date primary key,
    training_status             jsonb,
    training_readiness          jsonb,
    morning_training_readiness  jsonb,
    endurance_score              jsonb,
    hill_score                   jsonb,
    fitness_age                  jsonb,
    running_tolerance            jsonb,
    updated_at                   timestamptz not null default now()
);

-- Single-object "current state" endpoints that have no natural list/id
-- (race predictions, cycling FTP, lactate threshold) — one row per metric,
-- refreshed in full on every sync. Same idea as challenges/badges but for
-- endpoints that return one object instead of a list.
create table if not exists performance_snapshots (
    metric_name text primary key,
    raw         jsonb not null,
    updated_at  timestamptz not null default now()
);

-- Personal records. Garmin keys these by a numeric "id" field (verified
-- against a live response), same pattern as challenges/badges.
create table if not exists personal_records (
    record_id  bigint primary key,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);

-- Goals. NOTE: this account currently has zero goals in any status
-- (active/future/past), so the live API response's natural id field could
-- not be verified — unlike challenges/badges, do not assume one. Refreshed
-- by delete-then-insert per status instead of upsert-by-id. If you later
-- see real goal data, check for a stable id field and switch this to an
-- upsert like personal_records, using a real primary key instead of the
-- surrogate `id` below.
create table if not exists goals (
    id         bigserial primary key,
    status     text not null,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);

-- Badges the account has already earned.
create table if not exists earned_badges (
    badge_id   bigint primary key,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);

-- Full badge catalog — includes badges not yet earned, each carrying
-- badgeProgressValue / badgeTargetValue, which is what tells you
-- how close you are to earning it. This is the table the LLM should
-- query to find "achievable soon" badges.
create table if not exists available_badges (
    badge_id   bigint primary key,
    raw        jsonb not null,
    updated_at timestamptz not null default now()
);

-- Account-level profile/settings — essentially static, changes rarely.
-- This is what gives raw metrics (resting HR, VO2max, etc.) a personal
-- baseline to be interpreted against. Singleton row, refreshed in full
-- every run, same idea as performance_snapshots.
--
-- get_user_profile and get_userprofile_settings return meaningfully
-- different things (verified live, both confirmed against a real
-- response): `profile` carries physiological baseline data (weight,
-- height, birthDate, gender, vo2MaxRunning/Cycling, lactateThreshold*,
-- HR-zone auto-detection flags) under a `userData` key, while `settings`
-- is purely locale/unit/display preferences (timeZone, measurementSystem,
-- date/number/hydration formats) with no physiological content. Neither
-- response contains an explicit maxHR field or an HR-zone-boundaries
-- array — lactateThresholdHeartRate and vo2MaxRunning are the closest
-- baseline reference points actually present.
create table if not exists user_profile (
    id         int primary key default 1,  -- singleton row, one person's account
    profile    jsonb not null,             -- get_user_profile
    settings   jsonb not null,             -- get_userprofile_settings
    updated_at timestamptz not null default now(),
    constraint user_profile_singleton check (id = 1)
);

