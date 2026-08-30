"""Tests for the coaching-critical logic.

These cover the branches where a wrong number changes a plan decision: the lean
floor, the rate cap, night attribution, and thin-week visibility. Run with:

    .venv/bin/python -m pytest tests/ -q
"""
from datetime import date

from coach_sync import campaign, transform


def _daily(**kw):
    row = {"date": "2026-08-24", "weight_kg": "", "body_fat_pct": "", "lean_kg": "",
           "resting_hr": "", "hrv_rmssd": "", "sleep_hours": "", "campaign_week": 1}
    row.update(kw)
    return row


def _sleep_point(start, end, offset="3600s"):
    """Live response shape, observed 2026-08-30."""
    return {"sleep": {"interval": {"startTime": start, "startUtcOffset": offset,
                                   "endTime": end, "endUtcOffset": offset}}}


def test_bedtime_after_midnight_belongs_to_previous_night():
    """A 00:20 local bedtime is the night that began the evening before.

    Without this the week's sleep lands on the wrong dates and the 7-day mean is
    built from a misaligned window.
    """
    sleep, _ = transform.parse_sleep(
        [_sleep_point("2026-08-24T23:20:00Z", "2026-08-25T07:20:00Z")]
    )
    assert date(2026, 8, 24) in sleep, sleep


def test_bedtime_before_midnight_belongs_to_same_night():
    sleep, _ = transform.parse_sleep(
        [_sleep_point("2026-08-26T21:45:00Z", "2026-08-27T05:30:00Z")]
    )
    assert date(2026, 8, 26) in sleep, sleep


def test_utc_offset_is_applied_to_day_boundaries():
    """22:30Z + 1h BST = 23:30 local, so it is that evening's night, not the
    previous one. Ignoring the offset drifts the trend line by a day."""
    sleep, _ = transform.parse_sleep(
        [_sleep_point("2026-08-26T22:30:00Z", "2026-08-27T06:30:00Z")]
    )
    assert date(2026, 8, 26) in sleep, sleep


def test_weight_is_converted_from_grams():
    """Live payload is weightGrams: 84350. Missing this parsed 0 of 7 points."""
    points = [{"weight": {"sampleTime": {"physicalTime": "2026-08-30T08:59:51Z",
                                         "utcOffset": "3600s"},
                          "weightGrams": 84350}}]
    values, unparsed = transform.parse_scalar("weight", points)
    assert unparsed == 0
    assert values[date(2026, 8, 30)] == 84.35


def test_walking_is_not_a_training_session():
    """The watch logs ~9 passive walks a week; counting them inflates adherence."""
    points = [
        {"exercise": {"interval": {"startTime": "2026-08-29T13:58:20Z",
                                   "startUtcOffset": "3600s"},
                      "exerciseType": "WALKING", "activeDuration": "1680s"},
         "dataSource": {"recordingMethod": "PASSIVELY_MEASURED"}},
        {"exercise": {"interval": {"startTime": "2026-08-28T15:03:57Z",
                                   "startUtcOffset": "3600s"},
                      "exerciseType": "TENNIS", "activeDuration": "3763s"},
         "dataSource": {"recordingMethod": "ACTIVELY_MEASURED"}},
    ]
    rows = transform.parse_exercise(points)
    assert [r["exercise_type"] for r in rows] == ["TENNIS"]


def test_duplicate_session_prefers_actively_measured():
    """The watch records one session twice when it auto-detects a manual start."""
    def point(method, start):
        return {"exercise": {"interval": {"startTime": start,
                                          "startUtcOffset": "3600s"},
                             "exerciseType": "TENNIS", "activeDuration": "3600s"},
                "dataSource": {"recordingMethod": method}}
    rows = transform.parse_exercise([
        point("PASSIVELY_MEASURED", "2026-08-28T15:00:00Z"),
        point("ACTIVELY_MEASURED", "2026-08-28T15:05:00Z"),
    ])
    assert len(rows) == 1
    assert rows[0]["recording_method"] == "ACTIVELY_MEASURED"


def test_hrv_uses_the_field_the_baseline_was_computed_from():
    """Two HRV fields are returned. averageHeartRateVariabilityMilliseconds is
    the one SKILL.md's "102 ms mean, swings 56-176" baseline came from; the
    deep-sleep RMSSD field is on a different scale."""
    points = [{"dailyHeartRateVariability": {
        "date": {"year": 2026, "month": 8, "day": 30},
        "averageHeartRateVariabilityMilliseconds": 78.8,
        "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 51.75,
    }}]
    values, unparsed = transform.parse_scalar("daily_heart_rate_variability", points)
    assert unparsed == 0
    assert values[date(2026, 8, 30)] == 78.8


