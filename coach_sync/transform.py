"""Transform — raw data points into metrics_daily.csv and metrics_weekly.csv.

This is where most of the value sits (ARCHITECTURE.md section 3). The coach reads
7-day rolling means, derived lean mass, and deltas against a fixed checkpoint
ladder. Every number computed here is a number the model cannot get wrong later.

PARSING CAVEAT: the response schema below is inferred, not observed. The helpers
search the data point recursively for plausible keys rather than assuming a fixed
path, and `describe_shape` exists so the real structure can be read off a live
response and the parser pinned precisely. Wrong-but-loud beats wrong-but-quiet:
anything unparseable is reported, never silently dropped.
"""
from __future__ import annotations

import csv
import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import campaign

# ---------------------------------------------------------------- helpers


def deep_get(obj: Any, *candidate_keys: str) -> Any:
    """First value found for any of `candidate_keys`, searched depth-first.

    Tolerant by design: the API mixes camelCase in responses with snake_case in
    filters, and nests values differently per record type (sample / daily /
    session). Rather than hard-code a path per data type and be wrong, look for
    the key wherever it lives.
    """
    if isinstance(obj, dict):
        for key in candidate_keys:
            if key in obj:
                return obj[key]
        for value in obj.values():
            found = deep_get(value, *candidate_keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = deep_get(item, *candidate_keys)
            if found is not None:
                return found
    return None


def to_date(raw: Any) -> Optional[date]:
    """Coerce the several date shapes the API uses into a date."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        # {"year": 2026, "month": 8, "day": 30}
        if {"year", "month", "day"} <= set(raw):
            return date(int(raw["year"]), int(raw["month"]), int(raw["day"]))
        raw = deep_get(raw, "physicalTime", "startTime", "dateTime", "value")
    if isinstance(raw, str):
        text = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def to_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        return to_float(deep_get(raw, "value", "magnitude", "kg", "percentage", "bpm"))
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def describe_shape(points: List[dict], limit: int = 1) -> str:
    """Pretty-print the first data point(s) so the real schema can be read off."""
    if not points:
        return "  (no data points)"
    return "\n".join(
        "  " + line
        for line in json.dumps(points[:limit], indent=2).splitlines()[:60]
    )


# ---------------------------------------------------------------- parsing
#
# Schemas below are OBSERVED from live responses (2026-08-30), not inferred.
# Units are the trap: weight arrives in GRAMS, durations as "3763s" strings, and
# protobuf int64 fields (steps, heart rate) arrive as JSON STRINGS.

DATE_KEYS = ("date", "physicalTime", "sampleTime", "startTime", "localDate")

# Incidental movement, not a training slot. The watch logs ~9 of these a week and
# counting them inflates every adherence number the coach reads.
EXCLUDED_EXERCISE_TYPES = {"WALKING"}

# The watch occasionally emits a 1-2 minute passive "session" — e.g. a 1.4 min
# STRENGTH_TRAINING at 21:15 while cooking. Real training is never this short,
# and each phantom inflates the adherence numerator.
MIN_SESSION_MINUTES = 10.0

# exerciseType -> the plan's slot vocabulary (full_plan.md section 6).
SLOT_MAP = {
    "STRENGTH_TRAINING": "A",
    "TENNIS": "C",
    "SPORT": "C",
    "RUNNING": "B",
    "BIKING": "B",
    "CARDIO_WORKOUT": "B",
    "WORKOUT": "B",
    "SWIMMING": "B",
}


def parse_duration_seconds(raw: Any) -> Optional[float]:
    """Durations arrive as protobuf strings: "3763s"."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def local_datetime(interval_or_sample: dict, key: str = "startTime") -> Optional[datetime]:
    """Instant shifted by its utcOffset, so day boundaries are LOCAL.

    Fitbit reports UTC plus an offset. Attributing a 00:11Z reading to the UTC
    date would drift the whole trend line by a day during BST.
    """
    raw = interval_or_sample.get(key) or interval_or_sample.get("physicalTime")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    offset = parse_duration_seconds(
        interval_or_sample.get("startUtcOffset") or interval_or_sample.get("utcOffset")
    )
    return moment + timedelta(seconds=offset) if offset else moment


def parse_scalar(name: str, points: List[dict]) -> Tuple[Dict[date, float], int]:
    """-> ({date: value}, unparsed_count).

    Weight keeps the FIRST reading of a day: the protocol is one consistent
    morning weigh-in, and switching that rule mid-campaign would put a step
    change in the trend line that looks physiological.
    """
    from .datatypes import REGISTRY

    key = REGISTRY[name].payload_key
    out: Dict[date, float] = {}
    unparsed = 0

    for point in points:
        payload = point.get(key)
        if not isinstance(payload, dict):
            unparsed += 1
            continue

        sample = payload.get("sampleTime") or payload
        moment = local_datetime(sample, "physicalTime")
        if moment is not None:
            day = moment.date()
        else:
            day = to_date(payload.get("date"))

        if name == "weight":
            grams = to_float(payload.get("weightGrams"))
            value = round(grams / 1000.0, 2) if grams is not None else None
        elif name == "body_fat":
            value = to_float(payload.get("percentage"))
        elif name == "daily_resting_heart_rate":
            # Arrives as a STRING ("47") — protobuf int64 in JSON.
            value = to_float(payload.get("beatsPerMinute"))
        elif name == "daily_heart_rate_variability":
            # TWO HRV fields are returned. Use averageHeartRateVariability-
            # Milliseconds: it is the one the campaign baseline was computed
            # from (SKILL.md "102 ms mean, swings 56-176" reproduces exactly
            # from this column in the Takeout export, mean 102.7, range
            # 56.1-176.0). deepSleepRootMeanSquareOfSuccessiveDifferences-
            # Milliseconds is on a different scale and would silently break
            # every comparison against baseline.
            value = to_float(payload.get("averageHeartRateVariabilityMilliseconds"))
        else:
            value = to_float(deep_get(payload, "value"))

        if day is None or value is None:
            unparsed += 1
            continue
        if name == "weight" and day in out:
            continue
        out[day] = value

    return out, unparsed


def parse_sleep(points: List[dict]) -> Tuple[Dict[date, dict], int]:
    out: Dict[date, dict] = {}
    unparsed = 0
    for point in points:
        payload = point.get("sleep")
        if not isinstance(payload, dict):
            unparsed += 1
            continue
        interval = payload.get("interval", {})
        start = local_datetime(interval, "startTime")
        end_raw = interval.get("endTime")
        if start is None:
            unparsed += 1
            continue

        hours = None
        try:
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            begin = datetime.fromisoformat(str(interval["startTime"]).replace("Z", "+00:00"))
            hours = round((end - begin).total_seconds() / 3600.0, 2)
        except (ValueError, TypeError, KeyError):
            pass

        # He goes to bed around 00:20, so a session starting after midnight
        # belongs to the PREVIOUS day's night. Without this, half the week's
        # sleep lands on the wrong date and the 7-day window is misaligned.
        night = start.date() if start.hour >= 18 else start.date() - timedelta(days=1)
        out[night] = {"sleep_hours": hours, "sleep_bedtime": start.strftime("%H:%M")}
    return out, unparsed


def parse_exercise(points: List[dict]) -> List[dict]:
    """Training sessions only, deduplicated.

    Two filters matter here, and both change adherence numbers:

    1. WALKING is excluded — incidental movement, not a session.
    2. The watch records the SAME session twice when it auto-detects one he also
       started manually (PASSIVELY_MEASURED + ACTIVELY_MEASURED of the same type,
       overlapping in time). Actively-measured wins.
    """
    rows: List[dict] = []
    for point in points:
        payload = point.get("exercise")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("exerciseType", "")
        if kind in EXCLUDED_EXERCISE_TYPES:
            continue

        interval = payload.get("interval", {})
        start = local_datetime(interval, "startTime")
        if start is None:
            continue

        metrics = payload.get("metricsSummary", {}) or {}
        seconds = parse_duration_seconds(payload.get("activeDuration"))
        method = point.get("dataSource", {}).get("recordingMethod", "")

        if seconds is not None and seconds < MIN_SESSION_MINUTES * 60:
            continue

        rows.append({
            "date": start.date().isoformat(),
            "start_time": start.strftime("%H:%M"),
            "_start": start,
            "activity": payload.get("displayName") or kind.title().replace("_", " "),
            "exercise_type": kind,
            "slot": SLOT_MAP.get(kind, ""),
            "duration_min": round(seconds / 60.0, 1) if seconds else "",
            "active_zone_minutes": to_float(metrics.get("activeZoneMinutes")) or "",
            "avg_hr": to_float(metrics.get("averageHeartRateBeatsPerMinute")) or "",
            "recording_method": method,
            # Captured for completeness, NEVER used to set intake — the coach
            # explicitly distrusts wearable calorie figures.
            "calories": to_float(metrics.get("caloriesKcal")) or "",
        })

    rows.sort(key=lambda r: r["_start"])
    deduped: List[dict] = []
    for row in rows:
        clash = None
        for kept in deduped:
            if kept["exercise_type"] != row["exercise_type"]:
                continue
            gap = abs((row["_start"] - kept["_start"]).total_seconds())
            if gap < 3 * 3600:  # same type, within 3h -> same session
                clash = kept
                break
        if clash is None:
            deduped.append(row)
        elif (row["recording_method"] == "ACTIVELY_MEASURED"
              and clash["recording_method"] != "ACTIVELY_MEASURED"):
            deduped[deduped.index(clash)] = row

    for row in deduped:
        row.pop("_start", None)
    return deduped


def read_manual(path: Path) -> Dict[date, dict]:
    """Measurements no API can supply: waist, and anything else hand-entered.

    Waist is ground truth for body composition — the BIA scale gives a trend,
    the tape gives a fact — but it is monthly and manual. Keeping it in a file
    rather than in conversation means the trend survives the conversation.
    """
    if not path.exists():
        return {}
    out: Dict[date, dict] = {}
    with path.open() as handle:
        for row in csv.DictReader(handle):
            try:
                day = date.fromisoformat(row["date"])
            except (ValueError, KeyError):
                continue
            out[day] = {k: v for k, v in row.items() if k != "date" and v}
    return out


def window(mapping: dict, start: date, end: date) -> dict:
    """Client-side date filter, for types whose server-side filter syntax is
    unresolved (sleep, exercise). Without this they return all history."""
    return {k: v for k, v in mapping.items() if start <= k <= end}


# ---------------------------------------------------------------- aggregation

DAILY_COLUMNS = [
    "date", "weight_kg", "body_fat_pct", "lean_kg", "resting_hr",
    "hrv_rmssd", "sleep_hours", "sleep_bedtime", "campaign_week",
]

WEEKLY_COLUMNS = [
    "week", "phase", "week_start", "week_end", "is_maintenance", "benchmark",
    "weight_7d_mean", "weight_delta_kg", "bf_7d_mean", "lean_7d_mean",
    "lean_floor_breach", "checkpoint_target", "delta_vs_target",
    "rhr_7d_mean", "rhr_elevated", "hrv_7d_mean",
    "sleep_7d_mean_h", "weighins_count", "sessions_done", "losing_too_fast",
    "waist_navel_cm", "waist_delta_cm", "a1_done", "a2_done",
]


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(statistics.fmean(present), 2) if present else None


def build_daily(parsed: Dict[str, Any]) -> List[dict]:
    """One row per date on which anything at all was recorded."""
    all_days = set()
    for key in ("weight", "body_fat", "daily_resting_heart_rate",
                "daily_heart_rate_variability", "sleep"):
        all_days.update(parsed.get(key, {}).keys())

    rows = []
    for day in sorted(all_days):
        weight = parsed.get("weight", {}).get(day)
        bf = parsed.get("body_fat", {}).get(day)
        sleep = parsed.get("sleep", {}).get(day, {})

        # Derived lean mass is the real diagnostic: if weight falls and lean
        # holds, the loss is fat. Null-safe — a missing BF% must not invent one.
        lean = round(weight * (1 - bf / 100.0), 2) if (weight and bf) else None

        rows.append({
            "date": day.isoformat(),
            "weight_kg": weight if weight is not None else "",
            "body_fat_pct": bf if bf is not None else "",
            "lean_kg": lean if lean is not None else "",
            "resting_hr": parsed.get("daily_resting_heart_rate", {}).get(day, ""),
            "hrv_rmssd": parsed.get("daily_heart_rate_variability", {}).get(day, ""),
            "sleep_hours": sleep.get("sleep_hours", "") or "",
            "sleep_bedtime": sleep.get("sleep_bedtime", "") or "",
            "campaign_week": campaign.week_number(day) or "",
        })
    return rows


def build_weekly(daily: List[dict], sessions: List[dict],
                 manual: Optional[Dict[date, dict]] = None) -> List[dict]:
    by_week: Dict[int, List[dict]] = {}
    for row in daily:
        week = row["campaign_week"]
        if week:
            by_week.setdefault(int(week), []).append(row)

    sessions_by_week: Dict[int, int] = {}
    for row in sessions:
        week = campaign.week_number(date.fromisoformat(row["date"]))
        if week:
            sessions_by_week[week] = sessions_by_week.get(week, 0) + 1

    def col(rows, key):
        out = []
        for r in rows:
            value = r.get(key)
            out.append(float(value) if value not in ("", None) else None)
        return out

    manual = manual or {}
    baseline_waist = None
    for day in sorted(manual):
        value = to_float(manual[day].get("waist_navel_cm"))
        if value is not None:
            baseline_waist = value
            break

    weekly, previous_weight = [], None
    for week in sorted(by_week):
        rows = by_week[week]
        start, end = campaign.week_bounds(week)

        weights = col(rows, "weight_kg")
        weight_mean = mean(weights)
        bf_mean = mean(col(rows, "body_fat_pct"))
        lean_mean = mean(col(rows, "lean_kg"))
        rhr_mean = mean(col(rows, "resting_hr"))

        target = campaign.CHECKPOINTS.get(week)
        delta_prev = (
            round(weight_mean - previous_weight, 2)
            if (weight_mean and previous_weight) else None
        )

        benchmark = ""
        for bench_date, label in campaign.BENCHMARKS.items():
            if start <= bench_date <= end:
                benchmark = label

        waist = None
        for day, entry in manual.items():
            if start <= day <= end:
                waist = to_float(entry.get("waist_navel_cm"))

        weekly.append({
            "week": "W{:02d}".format(week),
            "phase": campaign.phase_for(week),
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            "is_maintenance": week in campaign.MAINTENANCE_WEEKS,
            "benchmark": benchmark,
            "weight_7d_mean": weight_mean if weight_mean is not None else "",
            "weight_delta_kg": delta_prev if delta_prev is not None else "",
            "bf_7d_mean": bf_mean if bf_mean is not None else "",
            "lean_7d_mean": lean_mean if lean_mean is not None else "",
            # The one rule that overrides everything else in the campaign.
            "lean_floor_breach": (
                lean_mean is not None and lean_mean < campaign.LEAN_FLOOR_KG
            ),
            "checkpoint_target": target if target is not None else "",
            "delta_vs_target": (
                round(weight_mean - target, 2)
                if (weight_mean is not None and target is not None) else ""
            ),
            "rhr_7d_mean": rhr_mean if rhr_mean is not None else "",
            "rhr_elevated": (
                rhr_mean is not None and rhr_mean > campaign.RHR_ELEVATED_THRESHOLD
            ),
            "hrv_7d_mean": mean(col(rows, "hrv_rmssd")) or "",
            "sleep_7d_mean_h": mean(col(rows, "sleep_hours")) or "",
            # A "7-day mean" over two readings is not a 7-day mean. Surfacing the
            # count keeps a thin week visible instead of silently confident.
            "weighins_count": sum(1 for w in weights if w is not None),
            "sessions_done": sessions_by_week.get(week, 0),
            "losing_too_fast": (
                delta_prev is not None
                and delta_prev < -campaign.MAX_SAFE_LOSS_KG_PER_WEEK
            ),
            # Goal 3 is "reduce waist against the week-1 baseline", so the
            # delta against that baseline is the number, not the raw value.
            "waist_navel_cm": waist if waist is not None else "",
            "waist_delta_cm": (
                round(waist - baseline_waist, 1)
                if (waist is not None and baseline_waist is not None) else ""
            ),
        })
        if weight_mean is not None:
            previous_weight = weight_mean
    return weekly


def write_csv(path: Path, columns: List[str], rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
