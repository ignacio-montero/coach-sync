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
    assert scheduler.next_run(_london(2026, 9, 1, 5, 0), 6, 30) == \
        _london(2026, 9, 1, 6, 30)


def test_next_run_rolls_to_tomorrow_once_the_slot_has_passed():
    assert scheduler.next_run(_london(2026, 9, 1, 6, 30), 6, 30) == \
        _london(2026, 9, 2, 6, 30)


def test_the_run_time_survives_the_october_clock_change():
    """Across BST -> GMT the job must still fire at 06:30 LOCAL.

    Naive `+ timedelta(days=1)` on a UTC instant would land at 05:30 local for
    the rest of the campaign — an hour before the morning weigh-in, which is
    the entire point of running at 06:30.
    """
    before = _london(2026, 10, 23, 7, 0)        # Fri, still BST (+01:00)
    first = scheduler.next_run(before, 6, 30)   # Sat 24 Oct, last BST morning
    assert first.date().isoformat() == "2026-10-24"
    assert (first.hour, first.minute) == (6, 30)
    assert first.utcoffset() == timedelta(hours=1)

    second = scheduler.next_run(first, 6, 30)   # Sun 25 Oct, now GMT
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
    target = scheduler.next_run(evening, 6, 30)     # 25 Oct 06:30 GMT
    assert scheduler.seconds_until(target, evening) == 8.5 * 3600


def test_previous_run_is_the_most_recent_past_slot():
    assert scheduler.previous_run(_london(2026, 9, 1, 6, 29), 6, 30) == \
        _london(2026, 8, 31, 6, 30)
    assert scheduler.previous_run(_london(2026, 9, 1, 6, 31), 6, 30) == \
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
    slot = scheduler.previous_run(clock.now(), 6, 30)
    heartbeat(last_success=(slot - timedelta(minutes=5)).isoformat())
    assert scheduler.missed_todays_run(6, 30) is True

    heartbeat(last_success=(slot + timedelta(minutes=5)).isoformat())
    assert scheduler.missed_todays_run(6, 30) is False


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
