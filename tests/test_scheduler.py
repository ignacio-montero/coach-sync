"""The container's runtime behaviour: clock, schedule, retention, health.

WHY THIS FILE EXISTS
--------------------
Everything here is code that only ever runs unattended, at 06:30, on a box
nobody is watching. It has no user to notice it misbehaving, which makes it
exactly the code most worth testing: a scheduler that drifts an hour at the
October clock change, or a retention sweep that deletes the last good raw file,
fails silently and stays failed.

The DST tests below are dated on purpose. BST ends at 02:00 on Sun 25 Oct 2026,
in campaign week 9 — mid-campaign, not a hypothetical.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import pytest

from coach_sync import clock, scheduler


# ------------------------------------------------------------------- clock

def test_today_is_the_london_date_not_the_process_local_one():
    """`clock.today()` names its zone, so it is correct whatever TZ says.

    This is the M4 regression: `date.today()` in a default-UTC container
    returns yesterday between 23:00 and midnight BST, closing the data window a
    day early and silently dropping that day's records.
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"     # UTC+14: a different DATE to London
    time.tzset()
    try:
        expected = datetime.now(clock.CAMPAIGN_TZ).date()
        assert clock.today() == expected
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_assert_local_timezone_rejects_a_mismatched_container():
    """The tripwire: a compose file that loses `TZ: Europe/London` must be loud."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        # Sydney is never UTC+0, so this is deterministic all year — unlike
        # comparing UTC against London, which are equal from October to March.
        with pytest.raises(clock.TimezoneMismatch):
            clock.assert_local_timezone("Australia/Sydney")
        clock.assert_local_timezone("UTC")      # matching zone: no raise
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


# ---------------------------------------------------------------- schedule

def _london(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=clock.CAMPAIGN_TZ)


def test_next_run_is_later_today_when_the_slot_has_not_passed():
    assert scheduler.next_run(_london(2026, 9, 1, 5, 0), [(6, 30)]) == \
        _london(2026, 9, 1, 6, 30)


def test_next_run_rolls_to_tomorrow_once_the_slot_has_passed():
    assert scheduler.next_run(_london(2026, 9, 1, 6, 30), [(6, 30)]) == \
        _london(2026, 9, 2, 6, 30)


def test_the_run_time_survives_the_october_clock_change():
    """Across BST -> GMT the job must still fire at 06:30 LOCAL.

    Naive `+ timedelta(days=1)` on a UTC instant would land at 05:30 local for
    the rest of the campaign — an hour before the morning weigh-in, which is
    the entire point of running at 06:30.
    """
    before = _london(2026, 10, 23, 7, 0)        # Fri, still BST (+01:00)
    first = scheduler.next_run(before, [(6, 30)])   # Sat 24 Oct, last BST morning
    assert first.date().isoformat() == "2026-10-24"
    assert (first.hour, first.minute) == (6, 30)
    assert first.utcoffset() == timedelta(hours=1)

    second = scheduler.next_run(first, [(6, 30)])   # Sun 25 Oct, now GMT
    assert second.date().isoformat() == "2026-10-25"
    assert (second.hour, second.minute) == (6, 30)
    assert second.utcoffset() == timedelta(0), "should be GMT after the change"

    # ⚠️ `second - first` is 24h here, NOT 25: subtracting two aware datetimes
    # that share a tzinfo object ignores the zone and compares wall clocks.
    # The real elapsed time is 25 hours, and only the UTC form shows it — which
    # is why the loop sleeps on `seconds_until` rather than a raw subtraction.
    assert second - first == timedelta(hours=24), "wall-clock subtraction"
    assert scheduler.seconds_until(second, first) == 25 * 3600


def test_the_sleep_length_is_real_elapsed_time_not_wall_clock():
    """The bug this guards: at 23:00 on the night the clocks go back, the next
    06:30 is 8.5 hours away, not the 7.5 the clock face suggests. Sleeping the
    wrong one wakes the job an hour early on the one night of the campaign when
    date attribution is most fragile."""
    evening = _london(2026, 10, 24, 23, 0)          # BST, +01:00
    target = scheduler.next_run(evening, [(6, 30)])     # 25 Oct 06:30 GMT
    assert scheduler.seconds_until(target, evening) == 8.5 * 3600


def test_previous_run_is_the_most_recent_past_slot():
    assert scheduler.previous_run(_london(2026, 9, 1, 6, 29), [(6, 30)]) == \
        _london(2026, 8, 31, 6, 30)
    assert scheduler.previous_run(_london(2026, 9, 1, 6, 31), [(6, 30)]) == \
        _london(2026, 9, 1, 6, 30)


# --------------------------------------------------------------- retention

def _raw(dirpath, name, stamp):
    path = dirpath / "{}_{}.json".format(name, stamp)
    path.write_text("[]")
    return path


def test_prune_keeps_the_newest_n_per_data_type(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "RAW_DIR", tmp_path)
    for day in range(1, 6):
        _raw(tmp_path, "weight", "2026080{}T060000Z".format(day))
        _raw(tmp_path, "hevy_workouts", "2026080{}T060000Z".format(day))

    scheduler.prune_raw(keep=2)

    kept = sorted(p.name for p in tmp_path.glob("*.json"))
    assert kept == [
        "hevy_workouts_20260804T060000Z.json",
        "hevy_workouts_20260805T060000Z.json",
        "weight_20260804T060000Z.json",
        "weight_20260805T060000Z.json",
    ], "retention is per data type, newest first"


def test_prune_never_empties_a_data_type_that_stopped_fetching(tmp_path,
                                                               monkeypatch):
    """The count-based rule chosen over `find -mtime +30`.

    If one source has been failing for a month, an age-based sweep deletes its
    last surviving capture — precisely when you most need it to diagnose what
    changed. Keeping N per type means there is always something to re-parse.
    """
    monkeypatch.setattr(scheduler, "RAW_DIR", tmp_path)
    _raw(tmp_path, "sleep", "20260101T060000Z")          # ancient, and the only one
    for day in range(1, 21):
        _raw(tmp_path, "weight", "202608{:02d}T060000Z".format(day))

    scheduler.prune_raw(keep=14)

    assert (tmp_path / "sleep_20260101T060000Z.json").exists()
    assert len(list(tmp_path.glob("weight_*.json"))) == 14


def test_prune_ignores_files_that_are_not_timestamped_captures(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(scheduler, "RAW_DIR", tmp_path)
    keeper = tmp_path / "notes.json"
    keeper.write_text("{}")
    scheduler.prune_raw(keep=1)
    assert keeper.exists()


# -------------------------------------------------------------- healthcheck

@pytest.fixture
def heartbeat(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(scheduler, "STATE_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "HEARTBEAT", path)

    def _write(**state):
        path.write_text(json.dumps(state))
    return _write


def test_healthcheck_is_green_before_the_first_run(heartbeat):
    """A container that has not run yet is not yet broken — the catch-up run
    covers the gap. Failing here would make every deploy look sick for hours."""
    assert scheduler.healthcheck() == 0


def test_healthcheck_goes_red_on_a_failed_run(heartbeat):
    heartbeat(last_exit_code=4, last_attempt=clock.now().isoformat())
    assert scheduler.healthcheck() == 1, "exit 4 = stale data must show unhealthy"


def test_healthcheck_goes_red_when_the_job_stops_running(heartbeat,
                                                         monkeypatch):
    """Silence is the failure this whole service exists to detect: exit 0 last
    time, but that was three days ago and nothing has happened since."""
    monkeypatch.setenv("MAX_SILENCE_HOURS", "30")
    stale = (clock.now() - timedelta(hours=49)).isoformat()
    heartbeat(last_exit_code=0, last_attempt=stale, last_success=stale)
    assert scheduler.healthcheck() == 1


def test_healthcheck_is_green_after_a_recent_success(heartbeat):
    fresh = (clock.now() - timedelta(hours=2)).isoformat()
    heartbeat(last_exit_code=0, last_attempt=fresh, last_success=fresh)
    assert scheduler.healthcheck() == 0


def test_catch_up_fires_when_the_last_slot_produced_no_success(heartbeat):
    """Reboot safety: the box coming back at 07:10 must not skip today."""
    slot = scheduler.previous_run(clock.now(), [(6, 30)])
    heartbeat(last_success=(slot - timedelta(minutes=5)).isoformat())
    assert scheduler.missed_todays_run([(6, 30)]) is True

    heartbeat(last_success=(slot + timedelta(minutes=5)).isoformat())
    assert scheduler.missed_todays_run([(6, 30)]) is False


# ----------------------------------------------------- exit-code vocabulary

def test_every_documented_build_failure_has_an_explanation():
    """Guards drift between __main__'s `return 2/3/4` and the alert text.

    If someone adds a new exit code and forgets the message, the phone
    notification degrades to a bare number — which is the same as no alert.
    """
    from coach_sync import __main__ as cli
    import inspect
    import re

    codes = {int(m) for m in
             re.findall(r"^\s+return (\d+)$", inspect.getsource(cli.cmd_build),
                        flags=re.M)}
    assert codes, "expected cmd_build to return explicit exit codes"
    missing = codes - set(scheduler.EXIT_MEANING)
    assert not missing, "no alert text for build exit code(s) {}".format(missing)


# --------------------------------------------------------- multiple slots

def test_parse_schedule_accepts_one_or_many_and_sorts_them():
    assert scheduler.parse_schedule("13:00") == [(13, 0)]
    assert scheduler.parse_schedule("20:00,13:00") == [(13, 0), (20, 0)]
    assert scheduler.parse_schedule(" 13:00 , 20:00 ") == [(13, 0), (20, 0)]


def test_parse_schedule_deduplicates():
    assert scheduler.parse_schedule("13:00,13:00") == [(13, 0)]


def test_parse_schedule_rejects_empty_and_invalid():
    with pytest.raises(SystemExit):
        scheduler.parse_schedule("")
    with pytest.raises(SystemExit):
        scheduler.parse_schedule("25:00")


def test_next_run_picks_the_soonest_upcoming_slot():
    """The whole point of two slots: at 14:00 the next run is 20:00 today,
    not 13:00 tomorrow."""
    times = [(13, 0), (20, 0)]
    assert scheduler.next_run(_london(2026, 9, 1, 14, 0), times) == \
        _london(2026, 9, 1, 20, 0)
    assert scheduler.next_run(_london(2026, 9, 1, 21, 0), times) == \
        _london(2026, 9, 2, 13, 0)
    assert scheduler.next_run(_london(2026, 9, 1, 9, 0), times) == \
        _london(2026, 9, 1, 13, 0)


def test_previous_run_picks_the_most_recent_past_slot():
    times = [(13, 0), (20, 0)]
    assert scheduler.previous_run(_london(2026, 9, 1, 14, 0), times) == \
        _london(2026, 9, 1, 13, 0)
    assert scheduler.previous_run(_london(2026, 9, 1, 21, 0), times) == \
        _london(2026, 9, 1, 20, 0)
    # Before the first slot of the day, the most recent is yesterday evening.
    assert scheduler.previous_run(_london(2026, 9, 1, 9, 0), times) == \
        _london(2026, 8, 31, 20, 0)


def test_a_second_slot_does_not_break_the_october_clock_change():
    """Both slots must stay on their wall-clock times across the DST boundary."""
    times = [(13, 0), (20, 0)]
    sat_evening = _london(2026, 10, 24, 21, 0)     # after Saturday's 20:00
    nxt = scheduler.next_run(sat_evening, times)   # Sunday 25 Oct, now GMT
    assert (nxt.hour, nxt.minute) == (13, 0)
    assert nxt.tzinfo is not None
    assert nxt.utcoffset().total_seconds() == 0    # GMT, not BST


# ------------------------------------------------ partial fetch (exit 5)
#
# WHY THIS CODE EXISTS
# --------------------
# `fetch` used to catch a Hevy failure, print FAILED, and return 0. Seven
# consecutive production fetches failed that way without a single Telegram
# message, and `build` kept running against a stale Hevy capture — which then
# mis-deduplicated the gym sessions and tripped the shrink guard instead. The
# alert fired for the symptom, two days after the cause.
#
# The fix has two halves and both need holding in place: the failure must be
# VISIBLE (a distinct exit code with alert text), and it must NOT be fatal
# (weight is what the campaign is scored on; a missing lift log must never cost
# a missing weight trend).

def test_a_partial_fetch_has_its_own_exit_code_and_alert_text():
    from coach_sync import PARTIAL_FETCH
    assert PARTIAL_FETCH in scheduler.EXIT_MEANING
    assert scheduler.EXIT_MEANING[PARTIAL_FETCH] != "ok"


def test_every_fetch_code_routed_through_EXIT_MEANING_has_text():
    """The sibling of the cmd_build drift guard.

    Narrower than the build one on purpose: a hard fetch failure (return 1) is
    reported by the scheduler's bespoke FETCH FAILED branch, which carries its
    own credentials-oriented text and never consults EXIT_MEANING. The codes
    that DO get looked up are the named ones, so those are what can silently
    degrade to a bare number on the phone.
    """
    from coach_sync import __main__ as cli
    import inspect

    source = inspect.getsource(cli.cmd_fetch)
    named = {cli.PARTIAL_FETCH} if "PARTIAL_FETCH" in source else set()
    assert named, "expected cmd_fetch to signal a partial fetch by name"
    missing = named - set(scheduler.EXIT_MEANING)
    assert not missing, "no alert text for fetch exit code(s) {}".format(missing)


def _run_cycle_with(monkeypatch, fetch_code, tmp_path):
    """Drive run_cycle with a scripted `fetch` result, recording what it did."""
    steps, alerts = [], []

    def fake_run_step(args, timeout_s):
        steps.append(args[0])
        return (fetch_code if args[0] == "fetch" else 0), ""

    monkeypatch.setattr(scheduler, "run_step", fake_run_step)
    monkeypatch.setattr(scheduler, "alert",
                        lambda message, tail, key: alerts.append(key))
    monkeypatch.setattr(scheduler, "write_heartbeat", lambda **kw: None)
    monkeypatch.setattr(scheduler, "prune_raw", lambda keep: None)
    code = scheduler.run_cycle()
    return code, steps, alerts


def test_a_partial_fetch_alerts_but_still_builds(monkeypatch, tmp_path):
    """The whole point. Hevy being down must not stop the weight trend."""
    from coach_sync import PARTIAL_FETCH
    code, steps, alerts = _run_cycle_with(monkeypatch, PARTIAL_FETCH, tmp_path)
    assert "build" in steps          # it carried on
    assert alerts == ["fetch-partial"]
    assert code == 0                 # the cycle as a whole succeeded


def test_a_partial_fetch_is_not_retried(monkeypatch, tmp_path):
    """The failing source already retried itself with backoff; re-running
    `fetch` would only re-hit Google for data it just fetched successfully."""
    from coach_sync import PARTIAL_FETCH
    monkeypatch.setenv("FETCH_RETRIES", "3")
    _, steps, _ = _run_cycle_with(monkeypatch, PARTIAL_FETCH, tmp_path)
    assert steps.count("fetch") == 1


def test_a_total_fetch_failure_still_aborts_before_build(monkeypatch, tmp_path):
    """The pre-existing contract, unchanged: no Google data, nothing to build."""
    monkeypatch.setenv("FETCH_RETRIES", "0")
    code, steps, alerts = _run_cycle_with(monkeypatch, 1, tmp_path)
    assert "build" not in steps
    assert alerts == ["fetch-failed"]
    assert code == 1
