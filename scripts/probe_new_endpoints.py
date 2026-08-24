# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "python-dotenv>=1.0",
#     "rich>=13.0",
# ]
# ///
"""
Throwaway probe script — prints real JSON shapes for Garmin endpoints not
yet wired into schema, so a human can paste the output back before any
schema gets written for them. Read-only against Garmin; never writes to
Postgres (only reads the stored token to log in).

Run this against the account each group needs:
  - "challenges": the primary account (any account with in-progress
    badges/challenges works).
  - "reproductive": an account that actually tracks menstrual/pregnancy
    data — NOT the primary account unless it happens to track it.

Usage:
    DATABASE_URL="postgresql://..." uv run scripts/probe_new_endpoints.py --group challenges
    DATABASE_URL="postgresql://..." uv run scripts/probe_new_endpoints.py --group reproductive
    DATABASE_URL="postgresql://..." uv run scripts/probe_new_endpoints.py --group reproductive \\
        --start 2026-06-01 --end 2026-08-24 --date 2026-08-01
"""
import argparse
import json
import os
import pathlib
import sys
from datetime import date, timedelta

import psycopg
from dotenv import load_dotenv
from garminconnect import Garmin
from rich.console import Console
from rich.panel import Panel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bootstrap.garmin_auth import TOKEN_DIR, load_token_from_db

load_dotenv()

console = Console()


def _print_result(label: str, method: str, value) -> None:
    console.print(Panel.fit(f"[bold]{method}[/bold]", title=label, border_style="cyan"))
    try:
        console.print_json(json.dumps(value, default=str))
    except Exception as e:
        console.print(f"[red]Couldn't serialize result: {e}[/red]")
        console.print(repr(value))
    if isinstance(value, list):
        console.print(f"[dim]-> {len(value)} item(s)[/dim]\n")
    else:
        console.print()


def probe_challenges(client: Garmin) -> None:
    console.print(Panel.fit(
        "Checking whether these are genuinely new lists, or filtered slices "
        "of get_available_badge_challenges / get_available_badges / "
        "get_earned_badges (already stored in `challenges` / "
        "`available_badges` / `earned_badges`). Compare the ids/uuids in "
        "each result against what's already in those tables before wiring "
        "any of these into schema.",
        title="challenges group", border_style="yellow",
    ))
    _print_result("In-progress badges", "get_in_progress_badges", client.get_in_progress_badges())
    _print_result(
        "In-progress virtual challenges", "get_inprogress_virtual_challenges",
        client.get_inprogress_virtual_challenges(),
    )
    _print_result(
        "Non-completed badge challenges", "get_non_completed_badge_challenges",
        client.get_non_completed_badge_challenges(),
    )
    # For comparison — what's already stored, to check for overlap.
    _print_result("(reference) available badge challenges", "get_available_badge_challenges(1, 100)",
                  client.get_available_badge_challenges(1, 100))
    _print_result("(reference) available badges", "get_available_badges", client.get_available_badges())
    _print_result("(reference) earned badges", "get_earned_badges", client.get_earned_badges())


def probe_reproductive(client: Garmin, start: str, end: str, single_date: str) -> None:
    console.print(Panel.fit(
        "Run this against an account that actually tracks menstrual/pregnancy "
        "data. Check: does the calendar range endpoint already carry "
        "per-date entries (making get_menstrual_data_for_date redundant, "
        "like get_daily_steps vs get_steps_data)? What's the per-date "
        "natural key? Is pregnancy a single snapshot with no natural list "
        "key (store like performance_snapshots)?",
        title="reproductive group", border_style="yellow",
    ))
    _print_result(
        "Menstrual calendar (range)", f"get_menstrual_calendar_data({start!r}, {end!r})",
        client.get_menstrual_calendar_data(start, end),
    )
    _print_result(
        "Menstrual data for one date", f"get_menstrual_data_for_date({single_date!r})",
        client.get_menstrual_data_for_date(single_date),
    )
    _print_result("Pregnancy summary", "get_pregnancy_summary", client.get_pregnancy_summary())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["challenges", "reproductive", "all"], default="all")
    today = date.today()
    parser.add_argument("--start", default=(today - timedelta(days=60)).isoformat())
    parser.add_argument("--end", default=today.isoformat())
    parser.add_argument("--date", default=today.isoformat())
    args = parser.parse_args()

    db_url = os.environ["DATABASE_URL"]
    with psycopg.connect(db_url) as conn:
        load_token_from_db(conn)

    client = Garmin()
    client.login(tokenstore=str(TOKEN_DIR))

    if args.group in ("challenges", "all"):
        probe_challenges(client)
    if args.group in ("reproductive", "all"):
        probe_reproductive(client, args.start, args.end, args.date)

    console.print(Panel.fit(
        "Paste the output above (or a summary of the shapes + which "
        "duplicates you found) back to continue Part 1 of the domains "
        "plan.", border_style="green",
    ))


if __name__ == "__main__":
    main()
