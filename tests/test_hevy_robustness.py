"""hevy.parse_workouts / gym_sessions / label_anchor_slots — malformed input.

WHY MOSTLY ROBUSTNESS TESTS HERE
--------------------------------
The Hevy schema is *published* (an OpenAPI spec), unlike the Google Health one
which was inferred. So the interesting risk is not "did we guess the shape
right" — it is "what happens on the day the shape is not what the spec says":
an in-progress workout with no sets, a bodyweight exercise with null weight, a
superset with a missing index. Those produce rows, and a wrong row here walks
next week's prescribed load in the wrong direction via the top-set.
"""
from __future__ import annotations

import pytest

from coach_sync import campaign, hevy

from conftest import day_in_week, hevy_workout


def test_empty_payload_is_an_empty_result_not_a_crash():
    assert hevy.parse_workouts([]) == []
    assert hevy.gym_sessions([]) == []
    assert hevy.label_anchor_slots([]) == {}


@pytest.mark.parametrize("workout", [
    {"id": "w", "start_time": "2026-08-24T17:30:00+00:00"},              # no exercises
    {"id": "w", "start_time": "2026-08-24T17:30:00+00:00", "exercises": None},
    {"id": "w", "start_time": "2026-08-24T17:30:00+00:00", "exercises": []},
], ids=["missing", "null", "empty"])
def test_a_workout_with_no_exercises_yields_no_rows(workout):
    """A workout started and abandoned. Must not raise, must not invent a set."""
    assert hevy.parse_workouts([workout]) == []


@pytest.mark.parametrize("sets", [None, []], ids=["null", "empty"])
def test_an_exercise_with_no_sets_yields_no_rows(sets):
    assert hevy.parse_workouts([hevy_workout(sets)]) == []


@pytest.mark.parametrize("start", [None, "", "yesterday", 12345, {"x": 1}])
def test_a_workout_with_an_unusable_start_time_is_skipped_not_fatal(start):
    """Skipping is the right call: a set with no date cannot be assigned to a
    campaign week, so it could only pollute the wrong week."""
    w = hevy_workout([{"index": 0, "type": "normal", "weight_kg": 70, "reps": 5}])
    w["start_time"] = start
    assert hevy.parse_workouts([w]) == []


def test_null_weight_sets_are_kept_as_rows_but_never_ranked_or_estimated():
    """Bodyweight and duration-only sets have weight_kg: null. They are real
    training (they belong in lifts.csv) but a null cannot be a top set and
    an Epley estimate off a null would be nonsense."""
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "normal", "weight_kg": None, "reps": 12, "rpe": 8},
    ])])
    assert len(rows) == 1
    assert rows[0]["weight_kg"] == ""
    assert rows[0]["is_top_set"] is False
    assert rows[0]["est_1rm_epley"] == ""


def test_a_zero_weight_set_is_recorded_as_zero_not_as_blank():
    """0 kg (assisted / bar-only) is a measurement. Blanking it would make an
    exercise look untrained. Epley is still skipped — 0 x 1.16 is not a 1RM."""
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "normal", "weight_kg": 0, "reps": 10, "rpe": 7},
    ])])
    assert rows[0]["weight_kg"] == 0
    assert rows[0]["est_1rm_epley"] == ""


def test_a_working_set_with_null_reps_gets_no_epley_estimate():
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "normal", "weight_kg": 70, "reps": None},
    ])])
    assert rows[0]["reps"] == ""
    assert rows[0]["est_1rm_epley"] == ""


def test_an_all_warmup_exercise_has_no_top_set():
    """Nothing counted, so nothing to autoregulate from. Must not fall back to
    the heaviest warm-up."""
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "warmup", "weight_kg": 60, "reps": 5},
        {"index": 1, "type": "warmup", "weight_kg": 80, "reps": 3},
    ])])
    assert [r["is_top_set"] for r in rows] == [False, False]


def test_an_all_null_weight_exercise_has_no_top_set():
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "normal", "weight_kg": None, "reps": 12},
        {"index": 1, "type": "normal", "weight_kg": None, "reps": 10},
    ])])
    assert not any(r["is_top_set"] for r in rows)


