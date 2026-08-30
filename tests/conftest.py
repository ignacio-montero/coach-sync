"""Shared fixtures and builders.

WHY A CONFTEST AT ALL
---------------------
`conftest.py` is pytest's implicit-import file: anything defined here is
available to every test module in this directory without an import. It is the
standard home for *fixtures* (reusable setup) and *builders* (functions that
make realistic test data with sane defaults).

WHY THE BUILDERS ARE CONFIG-DERIVED
-----------------------------------
The real thresholds and the campaign start date live in `campaign.toml`, which
is gitignored personal health data. A test that hard-codes "2026-08-24" or
"67.0" passes on this machine and fails for anyone who clones the public repo
and gets `campaign.example.toml` instead. So every date here is derived from
`campaign.week_bounds()` and every threshold from `campaign.<CONST>`.

That is a general testing rule worth naming: **assert on the relationship, not
on the literal**. "lean mean is 0.5 below the configured floor, therefore
breach" is a true statement about the code; "66.4 is a breach" is a true
statement about one person's config.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from coach_sync import campaign

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


# ------------------------------------------------------------------ builders

def day_in_week(week: int, offset: int = 0) -> date:
    """The `offset`-th day (0 = Monday) of campaign week `week`."""
    return campaign.week_bounds(week)[0] + timedelta(days=offset)


def daily_row(week: int, offset: int = 0, **kw) -> dict:
    """One `build_daily`-shaped row. Empty string is the 'no reading' marker,
    matching what build_daily actually emits (it writes "" not None)."""
    day = day_in_week(week, offset)
    row = {
        "date": day.isoformat(),
        "weight_kg": "",
        "body_fat_pct": "",
        "lean_kg": "",
        "resting_hr": "",
        "hrv_rmssd": "",
        "sleep_hours": "",
        "sleep_bedtime": "",
        "campaign_week": week,
    }
    row.update(kw)
    return row


def weighed(week: int, offset: int, weight: float, bf: float | None = None) -> dict:
    """A daily row with weight, and optionally body fat + the derived lean mass.

    Mirrors build_daily's own derivation so the fixture cannot drift from it.
    """
    lean = round(weight * (1 - bf / 100.0), 2) if bf is not None else ""
    return daily_row(week, offset, weight_kg=weight,
                     body_fat_pct=bf if bf is not None else "", lean_kg=lean)


def watch_session(week: int, offset: int, kind: str = "STRENGTH_TRAINING") -> dict:
    """A `parse_exercise`-shaped session row, as build_weekly consumes it."""
    return {"date": day_in_week(week, offset).isoformat(),
            "exercise_type": kind, "slot": "A"}


def exercise_point(kind: str, start: str, duration: str = "3600s",
                   method: str = "ACTIVELY_MEASURED", offset: str = "3600s") -> dict:
    """Live Google Health exercise shape, observed 2026-08-30."""
    interval = {"startTime": start, "endTime": start}
    if offset is not None:
        interval["startUtcOffset"] = offset
        interval["endUtcOffset"] = offset
    return {"exercise": {"interval": interval, "exerciseType": kind,
                         "activeDuration": duration, "metricsSummary": {}},
            "dataSource": {"recordingMethod": method}}


def weight_point(physical_time: str, grams: int, offset: str = "3600s") -> dict:
    """Live Google Health weight shape, observed 2026-08-30."""
    return {"weight": {"sampleTime": {"physicalTime": physical_time,
                                      "utcOffset": offset},
                       "weightGrams": grams}}


def hevy_workout(sets, wid="w1", start="2026-08-24T17:30:00+00:00",
                 title="A1", exercise="Squat (Barbell)") -> dict:
    """Hevy OpenAPI shape. Note the offset form: Hevy sends `+00:00`, Google `Z`."""
    return {"id": wid, "title": title, "start_time": start,
            "end_time": start,
            "exercises": [{"index": 0, "title": exercise,
                           "exercise_template_id": "ABC", "sets": sets}]}


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def manual_csv(tmp_path):
    """Writes a manual.csv and hands back its path.

    `tmp_path` is pytest's per-test temporary directory fixture — it guarantees
    isolation (no test can see another's file) and automatic cleanup, which is
    why we never write into the repo's own `data/`.
    """
    def _write(text: str, encoding: str = "utf-8") -> Path:
        path = tmp_path / "manual.csv"
        path.write_bytes(text.encode(encoding))
        return path
    return _write


def _latest_raw(name: str):
    matches = sorted(RAW_DIR.glob("{}_*.json".format(name)))
    return matches[-1] if matches else None


@pytest.fixture
def real_raw():
    """Loads a real saved API response, or skips the test if none exists.

    These are *characterisation tests*: they pin what the live API actually
    returns, which is the only defence against a silent upstream schema change.
    They must SKIP rather than fail when `data/` is absent, because `data/` is
    gitignored personal health data and will not exist in CI or on a clone.
    """
    def _load(name: str):
        path = _latest_raw(name)
        if path is None:
            pytest.skip("no raw {} capture in data/raw (gitignored)".format(name))
        return json.loads(path.read_text())
    return _load
