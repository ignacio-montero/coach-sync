"""Timezones, day attribution, and unit coercion.

WHY THIS DESERVES ITS OWN FILE
------------------------------
Every wrong number in this pipeline that is NOT a threshold bug is a
day-attribution bug or a unit bug. Both are silent by construction: a weight
attributed to the wrong day still looks like a plausible weight, and a weight
read in grams as kilograms is off by 1000 (loud) but a weight read at the wrong
time of day is off by 0.8 (invisible).

The October clock change (BST +1h -> GMT +0h) happens mid-campaign, so the
offset handling is exercised for real in week ~9.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from coach_sync import transform

from conftest import exercise_point, weight_point


# ------------------------------------------------------------ local_datetime

def test_bst_offset_shifts_the_instant_by_an_hour():
    """23:30Z + 1h BST = 00:30 the NEXT local day. Getting this wrong moves a
    late-evening session (or weigh-in) onto the wrong calendar day."""
    got = transform.local_datetime(
        {"startTime": "2026-10-25T23:30:00Z", "startUtcOffset": "3600s"})
    assert got.date() == date(2026, 10, 26)
    assert got.strftime("%H:%M") == "00:30"


def test_gmt_offset_of_zero_leaves_the_instant_alone():
    """REGRESSION GUARD for the October clock change.

    `local_datetime` ends `return moment + timedelta(...) if offset else moment`.
    A GMT offset parses to 0.0, which is FALSY, so the shift branch is skipped.
    Adding zero and skipping the addition happen to agree, but only by luck —
    if the offset handling is ever refactored, this test is what catches a
    regression on the GMT side of the clock change.
    """
    got = transform.local_datetime(
        {"startTime": "2026-10-26T23:30:00Z", "startUtcOffset": "0s"})
    assert got.date() == date(2026, 10, 26)
    assert got.strftime("%H:%M") == "23:30"


def test_the_same_wall_clock_time_maps_to_the_same_local_hour_either_side_of_the_change():
    """21:00 local is 20:00Z in BST and 21:00Z in GMT. Both must come back as
    21:00 local — otherwise the evening-session filter and the sleep 18:00
    night-attribution rule shift by an hour when the clocks go back."""
    bst = transform.local_datetime(
        {"startTime": "2026-10-24T20:00:00Z", "startUtcOffset": "3600s"})
    gmt = transform.local_datetime(
        {"startTime": "2026-10-31T21:00:00Z", "startUtcOffset": "0s"})
    assert bst.strftime("%H:%M") == gmt.strftime("%H:%M") == "21:00"


def test_a_negative_offset_is_applied_as_a_subtraction():
    """Two of the four confirmed trips are westward. A US-timezone session must
    not land on the following local day."""
    got = transform.local_datetime(
        {"startTime": "2026-09-20T02:30:00Z", "startUtcOffset": "-18000s"})
    assert got.date() == date(2026, 9, 19)
    assert got.strftime("%H:%M") == "21:30"


def test_missing_offset_falls_back_to_the_raw_instant():
    got = transform.local_datetime({"startTime": "2026-10-26T23:30:00Z"})
    assert got.date() == date(2026, 10, 26)


def test_missing_start_time_returns_none_rather_than_guessing():
    assert transform.local_datetime({}) is None
    assert transform.local_datetime({"startTime": ""}) is None


def test_unparseable_timestamp_returns_none_rather_than_raising():
    assert transform.local_datetime({"startTime": "not-a-time"}) is None


def test_physical_time_is_used_when_the_named_key_is_absent():
    """parse_scalar and parse_exercise call this with different key names; the
    fallback is what makes one helper serve both record shapes."""
    got = transform.local_datetime({"physicalTime": "2026-08-30T08:59:51Z",
                                    "utcOffset": "3600s"})
    assert got.strftime("%H:%M") == "09:59"


# ------------------------------------------------------------ sleep night attribution

def _sleep(start, end, offset="3600s"):
    return {"sleep": {"interval": {"startTime": start, "startUtcOffset": offset,
                                   "endTime": end, "endUtcOffset": offset}}}


@pytest.mark.parametrize("start,offset,expected_night", [
    # 18:00 local is the cutoff: at or after it, the night is "today"; before
    # it, the reading is treated as the tail of the PREVIOUS night.
    ("2026-08-26T17:00:00Z", "3600s", date(2026, 8, 26)),   # 18:00 local exactly
    ("2026-08-26T16:59:00Z", "3600s", date(2026, 8, 25)),   # 17:59 local — day nap
    ("2026-08-26T23:20:00Z", "3600s", date(2026, 8, 26)),   # 00:20 next day -> that night
    ("2026-08-26T21:45:00Z", "3600s", date(2026, 8, 26)),   # 22:45 local — early night
])
def test_night_attribution_boundary(start, offset, expected_night):
    """Boundary-value analysis on the 18:00 cutoff.

    Note the near-midnight cases: the +1h offset pushes 23:20Z to 00:20 the next
    LOCAL day, and the `< 18:00 -> previous day` rule then pulls it back. The two
    corrections cancel, which is why an offset bug here is invisible around
    midnight and only shows up in the 17:00-18:00Z band — see the test below.
    """
    sleep, unparsed = transform.parse_sleep([_sleep(start, start, offset)])
    assert unparsed == 0
    assert list(sleep) == [expected_night]


def test_the_offset_changes_the_attributed_night_in_the_17_to_18z_band():
    """The one window where dropping the offset actually moves a night.

    17:30Z is 18:30 local under BST (night = the 25th) but 17:30 local under GMT
    (night = the 24th). If the offset were ignored, every BST reading in this
    band would be filed a day early and the 7-day sleep window would misalign.
    """
    bst, _ = transform.parse_sleep(
        [_sleep("2026-10-25T17:30:00Z", "2026-10-26T01:30:00Z", "3600s")])
    gmt, _ = transform.parse_sleep(
        [_sleep("2026-10-25T17:30:00Z", "2026-10-26T01:30:00Z", "0s")])
    assert list(bst) == [date(2026, 10, 25)]
    assert list(gmt) == [date(2026, 10, 24)]


def test_sleep_duration_is_computed_from_the_raw_utc_instants():
    """Duration must use the UNSHIFTED instants: shifting both ends by the same
    offset cancels, but shifting one would silently add or drop an hour."""
    sleep, _ = transform.parse_sleep(
        [_sleep("2026-08-30T00:11:00Z", "2026-08-30T08:31:00Z")])
    assert list(sleep.values())[0]["sleep_hours"] == 8.33


def test_sleep_with_no_end_time_still_records_the_bedtime():
    """Partial data must degrade to a null duration, not drop the night."""
    point = {"sleep": {"interval": {"startTime": "2026-08-30T00:11:00Z",
                                    "startUtcOffset": "3600s"}}}
    sleep, unparsed = transform.parse_sleep([point])
    assert unparsed == 0
    night = list(sleep.values())[0]
    assert night["sleep_hours"] is None
    assert night["sleep_bedtime"] == "01:11"


def test_a_malformed_sleep_point_is_counted_not_swallowed():
    """`unparsed` is the pipeline's only smoke alarm. If bad points were
    dropped silently, a schema change would look like 'he stopped sleeping'."""
    sleep, unparsed = transform.parse_sleep([{"sleep": "nonsense"}, {}])
    assert sleep == {}
    assert unparsed == 2


def test_two_sleep_sessions_on_one_night_keep_the_last_written():
    """Documents current behaviour: a nap plus a night collapse to one entry.
    Not obviously wrong, but it IS a silent overwrite — pin it so a change shows."""
    sleep, _ = transform.parse_sleep([
        _sleep("2026-08-26T18:00:00Z", "2026-08-26T19:00:00Z"),
        _sleep("2026-08-26T23:00:00Z", "2026-08-27T06:00:00Z"),
    ])
    assert len(sleep) == 1


# ------------------------------------------------------------ units

def test_weight_grams_to_kilograms_rounds_to_two_places():
    values, unparsed = transform.parse_scalar(
        "weight", [weight_point("2026-08-30T08:59:51Z", 84350)])
    assert unparsed == 0 and values[date(2026, 8, 30)] == 84.35


def test_weight_uses_the_local_day_not_the_utc_day():
    """23:40Z in BST is 00:40 the next local morning. Attributing it to the UTC
    day would put two weigh-ins on one date and none on the next."""
    values, _ = transform.parse_scalar(
        "weight", [weight_point("2026-08-29T23:40:00Z", 84000)])
    assert list(values) == [date(2026, 8, 30)]


def test_two_weigh_ins_in_a_day_keeps_the_morning_one():
    """Points supplied in the order the live API returns them: newest first."""
    values, _ = transform.parse_scalar("weight", [
        weight_point("2026-08-30T20:10:00Z", 85200),   # 21:10 local, after dinner
        weight_point("2026-08-30T07:05:00Z", 84350),   # 08:05 local, protocol
    ])
    assert values[date(2026, 8, 30)] == 84.35


def test_weight_first_wins_rule_is_positional_not_chronological():
    """The control for the xfail above: fed oldest-first, the rule does what the
    docstring promises. That pins the defect precisely to the ORDERING
    assumption rather than to the dedup rule itself."""
    values, _ = transform.parse_scalar("weight", [
        weight_point("2026-08-30T07:05:00Z", 84350),
        weight_point("2026-08-30T20:10:00Z", 85200),
    ])
    assert values[date(2026, 8, 30)] == 84.35


def test_resting_hr_arrives_as_a_protobuf_string_and_is_coerced():
    points = [{"dailyRestingHeartRate": {"date": {"year": 2026, "month": 8, "day": 30},
                                         "beatsPerMinute": "47"}}]
    values, unparsed = transform.parse_scalar("daily_resting_heart_rate", points)
    assert unparsed == 0 and values[date(2026, 8, 30)] == 47.0


def test_body_fat_percentage_is_taken_from_the_percentage_field():
    points = [{"bodyFat": {"sampleTime": {"physicalTime": "2026-08-30T08:59:51Z",
                                          "utcOffset": "3600s"},
                           "percentage": 19.4}}]
    values, unparsed = transform.parse_scalar("body_fat", points)
    assert unparsed == 0 and values[date(2026, 8, 30)] == 19.4


def test_a_point_missing_its_payload_is_counted_as_unparsed():
    values, unparsed = transform.parse_scalar("weight", [{}, {"weight": None},
                                                         {"weight": []}])
    assert values == {} and unparsed == 3


def test_a_weight_point_with_no_value_is_counted_not_written_as_zero():
    """A 0 kg body weight in the trend line would be catastrophic and obvious;
    what we actually want is for it never to be written."""
    points = [{"weight": {"sampleTime": {"physicalTime": "2026-08-30T08:00:00Z",
                                         "utcOffset": "3600s"}}}]
    values, unparsed = transform.parse_scalar("weight", points)
    assert values == {} and unparsed == 1


@pytest.mark.parametrize("raw,expected", [
    ("3763s", 3763.0), ("0s", 0.0), ("-18000s", -18000.0),
    (1680, 1680.0), (1680.5, 1680.5), (None, None),
    ("", None), ("s", None), ("abc", None), ("3763", 3763.0),
])
def test_protobuf_duration_parsing(raw, expected):
    assert transform.parse_duration_seconds(raw) == expected


def test_a_sub_ten_minute_record_is_noise_not_a_session():
    """Observed live: a 1.4 min passive STRENGTH_TRAINING at 21:15 while cooking.
    Each phantom inflates the adherence numerator by a whole session."""
    assert transform.parse_exercise(
        [exercise_point("STRENGTH_TRAINING", "2026-08-26T20:15:00Z", "84s",
                        "PASSIVELY_MEASURED")]) == []


@pytest.mark.parametrize("seconds,kept", [
    ("599s", False),    # 9m59 - noise
    ("600s", True),     # exactly 10m - a session, by the stated rule
    ("601s", True),
])
def test_minimum_session_length_boundary(seconds, kept):
    """Boundary-value analysis: the filter is `< MIN * 60`, so exactly 10:00 is
    kept. Worth pinning because it is the line between 'trained' and 'didn't'."""
    rows = transform.parse_exercise(
        [exercise_point("TENNIS", "2026-08-26T17:00:00Z", seconds)])
    assert bool(rows) is kept


def test_a_session_with_no_duration_is_kept_rather_than_filtered_as_noise():
    """`seconds is None` skips the length filter. Documents that a missing
    duration errs towards counting the session, not towards dropping it."""
    point = exercise_point("TENNIS", "2026-08-26T17:00:00Z")
    del point["exercise"]["activeDuration"]
    rows = transform.parse_exercise([point])
    assert len(rows) == 1 and rows[0]["duration_min"] == ""
