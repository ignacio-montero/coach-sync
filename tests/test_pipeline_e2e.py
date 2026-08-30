"""End-to-end: real saved API responses -> the four CSVs.

WHY BOTH A SYNTHETIC E2E AND A REAL-DATA E2E
--------------------------------------------
The unit tests prove each function is right about the cases we THOUGHT of. They
cannot catch two other failure modes:

1. *Composition* bugs — each stage is fine, the wiring is not. `a1_done` is
   computed in __main__, not in build_weekly, and `write_csv` uses DictWriter,
   which fills a missing key with "" instead of complaining. So a column that
   never gets attached looks exactly like a week with no gym. Only an E2E over
   the real writer catches that.
2. *Schema drift* — the day Google renames `weightGrams`. The tests below are
   **characterisation tests** (a.k.a. golden / approval tests): they assert
   against what the live API actually returned on 2026-08-30 rather than against
   a hand-written fixture, so an upstream change shows up as a test failure
   instead of as a blank column in the coach's CSV.

The real-data tests SKIP when `data/` is absent — it is gitignored personal
health data and will not exist in CI or on a fresh clone. A skipped test is
honest; a test that passes because it silently found nothing is not.
"""
from __future__ import annotations

import csv
from datetime import date

import pytest

from coach_sync import campaign, hevy, transform
from coach_sync.__main__ import annotate_anchors, gym_as_sessions

from conftest import day_in_week, hevy_workout, weighed


def read_back(path):
    with path.open() as handle:
        return list(csv.DictReader(handle))


# ------------------------------------------------------------ composition

def test_annotate_anchors_attaches_a1_a2_to_every_weekly_row(tmp_path):
    """The two columns build_weekly does NOT produce. If annotate_anchors is
    ever skipped, DictWriter writes "" and the CSV looks like 'no gym this
    week' rather than 'this number was never computed'."""
    weekly = transform.build_weekly([weighed(1, 0, 84.0), weighed(2, 0, 83.6)], [])
    gym = [{"date": day_in_week(1, 0).isoformat(), "start_time": "17:30",
            "workout_id": "a", "title": "A1"},
           {"date": day_in_week(1, 3).isoformat(), "start_time": "18:00",
            "workout_id": "b", "title": "A2"}]
    annotate_anchors(weekly, gym)
    assert (weekly[0]["a1_done"], weekly[0]["a2_done"]) is not None
    assert weekly[0]["a1_done"] is True and weekly[0]["a2_done"] is True
    assert weekly[1]["a1_done"] is False and weekly[1]["a2_done"] is False


def test_one_gym_session_gives_a1_but_not_a2():
    """Boundary: >=1 and >=2. A single session must not satisfy both."""
    weekly = transform.build_weekly([weighed(1, 0, 84.0)], [])
    annotate_anchors(weekly, [{"date": day_in_week(1, 0).isoformat(),
                               "workout_id": "a"}])
    assert weekly[0]["a1_done"] is True and weekly[0]["a2_done"] is False


def test_hevy_workouts_re_enter_the_session_count_after_watch_dedup():
    """__main__ strips watch STRENGTH_TRAINING records on days Hevy also logged,
    then adds the Hevy workouts back. Drop either half and sessions_done is
    wrong in opposite directions."""
    gym = [{"date": day_in_week(1, 0).isoformat(), "workout_id": "a"}]
    weekly = transform.build_weekly([weighed(1, 0, 84.0)], gym_as_sessions(gym))
    assert weekly[0]["sessions_done"] == 1


