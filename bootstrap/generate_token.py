# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "rich>=13.0",
#     "questionary>=2.0",
# ]
# ///
"""
Run this ONCE, locally, on your own machine — never in CI.
Logs in with your Garmin credentials (handles MFA prompt if needed) and
writes the resulting token files to ~/.garminconnect.

After this, run load_token_to_db.py to push that token into Neon.
Your Garmin password is never stored anywhere after this step.

For a guided, all-in-one setup (including this step), use
`uv run install.py` instead. For re-authenticating later (e.g. after a
password change), `uv run install.py --reauth` does this same login step
plus the DB push, in one command.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bootstrap.garmin_auth import interactive_login


def main() -> None:
    interactive_login()
    print("Next: run bootstrap/load_token_to_db.py to push it to Neon.")


if __name__ == "__main__":
    main()
