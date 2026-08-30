"""Access-token handling.

Two modes, deliberately:

1. GHEALTH_ACCESS_TOKEN — paste a token straight from the OAuth 2.0 Playground.
   Expires in ~1 hour. Fine for a one-off run today; useless for a daily job.

2. Client ID + secret + refresh token — exchanges for a fresh access token on
   every run. This is what the homelab container will use.

Credentials are read from the environment or a local .env. They are never
logged, never written to the data files, and never leave this machine.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader — avoids a dependency for six lines of parsing."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):        # tolerate `export FOO=bar`
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        # setdefault alone would honour an EXISTING EMPTY variable — and Docker
        # Compose sets one for `environment: - HEVY_API_KEY` with no value, or
        # an env_file line `HEVY_API_KEY=`. The result was a silently skipped
        # Hevy fetch and last week's workouts reported as this week's.
        if not os.environ.get(key):
            os.environ[key] = value


def load_env(env_file: Path) -> None:
    """Load .env once, explicitly. Previously this happened as a side effect of
    get_access_token, so the Hevy key was only present because auth ran first —
    reordering two lines in the CLI would have silently disabled lifting data."""
    _load_dotenv(env_file)


def get_access_token(env_file: Path = Path(".env"),
                     refresh_key: str = "GHEALTH_REFRESH_TOKEN") -> str:
    """Exchange a refresh token for an access token.

    TWO refresh tokens, not one — and this is forced, not a preference.
    The Google Health API rejects any access token that ALSO carries a Drive
    scope:

        403 PERMISSION_DENIED  DISALLOWED_OAUTH_SCOPES
        disallowed_scopes: drive_resource

    So a single token holding both the health scopes and drive.file cannot read
    health data at all, even though the health scopes are present and valid.
    The scope families have to live on separate tokens, obtained from separate
    consent flows against the same OAuth client.

    A useful side effect: the health token cannot touch Drive, and the Drive
    token cannot read health data. Narrower than one combined token would be.

        GHEALTH_REFRESH_TOKEN  -> the three googlehealth.* scopes
        GSHEETS_REFRESH_TOKEN  -> drive.file only
    """
    _load_dotenv(env_file)

    client_id = os.environ.get("GHEALTH_CLIENT_ID")
    client_secret = os.environ.get("GHEALTH_CLIENT_SECRET")
    refresh_token = os.environ.get(refresh_key)
    direct = os.environ.get("GHEALTH_ACCESS_TOKEN")

    # Refresh-token credentials WIN over a pasted access token. They are the
    # durable path — an access token lives about an hour, so if both are
    # present the pasted one is almost certainly a stale leftover. Preferring
    # it would make the job fail an hour after it last worked, which is a
    # miserable thing to debug.
    if all([client_id, client_secret, refresh_token]):
        pass
    elif direct and refresh_key == "GHEALTH_REFRESH_TOKEN":
        return direct.strip()
    else:
        raise SystemExit(
            "No credentials found.\n\n"
            "Missing {} (plus GHEALTH_CLIENT_ID / GHEALTH_CLIENT_SECRET).\n"
            "Health and Sheets need SEPARATE refresh tokens — Google Health\n"
            "rejects any token that also carries a Drive scope.\n".format(refresh_key) +
            "Quick alternative: GHEALTH_ACCESS_TOKEN (expires in ~1 hour).\n"
            "See .env.example."
        )

    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        # invalid_grant here is very likely OQ-5: refresh tokens issued while the
        # OAuth app is in "Testing" status expire after 7 days.
        raise SystemExit(
            "Token refresh failed ({}): {}\n\n"
            "If this says 'invalid_grant', the refresh token has expired. An\n"
            "OAuth app in 'Testing' publishing status issues refresh tokens that\n"
            "die after 7 days. Publish the consent screen to 'In production'\n"
            "AND re-authorise — publishing does not extend tokens already\n"
            "issued under Testing. See ARCHITECTURE.md OQ-5.".format(
                resp.status_code, resp.text[:300]
            )
        )
    return resp.json()["access_token"]
