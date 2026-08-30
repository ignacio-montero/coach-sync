"""Campaign constants, loaded from configuration.

WHY THIS IS CONFIG AND NOT CODE
-------------------------------
A weight ladder, a lean-mass floor and a resting-heart-rate baseline are
personal health data. Hard-coding them means the repository cannot be published
without publishing them too. Loading them from a gitignored `campaign.toml`
lets the code be public while the numbers stay private.

This is also just better design — the same separation-of-config-from-code that
keeps credentials out of source. The plan is a hypothesis that gets recalibrated;
recalibrating it should not be a code change.

Real values live in `campaign.toml` (gitignored). `campaign.example.toml` is
committed as a template with obviously-fake numbers.
"""
from __future__ import annotations

import os
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

_ROOT = Path(__file__).resolve().parent.parent

# Overridable so the container can mount the real config from a DIRECTORY
# (`/app/config/campaign.toml`) instead of bind-mounting a single file over
# `/app/campaign.toml`. Single-file bind mounts are a footgun: if the host path
# does not exist when the container starts, Docker silently creates an empty
# DIRECTORY there, and the failure surfaces as `IsADirectoryError` deep in this
# module rather than as "your config is missing".
CONFIG_PATH = Path(os.environ.get("COACH_SYNC_CAMPAIGN_CONFIG")
                   or _ROOT / "campaign.toml")
_CONFIG = CONFIG_PATH
_EXAMPLE = _ROOT / "campaign.example.toml"


def _load() -> dict:
    path = _CONFIG if _CONFIG.exists() else _EXAMPLE
    if not path.exists():
        raise SystemExit(
            "No campaign config found. Copy campaign.example.toml to "
            "campaign.toml and fill in your real values."
        )
    if path is _EXAMPLE:
        print("  ! campaign.toml not found — using EXAMPLE values. "
              "Targets and thresholds will be wrong.")
    with path.open("rb") as handle:
        return tomllib.load(handle)


_CFG = _load()

CAMPAIGN_START = date.fromisoformat(_CFG["campaign"]["start_date"])
TOTAL_WEEKS = int(_CFG["campaign"]["total_weeks"])

CHECKPOINTS: Dict[int, float] = {
    i + 1: float(v) for i, v in enumerate(_CFG["targets"]["checkpoints"])
}
MAINTENANCE_WEEKS = set(_CFG["targets"]["maintenance_weeks"])

_TH = _CFG["thresholds"]
LEAN_FLOOR_KG = float(_TH["lean_floor_kg"])
RHR_BASELINE = float(_TH["rhr_baseline"])
RHR_ELEVATED_THRESHOLD = float(_TH["rhr_elevated"])
MAX_SAFE_LOSS_KG_PER_WEEK = float(_TH["max_loss_kg_per_week"])

PHASES = {
    name: range(bounds[0], bounds[1] + 1)
    for name, bounds in _CFG["phases"].items()
}

BENCHMARKS = {
    date.fromisoformat(d): label for d, label in _CFG["benchmarks"].items()
}


def week_number(d: date) -> Optional[int]:
    """Campaign week (1..TOTAL_WEEKS) containing `d`, or None if outside."""
    if d < CAMPAIGN_START:
        return None
    week = (d - CAMPAIGN_START).days // 7 + 1
    return week if week <= TOTAL_WEEKS else None


def week_bounds(week: int) -> tuple:
    """(Monday, Sunday) for a campaign week."""
    start = CAMPAIGN_START + timedelta(weeks=week - 1)
    return start, start + timedelta(days=6)


def phase_for(week: int) -> str:
    for name, weeks in PHASES.items():
        if week in weeks:
            return name
    return ""