@pytest.mark.parametrize("set_type", ["normal", "failure", "dropset"])
def test_all_three_working_set_types_can_be_the_top_set(set_type):
    """A dropset or a set taken to failure is still a working set. Excluding
    either would drop the heaviest evidence of the session."""
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "warmup", "weight_kg": 200, "reps": 1},
        {"index": 1, "type": set_type, "weight_kg": 70, "reps": 5},
    ])])
    tops = [r for r in rows if r["is_top_set"]]
    assert len(tops) == 1 and tops[0]["weight_kg"] == 70


def test_an_unknown_set_type_is_treated_as_non_working():
    """Fail closed: a new Hevy set type must not silently become a top set."""
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "brand_new_type", "weight_kg": 999, "reps": 5},
        {"index": 1, "type": "normal", "weight_kg": 70, "reps": 5},
    ])])
    tops = [r for r in rows if r["is_top_set"]]
    assert len(tops) == 1 and tops[0]["weight_kg"] == 70


def test_top_set_is_chosen_per_exercise_not_per_workout():
    """Autoregulation is per lift. One top set for the whole session would
    leave every other lift without a load reference."""
    workout = {"id": "w", "title": "A1", "start_time": "2026-08-24T17:30:00+00:00",
               "exercises": [
                   {"index": 0, "title": "Squat",
                    "sets": [{"index": 0, "type": "normal", "weight_kg": 100, "reps": 5}]},
                   {"index": 1, "title": "Bench",
                    "sets": [{"index": 0, "type": "normal", "weight_kg": 60, "reps": 5}]}]}
    rows = hevy.parse_workouts([workout])
    tops = {r["exercise"] for r in rows if r["is_top_set"]}
    assert tops == {"Squat", "Bench"}


@pytest.mark.xfail(strict=True, reason=(
    "BUG: the code comment promises 'EXACTLY ONE top set per exercise per "
    "workout', but the top set is computed per exercise ENTRY. Hevy lets the "
    "same lift appear as two entries in one workout (a back-off block, or a "
    "re-add after a superset) and then lifts.csv carries two rows flagged "
    "is_top_set for the same lift — one heavy, one light. A consumer that takes "
    "the last match reads the back-off weight as the top set and walks next "
    "week's prescription DOWN. Observed in 3 of 75 real captured workouts."))
def test_BUG_the_same_exercise_twice_in_one_workout_produces_two_top_sets():
    """Shape taken from a real capture: 4 sets topping at 30 kg, then the same
    lift re-entered later in the session at 14 kg."""
    workout = {"id": "w", "start_time": "2026-08-24T17:30:00+00:00", "exercises": [
        {"index": 1, "title": "Chest Supported Incline Row (Dumbbell)",
         "sets": [{"index": 0, "type": "normal", "weight_kg": 25, "reps": 8},
                  {"index": 1, "type": "normal", "weight_kg": 30, "reps": 8}]},
        {"index": 4, "title": "Chest Supported Incline Row (Dumbbell)",
         "sets": [{"index": 0, "type": "normal", "weight_kg": 10, "reps": 10},
                  {"index": 1, "type": "normal", "weight_kg": 14, "reps": 10}]}]}
    rows = hevy.parse_workouts([workout])
    tops = [r for r in rows if r["is_top_set"]]
    assert len(tops) == 1 and tops[0]["weight_kg"] == 30


@pytest.mark.xfail(strict=True, reason=(
    "BUG (low severity): a set with no `index` makes top_index None, which "
    "disables top-set flagging for the ENTIRE exercise, including the sets that "
    "do have an index. A single malformed set silently removes that lift from "
    "the autoregulation loop."))
def test_BUG_one_set_missing_its_index_does_not_disable_the_whole_exercise():
    rows = hevy.parse_workouts([hevy_workout([
        {"index": 0, "type": "normal", "weight_kg": 70, "reps": 5},
        {"type": "normal", "weight_kg": 90, "reps": 3},          # no index
    ])])
    assert any(r["is_top_set"] for r in rows)