def test_the_weekly_csv_round_trips_through_the_writer(tmp_path):
    """write_csv -> read back. Catches a column present in the dicts but missing
    from WEEKLY_COLUMNS (extrasaction='ignore' would drop it in silence)."""
    weekly = transform.build_weekly([weighed(1, 0, 84.0, 19.4)], [])
    annotate_anchors(weekly, [])
    path = tmp_path / "metrics_weekly.csv"
    transform.write_csv(path, transform.WEEKLY_COLUMNS, weekly)

    rows = read_back(path)
    assert len(rows) == 1
    assert list(rows[0]) == transform.WEEKLY_COLUMNS
    assert rows[0]["weight_7d_mean"] == "84.0"
    # Booleans survive the round trip as Python's repr, which is what the
    # coaching assistant reads. Pin it — "True"/"False", not "1"/"0".
    assert rows[0]["lean_floor_breach"] in ("True", "False")
    assert rows[0]["a1_done"] == "False"


def test_no_weekly_column_is_silently_dropped_by_the_writer(tmp_path):
    """extrasaction='ignore' means an extra key vanishes without error. Assert
    the produced keys are a SUBSET of the declared columns, so a new field added
    to build_weekly fails here instead of disappearing."""
    weekly = transform.build_weekly([weighed(1, 0, 84.0)], [])
    annotate_anchors(weekly, [])
    extra = set(weekly[0]) - set(transform.WEEKLY_COLUMNS)
    assert extra == set(), extra


def test_daily_csv_round_trips_and_derives_lean_mass(tmp_path):
    daily = transform.build_daily({
        "weight": {day_in_week(1, 0): 84.4},
        "body_fat": {day_in_week(1, 0): 19.4},
    })
    path = tmp_path / "metrics_daily.csv"
    transform.write_csv(path, transform.DAILY_COLUMNS, daily)
    row = read_back(path)[0]
    assert row["lean_kg"] == str(round(84.4 * 0.806, 2))
    assert row["campaign_week"] == "1"


def test_daily_rows_outside_the_campaign_get_a_blank_week_not_week_zero(tmp_path):
    """A pre-campaign weigh-in must not be filed into W01 and drag its mean."""
    from datetime import timedelta
    before = campaign.CAMPAIGN_START - timedelta(days=3)
    daily = transform.build_daily({"weight": {before: 85.0}})
    assert daily[0]["campaign_week"] == ""
    assert transform.build_weekly(daily, []) == []


# ------------------------------------------------------------ real captures

def test_real_weight_capture_still_parses(real_raw):
    """Characterisation test against the live 2026-08-30 response.

    Guards the unit trap the module docstring warns about: weight arrives in
    GRAMS. If this ever produces values around 84000, the CSV is 1000x wrong.
    """
    points = real_raw("weight")
    values, unparsed = transform.parse_scalar("weight", points)
    assert values, "parsed 0 of {} weight points — schema drift".format(len(points))
    assert unparsed == 0
    assert all(40.0 < v < 200.0 for v in values.values()), values


def test_real_body_fat_capture_is_a_percentage_not_a_fraction(real_raw):
    values, unparsed = transform.parse_scalar("body_fat", real_raw("body_fat"))
    assert values and unparsed == 0
    assert all(3.0 < v < 60.0 for v in values.values()), values


def test_real_hrv_capture_uses_the_baseline_field_not_the_deep_sleep_one(real_raw):
    """The two HRV fields are on different scales. The campaign baseline is
    ~102 ms from averageHeartRateVariabilityMilliseconds; the deep-sleep RMSSD
    field would silently break every comparison against it."""
    points = real_raw("daily_heart_rate_variability")
    values, unparsed = transform.parse_scalar("daily_heart_rate_variability", points)
    assert values and unparsed == 0
    for day, value in values.items():
        raw = next(p["dailyHeartRateVariability"] for p in points
                   if transform.to_date(p["dailyHeartRateVariability"]["date"]) == day)
        assert value == raw["averageHeartRateVariabilityMilliseconds"]


def test_real_resting_hr_capture_parses_the_protobuf_string(real_raw):
    values, unparsed = transform.parse_scalar(
        "daily_resting_heart_rate", real_raw("daily_resting_heart_rate"))
    assert values and unparsed == 0
    assert all(30.0 < v < 120.0 for v in values.values()), values


