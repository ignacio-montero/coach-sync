"""build_weekly — the file the coach actually reads.

BLAST RADIUS FIRST. Every column here maps to an instruction the coach gives:

    lean_floor_breach -> "cut the deficit NOW"     (worst possible false negative)
    losing_too_fast   -> "loosen the deficit"
    delta_vs_target   -> "am I on track"
    sessions_done     -> "tighten the plan, or fix adherence"

So these are *unit tests* (the logic is pure: dicts in, dicts out, no I/O) but
they are chosen by consequence, not by line coverage. A branch that can only
produce a cosmetic difference is not tested here; a branch that can flip a
coaching instruction is.

Tests named `test_BUG_*` are marked xfail(strict=True): they encode a defect
that exists today. `strict` means the suite goes RED if the bug is ever fixed
without removing the marker — so the marker cannot rot into a lie.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from coach_sync import campaign, transform

from conftest import daily_row, day_in_week, watch_session, weighed


def week(rows, sessions=(), manual=None, index=0):
    return transform.build_weekly(list(rows), list(sessions), manual)[index]


# ------------------------------------------------------- non-contiguous weeks
#
# Four confirmed trips means gaps in the weigh-in record are EXPECTED, not
# exceptional. Everything in this block is about what happens across a gap.

def test_weeks_with_no_data_at_all_are_simply_absent():
    """Documents the current contract: build_weekly emits a row only for weeks
    that appear in `daily`. Nothing invents an empty week."""
    rows = [weighed(1, 0, 84.0), weighed(4, 0, 82.4)]
    weeks = [r["week"] for r in transform.build_weekly(rows, [])]
    assert weeks == ["W01", "W04"]


@pytest.mark.xfail(strict=True, reason=(
    "BUG: weight_delta_kg carries `previous_weight` across skipped weeks, so a "
    "gap turns a multi-week change into a single-week delta. See report."))
def test_BUG_weight_delta_is_a_one_week_delta_even_across_a_gap():
    """-1.6 kg spread over three weeks is 0.53 kg/week, i.e. INSIDE the cap.

    Reported as a one-week delta it reads as -1.6 kg/week and trips
    `losing_too_fast`, which tells the coach to loosen a deficit that is fine.
    """
    rows = [weighed(1, 0, 84.0), weighed(4, 0, 82.4)]
    w4 = transform.build_weekly(rows, [])[1]
    assert w4["weight_delta_kg"] == "" or w4["weight_delta_kg"] > -0.6


def test_losing_too_fast_does_not_fire_on_a_gap_spanning_delta():
    rows = [weighed(1, 0, 84.0), weighed(4, 0, 82.4)]
    assert transform.build_weekly(rows, [])[1]["losing_too_fast"] is False


def test_rate_cap_fires_on_a_genuinely_contiguous_week():
    """The control for the two xfails above: consecutive weeks, real overshoot.
    Without this pair you cannot tell 'the flag works' from 'the flag is stuck on'."""
    over = campaign.MAX_SAFE_LOSS_KG_PER_WEEK + 0.2
    rows = [weighed(6, 0, 84.0), weighed(7, 0, 84.0 - over)]
    assert transform.build_weekly(rows, [])[1]["losing_too_fast"] is True


def test_rate_cap_does_not_fire_exactly_at_the_cap():
    """Boundary-value analysis: the condition is `< -cap`, so a loss of exactly
    the cap must NOT fire. Off-by-one at a threshold is the classic silent bug."""
    exact = campaign.MAX_SAFE_LOSS_KG_PER_WEEK
    rows = [weighed(6, 0, 84.0), weighed(7, 0, round(84.0 - exact, 2))]
    assert transform.build_weekly(rows, [])[1]["losing_too_fast"] is False


def test_gain_never_trips_the_loss_flag():
    rows = [weighed(6, 0, 82.0), weighed(7, 0, 83.5)]
    assert transform.build_weekly(rows, [])[1]["losing_too_fast"] is False


# ------------------------------------------------- previous week with no weigh-ins

def test_weight_delta_is_blank_when_there_is_no_earlier_weight_at_all():
    """First week of the campaign has nothing to diff against."""
    assert week([weighed(1, 0, 84.0)])["weight_delta_kg"] == ""


def test_a_week_with_only_sleep_does_not_reset_the_delta_chain():
    """W2 has sleep but no scale. W3's delta must still be computable, and must
    not silently become a delta-against-nothing."""
    rows = [weighed(1, 0, 84.0),
            daily_row(2, 0, sleep_hours=7.4),
            weighed(3, 0, 83.5)]
    weeks = transform.build_weekly(rows, [])
    assert [w["week"] for w in weeks] == ["W01", "W02", "W03"]
    assert weeks[1]["weight_7d_mean"] == ""
    assert weeks[1]["weight_delta_kg"] == ""
    assert weeks[1]["weighins_count"] == 0
    assert weeks[2]["weight_delta_kg"] == -0.5


def test_delta_vs_target_is_blank_rather_than_zero_when_the_week_has_no_weight():
    """A blank means 'unknown'. A 0 would read as 'exactly on target'."""
    assert week([daily_row(3, 0, resting_hr=48)])["delta_vs_target"] == ""


# ------------------------------------------------------------- the lean floor

def test_lean_floor_breach_fires_just_below_the_configured_floor():
    """Boundary-value analysis around the single most consequential threshold."""
    floor = campaign.LEAN_FLOOR_KG
    rows = [daily_row(11, 0, weight_kg=80.0, body_fat_pct=17.0,
                      lean_kg=round(floor - 0.1, 2))]
    assert week(rows)["lean_floor_breach"] is True


def test_lean_floor_breach_does_not_fire_exactly_at_the_floor():
    """Condition is `< floor`, so sitting exactly on it is not a breach."""
    rows = [daily_row(11, 0, weight_kg=80.0, body_fat_pct=17.0,
                      lean_kg=campaign.LEAN_FLOOR_KG)]
    assert week(rows)["lean_floor_breach"] is False


def test_lean_floor_uses_the_weekly_mean_not_a_single_bad_day():
    """One noisy BIA reading below the floor must not trigger the campaign's
    most disruptive instruction. The mean is the guard; pin it."""
    floor = campaign.LEAN_FLOOR_KG
    rows = [daily_row(11, 0, weight_kg=80.0, body_fat_pct=17.0,
                      lean_kg=round(floor - 1.0, 2)),
            daily_row(11, 1, weight_kg=80.0, body_fat_pct=15.0,
                      lean_kg=round(floor + 2.0, 2))]
    w = week(rows)
    assert w["lean_7d_mean"] == round(floor + 0.5, 2)
    assert w["lean_floor_breach"] is False


def test_lean_floor_breach_is_not_a_confident_false_when_lean_is_unknown():
    """The scale reports weight and BF% from the same step-on, but BF% is the
    flakier of the two and a travel week can produce weight-only rows."""
    w = week([weighed(11, 0, 80.0)])          # weight, no body fat
    assert w["lean_7d_mean"] == ""
    assert w["lean_floor_breach"] != False    # noqa: E712 - "" or None or True


def test_lean_mean_ignores_days_with_no_body_fat_rather_than_imputing_one():
    """Mixed week: one day with BF, one without. The mean must be over the day
    that had a reading, never over an invented zero."""
    rows = [weighed(11, 0, 80.0, 17.0), weighed(11, 1, 80.0)]
    w = week(rows)
    assert w["lean_7d_mean"] == round(80.0 * 0.83, 2)
    assert w["weighins_count"] == 2


# --------------------------------------------------------------- adherence

def test_sessions_are_counted_into_the_week_they_fall_in():
    rows = [weighed(2, 0, 84.0)]
    sessions = [watch_session(2, 0), watch_session(2, 3), watch_session(3, 0)]
    weeks = transform.build_weekly(rows, sessions)
    assert weeks[0]["sessions_done"] == 2


def test_a_week_with_training_but_no_weigh_ins_still_reports_its_sessions():
    rows = [weighed(1, 0, 84.0)]                      # W1 only
    sessions = [watch_session(2, 0), watch_session(2, 3)]   # trained in W2
    weeks = transform.build_weekly(rows, sessions)
    w2 = [w for w in weeks if w["week"] == "W02"]
    assert w2 and w2[0]["sessions_done"] == 2


def test_sessions_outside_the_campaign_window_are_not_counted():
    """week_number returns None before the start and after the last week."""
    rows = [weighed(1, 0, 84.0)]
    before = {"date": (campaign.CAMPAIGN_START - timedelta(days=2)).isoformat(),
              "exercise_type": "TENNIS", "slot": "C"}
    assert transform.build_weekly(rows, [before])[0]["sessions_done"] == 0


def test_sessions_done_is_zero_not_blank_for_a_week_with_no_training():
    """0 means 'trained nothing'. Blank would mean 'we don't know', and the
    adherence figure would quietly divide by the wrong denominator."""
    assert week([weighed(1, 0, 84.0)])["sessions_done"] == 0


# --------------------------------------------------------------- week metadata

def test_every_week_carries_its_phase_maintenance_flag_and_bounds():
    for w in transform.build_weekly([weighed(n, 0, 84.0) for n in (1, 5, 12)], []):
        n = int(w["week"][1:])
        start, end = campaign.week_bounds(n)
        assert w["week_start"] == start.isoformat()
        assert w["week_end"] == end.isoformat()
        assert (end - start).days == 6
        assert w["is_maintenance"] is (n in campaign.MAINTENANCE_WEEKS)
        assert w["phase"] == campaign.phase_for(n)


def test_maintenance_weeks_are_flagged_so_flat_reads_as_success():
    maint = sorted(campaign.MAINTENANCE_WEEKS)[0]
    assert week([weighed(maint, 0, 84.0)])["is_maintenance"] is True


def test_benchmark_label_lands_on_the_week_containing_its_date():
    bench_date, label = sorted(campaign.BENCHMARKS.items())[0]
    n = campaign.week_number(bench_date)
    assert n, "benchmark outside the campaign — config problem, not a code one"
    assert week([weighed(n, 0, 84.0)])["benchmark"] == label


def test_weeks_are_emitted_in_ascending_order_even_if_daily_rows_are_shuffled():
    """build_daily sorts, but build_weekly must not *rely* on that: the delta
    chain is order-dependent, so a shuffled input would silently invert deltas."""
    rows = [weighed(3, 0, 83.0), weighed(1, 0, 84.0), weighed(2, 0, 83.5)]
    weeks = transform.build_weekly(rows, [])
    assert [w["week"] for w in weeks] == ["W01", "W02", "W03"]
    assert [w["weight_delta_kg"] for w in weeks] == ["", -0.5, -0.5]


# --------------------------------------------------------------- rhr / waist

def test_rhr_elevated_uses_the_weekly_mean_against_the_configured_threshold():
    hi = campaign.RHR_ELEVATED_THRESHOLD + 2
    assert week([daily_row(4, 0, resting_hr=hi)])["rhr_elevated"] is True


def test_rhr_elevated_does_not_fire_exactly_at_the_threshold():
    """Condition is `>`, so sitting on the threshold is not elevated."""
    at = campaign.RHR_ELEVATED_THRESHOLD
    assert week([daily_row(4, 0, resting_hr=at)])["rhr_elevated"] is False


def test_waist_delta_is_measured_against_the_earliest_manual_entry():
    manual = {day_in_week(1, 0): {"waist_navel_cm": "92.0"},
              day_in_week(6, 2): {"waist_navel_cm": "89.5"}}
    weeks = transform.build_weekly(
        [weighed(1, 0, 84.0), weighed(6, 0, 82.0)], [], manual)
    assert weeks[0]["waist_delta_cm"] == 0.0
    assert weeks[1]["waist_navel_cm"] == 89.5
    assert weeks[1]["waist_delta_cm"] == -2.5


def test_waist_is_blank_in_weeks_with_no_tape_measurement():
    manual = {day_in_week(1, 0): {"waist_navel_cm": "92.0"}}
    weeks = transform.build_weekly(
        [weighed(1, 0, 84.0), weighed(2, 0, 83.6)], [], manual)
    assert weeks[1]["waist_navel_cm"] == ""
    assert weeks[1]["waist_delta_cm"] == ""


def test_manual_entries_outside_any_emitted_week_do_not_leak_into_another_week():
    """A tape measurement taken in W9 must not be attributed to W1."""
    manual = {day_in_week(9, 0): {"waist_navel_cm": "88.0"}}
    w1 = week([weighed(1, 0, 84.0)], manual=manual)
    assert w1["waist_navel_cm"] == ""


# --------------------------------------------------------------- schema contract

def test_build_weekly_emits_every_column_the_writer_expects_except_the_two_cli_adds():
    """`write_csv` uses DictWriter, which silently writes "" for a missing key.
    So a column that build_weekly forgets is INVISIBLE in the output — it just
    looks like a week with no data. Pin the contract explicitly.

    a1_done / a2_done are deliberately excluded: they are attached later, by
    __main__.annotate_anchors, because they need the Hevy workout list.
    """
    produced = set(week([weighed(1, 0, 84.0)]))
    expected = set(transform.WEEKLY_COLUMNS) - {"a1_done", "a2_done"}
    assert expected <= produced, expected - produced


def test_empty_input_produces_an_empty_report_not_a_crash():
    assert transform.build_weekly([], []) == []
    assert transform.build_daily({}) == []
