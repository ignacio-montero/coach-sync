"""Hevy — fetch lifting sessions and flatten them to one row per set.

Schema is from Hevy's published OpenAPI spec, not inferred:

    GET /v1/workouts?page=N&pageSize=10   header: api-key
    -> {page, page_count, workouts: [Workout]}

    Workout  : id, title, start_time, end_time, exercises[]
    Exercise : index, title, notes, exercise_template_id, sets[]
    Set      : index, type (normal|warmup|dropset|failure),
               weight_kg, reps, rpe, duration_seconds, distance_meters

pageSize maxes out at 10, so pagination is mandatory even for modest histories.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

BASE_URL = "https://api.hevyapp.com/v1"
PAGE_SIZE = 10  # API maximum

# Hevy rate-limits a BURST, not a sustained rate: the whole page run fired
# back-to-back returns 429, while the identical requests spaced a fraction of a
# second apart all return 200. Because PAGE_SIZE is capped at 10 by the API,
# the burst grows by one request per ~10 workouts logged — so this failure gets
# MORE likely over a campaign, not less. Pace the pages, and treat a 429 as
# transient rather than fatal.
PAGE_PAUSE_S = 0.35
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 2.0

# Warm-ups are not working sets. Including them would drag the top-set number
# down and corrupt the autoregulation loop, which sets next week's load from the
# heaviest set that actually counted.
WORKING_SET_TYPES = {"normal", "failure", "dropset"}

# Module-level so a test can swap it; see _get_page.
_sleep = time.sleep


def get_api_key() -> str:
    key = os.environ.get("HEVY_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "HEVY_API_KEY not set. Get one at https://hevy.com/settings?developer\n"
            "(requires Hevy Pro) and put it in pipeline/.env"
        )
    return key


def _retry_after_seconds(resp) -> Optional[float]:
    """The server's own advice, when it gives any.

    Retry-After is the polite contract for a 429: obeying it beats guessing,
    because a backoff shorter than the server's window just re-trips the limit.
    """
    raw = resp.headers.get("Retry-After", "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _get_page(client, page: int, sleep=None) -> Dict[str, Any]:
    """One /workouts page, retrying the statuses that are transient.

    `sleep` is injected so the backoff is exercisable in a test without the
    test actually waiting — a retry loop that cannot be run fast tends to be an
    untested one, and this one only ever runs on the bad day.

    401 is deliberately NOT retried: a dead key is not transient, and hammering
    it would bury a clear "re-consent" message under a rate-limit one.
    """
    sleep = sleep or _sleep
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = client.get(
            "{}/workouts".format(BASE_URL),
            params={"page": page, "pageSize": PAGE_SIZE},
        )
        if resp.status_code == 401:
            raise SystemExit(
                "Hevy returned 401 Unauthorized — check HEVY_API_KEY, and that\n"
                "the Hevy Pro subscription is active."
            )
        if resp.status_code == 200:
            return resp.json()
        last = resp
        if resp.status_code not in RETRY_STATUSES:
            break
        if attempt < MAX_ATTEMPTS:
            sleep(_retry_after_seconds(resp)
                  or BACKOFF_BASE_S * (2 ** (attempt - 1)))

    raise RuntimeError(
        "Hevy /workouts page {} -> {} {} (gave up after {} attempt(s))\n{}".format(
            page, last.status_code, last.reason_phrase,
            MAX_ATTEMPTS if last.status_code in RETRY_STATUSES else 1,
            last.text[:400],
        )
    )


def fetch_workouts(api_key: str, raw_dir: Path, sleep=None) -> Path:
    """Fetch every workout, following pagination, and persist the raw JSON."""
    sleep = sleep or _sleep
    raw_dir.mkdir(parents=True, exist_ok=True)
    headers = {"api-key": api_key}
    workouts: List[Dict[str, Any]] = []

    with httpx.Client(headers=headers, timeout=60) as client:
        page = 1
        while True:
            body = _get_page(client, page, sleep=sleep)
            workouts.extend(body.get("workouts", []) or [])
            page_count = body.get("page_count", 1)
            if page >= page_count or page > 200:
                break
            page += 1
            # Pause BEFORE the next request rather than after the last one, so
            # a single-page fetch pays no pause at all.
            sleep(PAGE_PAUSE_S)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / "hevy_workouts_{}.json".format(stamp)
    path.write_text(json.dumps(workouts, indent=2))
    return path


def epley_1rm(weight: float, reps: float) -> float:
    """Epley: 1RM = w x (1 + reps/30).

    NAMED DELIBERATELY. The campaign's starting loads came from Bevel's
    estimator, which uses a different formula — so these numbers will not agree
    exactly with the 19 Aug baseline. That difference is arithmetic, not
    progress, and must not be read as either.
    """
    return round(weight * (1 + reps / 30.0), 1)


def parse_workouts(workouts: List[dict]) -> List[dict]:
    """Flatten to one row per set. Grain: set, not day (ARCHITECTURE.md 5.4)."""
    rows: List[dict] = []

    for workout in workouts:
        start = workout.get("start_time")
        try:
            begin = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        day = begin.date()

        for exercise in workout.get("exercises", []) or []:
            title = exercise.get("title", "")
            sets = exercise.get("sets", []) or []

            working = [
                s for s in sets
                if s.get("type") in WORKING_SET_TYPES
                and s.get("weight_kg") is not None
            ]
            # EXACTLY ONE top set per exercise per workout. A straight 3x5 at
            # 70 has three sets tied on weight; flagging all three would make
            # "the top set" ambiguous for the autoregulation loop. Rank by
            # weight, then reps, then earliest — deterministic and explicable.
            top_index = None
            if working:
                best = max(working, key=lambda s: (s.get("weight_kg") or 0,
                                                   s.get("reps") or 0,
                                                   -(s.get("index") or 0)))
                top_index = best.get("index")

            for item in sets:
                weight = item.get("weight_kg")
                reps = item.get("reps")
                is_working = item.get("type") in WORKING_SET_TYPES
                rows.append({
                    "date": day.isoformat(),
                    "start_time": begin.strftime("%H:%M"),
                    "workout_id": workout.get("id", ""),
                    "workout_title": workout.get("title", ""),
                    "exercise": title,
                    "set_index": item.get("index", ""),
                    "set_type": item.get("type", ""),
                    "weight_kg": weight if weight is not None else "",
                    "reps": reps if reps is not None else "",
                    "rpe": item.get("rpe") if item.get("rpe") is not None else "",
                    "is_top_set": bool(
                        is_working and top_index is not None
                        and item.get("index") == top_index
                    ),
                    "est_1rm_epley": (
                        epley_1rm(weight, reps)
                        if (is_working and weight and reps) else ""
                    ),
                })
    return rows


def gym_sessions(rows: List[dict]) -> List[dict]:
    """One row per Hevy workout — used to label A1/A2 and to dedupe against
    the watch's auto-detected STRENGTH_TRAINING records."""
    seen: Dict[str, dict] = {}
    for row in rows:
        key = row["workout_id"]
        if key not in seen:
            seen[key] = {
                "date": row["date"],
                "start_time": row["start_time"],
                "workout_id": key,
                "title": row["workout_title"],
            }
    return sorted(seen.values(), key=lambda r: (r["date"], r["start_time"]))


def label_anchor_slots(sessions: List[dict]) -> Dict[str, str]:
    """-> {workout_id: 'A1'|'A2'|'A3'} by order within each campaign week.

    The plan caps gym at two sessions a week (A1 squat-led, A2 hinge-led). Which
    weekday they land on shifts, because the gym follows the office rather than
    the calendar — so ORDER within the week is the reliable signal, not weekday.
    Inferred, and user-correctable.
    """
    from . import campaign

    by_week: Dict[int, List[dict]] = {}
    for session in sessions:
        week = campaign.week_number(date.fromisoformat(session["date"]))
        if week:
            by_week.setdefault(week, []).append(session)

    labels: Dict[str, str] = {}
    for week_sessions in by_week.values():
        for i, session in enumerate(week_sessions):
            labels[session["workout_id"]] = "A{}".format(i + 1)
    return labels
