"""Extract — fetch data points from the Google Health API and persist them raw.

Design note: fetch and parse are deliberately SEPARATE steps, with the raw JSON
written to disk in between.

The parser in transform.py is written against an inferred response schema. When
it turns out to be wrong, we fix the parser and re-run it against the saved raw
files — no re-fetching, no re-authenticating, no extra API calls. This is the
"raw landing zone" pattern (bronze layer, in medallion terms): never throw away
what the source actually said, because your understanding of it will change.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx

from .datatypes import REGISTRY, DataType

BASE_URL = "https://health.googleapis.com/v4"


def fetch_data_type(
    client: httpx.Client, dt: DataType, since: date
) -> List[Dict[str, Any]]:
    """Fetch every data point for one data type, following pagination."""
    url = "{}/users/me/dataTypes/{}/dataPoints".format(BASE_URL, dt.path)
    params: Dict[str, Any] = {"pageSize": dt.page_size}

    filter_expr = dt.filter_expr(since)
    if filter_expr:
        params["filter"] = filter_expr

    points: List[Dict[str, Any]] = []
    page = 0
    while True:
        resp = client.get(url, params=params, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(
                "{} -> {} {}\n{}".format(
                    dt.path, resp.status_code, resp.reason_phrase, resp.text[:600]
                )
            )
        body = resp.json()
        batch = body.get("dataPoints", []) or []
        points.extend(batch)
        page += 1

        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
        if page > 200:  # guard against a pagination loop
            break

    return points


def extract_all(access_token: str, since: date, raw_dir: Path) -> Dict[str, Path]:
    """Fetch all six data types, write each to raw_dir, return the paths."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written: Dict[str, Path] = {}

    headers = {"Authorization": "Bearer {}".format(access_token)}
    with httpx.Client(headers=headers) as client:
        for name, dt in REGISTRY.items():
            print("  fetching {:32s}".format(name), end="", flush=True)
            try:
                points = fetch_data_type(client, dt, since)
            except RuntimeError as exc:
                print("  FAILED")
                print("    {}".format(str(exc).replace("\n", "\n    ")))
                continue

            path = raw_dir / "{}_{}.json".format(name, stamp)
            # Atomic: a container kill, OOM or full disk mid-write would
            # otherwise leave truncated JSON. latest_raw() picks the newest
            # filename, so that broken file would be chosen forever and every
            # subsequent build would fail with no way to self-heal.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(points, indent=2))
            os.replace(tmp, path)
            written[name] = path
            print("  {:4d} points -> {}".format(len(points), path.name))

    return written


def latest_raw(raw_dir: Path, name: str) -> Path | None:
    """Most recent PARSEABLE raw file, so transform can re-run offline.

    Newest-first with a parse check, rather than blindly taking the newest:
    a corrupt file would otherwise poison every future build permanently, since
    it stays newest forever. Falling back to the previous good file degrades to
    stale-but-valid, which the staleness check in `build` then surfaces.
    """
    matches = sorted(raw_dir.glob("{}_*.json".format(name)), reverse=True)
    for path in matches:
        try:
            json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            print("  ! ignoring unreadable raw file: {}".format(path.name))
            continue
        return path
    return None
