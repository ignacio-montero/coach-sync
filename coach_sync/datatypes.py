"""The Google Health data-type registry.

ARCHITECTURE.md D-016 — the API uses HYPHENS in URL paths and UNDERSCORES in
filter expressions:

    /v4/users/me/dataTypes/body-fat/dataPoints?filter=body_fat.sample_time...
                           ^^^^^^^^                    ^^^^^^^^

Getting this wrong returns INVALID_PARENT_DATA_TYPE_COLLECTION, which reads like
a data-availability problem and is not. So the underscore form is the single
source of truth and the path is DERIVED from it. Never hand-write both.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

SCOPE_METRICS = "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
SCOPE_SLEEP = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
SCOPE_ACTIVITY = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"

ALL_SCOPES = [SCOPE_METRICS, SCOPE_SLEEP, SCOPE_ACTIVITY]


@dataclass(frozen=True)
class DataType:
    id: str                      # underscore form — used in filter expressions
    scope: str
    time_field: Optional[str]    # filter member, or None if filtering unsupported
    page_size: int = 100

    @property
    def path(self) -> str:
        """Hyphenated form for the URL path. DERIVED — see module docstring."""
        return self.id.replace("_", "-")

    @property
    def payload_key(self) -> str:
        """camelCase key holding this type's payload in a data point. DERIVED.

        Same fact, a THIRD spelling: id `body_fat` -> path `body-fat` ->
        payload `bodyFat`. Verified against live responses 2026-08-30.
        """
        head, *rest = self.id.split("_")
        return head + "".join(word.capitalize() for word in rest)

    def filter_expr(self, since: date) -> Optional[str]:
        """Server-side filter, or None when we must filter client-side instead.

        Daily-summary types filter on a CIVIL DATE ("2026-08-24"); sample types
        filter on an instant ("2026-08-24T00:00:00Z"). Passing a timestamp to a
        daily type returns INVALID_DATA_POINT_FILTER_CIVIL_DATE_TIME_FORMAT.
        """
        if self.time_field is None:
            return None
        if self.time_field == "date":
            value = since.isoformat()
        else:
            value = since.isoformat() + "T00:00:00Z"
        return '{}.{} >= "{}"'.format(self.id, self.time_field, value)


# sleep and exercise cap pageSize at 25 (verified against the API).
#
# Their filter syntax is unresolved: they are Session record types, and
# `sleep.interval.start_time` is rejected with
# INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER even though the response body does
# contain interval.startTime. Rather than guess a fourth time (D-018), we fetch
# them unfiltered and filter client-side. Correct, slightly wasteful, and it can
# be tightened once the real member name is known from a live response.
REGISTRY = {
    "weight": DataType("weight", SCOPE_METRICS, "sample_time.physical_time"),
    "body_fat": DataType("body_fat", SCOPE_METRICS, "sample_time.physical_time"),
    "daily_resting_heart_rate": DataType(
        "daily_resting_heart_rate", SCOPE_METRICS, "date"
    ),
    "daily_heart_rate_variability": DataType(
        "daily_heart_rate_variability", SCOPE_METRICS, "date"
    ),
    "sleep": DataType("sleep", SCOPE_SLEEP, None, page_size=25),
    "exercise": DataType("exercise", SCOPE_ACTIVITY, None, page_size=25),
}
