"""Telegram alerting for unattended runs.

WHY TELEGRAM, AND WHY NOT SOMETHING CLEVERER
--------------------------------------------
A daily job that fails silently is worse than no job at all: the CSVs still
parse, the coach still reads them, and the numbers are just quietly out of
date. Every non-zero exit therefore has to reach a human. The homelab already
alerts through Telegram (`legobot`, the `tennisbot-*` trio), so this reuses that
channel rather than adding email/SMTP, a webhook receiver, or a monitoring
stack — one more moving part on a 4-core box, and one more thing to get wrong.

Rejected: exit-code-only alerting via Docker health status (nothing on the box
watches health transitions); e-mail (needs an SMTP relay + credentials, and the
box has neither).

⚠️ SAFE TO SHARE A BOT TOKEN, WITH ONE RULE
Only ONE process may LONG-POLL a token (`getUpdates`) — a second one gets HTTP
409, which is the gotcha recorded against legobot in the homelab's services.md.
This module only ever calls `sendMessage`, an ordinary request, so it can reuse
the existing bot token safely. It must never call getUpdates.

⚠️ NO HEALTH NUMBERS IN ALERTS, BY DEFAULT
Telegram is a third-party custodian, and ARCHITECTURE.md section 7 says to
minimise copies of body-composition data. `build`'s stdout prints weights, lean
mass and targets, so the message carries only the *shape* of the failure — exit
code, meaning, counts — not the data. `ALERT_INCLUDE_OUTPUT=true` opts in to
attaching the command's tail if a failure ever needs it; the full log always
lives in `docker logs` / Dozzle on the box, which is not a third party.
"""
from __future__ import annotations

import os
import time
from typing import Dict, Optional

import httpx

API = "https://api.telegram.org/bot{token}/sendMessage"

# Suppression window for an identical alert. Without it, a broken refresh token
# would fire the same message on every retry and every restart, and an alert
# channel that cries wolf gets muted — which reintroduces exactly the silent
# failure this exists to prevent.
DEFAULT_COOLDOWN_S = 6 * 3600

_last_sent: Dict[str, float] = {}


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str, key: Optional[str] = None,
         cooldown_s: int = DEFAULT_COOLDOWN_S) -> bool:
    """Send one message. Returns True if it went out.

    `key` identifies the *kind* of alert for de-duplication (e.g. "fetch-failed"),
    so a recurring failure alerts once per cooldown rather than every cycle.
    Never raises: a notifier that can take down the job it is reporting on is a
    liability. Failures to notify are printed, and stdout is captured by Docker.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  [notify] TELEGRAM_BOT_TOKEN/CHAT_ID unset — alert not sent:")
        print("  [notify] {}".format(text.replace("\n", " | ")[:300]))
        return False

    if key:
        last = _last_sent.get(key)
        if last is not None and (time.time() - last) < cooldown_s:
            mins = int((time.time() - last) // 60)
            print("  [notify] suppressed duplicate alert {!r} "
                  "(last sent {} min ago)".format(key, mins))
            return False

    try:
        resp = httpx.post(
            API.format(token=token),
            json={"chat_id": chat_id, "text": text[:3900],
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if resp.status_code != 200:
            # resp.text can echo the request; never log the URL (it holds the
            # token). Status + body prefix is enough to diagnose.
            print("  [notify] telegram returned {}: {}".format(
                resp.status_code, resp.text[:200]))
            return False
    except httpx.HTTPError as exc:
        print("  [notify] telegram unreachable: {}".format(type(exc).__name__))
        return False

    if key:
        _last_sent[key] = time.time()
    return True
