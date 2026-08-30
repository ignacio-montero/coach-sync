"""parse_exercise deduplication — the 3-hour same-type window.

WHY THIS MATTERS
----------------
`sessions_done` feeds an adherence figure, and the plan's response to a low
figure ("fix adherence") is the opposite of its response to a high one
("tighten the deficit"). So both directions of error change the instruction:

    over-count  -> the watch's duplicate record inflates adherence
    under-count -> a real second session is merged away and adherence deflates

The dedup rule is "same exerciseType within 3 hours -> same session". That rule
cannot distinguish a duplicate from a genuine second session, which is what the
tests below make explicit.
"""
from __future__ import annotations

import pytest

from coach_sync import transform

from conftest import exercise_point


def types(rows):
    return [r["exercise_type"] for r in rows]


# ------------------------------------------------------- what dedup should catch

def test_the_same_session_recorded_twice_collapses_to_the_active_record():
    """The watch auto-detects a session the user also started manually. Both
    land in the feed; keeping both double-counts one workout."""
    rows = transform.parse_exercise([
        exercise_point("TENNIS", "2026-08-28T15:00:00Z", method="PASSIVELY_MEASURED"),
        exercise_point("TENNIS", "2026-08-28T15:05:00Z", method="ACTIVELY_MEASURED"),
    ])
    assert len(rows) == 1
    assert rows[0]["recording_method"] == "ACTIVELY_MEASURED"


def test_the_active_record_wins_regardless_of_which_arrived_first():
    """Order-independence matters because the API returns newest-first while
    parse_exercise sorts ascending — so the pair can arrive either way round."""
    rows = transform.parse_exercise([
        exercise_point("TENNIS", "2026-08-28T15:05:00Z", method="ACTIVELY_MEASURED"),
        exercise_point("TENNIS", "2026-08-28T15:00:00Z", method="PASSIVELY_MEASURED"),
    ])
    assert len(rows) == 1
    assert rows[0]["recording_method"] == "ACTIVELY_MEASURED"


def test_different_activities_at_the_same_time_are_never_merged():
    """A gym session and a bike commute an hour apart are two sessions.
    The type check is the only thing preventing a same-day collapse."""
    rows = transform.parse_exercise([
        exercise_point("STRENGTH_TRAINING", "2026-08-28T17:00:00Z"),
        exercise_point("BIKING", "2026-08-28T18:00:00Z"),
    ])
    assert sorted(types(rows)) == ["BIKING", "STRENGTH_TRAINING"]


def test_same_type_more_than_three_hours_apart_stays_two_sessions():
    """Boundary-value analysis on the window: 3h01 apart survives as two."""
    rows = transform.parse_exercise([
        exercise_point("TENNIS", "2026-08-28T09:00:00Z"),
        exercise_point("TENNIS", "2026-08-28T12:01:00Z"),
    ])
    assert len(rows) == 2


def test_same_type_on_consecutive_days_stays_two_sessions():
    rows = transform.parse_exercise([
        exercise_point("STRENGTH_TRAINING", "2026-08-24T17:00:00Z"),
        exercise_point("STRENGTH_TRAINING", "2026-08-27T17:00:00Z"),
    ])
    assert len(rows) == 2


# ------------------------------------------------------- what dedup wrongly catches

@pytest.mark.xfail(strict=True, reason=(
    "BUG (by design, but under-documented): the rule is purely 'same type + <3h', "
    "so two GENUINELY separate sessions of the same type inside a 3h window are "
    "merged. Two tennis sets 2h15 apart on a Saturday count as one, deflating "
    "adherence. Recording method cannot disambiguate — both are ACTIVELY_MEASURED."))
def test_BUG_two_genuinely_separate_same_type_sessions_are_not_merged():
    rows = transform.parse_exercise([
        exercise_point("TENNIS", "2026-08-28T17:00:00Z", "3600s"),
        exercise_point("TENNIS", "2026-08-28T19:15:00Z", "3600s"),
    ])
    assert len(rows) == 2


