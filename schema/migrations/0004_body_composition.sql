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
