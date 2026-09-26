"""coach-sync — data pipeline for the Marta Ibanez coaching campaign."""
__version__ = "0.3.3"

# Exit codes are a CONTRACT between the CLI (which emits them) and the
# scheduler (which turns them into Telegram alerts), so they live here rather
# than in either one — a duplicated magic number across that boundary drifts.
#
# 5 = the primary source succeeded, a secondary one did not. The scheduler must
# alert but CONTINUE: blocking the build on a Hevy outage would trade a missing
# lift log for a missing weight trend, and weight is what the campaign is
# scored on.
PARTIAL_FETCH = 5

# Same kind of contract: `build` prints one line per row a refused write would
# have lost, and the scheduler copies exactly those lines into the alert.
MISSING_PREFIX = "  missing: "