@pytest.mark.xfail(strict=True, reason=(
    "BUG: when a clash is found and NEITHER record is ACTIVELY_MEASURED, the "
    "later record is dropped outright rather than kept as a distinct session. "
    "Two passively-detected runs 2h apart become one."))
def test_BUG_two_passive_records_of_the_same_type_are_not_silently_collapsed():
    rows = transform.parse_exercise([
        exercise_point("RUNNING", "2026-08-28T07:00:00Z", method="PASSIVELY_MEASURED"),
        exercise_point("RUNNING", "2026-08-28T09:00:00Z", method="PASSIVELY_MEASURED"),
    ])
    assert len(rows) == 2


def test_merging_is_chained_and_therefore_order_dependent():
    """Documents a subtle property rather than asserting it is right.

    Three sessions at 11:00, 13:30 and 16:00 local are each 2h30 from the next
    but 5h across. The loop compares each new row only against rows already
    KEPT, so #2 merges into #1, and #3 (5h from #1) survives -> 2 rows. The
    result depends on iteration order, not on the data. Pinned so a refactor
    that changes the grouping shows up here instead of in adherence.
    """
    rows = transform.parse_exercise([
        exercise_point("TENNIS", "2026-08-28T10:00:00Z"),
        exercise_point("TENNIS", "2026-08-28T12:30:00Z"),
        exercise_point("TENNIS", "2026-08-28T15:00:00Z"),
    ])
    assert len(rows) == 2
    assert [r["start_time"] for r in rows] == ["11:00", "16:00"]


# ------------------------------------------------------- robustness

@pytest.mark.xfail(strict=True, reason=(
    "BUG: a record missing startUtcOffset yields a NAIVE datetime, and sorting "
    "it alongside offset-aware ones raises TypeError. One malformed point takes "
    "down the whole daily run rather than being counted as unparsed."))
def test_BUG_a_point_with_no_utc_offset_does_not_crash_the_whole_parse():
    good = exercise_point("TENNIS", "2026-08-28T10:00:00Z")
    bad = exercise_point("TENNIS", "2026-08-28T14:00:00", offset=None)
    rows = transform.parse_exercise([good, bad])
    assert len(rows) >= 1


def test_walking_is_excluded_before_dedup_so_it_cannot_shadow_a_real_session():
    """~9 passive walks a week. Counting them roughly triples adherence."""
    rows = transform.parse_exercise([
        exercise_point("WALKING", "2026-08-29T13:58:20Z", "1680s", "PASSIVELY_MEASURED"),
        exercise_point("TENNIS", "2026-08-28T15:03:57Z", "3763s"),
    ])
    assert types(rows) == ["TENNIS"]


def test_points_with_no_exercise_payload_are_skipped_not_fatal():
    rows = transform.parse_exercise([{}, {"exercise": None}, {"exercise": "x"},
                                     exercise_point("TENNIS", "2026-08-28T15:00:00Z")])
    assert types(rows) == ["TENNIS"]


def test_a_point_with_no_start_time_is_skipped():
    point = exercise_point("TENNIS", "2026-08-28T15:00:00Z")
    point["exercise"]["interval"] = {}
    assert transform.parse_exercise([point]) == []


def test_the_sort_key_is_removed_before_the_row_is_written():
    """`_start` is an internal datetime. DictWriter would ignore it, but a leaked
    private key is how a CSV schema quietly grows a column."""
    rows = transform.parse_exercise([exercise_point("TENNIS", "2026-08-28T15:00:00Z")])
    assert "_start" not in rows[0]


def test_slot_mapping_covers_every_type_the_watch_actually_emits():
    """Types observed in the live capture. An unmapped type gets slot "" and
    silently stops counting towards a named slot in the plan."""
    observed = {"STRENGTH_TRAINING", "TENNIS", "RUNNING", "WORKOUT",
                "CARDIO_WORKOUT", "SPORT", "BIKING"}
    unmapped = observed - set(transform.SLOT_MAP)
    assert unmapped == set(), unmapped


def test_empty_input_is_an_empty_list():
    assert transform.parse_exercise([]) == []
