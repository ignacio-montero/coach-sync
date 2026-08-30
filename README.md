# coach-sync

A daily ETL pipeline that pulls personal body-composition and training data from
the **Google Health API** and **Hevy**, derives weekly training metrics, and
writes them to CSV.

Built to solve a specific problem: the numbers needed to make weekly training
decisions were spread across three apps, and the summaries that mattered —
7-day rolling means, derived lean mass, deltas against a target — had to be
recomputed by hand every week, or not at all.

```
Google Health API ─┐
  weight, body fat │
  sleep, RHR, HRV  ├─→  extract  →  raw JSON  →  transform  →  metrics_daily.csv
  exercise         │    (fetch)    (landing)    (aggregate)   metrics_weekly.csv
Hevy API ──────────┘                                          sessions.csv
  workouts, sets                                              lifts.csv
```

## Design notes

**Fetch and parse are separate steps, with raw JSON persisted between them.**
The parser was written against an inferred schema and was wrong in four places
on first contact with live data. Because the raw responses are kept, each fix
was re-run offline against saved files — no re-fetching, no re-authenticating.
This is the raw-landing-zone (or "bronze layer") pattern: never discard what the
source actually said, because your understanding of it will change.

**Aggregation lives in the pipeline, not in the consumer.** The output is read
by an LLM-based coaching assistant. Every threshold the pipeline computes —
lean-mass floor breaches, rate-of-loss caps, target deltas — is one the model
cannot get wrong at inference time.

**One constant, three spellings.** The Google Health API uses hyphens in URL
paths (`body-fat`), underscores in filter expressions (`body_fat`), and camelCase
in response payloads (`bodyFat`). All three derive from a single identifier in
[`datatypes.py`](coach_sync/datatypes.py) rather than being written out
separately, because three hand-maintained spellings of one fact will drift.

**Config is separate from code.** Targets, thresholds and phase boundaries load
from a gitignored `campaign.toml`. They are personal health data, and they also
change independently of the code — recalibrating a plan should not be a commit.

**Nulls are preserved.** A missing weigh-in is information about adherence.
Interpolating it would launder a gap into a measurement.

## Things live data taught that synthetic fixtures did not

- Weight arrives in **grams** (`weightGrams: 84350`), not kilograms.
- Daily-summary types filter on a **civil date**; sample types filter on an
  **instant**. Mixing them returns `INVALID_DATA_POINT_FILTER_CIVIL_DATE_TIME_FORMAT`.
- Protobuf `int64` fields arrive as JSON **strings** (`"47"`, `"3763s"`).
- A wrist wearable logs ~9 passive walks a week and the occasional 84-second
  phantom "strength training". Both inflate adherence if counted.
- The same session can be recorded twice — once auto-detected, once manually
  started — and needs deduplicating by type and time window.
- Two different HRV fields are returned. Picking the wrong one silently breaks
  every comparison against a historical baseline.

## Setup

```bash
uv venv --python 3.12 && uv pip install httpx
cp .env.example .env                      # credentials
cp campaign.example.toml campaign.toml    # targets and thresholds
```

Requires a Google Cloud project with the Google Health API enabled, and a Hevy
Pro subscription for the lifting data.

## Usage

```bash
python -m coach_sync fetch      # APIs  -> data/raw/*.json
python -m coach_sync inspect    # print the real response shape
python -m coach_sync build      # raw   -> data/*.csv
```

`build` never calls the API, so parsing can be iterated offline.

## Tests

```bash
python -m pytest tests/ -q
```

25 tests, aimed at the branches where a wrong number changes a decision —
threshold breaches, rate caps, sleep attribution across midnight, top-set
selection when sets tie on weight — rather than at line coverage.

## Privacy and publishing

No personal data is in this repository. See [PRIVACY.md](PRIVACY.md) for what
the application accesses, and [PUBLISHING.md](PUBLISHING.md) for the guardrails
that keep it out — including a pre-commit scanner that blocks credentials and
health-data paths from being committed.

## Licence

MIT
