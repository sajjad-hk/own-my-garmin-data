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