def test_resting_hr_string_is_coerced():
    """beatsPerMinute arrives as a JSON string: "47"."""
    points = [{"dailyRestingHeartRate": {
        "date": {"year": 2026, "month": 8, "day": 30},
        "beatsPerMinute": "47"}}]
    values, unparsed = transform.parse_scalar("daily_resting_heart_rate", points)
    assert unparsed == 0
    assert values[date(2026, 8, 30)] == 47.0


def test_sub_ten_minute_session_is_noise_not_training():
    """Observed live: a 1.4 min passive STRENGTH_TRAINING at 21:15."""
    points = [{"exercise": {"interval": {"startTime": "2026-08-26T20:15:00Z",
                                         "startUtcOffset": "3600s"},
                            "exerciseType": "STRENGTH_TRAINING",
                            "activeDuration": "84s"},
               "dataSource": {"recordingMethod": "PASSIVELY_MEASURED"}}]
    assert transform.parse_exercise(points) == []


def test_protobuf_duration_strings_are_parsed():
    assert transform.parse_duration_seconds("3763s") == 3763.0
    assert transform.parse_duration_seconds(None) is None


def test_lean_floor_breach_fires_below_the_floor():
    """The one rule that overrides everything else in the campaign."""
    rows = [_daily(date="2026-11-0%d" % (2 + i), weight_kg=80.0,
                   body_fat_pct=17.0, lean_kg=campaign.LEAN_FLOOR_KG - 0.6,
                   campaign_week=11)
            for i in range(transform.MIN_WEIGHINS_FOR_FLAGS)]
    assert transform.build_weekly(rows, [])[0]["lean_floor_breach"] is True


def test_lean_floor_not_breached_above_the_floor():
    """Derived from campaign.LEAN_FLOOR_KG, not hard-coded: the real floor lives
    in the gitignored campaign.toml, so a literal here fails on a fresh clone
    that only has campaign.example.toml."""
    rows = [_daily(date="2026-11-0%d" % (2 + i), weight_kg=80.0,
                   body_fat_pct=15.0, lean_kg=campaign.LEAN_FLOOR_KG + 1.0,
                   campaign_week=11)
            for i in range(transform.MIN_WEIGHINS_FOR_FLAGS)]
    assert transform.build_weekly(rows, [])[0]["lean_floor_breach"] is False


def test_thin_week_reports_its_weighin_count():
    """A 7-day mean over two readings is not a 7-day mean."""
    rows = [_daily(date="2026-09-01", weight_kg=84.0, campaign_week=2),
            _daily(date="2026-09-03", weight_kg=83.6, campaign_week=2)]
    week = transform.build_weekly(rows, [])[0]
    assert week["weighins_count"] == 2


def test_losing_faster_than_cap_is_flagged():
    """Above 0.6 kg/week costs lean mass — the plan says loosen."""
    rows = ([_daily(date="2026-11-1%d" % (6 + i), weight_kg=81.0,
                    campaign_week=13) for i in range(4)]
            + [_daily(date="2026-11-2%d" % (3 + i), weight_kg=80.1,
                      campaign_week=14) for i in range(4)])
    weeks = transform.build_weekly(rows, [])
    assert weeks[1]["losing_too_fast"] is True


def test_lean_is_null_when_body_fat_missing():
    """Never invent a body-fat reading to complete a derivation."""
    rows = transform.build_daily({"weight": {date(2026, 8, 24): 84.0}})
    assert rows[0]["lean_kg"] == ""


def test_maintenance_weeks_match_the_plan():
    assert campaign.MAINTENANCE_WEEKS == {4, 5, 14, 18}


def test_every_benchmark_falls_inside_the_campaign_and_on_a_distinct_week():
    """Property, not literals: a benchmark dated outside the campaign silently
    never appears in metrics_weekly.csv, and two benchmarks in one week means
    one of them is overwritten by the other (build_weekly keeps the last)."""
    weeks = {label: campaign.week_number(d)
             for d, label in campaign.BENCHMARKS.items()}
    assert all(w is not None for w in weeks.values()), weeks
    assert len(set(weeks.values())) == len(weeks), weeks


def test_datatype_path_derivation_matches_verified_api_paths():
    """D-016: hyphens in paths, underscores in filters — derive, never hand-write."""
    from coach_sync.datatypes import REGISTRY
    assert REGISTRY["body_fat"].path == "body-fat"
    assert REGISTRY["daily_resting_heart_rate"].path == "daily-resting-heart-rate"
    assert REGISTRY["body_fat"].payload_key == "bodyFat"
    assert REGISTRY["daily_resting_heart_rate"].payload_key == "dailyRestingHeartRate"


