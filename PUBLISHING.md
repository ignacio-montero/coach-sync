# Publishing this repository

This repo is intended to be **public**. The parent project it lives inside is
**private** and contains personal health data. Read this before pushing.

## The split, and why

| Stays private | Goes public |
|---|---|
| `my-coach/` — coaching content: targets, psychological profile, household | `pipeline/` — the code |
| `build/` — intake notes | tests |
| `Takeout/` — raw health export (569 MB) | architecture docs |
| `pipeline/data/` — every weight, body-fat, sleep and lift record | `campaign.example.toml` |
| `pipeline/campaign.toml` — real targets and thresholds | |
| `.env` — all credentials | |

The publishable artifact is the **engineering**: ETL design, API integration,
test strategy. Not the body-composition numbers. Those are separable now because
campaign constants live in config rather than in code.

## Guardrails, in order of reliability

1. **`scripts/check_publishable.py`** — scans what is actually STAGED for
   credentials, tokens, the Google Health user id, and health-data file paths.
   Installed as a `pre-commit` hook, it blocks the commit. This is the real
   control: it survives `git add -f`, which `.gitignore` does not.
2. **`.gitignore`** — a convenience. It stops accidents, not overrides.
3. **`campaign.toml` is gitignored**; `campaign.example.toml` is committed with
   obviously-fake numbers. A fresh clone prints a warning and uses the fakes.

Install the hook after cloning:

```bash
ln -sf ../../scripts/check_publishable.py .git/hooks/pre-commit
```

Audit everything already tracked at any time:

```bash
python3 scripts/check_publishable.py --all
```

## Before the first push — check by hand

```bash
git ls-files                                    # is anything here personal?
python3 scripts/check_publishable.py --all      # scanner over tracked files
git log -p | grep -iE "ya29|GOCSPX|1//|users/[0-9]{15}"   # history is forever
```

That last one matters most. **Git history cannot be quietly edited once pushed.**
A secret committed and then deleted is still in the history, still clonable, and
must be treated as compromised — rotate it, don't just remove it.

## Rules that must not drift

- **Never commit `campaign.toml`, `.env`, or anything under `data/`.**
- **Never widen this repo** to include `my-coach/`, `build/`, or `Takeout/`.
  If the CV story needs the coaching design explained, write *about* it in prose
  — do not vendor the personal documents.
- **Rotate, don't delete.** If a credential ever reaches a commit, revoke it at
  the provider first. Removing the file changes nothing.
