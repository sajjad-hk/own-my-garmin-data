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