def test_daily_types_filter_on_civil_date_not_timestamp():
    """Passing a timestamp to a daily type returns
    INVALID_DATA_POINT_FILTER_CIVIL_DATE_TIME_FORMAT."""
    from coach_sync.datatypes import REGISTRY
    daily = REGISTRY["daily_resting_heart_rate"].filter_expr(date(2026, 8, 24))
    sample = REGISTRY["weight"].filter_expr(date(2026, 8, 24))
    assert daily.endswith('>= "2026-08-24"')
    assert sample.endswith('>= "2026-08-24T00:00:00Z"')


# ---------------------------------------------------------------- Hevy

def _workout(sets, wid="w1", start="2026-08-24T17:30:00Z", title="Morning Workout"):
    """Shape from Hevy's published OpenAPI spec."""
    return {"id": wid, "title": title, "start_time": start,
            "end_time": "2026-08-24T18:20:00Z",
            "exercises": [{"index": 0, "title": "Squat (Barbell)",
                           "exercise_template_id": "ABC", "sets": sets}]}


def test_warmup_sets_do_not_become_the_top_set():
    """Top set drives next week's load. A warm-up counted as the top set would
    walk the prescription downwards week on week."""
    from coach_sync import hevy
    rows = hevy.parse_workouts([_workout([
        {"index": 0, "type": "warmup", "weight_kg": 100, "reps": 5, "rpe": None},
        {"index": 1, "type": "normal", "weight_kg": 70, "reps": 5, "rpe": 7},
        {"index": 2, "type": "normal", "weight_kg": 75, "reps": 5, "rpe": 8},
    ])])
    tops = [r for r in rows if r["is_top_set"]]
    assert len(tops) == 1
    assert tops[0]["weight_kg"] == 75


def test_exactly_one_top_set_when_sets_tie_on_weight():
    """A straight 3x5 at 70 has three sets at the same weight. Flagging all
    three makes "the top set" ambiguous for the autoregulation loop."""
    from coach_sync import hevy
    rows = hevy.parse_workouts([_workout([
        {"index": 0, "type": "normal", "weight_kg": 70, "reps": 5, "rpe": 7},
        {"index": 1, "type": "normal", "weight_kg": 70, "reps": 5, "rpe": 8},
        {"index": 2, "type": "normal", "weight_kg": 70, "reps": 5, "rpe": 9},
    ])])
    assert sum(1 for r in rows if r["is_top_set"]) == 1


def test_top_set_breaks_weight_ties_on_reps():
    from coach_sync import hevy
    rows = hevy.parse_workouts([_workout([
        {"index": 0, "type": "normal", "weight_kg": 70, "reps": 5, "rpe": 7},
        {"index": 1, "type": "normal", "weight_kg": 70, "reps": 8, "rpe": 9},
    ])])
    top = [r for r in rows if r["is_top_set"]]
    assert len(top) == 1 and top[0]["reps"] == 8


def test_epley_1rm_is_computed_for_working_sets_only():
    from coach_sync import hevy
    rows = hevy.parse_workouts([_workout([
        {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10, "rpe": None},
        {"index": 1, "type": "normal", "weight_kg": 70, "reps": 5, "rpe": 7},
    ])])
    assert rows[0]["est_1rm_epley"] == ""
    assert rows[1]["est_1rm_epley"] == hevy.epley_1rm(70, 5) == 81.7


def test_anchor_slots_are_ordered_within_the_week_not_by_weekday():
    """The gym follows the office, not the calendar — Mon/Thu shift week to
    week, so order within the campaign week is the reliable signal."""
    from coach_sync import hevy
    from datetime import timedelta
    w1 = campaign.week_bounds(1)[0]
    w2 = campaign.week_bounds(2)[0]
    sessions = [
        {"date": (w1 + timedelta(days=1)).isoformat(), "start_time": "18:00",
         "workout_id": "b", "title": ""},
        {"date": w1.isoformat(), "start_time": "17:30",
         "workout_id": "a", "title": ""},
        {"date": w2.isoformat(), "start_time": "17:30",
         "workout_id": "c", "title": ""},
    ]
    labels = hevy.label_anchor_slots(sorted(sessions, key=lambda s: s["date"]))
    assert labels["a"] == "A1"   # first of W1
    assert labels["b"] == "A2"   # second of W1
    assert labels["c"] == "A1"   # first of W2 — counter resets


def test_sets_with_no_weight_are_kept_but_not_ranked():
    """Bodyweight and duration-only sets have weight_kg: null."""
    from coach_sync import hevy
    rows = hevy.parse_workouts([_workout([
        {"index": 0, "type": "normal", "weight_kg": None, "reps": 12, "rpe": 8},
    ])])
    assert len(rows) == 1
    assert rows[0]["weight_kg"] == ""
    assert rows[0]["is_top_set"] is False