@pytest.mark.xfail(strict=True, reason=(
    "BUG (low severity): parse_workouts defaults a missing workout `id` to \"\", "
    "and gym_sessions dedupes on that key — so N id-less workouts collapse into "
    "ONE gym session, under-reporting a1_done/a2_done."))
def test_BUG_workouts_with_no_id_are_not_collapsed_into_one_session():
    a = hevy_workout([{"index": 0, "type": "normal", "weight_kg": 70, "reps": 5}],
                     start="2026-08-24T17:30:00+00:00")
    b = hevy_workout([{"index": 0, "type": "normal", "weight_kg": 80, "reps": 5}],
                     start="2026-08-27T17:30:00+00:00")
    del a["id"], b["id"]
    assert len(hevy.gym_sessions(hevy.parse_workouts([a, b]))) == 2


@pytest.mark.xfail(strict=True, reason=(
    "BUG (low severity): a non-dict entry in `exercises` raises AttributeError "
    "and aborts the whole build. Elsewhere the pipeline counts bad records; "
    "here it dies."))
def test_BUG_a_malformed_exercise_entry_does_not_abort_the_run():
    w = {"id": "w", "start_time": "2026-08-24T17:30:00+00:00",
         "exercises": ["not-a-dict",
                       {"index": 1, "title": "Squat",
                        "sets": [{"index": 0, "type": "normal",
                                  "weight_kg": 70, "reps": 5}]}]}
    assert len(hevy.parse_workouts([w])) == 1


# ------------------------------------------------------------ anchor slots

def test_anchor_slots_number_gym_sessions_in_order_within_each_campaign_week():
    """Config-derived dates: the gym follows the office, so ORDER within the
    week is the signal, not the weekday."""
    sessions = [
        {"date": day_in_week(1, 0).isoformat(), "start_time": "17:30",
         "workout_id": "a", "title": "A1"},
        {"date": day_in_week(1, 3).isoformat(), "start_time": "18:00",
         "workout_id": "b", "title": "A2"},
        {"date": day_in_week(2, 0).isoformat(), "start_time": "17:30",
         "workout_id": "c", "title": "A1"},
    ]
    labels = hevy.label_anchor_slots(sessions)
    assert labels == {"a": "A1", "b": "A2", "c": "A1"}


def test_a_third_gym_session_in_a_week_is_labelled_a3_not_silently_dropped():
    """The plan caps gym at two. A third is an adherence SIGNAL (over-reaching),
    so it must stay visible rather than be clipped to A2."""
    sessions = [{"date": day_in_week(1, i).isoformat(), "start_time": "17:30",
                 "workout_id": str(i), "title": ""} for i in (0, 2, 4)]
    assert hevy.label_anchor_slots(sessions)["4"] == "A3"


def test_gym_sessions_outside_the_campaign_window_get_no_slot():
    from datetime import timedelta
    outside = (campaign.CAMPAIGN_START - timedelta(days=7)).isoformat()
    labels = hevy.label_anchor_slots(
        [{"date": outside, "start_time": "17:30", "workout_id": "x", "title": ""}])
    assert labels == {}


def test_gym_sessions_are_returned_sorted_by_date_then_time():
    """label_anchor_slots numbers by list order, so an unsorted list would
    label the LATER session A1. The sort in gym_sessions is load-bearing."""
    rows = hevy.parse_workouts([
        hevy_workout([{"index": 0, "type": "normal", "weight_kg": 70, "reps": 5}],
                     wid="late", start="2026-08-27T18:00:00+00:00"),
        hevy_workout([{"index": 0, "type": "normal", "weight_kg": 70, "reps": 5}],
                     wid="early", start="2026-08-24T17:30:00+00:00"),
    ])
    assert [s["workout_id"] for s in hevy.gym_sessions(rows)] == ["early", "late"]


def test_epley_matches_the_named_formula():
    """Named deliberately: the campaign's starting loads came from a different
    estimator, so the numbers will not agree with the 19 Aug baseline. Pinning
    the formula stops that known discrepancy drifting into an unknown one."""
    assert hevy.epley_1rm(70, 5) == round(70 * (1 + 5 / 30.0), 1) == 81.7
    assert hevy.epley_1rm(100, 1) == 103.3
