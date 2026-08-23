"""
Trigger and poll this repo's own GitHub Actions workflows (sync.yml,
backfill.yml) on demand, instead of running anything long-running on this
machine. Used by install.py to kick off the initial backfill and by anyone
who wants to trigger a sync/backfill or check on one from a script instead
of the Actions tab.

Requires a GitHub fine-grained personal access token scoped to ONLY this
repo, with Actions set to "Read and write" and nothing else. Do not use
a classic PAT or a token with broader repo/account access here.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # format: "owner/repo"
SYNC_WORKFLOW_FILE = os.environ.get("GITHUB_SYNC_WORKFLOW_FILE", "sync.yml")
BACKFILL_WORKFLOW_FILE = os.environ.get("GITHUB_BACKFILL_WORKFLOW_FILE", "backfill.yml")
BRANCH = os.environ.get("GITHUB_SYNC_BRANCH", "main")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _api_base(workflow_file: str) -> str:
    return f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}"


def _latest_run(workflow_file: str) -> dict | None:
    resp = requests.get(
        f"{_api_base(workflow_file)}/runs", headers=HEADERS, params={"per_page": 1}, timeout=10
    )
    resp.raise_for_status()
    runs = resp.json().get("workflow_runs", [])
    return runs[0] if runs else None


def check_workflow_status(workflow_file: str) -> dict:
    """Check the status of the most recent run of the given workflow file
    (scheduled, manual, or API-triggered)."""
    run = _latest_run(workflow_file)
    if not run:
        return {"status": "no runs found"}
    return {
        "status": run["status"],              # queued, in_progress, completed
        "conclusion": run.get("conclusion"),  # success, failure, or None if not finished yet
        "started_at": run["run_started_at"],
        "html_url": run["html_url"],
    }


def trigger_workflow(workflow_file: str, inputs: dict | None = None) -> dict:
    """Trigger a workflow_dispatch run of the given workflow file right now.
    Refuses if a run of that same workflow is already queued/in progress, to
    avoid piling up duplicate runs."""
    latest = _latest_run(workflow_file)
    if latest and latest["status"] in ("queued", "in_progress"):
        return {
            "triggered": False,
            "reason": "A run of this workflow is already in progress.",
            "html_url": latest["html_url"],
        }

    payload: dict = {"ref": BRANCH}
    if inputs:
        payload["inputs"] = inputs

    resp = requests.post(f"{_api_base(workflow_file)}/dispatches", headers=HEADERS, json=payload, timeout=10)
    if resp.status_code != 204:
        return {"triggered": False, "reason": f"GitHub API error: {resp.status_code} {resp.text}"}

    return {
        "triggered": True,
        "note": "Started — takes a moment to show up under the workflow's Actions runs.",
    }


def check_sync_status() -> dict:
    """Check the status of the most recent sync run (scheduled or manually triggered)."""
    return check_workflow_status(SYNC_WORKFLOW_FILE)


def trigger_sync() -> dict:
    """Trigger a fresh Garmin sync right now instead of waiting for the
    daily schedule. Refuses if a run is already queued/in progress, to
    avoid piling up duplicate runs."""
    return trigger_workflow(SYNC_WORKFLOW_FILE)


def check_backfill_status() -> dict:
    """Check the status of the most recent backfill run."""
    return check_workflow_status(BACKFILL_WORKFLOW_FILE)


def trigger_backfill(backfill_start_date: str | None = None) -> dict:
    """Trigger the full-history backfill workflow right now. `backfill_start_date`
    (YYYY-MM-DD) is passed through as the workflow's input; omit for its
    default (2 years back for daily metrics — activities/weigh-ins/body
    battery always backfill in full regardless). Refuses if a backfill run
    is already queued/in progress."""
    inputs = {"backfill_start_date": backfill_start_date} if backfill_start_date else None
    result = trigger_workflow(BACKFILL_WORKFLOW_FILE, inputs=inputs)
    if result.get("triggered"):
        result["note"] = (
            "Backfill started — this pulls years of history and can take a "
            "few hours. Call check_backfill_status() to see progress, or "
            "watch it at the html_url from that call."
        )
    return result
