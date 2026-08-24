# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "rich>=13.0",
#     "questionary>=2.0",
#     "requests>=2.31",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Guided first-time setup (and re-auth) for garmin-data.

    uv run install.py            # full first-time setup
    uv run install.py --reauth   # just re-log-in to Garmin and push the
                                  # refreshed token to the DB (e.g. after a
                                  # Garmin password change) — no schema/
                                  # secrets/backfill steps.

Everything here is local orchestration of steps you could also do by hand
(see README.md) — it doesn't introduce any new credential storage.
"""

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import psycopg
import questionary
from rich.console import Console
from rich.panel import Panel

from apply_schema import apply_schema, apply_migrations, applied_versions, list_migrations
from bootstrap.garmin_auth import interactive_login, save_token_to_db
from config import get_enabled_domains, set_enabled_domains
from domains import DOMAINS, DOMAINS_BY_KEY, Domain

console = Console()

ENV_PATH = pathlib.Path(__file__).resolve().parent / ".env"

_POSTGRES_URL_RE = re.compile(r"^postgres(?:ql)?://")


def _read_existing_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    values = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def _write_env(values: dict[str, str]) -> None:
    existing = _read_existing_env()
    existing.update(values)
    lines = [f'{k}="{v}"' for k, v in existing.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    for k, v in values.items():
        os.environ[k] = v


def _prompt_db_url(label: str, env_key: str, existing: dict[str, str]) -> str:
    default = existing.get(env_key, "")
    while True:
        value = questionary.password(f"{label} (postgresql://...):", default=default).ask()
        if value is None:
            console.print("[red]Setup cancelled.[/red]")
            sys.exit(1)
        value = value.strip()
        if _POSTGRES_URL_RE.match(value):
            return value
        console.print("[red]That doesn't look like a postgres connection string — try again.[/red]")


def _check_connection(url: str, label: str) -> None:
    try:
        with psycopg.connect(url, connect_timeout=10) as conn:
            conn.execute("select 1")
    except Exception as e:
        console.print(Panel(f"Couldn't connect using {label}: {e}", title="✗ Connection failed", border_style="red"))
        sys.exit(1)
    console.print(f"[green]✓[/green] Connected using {label}.")


def _detect_github_repo() -> str | None:
    if shutil.which("gh"):
        try:
            out = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            import json as _json
            return _json.loads(out.stdout)["nameWithOwner"]
        except Exception:
            pass
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10, check=True,
        )
        url = out.stdout.strip()
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _gh_available() -> tuple[bool, str]:
    if not shutil.which("gh"):
        return False, (
            "The `gh` CLI isn't installed — can't set repo secrets or trigger the "
            "backfill automatically. Install it (https://cli.github.com), run "
            "`gh auth login`, then re-run this wizard — or follow the manual "
            "instructions below."
        )
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return False, (
            "`gh` is installed but not authenticated — run `gh auth login`, then "
            "re-run this wizard. Or follow the manual instructions below."
        )
    return True, ""


def _set_gh_secret(repo: str, name: str, value: str) -> None:
    subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo, "--body", value],
        check=True, capture_output=True, text=True,
    )


def _print_manual_secret_instructions(database_url: str) -> None:
    console.print(Panel(
        "Set this repo secret by hand:\n\n"
        "  1. On GitHub, go to your repo → [bold]Settings[/bold] → "
        "[bold]Secrets and variables[/bold] → [bold]Actions[/bold] → "
        "[bold]New repository secret[/bold]\n"
        f"  2. Name: [bold]NEON_DATABASE_URL[/bold]\n"
        f"  3. Value: {database_url}\n\n"
        "Then trigger the initial backfill by hand: [bold]Actions[/bold] tab → "
        "[bold]garmin-backfill[/bold] → [bold]Run workflow[/bold].",
        title="Manual GitHub Actions setup", border_style="yellow",
    ))


def _parse_domains_arg(raw: str) -> set[str]:
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = keys - DOMAINS_BY_KEY.keys()
    if unknown:
        console.print(
            f"[red]Unknown domain(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(d.key for d in DOMAINS)}[/red]"
        )
        sys.exit(1)
    return keys


def _prompt_domain_checklist(preselected: set[str]) -> set[str]:
    choices = []
    last_category = None
    for d in DOMAINS:
        if d.category != last_category:
            choices.append(questionary.Separator(f"-- {d.category} --"))
            last_category = d.category
        choices.append(questionary.Choice(
            title=f"{d.label} — {d.description}",
            value=d.key,
            checked=d.key in preselected,
        ))
    selected = questionary.checkbox("Which data domains should this sync?", choices=choices).ask()
    if selected is None:
        console.print("[red]Setup cancelled.[/red]")
        sys.exit(1)
    return set(selected)


def _check_requirements() -> None:
    """Show a pass/warn checklist for the tools this wizard shells out to,
    and let the user bail out *before* spending time on DB setup + Garmin
    login if something's missing — rather than discovering it only once the
    wizard reaches the GitHub step."""
    console.print("[bold]Checking requirements...[/bold]\n")

    warnings: list[str] = []

    if shutil.which("git"):
        console.print("  [green]✓[/green] git   found")
    else:
        console.print(
            "  [yellow]![/yellow] git   not found — you'll need to type your GitHub "
            "repo (owner/repo) manually instead of it being auto-detected"
        )
        warnings.append("git is not installed")

    if not shutil.which("gh"):
        console.print(
            "  [red]✗[/red] gh    not installed — can't set GitHub secrets or "
            "trigger the backfill automatically (install: https://cli.github.com)"
        )
        warnings.append("gh CLI is not installed")
    else:
        try:
            auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
        except Exception:
            auth = None
        if auth is not None and auth.returncode == 0:
            console.print("  [green]✓[/green] gh    found and authenticated")
        else:
            console.print("  [yellow]![/yellow] gh    found, but not authenticated — run `gh auth login`")
            warnings.append("gh is installed but not authenticated (run `gh auth login`)")

    console.print()

    if not warnings:
        console.print("[green]All requirements satisfied.[/green]\n")
        return

    console.print(Panel(
        "Missing:\n" + "\n".join(f"  • {w}" for w in warnings) + "\n\n"
        "You can still continue — the Neon/Garmin steps below work regardless. "
        "The GitHub Actions secrets + automatic backfill trigger step will be "
        "skipped (with manual instructions) until these are fixed.",
        title="!", border_style="yellow",
    ))
    if not questionary.confirm("Continue anyway?", default=True).ask():
        console.print("[yellow]Stopped — fix the above and re-run `uv run install.py`.[/yellow]")
        sys.exit(0)
    console.print()


def run_setup(domains_override: set[str] | None = None) -> None:
    console.print(Panel.fit(
        "[bold]garmin-data setup[/bold]\n\n"
        "This will: connect to your Neon database, log you in to Garmin, "
        "apply the schema, push your GitHub Actions secrets, and trigger "
        "the initial history backfill to run on GitHub's servers (not this "
        "machine).",
        border_style="cyan",
    ))

    _check_requirements()

    existing = _read_existing_env()

    database_url = _prompt_db_url("Neon connection string (DATABASE_URL)", "DATABASE_URL", existing)
    _check_connection(database_url, "DATABASE_URL")

    console.print("\n[bold]Applying base schema...[/bold]")
    apply_schema(database_url)

    console.print("\n[bold]Choose data domains[/bold]")
    preselected = {d.key for d in DOMAINS if d.default_enabled}
    selected_domains = domains_override if domains_override is not None else _prompt_domain_checklist(preselected)

    with psycopg.connect(database_url) as conn:
        set_enabled_domains(conn, selected_domains)

    applied = apply_migrations(database_url, enabled_domains=selected_domains)
    console.print(f"[green]✓[/green] Schema applied ({len(applied)} migration(s)).")

    console.print("\n[bold]Garmin login[/bold]")
    interactive_login()
    with psycopg.connect(database_url) as conn:
        save_token_to_db(conn)
    console.print("[green]✓[/green] Token saved to database.")

    env_updates = {"DATABASE_URL": database_url}

    console.print("\n[bold]GitHub Actions secrets + backfill trigger[/bold]")
    gh_ok, gh_message = _gh_available()
    if not gh_ok:
        console.print(f"[yellow]![/yellow] {gh_message}")
        _write_env(env_updates)
        _print_manual_secret_instructions(database_url)
        _print_manual_next_steps()
        return

    repo = _detect_github_repo()
    if not repo:
        repo = questionary.text("GitHub repo (owner/repo) this is pushed to:").ask()
    if not repo:
        console.print("[yellow]![/yellow] No repo detected — skipping secrets + backfill trigger.")
        _write_env(env_updates)
        _print_manual_secret_instructions(database_url)
        _print_manual_next_steps()
        return

    _set_gh_secret(repo, "NEON_DATABASE_URL", database_url)
    console.print("[green]✓[/green] Set NEON_DATABASE_URL secret.")

    ntfy_topic = questionary.text(
        "Optional: ntfy.sh topic for failure notifications (blank to skip):"
    ).ask() or ""
    if ntfy_topic.strip():
        _set_gh_secret(repo, "NTFY_TOPIC", ntfy_topic.strip())
        console.print("[green]✓[/green] Set NTFY_TOPIC secret.")

    github_token = questionary.password(
        "Fine-grained GitHub PAT (Actions: Read and write, scoped to this repo only) "
        "— needed so this wizard can trigger workflow runs:"
    ).ask()
    if github_token and github_token.strip():
        env_updates["GITHUB_TOKEN"] = github_token.strip()
        env_updates["GITHUB_REPO"] = repo
        _write_env(env_updates)

        start_date = questionary.text(
            "Backfill start date (YYYY-MM-DD, blank = 2 years ago):"
        ).ask() or ""

        from workflow_tools import check_backfill_status, trigger_backfill  # lazy: needs GITHUB_TOKEN/GITHUB_REPO in os.environ first

        result = trigger_backfill(start_date.strip() or None, domains=selected_domains)
        if result.get("triggered"):
            status = check_backfill_status()
            console.print(Panel.fit(
                f"[bold green]✓ Backfill triggered[/bold green]\n\n{result['note']}\n\n"
                f"Current status: {status.get('status', 'unknown')} — "
                f"{status.get('html_url', '(no run URL yet, check the Actions tab)')}",
                border_style="green",
            ))
        else:
            console.print(Panel(
                f"Didn't trigger backfill: {result.get('reason')}\n"
                f"{result.get('html_url', '')}",
                title="!", border_style="yellow",
            ))
    else:
        console.print("[yellow]![/yellow] No PAT provided — skipping automatic backfill trigger.")
        _write_env(env_updates)

    _print_manual_next_steps()


def _print_manual_next_steps() -> None:
    console.print(Panel.fit(
        "[bold]Setup complete.[/bold]\n\n"
        "- Check backfill progress in your repo's Actions tab (workflow: "
        "backfill.yml) — it can take a few hours; safe to leave running.\n"
        "- Password changed later? Run `uv run install.py --reauth`.",
        border_style="cyan",
    ))
    console.print(Panel.fit(
        "Want Claude to be able to query this data for you? See the separate "
        "[bold]postgres-mcp[/bold] project — point it at this same database "
        "using a read-only role you create there. Not something this wizard "
        "sets up.",
        title="Optional: querying with Claude", border_style="blue",
    ))


def run_reauth() -> None:
    console.print(Panel.fit("[bold]Garmin re-authentication[/bold]", border_style="cyan"))
    existing = _read_existing_env()
    database_url = existing.get("DATABASE_URL") or _prompt_db_url(
        "Neon connection string (DATABASE_URL)", "DATABASE_URL", existing
    )
    _check_connection(database_url, "DATABASE_URL")
    interactive_login()
    with psycopg.connect(database_url) as conn:
        save_token_to_db(conn)
    console.print("[green]✓[/green] Refreshed token saved to database. Future syncs will use it.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reauth", action="store_true", help="Re-authenticate to Garmin only, skip full setup.")
    parser.add_argument(
        "--domains", type=str, default=None,
        help="Comma-separated domain keys (see domains.py), bypasses the interactive checklist. Also settable via GARMIN_DOMAINS.",
    )
    args = parser.parse_args()

    raw_domains = args.domains or os.environ.get("GARMIN_DOMAINS")
    domains_override = _parse_domains_arg(raw_domains) if raw_domains else None

    try:
        if args.reauth:
            run_reauth()
        else:
            run_setup(domains_override=domains_override)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    main()
