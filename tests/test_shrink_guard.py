"""transform.write_csv — the guard that refuses to replace good data with less.

WHY THIS FILE EXISTS
--------------------
The guard is the pipeline's last line of defence against the most dangerous
shape of failure: output that is smaller but still WELL-FORMED. An empty API
response that produces a valid CSV with every value blanked exits 0 and looks
fine to everything downstream — the coach simply reads "no data this week".

But a guard that cries wolf gets switched off, and that is the second failure
mode covered here. sessions.csv shrinks by one row as a matter of NORMAL
operation: the watch auto-detects a gym session, that row is written, and then
Hevy's own record of the same session arrives on a later run and supersedes it.
Nothing is lost — the sets live in lifts.csv — but the row count goes down. In
production this fired twice in two days and sent two false alarms.

So the property under test is narrow: forgive the shrink we can NAME, and
nothing else. The tests that matter most are the ones asserting it still
refuses.
"""
from __future__ import annotations

import csv

import pytest

from coach_sync import transform

SESSION_COLUMNS = ["date", "start_time", "activity", "exercise_type", "slot"]
KEY = ("date", "start_time", "exercise_type")


def session(date_, time_, kind, activity="x", slot="A"):
    return {"date": date_, "start_time": time_, "activity": activity,
            "exercise_type": kind, "slot": slot}


# The real predicate from __main__: a watch strength record is superseded once
# Hevy covers that date.
def superseded_on(gym_days):
    return lambda row: (row.get("exercise_type") == "STRENGTH_TRAINING"
                        and row.get("date") in gym_days)


@pytest.fixture
def csv_path(tmp_path):
    return tmp_path / "sessions.csv"


def write(path, rows, **kw):
    return transform.write_csv(path, SESSION_COLUMNS, rows, **kw)


def read(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


# ------------------------------------------------ the guard still guards

def test_a_plain_row_loss_is_still_refused(csv_path):
    """The original purpose. No explanation offered, so no forgiveness."""
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS"),
                     session("2026-09-02", "18:00", "RUNNING")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [session("2026-09-01", "19:19", "TENNIS")])
    assert len(read(csv_path)) == 2          # untouched


def test_the_same_rows_with_fewer_populated_cells_is_refused(csv_path):
    """'Same shape, less data' — the degradation row counts alone cannot see."""
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS", slot="B")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [session("2026-09-01", "19:19", "TENNIS", slot="")])


def test_an_empty_result_never_overwrites_a_populated_file(csv_path):
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [])
    assert len(read(csv_path)) == 1


# ------------------------------------------ the shrink that is legitimate

def test_a_watch_gym_row_superseded_by_hevy_is_allowed_through(csv_path):
    """The production false alarm, reproduced: the watch's copy of the 4 Sep
    gym session disappears once Hevy's record of that date lands."""
    before = [session("2026-09-01", "19:19", "TENNIS", slot="B"),
              session("2026-09-04", "15:11", "STRENGTH_TRAINING")]
    write(csv_path, before)

    after = [session("2026-09-01", "19:19", "TENNIS", slot="B")]
    note = write(csv_path, after, key_fields=KEY,
                 loss_is_expected=superseded_on({"2026-09-04"}))

    assert len(read(csv_path)) == 1          # the write went through
    assert note and "superseded" in note     # and it said so out loud


def test_the_allowance_is_reported_rather_than_silent(csv_path):
    """A safety check that makes silent exceptions has stopped being one."""
    write(csv_path, [session("2026-09-04", "15:11", "STRENGTH_TRAINING"),
                     session("2026-09-01", "19:19", "TENNIS")])
    note = write(csv_path, [session("2026-09-01", "19:19", "TENNIS")],
                 key_fields=KEY, loss_is_expected=superseded_on({"2026-09-04"}))
    assert note is not None


def test_a_write_that_does_not_shrink_returns_no_note(csv_path):
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS")])
    note = write(csv_path, [session("2026-09-01", "19:19", "TENNIS"),
                            session("2026-09-02", "18:00", "RUNNING")],
                 key_fields=KEY, loss_is_expected=superseded_on({"2026-09-04"}))
    assert note is None


# ------------------------------------- forgiveness is narrow, not a bypass

def test_a_gym_row_on_a_day_hevy_does_NOT_cover_is_still_refused(csv_path):
    """He lifted and never logged it in Hevy. That row IS the only evidence
    the session happened, so losing it must still stop the write."""
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS"),
                     session("2026-09-04", "15:11", "STRENGTH_TRAINING")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [session("2026-09-01", "19:19", "TENNIS")],
              key_fields=KEY, loss_is_expected=superseded_on(set()))


def test_a_non_gym_row_is_never_forgiven(csv_path):
    """Tennis has no Hevy equivalent; nothing can supersede it."""
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS"),
                     session("2026-09-02", "18:00", "RUNNING")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [session("2026-09-01", "19:19", "TENNIS")],
              key_fields=KEY,
              loss_is_expected=superseded_on({"2026-09-02", "2026-09-01"}))


def test_a_real_loss_alongside_an_explained_one_is_still_refused(csv_path):
    """The important one. Forgiving the explained row must not smuggle the
    unexplained row through with it — the guard re-runs against the old file
    minus only what it could account for."""
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS"),
                     session("2026-09-02", "18:00", "RUNNING"),
                     session("2026-09-04", "15:11", "STRENGTH_TRAINING")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [session("2026-09-01", "19:19", "TENNIS")],
              key_fields=KEY, loss_is_expected=superseded_on({"2026-09-04"}))
    assert len(read(csv_path)) == 3


def test_an_explained_drop_that_also_blanks_a_surviving_row_is_refused(csv_path):
    """Cell loss on a row that stayed is not covered by any explanation."""
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS", slot="B"),
                     session("2026-09-04", "15:11", "STRENGTH_TRAINING")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [session("2026-09-01", "19:19", "TENNIS", slot="")],
              key_fields=KEY, loss_is_expected=superseded_on({"2026-09-04"}))


def test_without_a_predicate_the_guard_behaves_exactly_as_before(csv_path):
    """Every other CSV passes no predicate and must be unaffected."""
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS"),
                     session("2026-09-04", "15:11", "STRENGTH_TRAINING")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [session("2026-09-01", "19:19", "TENNIS")])


def test_allow_shrink_still_overrides_everything(csv_path):
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS"),
                     session("2026-09-02", "18:00", "RUNNING")])
    write(csv_path, [], allow_shrink=True)
    assert read(csv_path) == []


# --------------------------------------------------------- the write itself

def test_a_first_write_to_a_missing_file_is_never_a_shrink(csv_path):
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS")])
    assert len(read(csv_path)) == 1


def test_a_refused_write_leaves_no_temp_file_behind(csv_path, tmp_path):
    write(csv_path, [session("2026-09-01", "19:19", "TENNIS")])
    with pytest.raises(transform.ShrinkGuard):
        write(csv_path, [])
    assert list(tmp_path.glob("*.tmp")) == []