def test_real_sleep_capture_parses_and_produces_plausible_durations(real_raw):
    sleep, unparsed = transform.parse_sleep(real_raw("sleep"))
    assert sleep and unparsed == 0
    hours = [n["sleep_hours"] for n in sleep.values() if n["sleep_hours"] is not None]
    assert hours and all(0 < h < 16 for h in hours), hours


def test_real_exercise_capture_excludes_walking_and_keeps_training(real_raw):
    rows = transform.parse_exercise(real_raw("exercise"))
    assert rows
    assert "WALKING" not in {r["exercise_type"] for r in rows}
    assert all(r["duration_min"] == "" or r["duration_min"] >= 10.0 for r in rows)


def test_real_exercise_dedup_actually_removes_something(real_raw):
    """If dedup ever became a no-op, adherence would inflate and nothing would
    say so. The live capture is known to contain duplicate records."""
    points = real_raw("exercise")
    non_walking = [p for p in points
                   if p.get("exercise", {}).get("exerciseType") != "WALKING"]
    assert len(transform.parse_exercise(points)) < len(non_walking)


def test_real_hevy_capture_flattens_to_one_row_per_set(real_raw):
    workouts = real_raw("hevy_workouts")
    rows = hevy.parse_workouts(workouts)
    expected = sum(len(e.get("sets") or [])
                   for w in workouts for e in (w.get("exercises") or [])
                   if w.get("start_time"))
    assert len(rows) == expected, "sets lost or duplicated in the flatten"


def test_real_in_campaign_lifts_have_at_most_one_top_set_per_lift(real_raw):
    """Scoped to the campaign window, because that is what reaches lifts.csv.

    Over the FULL history this assertion fails: three of the 75 captured
    workouts list the same exercise as two separate entries, and each entry gets
    its own top set. See test_hevy_robustness.test_BUG_the_same_exercise_twice_
    in_one_workout_produces_two_top_sets — the defect is real, it just has not
    landed inside the campaign window yet.
    """
    from collections import Counter
    rows = [r for r in hevy.parse_workouts(real_raw("hevy_workouts"))
            if campaign.week_number(date.fromisoformat(r["date"]))]
    tops = Counter((r["workout_id"], r["exercise"]) for r in rows if r["is_top_set"])
    assert tops, "no in-campaign lifts in the capture — test proved nothing"
    assert max(tops.values()) == 1, tops.most_common(3)


def test_real_full_history_repeated_exercise_entries_are_still_rare(real_raw):
    """A canary on the bug above. It fires when a repeated-exercise workout
    enters the campaign window, at which point lifts.csv gains an ambiguous top
    set and the autoregulation loop can pick the lighter of the two."""
    from collections import Counter
    offenders = []
    for workout in real_raw("hevy_workouts"):
        counts = Counter(e.get("title") for e in (workout.get("exercises") or []))
        if any(v > 1 for v in counts.values()):
            day = date.fromisoformat(str(workout["start_time"])[:10])
            offenders.append((day, campaign.week_number(day)))
    in_campaign = [o for o in offenders if o[1] is not None]
    assert in_campaign == [], (
        "a workout with a repeated exercise entry is now inside the campaign: "
        "{} — lifts.csv now has two rows flagged is_top_set for one lift"
        .format(in_campaign))


def test_real_hevy_top_set_is_never_lighter_than_another_working_set(real_raw):
    """The property that makes the top set usable for autoregulation."""
    from collections import defaultdict
    rows = hevy.parse_workouts(real_raw("hevy_workouts"))
    groups = defaultdict(list)
    for row in rows:
        if row["set_type"] in hevy.WORKING_SET_TYPES and row["weight_kg"] != "":
            groups[(row["workout_id"], row["exercise"])].append(row)
    checked = 0
    for group in groups.values():
        tops = [r for r in group if r["is_top_set"]]
        if not tops:
            continue
        checked += 1
        assert tops[0]["weight_kg"] == max(r["weight_kg"] for r in group)
    assert checked > 0, "no working sets in the capture — test proved nothing"


