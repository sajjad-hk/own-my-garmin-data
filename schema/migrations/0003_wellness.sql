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
