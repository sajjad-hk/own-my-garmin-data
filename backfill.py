# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
# ]
# ///
"""
Run this ONCE, locally, to backfill history that pull.py's incremental
lookback window doesn't cover. Safe to re-run — it skips dates whose new
fields are already populated (see already_backfilled), so an interrupted
run can just be restarted.

Usage:
    DATABASE_URL="postgresql://..." uv run backfill.py

Optional env vars:
    BACKFILL_START_DATE   YYYY-MM-DD. Defaults to 2 years ago.
                          Activities, weigh-ins, and body battery always
                          backfill fully regardless of this (Garmin returns
                          those as ranges, it's cheap). This setting only
                          controls how far back the per-day loop goes —
                          ~20 API calls per day now (wellness + training
                          insight, not just stats/sleep/stress/hrv/VO2max),
                          so 2 years means ~14,600 requests. Expect this to
                          take a few hours; it's safe to stop and resume.
"""

import os
import json
import time
from datetime import date, timedelta

import psycopg
from garminconnect import Garmin

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db, save_token_to_db

DB_URL = os.environ["DATABASE_URL"]

DEFAULT_START = (date.today() - timedelta(days=365 * 2)).isoformat()
# `or` (not .get(key, default)) so an empty-string env var — e.g. a blank
# workflow_dispatch input from backfill.yml — also falls through to the
# default, instead of hitting date.fromisoformat("").
START_DATE = os.environ.get("BACKFILL_START_DATE") or DEFAULT_START

# Very early date — Garmin accounts don't predate this, so this safely
# captures "everything" without needing to know the real account creation date.
ACTIVITY_HISTORY_START = "2000-01-01"


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


def already_backfilled(conn: psycopg.Connection, d: date) -> bool:
    # respiration and the training_insight row are always written together
    # as a full batch (never partially, unlike weigh_in which is only set
    # on days with an actual scale reading) — so their presence is a
    # reliable "this date's new fields were already fetched" marker.
    row = conn.execute(
        """
        select dm.stats is not null and dm.respiration is not null and ti.metric_date is not null
        from daily_metrics dm
        left join training_insight ti on ti.metric_date = dm.metric_date
        where dm.metric_date = %s
        """,
        (d,),
    ).fetchone()
    return bool(row and row[0])


DAILY_METRICS_COLUMNS = [
    "stats", "sleep", "stress", "hrv", "max_metrics",
    "respiration", "spo2", "body_battery", "body_battery_events",
    "intensity_minutes", "floors", "steps_intraday", "heart_rates",
    "day_events", "weigh_in",
]


def upsert_daily_metrics(conn: psycopg.Connection, d: date, fields: dict) -> None:
    # Identical to ingestion/pull.py's version — keep the two in lockstep.
    # coalesce(excluded.x, daily_metrics.x): a column not passed this call
    # keeps whatever was already stored, rather than nulling it out.
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
    # Identical to ingestion/pull.py's version — keep the two in lockstep.
    cols = [
        "training_status", "training_readiness", "morning_training_readiness",
        "endurance_score", "hill_score", "fitness_age", "running_tolerance",
    ]
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


# Chunk size for range endpoints (weigh-ins, body battery) during backfill —
# keeps individual requests small rather than asking for years in one call.
RANGE_CHUNK_DAYS = 90


def backfill_range_endpoint(client, start: date, today: date, label: str, fetch_and_upsert) -> None:
    print(f"Backfilling {label} from {start} to {today}...")
    chunk_start = start
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=RANGE_CHUNK_DAYS - 1), today)
        fetch_and_upsert(client, chunk_start.isoformat(), chunk_end.isoformat())
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(1)
    print(f"  -> {label} done.")


def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        load_token_from_db(conn)

        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))

        print(f"Backfilling all activities since {ACTIVITY_HISTORY_START}...")
        activities = client.get_activities_by_date(
            ACTIVITY_HISTORY_START, date.today().isoformat(), sortorder="asc"
        )
        upsert_activities(conn, activities)
        print(f"  -> {len(activities)} activities upserted.")

        start = date.fromisoformat(START_DATE)
        today = date.today()

        backfill_range_endpoint(
            client, start, today, "weigh-ins",
            lambda c, s, e: upsert_weigh_ins(conn, c.get_weigh_ins(s, e)),
        )
        backfill_range_endpoint(
            client, start, today, "body battery",
            lambda c, s, e: upsert_body_battery(conn, c.get_body_battery(s, e)),
        )

        total_days = (today - start).days + 1
        # 20 calls/date (5 wellness already tracked + 8 new wellness + 7
        # training) — see docs/garmin_api_coverage.md for the full list.
        print(
            f"Backfilling daily metrics + training insight from {start} to {today} "
            f"({total_days} days, ~20 API calls/day)..."
        )

        done = 0
        skipped = 0
        for offset in range(total_days):
            d = start + timedelta(days=offset)

            if already_backfilled(conn, d):
                skipped += 1
                continue

            iso = d.isoformat()

            upsert_daily_metrics(conn, d, {
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
            })

            upsert_training_insight(conn, d, {
                "training_status": client.get_training_status(iso),
                "training_readiness": client.get_training_readiness(iso),
                "morning_training_readiness": client.get_morning_training_readiness(iso),
                "endurance_score": client.get_endurance_score(iso),
                "hill_score": client.get_hill_score(iso),
                "fitness_age": client.get_fitnessage_data(iso),
                "running_tolerance": client.get_running_tolerance(iso, iso, aggregation="daily"),
            })

            done += 1
            if done % 20 == 0:
                print(f"  ...{done} days pulled, {skipped} already present, at {iso}")
                save_token_to_db(conn)  # checkpoint token periodically on long runs
            time.sleep(1)

        save_token_to_db(conn)
        print(f"Done. {done} days pulled, {skipped} already present.")


if __name__ == "__main__":
    main()
