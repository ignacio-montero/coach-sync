"""Tests for the Google Sheet mirror.

Uses httpx.MockTransport — a fake network, not a mocked function. The bug
surface here IS the request sequence (does it clear before writing? does it
create the sheet only once?), and that is only observable by watching the
requests.
"""
from __future__ import annotations

import csv
import json

import httpx
import pytest

from coach_sync import sheets


class Recorder:
    """Fake Sheets API. Records every request; returns plausible responses."""

    def __init__(self, sheet_id="SHEET123"):
        self.sheet_id = sheet_id
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        if request.method == "POST" and request.url.path.endswith("/spreadsheets"):
            return httpx.Response(200, json={"spreadsheetId": self.sheet_id})
        return httpx.Response(200, json={})

    def client(self):
        # _REAL_CLIENT, not httpx.Client: the test patches httpx.Client, and
        # constructing one here would re-enter the patch and recurse forever.
        return _REAL_CLIENT(transport=httpx.MockTransport(self.handler))


_REAL_CLIENT = httpx.Client


@pytest.fixture
def data_dir(tmp_path):
    def write(name, rows):
        with (tmp_path / name).open("w", newline="") as h:
            csv.writer(h).writerows(rows)
    write("metrics_weekly.csv", [["week", "weight_7d_mean"], ["W01", "84.04"]])
    write("metrics_daily.csv", [["date", "weight_kg"], ["2026-08-24", "84.3"]])
    return tmp_path


def _publish(monkeypatch, rec, data_dir, sheet_id=None):
    monkeypatch.setattr(sheets.httpx, "Client", lambda **kw: rec.client())
    return sheets.publish("fake-token", data_dir, sheet_id)


def test_creates_the_sheet_when_no_id_is_configured(monkeypatch, data_dir):
    rec = Recorder()
    assert _publish(monkeypatch, rec, data_dir) == "SHEET123"
    creates = [c for c in rec.calls
               if c[0] == "POST" and c[1].endswith("/spreadsheets")]
    assert len(creates) == 1


def test_does_not_recreate_the_sheet_when_an_id_exists(monkeypatch, data_dir):
    """Creating a second sheet every run would scatter orphans across Drive and
    leave the Project reading whichever one it was first pointed at."""
    rec = Recorder()
    assert _publish(monkeypatch, rec, data_dir, "EXISTING") == "EXISTING"
    creates = [c for c in rec.calls
               if c[0] == "POST" and c[1].endswith("/spreadsheets")]
    assert creates == []


def test_clears_each_tab_before_writing_it(monkeypatch, data_dir):
    """The CSVs are rebuilt in full each run. Writing without clearing leaves
    stale rows below shorter new data — and they read as real."""
    rec = Recorder()
    _publish(monkeypatch, rec, data_dir, "EXISTING")
    for tab in ("weekly", "daily"):
        clear_at = next(i for i, c in enumerate(rec.calls) if ":clear" in c[1] and tab in c[1])
        write_at = next(i for i, c in enumerate(rec.calls) if c[0] == "PUT" and tab in c[1])
        assert clear_at < write_at, "{}: cleared after writing".format(tab)


def test_skips_tabs_whose_csv_is_missing(monkeypatch, data_dir):
    """sessions.csv and lifts.csv are absent in this fixture. A missing file
    must not blank an existing tab — absent is not the same as empty."""
    rec = Recorder()
    _publish(monkeypatch, rec, data_dir, "EXISTING")
    touched = [c[1] for c in rec.calls]
    assert not any("sessions" in u for u in touched)
    assert not any("lifts" in u for u in touched)


def test_weekly_is_the_first_tab():
    """Connectors read the first tab most reliably, and the weekly rollup is
    what the coach actually needs."""
    assert sheets.TABS[0][0] == "weekly"


def test_scope_is_drive_file_not_spreadsheets():
    """drive.file reaches only files this app created. `spreadsheets` would
    reach every spreadsheet in the account."""
    assert sheets.SCOPE.endswith("/drive.file")
    from coach_sync.datatypes import ALL_SCOPES
    assert sheets.SCOPE in ALL_SCOPES
    assert not any(s.endswith("/spreadsheets") for s in ALL_SCOPES)


def test_disabled_by_default():
    """Opt-in: an unconfigured deployment must not start creating spreadsheets."""
    assert sheets.configured() is False
