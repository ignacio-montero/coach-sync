# coach-sync

Vertical slice of the data pipeline described in [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

Pulls body-composition and training data from the Google Health API and writes
the CSVs the coach reads. **This slice covers extract + transform only** — no
Hevy (blocked on Pro), no Docker, no homelab, no Google Sheet, no scheduling.

## Setup

```bash
uv venv --python 3.12
uv pip install httpx
cp .env.example .env      # then fill it in — .env is gitignored
```

For today's run, paste an access token from the OAuth 2.0 Playground into
`GHEALTH_ACCESS_TOKEN`. It expires in about an hour, which is fine for one run.

## Run

```bash
.venv/bin/python -m coach_sync fetch      # API -> data/raw/*.json
.venv/bin/python -m coach_sync inspect    # print the real JSON shape
.venv/bin/python -m coach_sync build      # raw -> data/*.csv
```

`build` never calls the API. Once `fetch` has run you can iterate on parsing
offline — which matters, because the parser is written against an *inferred*
schema and will probably need one correction on first contact.

**If `build` reports "parsed 0 of N points"**, the schema guess is wrong. Run
`inspect`, and the real field names can be pinned in `transform.py`
(`VALUE_KEYS` and `DATE_KEYS`).

## Output

| File | Grain | Notes |
|---|---|---|
| `data/metrics_daily.csv` | one row per date | Raw. Nulls preserved — a missed weigh-in is data |
| `data/metrics_weekly.csv` | one row per campaign week | **What the coach reads.** All derived |
| `data/sessions.csv` | one row per session | Tennis, football, running — the non-Hevy slots |

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Covers the branches where a wrong number changes a plan decision: the 67 kg lean
floor, the 0.6 kg/week rate cap, sleep night-attribution across midnight, and
thin-week visibility.

## Known gaps

- **Hevy** — blocked on Pro. Top sets still come from the app by hand.
- **Sleep/exercise filtering** — done client-side; their server-side filter
  syntax is unresolved (see ARCHITECTURE.md §9). Correct, mildly wasteful.
- **Session dedup** — a gym session may appear in both Hevy and `sessions.csv`.
  Not implemented yet; matters once Hevy is connected.
- **OQ-5** — refresh tokens expire after 7 days while the OAuth app is in
  "Testing". `auth.py` detects `invalid_grant` and says so explicitly.
