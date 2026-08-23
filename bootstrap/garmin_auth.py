"""
Shared Garmin authentication + token-store logic.

Used by:
  - install.py (the setup wizard) and bootstrap/generate_token.py, for the
    interactive login flow (masked password, MFA, retry-on-failure).
  - bootstrap/load_token_to_db.py, ingestion/pull.py, backfill.py, and
    install.py, for the "load token from DB at start of a run, save the
    (possibly refreshed) token back at the end" pattern ephemeral GitHub
    Actions runners depend on.

Only interactive_login() needs rich/questionary, and those two are NOT in
ingestion/requirements.txt (pull.py runs on a bare Actions runner) — so
they're imported lazily inside the function, never at module scope, to keep
load_token_from_db/save_token_to_db importable with zero extra dependencies.
"""

import getpass
import json
import pathlib
import re

import psycopg
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

TOKEN_DIR = pathlib.Path.home() / ".garminconnect"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MFA_CODE_LENGTH = 6


def load_token_from_db(conn: psycopg.Connection) -> None:
    row = conn.execute(
        "select payload from auth_tokens where provider = 'garmin'"
    ).fetchone()
    if not row:
        raise RuntimeError(
            "No stored Garmin token found. Run `uv run install.py --reauth` "
            "(or bootstrap/generate_token.py + bootstrap/load_token_to_db.py) "
            "once before the first sync."
        )
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    for filename, content in row[0].items():
        (TOKEN_DIR / filename).write_text(content)


def save_token_to_db(conn: psycopg.Connection) -> None:
    payload = {f.name: f.read_text() for f in TOKEN_DIR.glob("*.json")}
    conn.execute(
        """
        insert into auth_tokens (provider, payload, updated_at)
        values ('garmin', %s, now())
        on conflict (provider) do update set payload = excluded.payload, updated_at = now()
        """,
        (json.dumps(payload),),
    )
    conn.commit()


def _validate_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))


def _validate_mfa_code(value: str) -> bool:
    return value.strip().isdigit() and len(value.strip()) == _MFA_CODE_LENGTH


def interactive_login(max_attempts: int = 3) -> Garmin:
    """Prompt for Garmin credentials (masked password, MFA-aware), log in,
    and write the resulting token files to TOKEN_DIR. Returns the logged-in
    client. Raises RuntimeError if login doesn't succeed within max_attempts.

    This is the one interactive login flow shared by install.py and
    bootstrap/generate_token.py, so first-time login and re-auth are the
    same code path.
    """
    from rich.console import Console
    from rich.panel import Panel
    import questionary

    console = Console()

    console.print(Panel.fit(
        "This uses the [italic]unofficial[/italic] garminconnect library, "
        "since Garmin doesn't offer a public API for individuals. Your "
        "email and password are sent directly to Garmin's own login "
        "endpoint from this machine — they are never sent anywhere else, "
        "never logged, and never stored. Only the resulting session token "
        "is saved (to your database), so future syncs don't need your "
        "password again.",
        title="Garmin login — before you continue",
        border_style="cyan",
    ))

    email = questionary.text(
        "Garmin email:",
        validate=lambda v: True if _validate_email(v) else "Enter a valid email address",
    ).ask()
    if email is None:
        raise RuntimeError("Login cancelled.")
    email = email.strip()

    def prompt_mfa() -> str:
        code = questionary.text(
            "Garmin sent you an MFA code — enter it:",
            validate=lambda v: True
            if _validate_mfa_code(v)
            else f"Enter the {_MFA_CODE_LENGTH}-digit numeric code",
        ).ask()
        if code is None:
            raise RuntimeError("Login cancelled during MFA prompt.")
        return code.strip()

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_attempts + 1):
        password = getpass.getpass(f"Garmin password (attempt {attempt}/{max_attempts}): ")

        client = Garmin(email, password, prompt_mfa=prompt_mfa)
        try:
            with console.status("Logging in to Garmin..."):
                client.login(tokenstore=str(TOKEN_DIR))
        except GarminConnectTooManyRequestsError as e:
            console.print(Panel(
                f"Garmin is rate-limiting login attempts: {e}\n\n"
                "Wait a few minutes before trying again, then re-run this command.",
                title="✗ Login failed", border_style="red",
            ))
            raise RuntimeError("Login failed: rate limited by Garmin.") from e
        except GarminConnectAuthenticationError as e:
            console.print(f"[red]✗ Login failed: {e}[/red]")
            if attempt == max_attempts:
                console.print(Panel(
                    f"Giving up after {max_attempts} attempts. Double-check your "
                    "email/password and MFA code, then re-run this command.",
                    title="✗ Login failed", border_style="red",
                ))
                raise RuntimeError(f"Login failed after {max_attempts} attempts.") from e
            console.print("Let's try again.\n")
            continue

        # The library's own dump-on-login is wrapped in contextlib.suppress(Exception),
        # so it can fail silently. Call it again ourselves, unsuppressed, to be sure.
        client.client.dump(str(TOKEN_DIR))
        token_file = TOKEN_DIR / "garmin_tokens.json"
        if not token_file.exists():
            raise RuntimeError(f"dump() ran without error but {token_file} still doesn't exist.")

        console.print(Panel.fit(
            f"[bold green]✓ Logged in[/bold green] — token saved to {token_file}",
            border_style="green",
        ))
        return client

    raise RuntimeError("Login failed.")  # unreachable; keeps type checkers happy
