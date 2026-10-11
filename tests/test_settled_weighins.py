"""The first step-on reads high — which reading of a burst the day keeps.

Stepped on two or three times within a minute, the scale's first reading sits
~1 kg above the ones that follow, and those agree with each other. The rule was
"earliest reading of the morning", which always kept the high one.

Two things are tested here, by consequence:

    pick_reading   -> the day's weight (and, identically, its body fat)
    basis break    -> a week-on-week comparison spanning the first settled day
                      compares two different measurements; it must read
                      UNKNOWN, or it fires losing_too_fast on a loss nobody made

Values are illustrative, not anyone's real readings.
"""
from __future__ import annotations

from datetime import date

from coach_sync import campaign, transform

from conftest import weighed, weight_point


def bf_point(physical_time: str, pct: float, offset: str = "3600s") -> dict:
    return {"bodyFat": {"sampleTime": {"physicalTime": physical_time,
                                       "utcOffset": offset},
                        "percentage": pct}}


DAY = date(2026, 8, 30)


# ------------------------------------------------------------ pick_reading

def test_a_burst_keeps_its_last_reading_not_its_first():
    values, settled, _ = transform.parse_weighins("weight", [
        weight_point("2026-08-30T08:11:23Z", 84000),   # first step-on, high
        weight_point("2026-08-30T08:11:35Z", 83000),
        weight_point("2026-08-30T08:11:54Z", 83000),
    ])
    assert values[DAY] == 83.0 and DAY in settled


def test_the_rule_does_not_depend_on_api_order():
    """The live API returns newest-first; the answer must not change."""
    points = [weight_point("2026-08-30T08:11:23Z", 84000),
              weight_point("2026-08-30T08:11:35Z", 83000)]
    forward, _, _ = transform.parse_weighins("weight", points)
    backward, _, _ = transform.parse_weighins("weight", points[::-1])
    assert forward == backward == {DAY: 83.0}


def test_a_lone_reading_is_kept_but_marked_unsettled():
    values, settled, _ = transform.parse_weighins(
        "weight", [weight_point("2026-08-30T08:00:00Z", 84000)])
    assert values[DAY] == 84.0 and DAY not in settled


def test_readings_more_than_the_session_gap_apart_are_not_a_burst():
    later = transform.WEIGHIN_SESSION_GAP.total_seconds() // 60 + 1
    values, settled, _ = transform.parse_weighins("weight", [
        weight_point("2026-08-30T08:00:00Z", 84000),
        weight_point("2026-08-30T08:{:02d}:00Z".format(int(later)), 83000),
    ])
    # Two lone readings: the morning rule (earliest) still applies.
    assert values[DAY] == 84.0 and DAY not in settled


def test_a_later_morning_burst_beats_an_earlier_lone_reading():
    """Seen live: a lone reading, then a burst ninety minutes later."""
    values, settled, _ = transform.parse_weighins("weight", [
        weight_point("2026-08-30T07:50:00Z", 84000),
        weight_point("2026-08-30T09:20:00Z", 84100),
        weight_point("2026-08-30T09:20:12Z", 83100),
    ])
    assert values[DAY] == 83.1 and DAY in settled


def test_an_evening_burst_does_not_displace_the_morning_reading():
    """Preferring any burst would put dinner into the trend line."""
    values, settled, _ = transform.parse_weighins("weight", [
        weight_point("2026-08-30T07:00:00Z", 84000),
        weight_point("2026-08-30T19:00:00Z", 85500),
        weight_point("2026-08-30T19:00:20Z", 85200),
    ])
    assert values[DAY] == 84.0 and DAY not in settled


def test_of_two_bursts_the_earlier_one_wins():
    values, _, _ = transform.parse_weighins("weight", [
        weight_point("2026-08-30T08:00:00Z", 84000),
        weight_point("2026-08-30T08:00:15Z", 83000),
        weight_point("2026-08-30T09:00:00Z", 83900),
        weight_point("2026-08-30T09:00:15Z", 82800),
    ])
    assert values[DAY] == 83.0


def test_a_pre_dawn_burst_does_not_count_as_the_morning():
    values, _, _ = transform.parse_weighins("weight", [
        weight_point("2026-08-30T01:00:00Z", 85000),   # 02:00 local
        weight_point("2026-08-30T01:00:15Z", 84500),
        weight_point("2026-08-30T08:00:00Z", 84000),
    ])
    assert values[DAY] == 84.0


