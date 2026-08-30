"""Publish the CSVs to a Google Sheet, so a Claude Project can read them.

WHY A SHEET AT ALL
------------------
Claude Code reads the CSVs off the local filesystem. A Claude Project cannot —
it has no filesystem. Its route to live data is the Google Drive connector,
which reads a Sheet at conversation time. Uploading a CSV to Project knowledge
would NOT work: that is a frozen snapshot and would reintroduce the manual
re-upload this pipeline exists to delete.

WHY drive.file AND NOT spreadsheets
-----------------------------------
`drive.file` grants access ONLY to files this application created. `spreadsheets`
would grant access to every spreadsheet in the account. The pipeline needs to
write one file, so it asks for permission to write one file — a leaked token
reaches that Sheet and nothing else. This also avoids a service account
entirely: no second credential type, no JSON key to mount and rotate, no
sharing dance. It reuses the OAuth client that already fetches health data.

The Sheet is created on first run and its id stored in COACH_SYNC_SHEET_ID.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import List, Optional

import httpx

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/drive.file"

# Tab order matters: connectors read the first tab most reliably, and the
# weekly rollup is what the coach actually needs.
TABS = [
    ("weekly", "metrics_weekly.csv"),
    ("daily", "metrics_daily.csv"),
    ("sessions", "sessions.csv"),
    ("lifts", "lifts.csv"),
]


def configured() -> bool:
    return bool(os.environ.get("COACH_SYNC_SHEET_ENABLED", "").strip())


def _read_csv(path: Path) -> List[List[str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return [row for row in csv.reader(handle)]


def create_sheet(client: httpx.Client, title: str) -> str:
    """Create the spreadsheet once; return its id."""
    body = {"properties": {"title": title},
            "sheets": [{"properties": {"title": name}} for name, _ in TABS]}
    resp = client.post(SHEETS_API, json=body, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError("could not create sheet: {} {}".format(
            resp.status_code, resp.text[:300]))
    return resp.json()["spreadsheetId"]


def publish(access_token: str, data_dir: Path, sheet_id: Optional[str],
            title: str = "coach-sync data") -> str:
    """Write every CSV to its tab. Returns the sheet id (created if needed).

    Values are REPLACED, not appended: the CSVs are rebuilt in full on every
    run, so the Sheet is a mirror rather than a log. Appending would duplicate
    the entire history daily.
    """
    headers = {"Authorization": "Bearer {}".format(access_token)}
    with httpx.Client(headers=headers) as client:
        if not sheet_id:
            sheet_id = create_sheet(client, title)
            print("  created sheet {}".format(sheet_id))

        for tab, filename in TABS:
            rows = _read_csv(data_dir / filename)
            if not rows:
                continue

            # Clear first: a shorter dataset would otherwise leave stale rows
            # below the new data, which read as real and are not.
            clear = client.post(
                "{}/{}/values/{}:clear".format(SHEETS_API, sheet_id, tab),
                json={}, timeout=60)
            if clear.status_code not in (200, 400):
                raise RuntimeError("clear {} -> {} {}".format(
                    tab, clear.status_code, clear.text[:200]))

            resp = client.put(
                "{}/{}/values/{}!A1".format(SHEETS_API, sheet_id, tab),
                params={"valueInputOption": "RAW"},
                json={"values": rows}, timeout=60)
            if resp.status_code != 200:
                raise RuntimeError("write {} -> {} {}".format(
                    tab, resp.status_code, resp.text[:300]))
            print("  {:10s} {:3d} rows -> sheet".format(tab, len(rows) - 1))

    return sheet_id
