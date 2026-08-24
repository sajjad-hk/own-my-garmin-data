-- domain: activities
-- Raw activity payloads, keyed by Garmin's own activity id.
create table if not exists activities (
    activity_id bigint primary key,
    raw         jsonb not null,
    started_at  timestamptz
);