def test_body_fat_lands_on_the_same_reading_as_weight():
    """Lean mass is weight x (1 - bf): both must come from ONE step-on."""
    stamps = ["2026-08-30T08:11:23Z", "2026-08-30T08:11:35Z"]
    weight, _, _ = transform.parse_weighins(
        "weight", [weight_point(stamps[0], 84000), weight_point(stamps[1], 83000)])
    bf, _, _ = transform.parse_weighins(
        "body_fat", [bf_point(stamps[0], 19.4), bf_point(stamps[1], 18.9)])
    assert (weight[DAY], bf[DAY]) == (83.0, 18.9)


def test_parse_scalar_keeps_its_two_value_contract():
    values, unparsed = transform.parse_scalar(
        "weight", [weight_point("2026-08-30T08:00:00Z", 84000)])
    assert values == {DAY: 84.0} and unparsed == 0


# ------------------------------------------------------------ daily column

def test_build_daily_marks_each_weighed_day():
    lone, burst = date(2026, 8, 29), date(2026, 8, 30)
    rows = transform.build_daily({"weight": {lone: 84.0, burst: 83.0},
                                  "weight_settled": {burst},
                                  "daily_resting_heart_rate": {date(2026, 8, 31): 48.0}})
    flags = {r["date"]: r["weight_settled"] for r in rows}
    assert flags == {"2026-08-29": False, "2026-08-30": True,
                     "2026-08-31": ""}       # no weight -> no claim either way


# ------------------------------------------------------------ basis break

def _settled(week, offset, weight):
    row = weighed(week, offset, weight)
    row["weight_settled"] = True
    return row


def _break_campaign():
    """Lone readings through mid-week 2, settled from then on, and a body that
    never actually changed: the true weight is flat throughout. The only thing
    that moves is the measurement — exactly the live situation."""
    lone, settled = 84.0, 83.0
    rows = [weighed(1, i, lone) for i in range(7)]
    rows += [weighed(2, i, lone) for i in range(3)]
    rows += [_settled(2, i, settled) for i in range(3, 7)]
    rows += [_settled(3, i, settled) for i in range(7)]
    rows += [_settled(4, i, settled) for i in range(7)]
    return {w["week"]: w for w in transform.build_weekly(rows, [])}


def test_comparisons_spanning_the_break_are_unknown_not_a_loss():
    weeks = _break_campaign()
    for label in ("W02", "W03"):
        week = weeks[label]
        assert week["weight_delta_kg"] == ""
        assert week["weekly_rate_kg"] == ""
        assert week["losing_too_fast"] == ""       # UNKNOWN, never False


def test_comparisons_resume_once_both_weeks_are_past_the_break():
    w4 = _break_campaign()["W04"]
    assert w4["weight_delta_kg"] == 0.0 and w4["losing_too_fast"] is False


def test_the_week_means_are_still_reported_across_the_break():
    """Only the COMPARISON is withheld; each week's own level is real data."""
    weeks = _break_campaign()
    assert weeks["W03"]["weight_7d_mean"] == 83.0
    assert weeks["W03"]["delta_vs_target"] != ""


def test_without_the_break_the_same_drop_would_have_fired_the_flag():
    """The control: proves the blanking is what prevents the false alarm,
    rather than the drop being too small to fire anyway."""
    rows = ([weighed(1, i, 84.0) for i in range(7)]
            + [weighed(2, i, 83.0) for i in range(7)])
    w2 = {w["week"]: w for w in transform.build_weekly(rows, [])}["W02"]
    assert 1.0 > campaign.MAX_SAFE_LOSS_KG_PER_WEEK
    assert w2["losing_too_fast"] is True


def test_weighins_settled_counts_settled_days():
    weeks = _break_campaign()
    assert [weeks[w]["weighins_settled"] for w in ("W01", "W02", "W03")] == [0, 4, 7]


def test_a_campaign_with_no_settled_readings_is_unaffected():
    rows = ([weighed(1, i, 84.0) for i in range(7)]
            + [weighed(2, i, 83.8) for i in range(7)])
    w2 = {w["week"]: w for w in transform.build_weekly(rows, [])}["W02"]
    assert w2["weight_delta_kg"] == -0.2 and w2["weighins_settled"] == 0
