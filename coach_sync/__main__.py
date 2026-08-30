"""CLI. Three commands, deliberately separable:

    fetch     hit the API, write raw JSON, stop
    inspect   print the real shape of the raw JSON (pin the parser from this)
    build     parse raw -> metrics_daily.csv / metrics_weekly.csv / sessions.csv

`build` never calls the API. Once raw exists you can iterate on parsing offline,
which matters because the parser is written against an inferred schema.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import campaign, hevy, sheets, transform
from .auth import get_access_token, load_env
from .clock import today as london_today
from .datatypes import REGISTRY
from .extract import extract_all, latest_raw

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "data"
# Hand-entered ground truth lives OUTSIDE data/. data/ is derived and
# regenerable — a clean rebuild, or a container started against an empty
# volume, would destroy the waist history, which no API can recover.
INPUT_DIR = ROOT / "input"

# How stale the newest data may be before `build` treats it as a failure.
MAX_DATA_AGE_DAYS = 2


def cmd_fetch(args):
    load_env(ROOT / ".env")
    since = (date.fromisoformat(args.since) if args.since
             else campaign.CAMPAIGN_START)
    print("Fetching from {} ...".format(since.isoformat()))
    token = get_access_token(ROOT / ".env")
    written = extract_all(token, since, RAW_DIR)
    if not written:
        print("\nNothing fetched. Check credentials and scopes.")
        return 1
    print("\n{} data types written to {}".format(len(written), RAW_DIR))

    if os.environ.get("HEVY_API_KEY"):
        print("\n  fetching hevy workouts", end="", flush=True)
        try:
            path = hevy.fetch_workouts(hevy.get_api_key(), RAW_DIR)
            count = len(json.loads(path.read_text()))
            print("           {:4d} workouts -> {}".format(count, path.name))
        except (RuntimeError, SystemExit) as exc:
            print("  FAILED\n    {}".format(exc))
    else:
        print("\n  (HEVY_API_KEY not set - skipping lifting data)")

    print("\nNext: python -m coach_sync build")
    return 0


def cmd_inspect(args):
    for name in REGISTRY:
        path = latest_raw(RAW_DIR, name)
        print("\n=== {} ===".format(name))
        if path is None:
            print("  (no raw file — run fetch first)")
            continue
        points = json.loads(path.read_text())
        print("  {} points in {}".format(len(points), path.name))
        print(transform.describe_shape(points))
    return 0


def cmd_build(args):
    parsed, problems = {}, []

    for name in ("weight", "body_fat", "daily_resting_heart_rate",
                 "daily_heart_rate_variability"):
        path = latest_raw(RAW_DIR, name)
        if path is None:
            problems.append("{}: no raw file".format(name))
            continue
        points = json.loads(path.read_text())
        values, unparsed = transform.parse_scalar(name, points)
        parsed[name] = values
        print("  {:32s} {:4d} parsed, {} unparsed".format(name, len(values), unparsed))
        if unparsed and not values:
            problems.append(
                "{}: parsed 0 of {} points — schema mismatch".format(name, len(points))
            )

    # sleep and exercise are fetched unfiltered (their server-side filter syntax
    # is unresolved), so the campaign window is applied here instead.
    # `london_today()`, not `date.today()`: the window's closing edge is a
    # LONDON civil date. A default-UTC container running late in the evening
    # during BST would close the window a day early and silently drop that
    # day's weigh-in, sleep and sessions. See clock.py.
    win_start, win_end = campaign.CAMPAIGN_START, london_today()

    sleep_path = latest_raw(RAW_DIR, "sleep")
    if sleep_path:
        sleep, unparsed = transform.parse_sleep(json.loads(sleep_path.read_text()))
        kept = transform.window(sleep, win_start, win_end)
        parsed["sleep"] = kept
        print("  {:32s} {:4d} parsed, {} unparsed, {} in window".format(
            "sleep", len(sleep), unparsed, len(kept)))

    sessions = []
    ex_path = latest_raw(RAW_DIR, "exercise")
    if ex_path:
        all_sessions = transform.parse_exercise(json.loads(ex_path.read_text()))
        sessions = [r for r in all_sessions
                    if win_start <= date.fromisoformat(r["date"]) <= win_end]
        print("  {:32s} {:4d} sessions ({} in window, walking excluded)".format(
            "exercise", len(all_sessions), len(sessions)))

    if problems:
        print("\n!! Parsing problems — the inferred schema is wrong somewhere:")
        for problem in problems:
            print("   - {}".format(problem))
        print("   Run `python -m coach_sync inspect` and pin the parser.")

    lifts, gym = [], []
    hevy_path = latest_raw(RAW_DIR, "hevy_workouts")
    if hevy_path:
        workouts = json.loads(hevy_path.read_text())
        all_lifts = hevy.parse_workouts(workouts)
        lifts = [r for r in all_lifts
                 if win_start <= date.fromisoformat(r["date"]) <= win_end]
        gym = hevy.gym_sessions(lifts)
        slots = hevy.label_anchor_slots(gym)
        for row in lifts:
            row["slot"] = slots.get(row["workout_id"], "")
        print("  {:32s} {:4d} sets across {} workouts in window".format(
            "hevy", len(lifts), len(gym)))

        # The watch auto-detects gym sessions Hevy also logged. Hevy is
        # authoritative for A1/A2; keeping both double-counts adherence.
        gym_days = {g["date"] for g in gym}
        before = len(sessions)
        sessions = [s for s in sessions
                    if not (s["exercise_type"] == "STRENGTH_TRAINING"
                            and s["date"] in gym_days)]
        if before != len(sessions):
            print("  {:32s} {:4d} watch strength records deduped".format(
                "", before - len(sessions)))
    else:
        print("  {:32s} no raw file - is HEVY_API_KEY set?".format("hevy"))

    manual_path = INPUT_DIR / "manual.csv"
    legacy = OUT_DIR / "manual.csv"
    if legacy.exists() and not manual_path.exists():
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        legacy.rename(manual_path)
        print("  moved manual.csv out of the regenerable data/ dir -> input/")
    manual = transform.read_manual(manual_path)
    if manual:
        print("  {:32s} {:4d} manual entries".format("manual.csv", len(manual)))

    daily = transform.build_daily(parsed)
    weekly = transform.build_weekly(daily, sessions + gym_as_sessions(gym), manual)
    annotate_anchors(weekly, gym)

    allow_shrink = getattr(args, "allow_shrink", False)
    try:
        transform.write_csv(OUT_DIR / "metrics_daily.csv",
                            transform.DAILY_COLUMNS, daily, allow_shrink)
        transform.write_csv(OUT_DIR / "metrics_weekly.csv",
                            transform.WEEKLY_COLUMNS, weekly, allow_shrink)
        transform.write_csv(
            OUT_DIR / "lifts.csv",
            ["date", "start_time", "workout_id", "workout_title", "slot",
             "exercise", "set_index", "set_type", "weight_kg", "reps", "rpe",
             "is_top_set", "est_1rm_epley"],
            lifts, allow_shrink,
        )
        transform.write_csv(
            OUT_DIR / "sessions.csv",
            ["date", "start_time", "activity", "exercise_type", "slot",
             "duration_min", "active_zone_minutes", "avg_hr",
             "recording_method", "calories"],
            sessions, allow_shrink,
        )
    except transform.ShrinkGuard as exc:
        print("\n!! REFUSED TO WRITE\n{}".format(exc))
        return 2

    print("\nWrote {} daily rows, {} weekly rows, {} sessions -> {}".format(
        len(daily), len(weekly), len(sessions), OUT_DIR))

    # C3: staleness must be MACHINE-visible, not just readable. `build` runs
    # against whatever raw files exist, so a failed fetch produces a perfectly
    # well-formed CSV of yesterday's data and exits 0. The columns alone do not
    # help — nobody reads the CSV daily. A non-zero exit is what a container
    # can alert on.
    # Publish to the Sheet so a Claude Project can read it. Deliberately AFTER
    # the CSVs are written and BEFORE the staleness verdict: a Sheet failure
    # must not stop the local files being correct, and must not mask a stale
    # exit code either.
    if sheets.configured():
        try:
            token = get_access_token(ROOT / ".env")
            sheet_id = sheets.publish(token, OUT_DIR,
                                      os.environ.get("COACH_SYNC_SHEET_ID"))
            if not os.environ.get("COACH_SYNC_SHEET_ID"):
                print("\n  !! Save this to .env so the sheet is reused:")
                print("     COACH_SYNC_SHEET_ID={}".format(sheet_id))
        except (RuntimeError, SystemExit) as exc:
            # Not fatal. The CSVs are the source of truth; the Sheet is a
            # convenience mirror for the Project.
            print("  !! sheet publish failed (CSVs are still correct): {}".format(exc))

    stale_days = transform.staleness_days(weekly)
    data_through = weekly[-1]["data_through"] if weekly else ""

    for row in weekly:
        print("\n  {} ({})  {}".format(row["week"], row["phase"],
                                       row["benchmark"] or ""))
        print("    weight 7d mean : {} kg  (target {}, delta {})".format(
            row["weight_7d_mean"], row["checkpoint_target"], row["delta_vs_target"]))
        print("    lean 7d mean   : {} kg   floor breach: {}".format(
            row["lean_7d_mean"], row["lean_floor_breach"]))
        print("    weigh-ins      : {} of 7".format(row["weighins_count"]))
        print("    sessions       : {}  (A1={} A2={})".format(
            row["sessions_done"], row.get("a1_done"), row.get("a2_done")))
        if row["waist_navel_cm"]:
            print("    waist (navel)  : {} cm  (vs baseline {})".format(
                row["waist_navel_cm"], row["waist_delta_cm"]))

    if stale_days is None:
        print("\n!! NO DATED DATA AT ALL — nothing was parsed.")
        return 3
    if stale_days > MAX_DATA_AGE_DAYS:
        print("\n" + "=" * 60)
        print("!! STALE DATA — newest record is {} days old ({}).".format(
            stale_days, data_through))
        print("   The CSVs were written, but they describe the past. This")
        print("   almost always means `fetch` failed and `build` ran against")
        print("   old raw files. Check credentials first: an OAuth refresh")
        print("   token that has expired takes out the Hevy fetch too.")
        print("=" * 60)
        return 4

    print("\n  data through {} ({} day(s) old)".format(data_through, stale_days))
    return 0


def gym_as_sessions(gym):
    """Hevy workouts re-enter the session count after the watch copies were
    removed - otherwise dedup would erase the gym sessions entirely."""
    return [{"date": g["date"], "exercise_type": "STRENGTH_TRAINING", "slot": "A"}
            for g in gym]


def annotate_anchors(weekly, gym):
    """a1_done / a2_done - the two sessions the plan protects above all else."""
    from collections import defaultdict
    by_week = defaultdict(list)
    for session in gym:
        week = campaign.week_number(date.fromisoformat(session["date"]))
        if week:
            by_week[week].append(session)
    for row in weekly:
        week = int(row["week"][1:])
        row["a1_done"] = len(by_week.get(week, [])) >= 1
        row["a2_done"] = len(by_week.get(week, [])) >= 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="coach_sync")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="fetch raw data from Google Health")
    p_fetch.add_argument("--since", help="YYYY-MM-DD (default: campaign start)")
    p_fetch.set_defaults(func=cmd_fetch)

    sub.add_parser("inspect", help="print the real shape of the raw JSON").set_defaults(
        func=cmd_inspect)
    p_build = sub.add_parser("build", help="parse raw -> CSVs")
    p_build.add_argument("--allow-shrink", action="store_true",
                         help="permit overwriting with less data than before")
    p_build.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
