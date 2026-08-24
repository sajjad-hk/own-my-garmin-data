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
