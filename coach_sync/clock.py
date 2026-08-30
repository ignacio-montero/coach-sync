"""The one place that is allowed to ask what time it is.

WHY THIS MODULE EXISTS
----------------------
`date.today()` returns the date in the *process's local time zone*, which in a
container is whatever `TZ` happens to be — UTC by default. This pipeline uses
"today" as the closing edge of the window it keeps records for:

    win_start, win_end = campaign.CAMPAIGN_START, date.today()

Between 23:00 and 00:00 London time in summer (BST = UTC+1), a UTC container
still thinks it is *yesterday*. The window closes a day early and that day's
weigh-in, sleep and sessions are silently dropped — no error, no missing file,
just a CSV that is quietly one row short. That is the exact failure class this
project exists to eliminate.

TWO DEFENCES, DELIBERATELY
--------------------------
1. **Explicit `ZoneInfo("Europe/London")` (the fix).** `today()` names the zone
   rather than inheriting it, so the answer is correct even if `TZ` is unset,
   wrong, or dropped from the compose file in a future edit. Correctness must
   not depend on ambient configuration.
2. **`assert_local_timezone()` (the tripwire).** The scheduler calls it at
   startup and refuses to run if the container's local zone disagrees with
   London. Defence 1 makes the code right; defence 2 makes a misconfigured
   deployment *loud* instead of merely harmless — so log timestamps stay
   readable and a future `date.today()` reintroduced by an unrelated edit is
   caught by CI/first boot rather than by a missing row in December.

The zone is named, never an offset. Europe/London is UTC+1 until 25 Oct 2026
and UTC+0 after; hard-coding `+01:00` would go wrong mid-campaign.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# The campaign is run, weighed and coached in London. Every civil date this
# pipeline emits is a London date.
CAMPAIGN_TZ_NAME = "Europe/London"
CAMPAIGN_TZ = ZoneInfo(CAMPAIGN_TZ_NAME)


def now() -> datetime:
    """Timezone-aware 'now' in the campaign zone."""
    return datetime.now(CAMPAIGN_TZ)


def today() -> date:
    """The civil date in London — never the container's idea of local time."""
    return now().date()


class TimezoneMismatch(RuntimeError):
    """The container's local time zone is not the campaign's."""


def assert_local_timezone(expected: str = CAMPAIGN_TZ_NAME) -> None:
    """Fail loudly if the process's local zone is not the campaign zone.

    Compares UTC OFFSETS at a single instant rather than zone name strings:
    "Europe/London", "GB" and a copied /etc/localtime all name the same zone,
    and a name comparison would reject two of them for no reason. The offset is
    the thing that actually changes behaviour, and comparing it is automatically
    DST-correct — in November this asserts +00:00, in August +01:00, with no
    edit at the clock change.
    """
    expected_zone = ZoneInfo(expected)
    instant = datetime.now(timezone.utc)
    want = instant.astimezone(expected_zone).utcoffset()
    have = instant.astimezone().utcoffset()          # process-local zone
    if want != have:
        raise TimezoneMismatch(
            "Container time zone is wrong: local offset {} but {} is {} right "
            "now (TZ={!r}).\n"
            "Set `TZ: \"Europe/London\"` in the compose environment. Derived "
            "dates are computed in London explicitly and are still correct, but "
            "a container whose clock disagrees with its data is a bug waiting "
            "to surface somewhere this module does not cover."
            .format(have, expected, want, os.environ.get("TZ"))
        )
