# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Runs on every scheduled GitHub Actions execution.
Assumes a Garmin token was already generated once via bootstrap/generate_token.py
and pushed to the auth_tokens table via bootstrap/load_token_to_db.py.

Each run:
  1. Pulls the current token from Postgres, writes it to the expected local path.
  2. Logs in to Garmin using that token (no password needed — resumes session).
  3. Pulls activities, daily wellness metrics, and training insight for a
     lookback window (not just "today"), so a missed run doesn't leave a
     permanent gap. Also refreshes challenges, the full badges list, and
     the snapshot-style tables (performance snapshots, personal records,
     goals, user profile/settings) that only ever reflect current state,
     not history.
  4. Upserts everything into the normalized tables.
  5. Reads back the (possibly refreshed) token files and saves them to Postgres,
     so the next ephemeral runner picks up the latest one.

For pulling FULL history (years of past data), use backfill.py instead —
this script is deliberately a small, cheap, incremental sync.
"""

import os
import json
import pathlib
import sys
import time
from datetime import date, timedelta

import psycopg
from dotenv import load_dotenv
from garminconnect import Garmin

# `python ingestion/pull.py` puts ingestion/ (this file's own directory) on
# sys.path[0], not the repo root — so bootstrap/ isn't importable without
# this explicit insert.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db, save_token_to_db

load_dotenv()  # reads .env in the current directory into os.environ, if present

DB_URL = os.environ["DATABASE_URL"]

# How many days back to re-check on every run. Covers gaps if a scheduled
# run is skipped (e.g. runner outage) without re-pulling everything.
LOOKBACK_DAYS = 5


def table_count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608 (fixed table names, not user input)


def write_step_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text)
    else:
        print(text)  # local run — no GITHUB_STEP_SUMMARY file, just print instead


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


def upsert_challenges(conn: psycopg.Connection, challenges: list[dict]) -> None:
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


def upsert_earned_badges(conn: psycopg.Connection, badges: list[dict]) -> None:
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


def upsert_available_badges(conn: psycopg.Connection, badges: list[dict]) -> None:
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


DAILY_METRICS_COLUMNS = [
    "stats", "sleep", "stress", "hrv", "max_metrics",
    "respiration", "spo2", "body_battery", "body_battery_events",
    "intensity_minutes", "floors", "steps_intraday", "heart_rates",
    "day_events", "weigh_in",
]


def upsert_daily_metrics(conn: psycopg.Connection, d: date, fields: dict) -> None:
    # coalesce(excluded.x, daily_metrics.x): a column not passed this call
    # (fields.get(...) is None) keeps whatever was already stored, rather
    # than nulling it out. Every call site must pass the *same* set of keys
    # for this to stay predictable — see backfill.py's identical function.
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


def upsert_user_profile(conn: psycopg.Connection, profile: dict, settings: dict) -> None:
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


def upsert_performance_snapshots(conn: psycopg.Connection, snapshots: dict) -> None:
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


def upsert_personal_records(conn: psycopg.Connection, records: list[dict]) -> None:
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


def replace_goals(conn: psycopg.Connection, status: str, goals: list[dict]) -> None:
    # No verified id field exists for goal records (this account has none
    # in any status to check against — see schema/init.sql). Full-replace
    # per status instead of guessing a primary key to upsert on.
    conn.execute("delete from goals where status = %s", (status,))
    for g in goals:
        conn.execute(
            "insert into goals (status, raw, updated_at) values (%s, %s, now())",
            (status, json.dumps(g)),
        )
    conn.commit()


def upsert_body_battery(conn: psycopg.Connection, days: list[dict]) -> None:
    for entry in days:
        upsert_daily_metrics(conn, date.fromisoformat(entry["date"]), {"body_battery": entry})


def upsert_weigh_ins(conn: psycopg.Connection, weigh_in_response: dict) -> None:
    for summary in weigh_in_response.get("dailyWeightSummaries", []):
        d = date.fromisoformat(summary["summaryDate"])
        upsert_daily_metrics(conn, d, {"weigh_in": summary})


def main() -> None:
    with psycopg.connect(DB_URL) as conn:
        load_token_from_db(conn)

        client = Garmin()
        client.login(tokenstore=str(TOKEN_DIR))

        today = date.today()
        tables = [
            "activities", "challenges", "earned_badges", "available_badges",
            "daily_metrics", "training_insight", "performance_snapshots",
            "personal_records", "goals", "user_profile",
        ]
        before = {t: table_count(conn, t) for t in tables}

        window_start = today - timedelta(days=LOOKBACK_DAYS)

        # Activities: date-range pagination, not offset-based — offset-based
        # (start=0, limit=N) only ever returns the N most recent, which is
        # what silently dropped history before.
        activities = client.get_activities_by_date(
            window_start.isoformat(), today.isoformat(), sortorder="asc"
        )
        upsert_activities(conn, activities)

        challenges = client.get_available_badge_challenges(1, 100)
        upsert_challenges(conn, challenges)

        earned_badges = client.get_earned_badges()
        upsert_earned_badges(conn, earned_badges)

        available_badges = client.get_available_badges()
        upsert_available_badges(conn, available_badges)

        # Account-level profile/settings — essentially static, refreshed in
        # full every run, same idea as challenges/badges above.
        user_profile = client.get_user_profile()
        user_settings = client.get_userprofile_settings()
        upsert_user_profile(conn, user_profile, user_settings)

        # Range endpoints that return a list keyed by date — fetched once
        # for the whole lookback window rather than looped per date.
        body_battery_days = client.get_body_battery(window_start.isoformat(), today.isoformat())
        upsert_body_battery(conn, body_battery_days)

        weigh_ins = client.get_weigh_ins(window_start.isoformat(), today.isoformat())
        upsert_weigh_ins(conn, weigh_ins)

        # Single-object "current state" snapshots — no history, refreshed
        # in full every run, same idea as challenges/badges above.
        performance_snapshots = {
            "race_predictions": client.get_race_predictions(),
            "cycling_ftp": client.get_cycling_ftp(),
            "lactate_threshold": client.get_lactate_threshold(),
        }
        upsert_performance_snapshots(conn, performance_snapshots)

        personal_records = client.get_personal_record()
        upsert_personal_records(conn, personal_records)

        goals_by_status = {}
        for status in ("active", "future", "past"):
            goals_by_status[status] = client.get_goals(status=status)
            replace_goals(conn, status, goals_by_status[status])
            time.sleep(1)

        # Daily metrics are per-date endpoints — loop the lookback window.
        for offset in range(LOOKBACK_DAYS + 1):
            d = window_start + timedelta(days=offset)
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

            time.sleep(1)  # be gentle — many calls per day, several days

        save_token_to_db(conn)

        after = {t: table_count(conn, t) for t in tables}

    api_counts = {
        "activities": len(activities),
        "challenges": len(challenges),
        "earned_badges": len(earned_badges),
        "available_badges": len(available_badges),
        "personal_records": len(personal_records),
        "goals": sum(len(g) for g in goals_by_status.values()),
        "performance_snapshots": len(performance_snapshots),
        "user_profile": 1,
    }

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

    write_step_summary(summary)


if __name__ == "__main__":
    main()