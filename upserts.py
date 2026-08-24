"""
Upsert helpers shared by ingestion/pull.py (incremental) and backfill.py
(full-history) — kept in one place so the two entry points can't drift
apart on SQL. Only depends on psycopg, so it's safe to import from the
bare GitHub Actions runner (see ingestion/requirements.txt) and from
domains.py, which both pull.py and backfill.py load.
"""
import json
from datetime import date

import psycopg

DAILY_METRICS_COLUMNS = [
    "stats", "sleep", "stress", "hrv", "max_metrics",
    "respiration", "spo2", "body_battery", "body_battery_events",
    "intensity_minutes", "floors", "steps_intraday", "heart_rates",
    "day_events", "weigh_in",
]

TRAINING_INSIGHT_COLUMNS = [
    "training_status", "training_readiness", "morning_training_readiness",
    "endurance_score", "hill_score", "fitness_age", "running_tolerance",
]


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


def upsert_daily_metrics(conn: psycopg.Connection, d: date, fields: dict) -> None:
    # coalesce(excluded.x, daily_metrics.x): a column not passed this call
    # (fields.get(...) is None) keeps whatever was already stored, rather
    # than nulling it out.
    #
    # The column list is built from fields.keys() (validated below against
    # DAILY_METRICS_COLUMNS, the whitelist of every known daily_metrics
    # column) rather than the full constant, because daily_metrics is
    # split across two migrations (0003_wellness.sql,
    # 0004_body_composition.sql) — an install that only enabled one of
    # those two domains doesn't have every column in DAILY_METRICS_COLUMNS,
    # so unconditionally referencing all of them would break with
    # UndefinedColumn on a fresh subset install. Referencing only the keys
    # the caller actually passed means each call only ever touches columns
    # owned by the domain that's calling it.
    if not fields:
        return
    unknown = set(fields) - set(DAILY_METRICS_COLUMNS)
    if unknown:
        raise ValueError(f"upsert_daily_metrics got unknown field(s): {sorted(unknown)}")
    cols = list(fields.keys())
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
    cols = TRAINING_INSIGHT_COLUMNS
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