def test_the_full_build_over_real_captures_produces_a_coherent_weekly_report(
        real_raw, tmp_path):
    """The whole pipeline, wired the way __main__ wires it, over real data.

    Deliberately asserts on INVARIANTS rather than on values: the values are
    personal health data and would have to be committed to assert on them.
    """
    parsed = {}
    for name in ("weight", "body_fat", "daily_resting_heart_rate",
                 "daily_heart_rate_variability"):
        parsed[name], _ = transform.parse_scalar(name, real_raw(name))
    parsed["sleep"], _ = transform.parse_sleep(real_raw("sleep"))

    win_start, win_end = campaign.CAMPAIGN_START, date.today()
    sessions = [r for r in transform.parse_exercise(real_raw("exercise"))
                if win_start <= date.fromisoformat(r["date"]) <= win_end]

    lifts = [r for r in hevy.parse_workouts(real_raw("hevy_workouts"))
             if win_start <= date.fromisoformat(r["date"]) <= win_end]
    gym = hevy.gym_sessions(lifts)
    gym_days = {g["date"] for g in gym}
    sessions = [s for s in sessions
                if not (s["exercise_type"] == "STRENGTH_TRAINING"
                        and s["date"] in gym_days)]

    daily = transform.build_daily(parsed)
    weekly = transform.build_weekly(daily, sessions + gym_as_sessions(gym), {})
    annotate_anchors(weekly, gym)

    assert daily and weekly

    # Daily rows are sorted, unique, and inside the campaign or blank-weeked.
    dates = [r["date"] for r in daily]
    assert dates == sorted(dates) == sorted(set(dates))

    for row in weekly:
        n = int(row["week"][1:])
        assert 1 <= n <= campaign.TOTAL_WEEKS
        assert row["week_start"] <= row["week_end"]
        assert 0 <= row["weighins_count"] <= 7
        # Tri-state by design: True / False / "" for UNKNOWN. A flag that had
        # nothing to check must not render as a confident False.
        assert row["lean_floor_breach"] in (True, False, "")
        assert row["losing_too_fast"] in (True, False, "")
        assert row["sessions_done"] >= 0
        if row["weight_7d_mean"] != "":
            assert 40.0 < row["weight_7d_mean"] < 200.0
        if row["lean_7d_mean"] != "" and row["weight_7d_mean"] != "":
            assert row["lean_7d_mean"] < row["weight_7d_mean"]
        # a2 implies a1 — a week cannot have a second gym session without a first
        assert not (row["a2_done"] and not row["a1_done"])

    # The CSV the coach reads must be writable and complete.
    path = tmp_path / "metrics_weekly.csv"
    transform.write_csv(path, transform.WEEKLY_COLUMNS, weekly)
    back = read_back(path)
    assert len(back) == len(weekly)
    assert list(back[0]) == transform.WEEKLY_COLUMNS


def test_real_capture_weeks_are_contiguous_so_far(real_raw):
    """A canary, not a rule. The campaign has four confirmed travel blocks, so
    this WILL start failing in September — and when it does, the gap-handling
    bugs (weight_delta_kg across a gap; a training week with no weigh-ins
    producing no row at all) become live rather than theoretical."""
    parsed = {}
    for name in ("weight", "body_fat"):
        parsed[name], _ = transform.parse_scalar(name, real_raw(name))
    weekly = transform.build_weekly(transform.build_daily(parsed), [])
    weeks = [int(r["week"][1:]) for r in weekly]
    if weeks:
        assert weeks == list(range(weeks[0], weeks[-1] + 1)), (
            "travel gap has arrived — re-read the xfailed gap tests in "
            "tests/test_weekly_aggregation.py, they are now affecting real output")
