"""read_manual — the hand-entered file.

WHY THIS IS WORTH REAL TESTS
----------------------------
Waist is the campaign's ground truth for body composition (the BIA scale gives a
trend; the tape gives a fact) and it is the ONLY input with no API behind it.
Every other source has a `unparsed` counter that shouts when parsing fails.
`read_manual` has none: a file it cannot read comes back as `{}` and the waist
column simply goes blank, which is indistinguishable from "he didn't measure".

That is the exact failure mode the brief calls out — silently-wrong beats
crashing, in the worst way.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from coach_sync import transform


@pytest.mark.parametrize("text,expected", [
    ("date,waist_navel_cm\n2026-08-24,92.0\n",
     {date(2026, 8, 24): {"waist_navel_cm": "92.0"}}),
    # Extra columns ride along untouched — the file is a general escape hatch.
    ("date,waist_navel_cm,note\n2026-08-24,92.0,fasted\n",
     {date(2026, 8, 24): {"waist_navel_cm": "92.0", "note": "fasted"}}),
    # Empty cells are dropped rather than stored as "".
    ("date,waist_navel_cm,note\n2026-08-24,92.0,\n",
     {date(2026, 8, 24): {"waist_navel_cm": "92.0"}}),
    # Quoted values containing the delimiter.
    ('date,note\n2026-08-24,"a,b"\n', {date(2026, 8, 24): {"note": "a,b"}}),
], ids=["basic", "extra-column", "empty-cell", "quoted-comma"])
def test_well_formed_rows(manual_csv, text, expected):
    assert transform.read_manual(manual_csv(text)) == expected


def test_a_missing_file_is_an_empty_dict_not_an_error():
    """The file is optional — most weeks have no tape measurement."""
    assert transform.read_manual(Path("/nonexistent/manual.csv")) == {}


@pytest.mark.parametrize("text", ["", "date,waist_navel_cm\n", "\n\n"],
                         ids=["empty", "header-only", "blank-lines"])
def test_files_with_no_data_rows_are_empty(manual_csv, text):
    assert transform.read_manual(manual_csv(text)) == {}


@pytest.mark.parametrize("bad_date", ["", "not-a-date", "24/08/2026", "2026-13-45"])
def test_an_unparseable_date_skips_that_row_and_keeps_the_others(manual_csv, bad_date):
    """Skipping one bad row is the right trade — but note it is SILENT. See the
    coverage-gap note in the report: there is no `unparsed` counter here."""
    text = "date,waist_navel_cm\n{},92.0\n2026-08-24,91.0\n".format(bad_date)
    assert transform.read_manual(manual_csv(text)) == {
        date(2026, 8, 24): {"waist_navel_cm": "91.0"}}


def test_a_repeated_date_lets_the_later_row_correct_the_earlier_one(manual_csv):
    """Documents current behaviour, and it is the useful one: appending a
    corrected line to the file overrides the typo above it."""
    text = "date,waist_navel_cm\n2026-08-24,92.0\n2026-08-24,90.5\n"
    assert transform.read_manual(manual_csv(text)) == {
        date(2026, 8, 24): {"waist_navel_cm": "90.5"}}


def test_a_ragged_row_with_extra_fields_does_not_lose_the_named_columns(manual_csv):
    """csv.DictReader parks surplus fields under the None key. Ugly, but the
    named columns survive, so a stray trailing comma does not lose the reading."""
    text = "date,waist_navel_cm\n2026-08-24,92.0,oops\n"
    got = transform.read_manual(manual_csv(text))
    assert got[date(2026, 8, 24)]["waist_navel_cm"] == "92.0"


def test_a_file_saved_with_a_bom_is_still_read(manual_csv):
    path = manual_csv("date,waist_navel_cm\r\n2026-08-24,92.0\r\n", encoding="utf-8-sig")
    assert transform.read_manual(path) == {
        date(2026, 8, 24): {"waist_navel_cm": "92.0"}}


def test_a_short_row_does_not_crash_the_build(manual_csv):
    text = "waist_navel_cm,date\n92.0\n89.0,2026-08-24\n"
    assert transform.read_manual(manual_csv(text)) == {
        date(2026, 8, 24): {"waist_navel_cm": "89.0"}}


def test_values_stay_strings_and_are_coerced_downstream(manual_csv):
    """read_manual is deliberately dumb about types; build_weekly runs the
    values through to_float. Pinning this stops a 'helpful' coercion here from
    turning a note column into NaN."""
    got = transform.read_manual(manual_csv("date,waist_navel_cm\n2026-08-24,92.0\n"))
    assert got[date(2026, 8, 24)]["waist_navel_cm"] == "92.0"
    assert transform.to_float("92.0") == 92.0
    assert transform.to_float("about 92") is None
