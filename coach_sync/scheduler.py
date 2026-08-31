"""Self-scheduling daily runner — the container's entrypoint.

WHY A LONG-RUNNING LOOP AND NOT A ONE-SHOT CONTAINER
----------------------------------------------------
The work itself is one-shot: fetch, build, exit. But Docker Compose has no
scheduler. A one-shot service under `docker compose up -d` runs once and then
sits in `Exited (0)` forever, which is indistinguishable at a glance from
"crashed", and `restart: unless-stopped` would restart it *immediately* in a
tight loop rather than tomorrow at 06:30.

The three real options were:
  1. host crontab / systemd timer calling `docker run` — breaks the homelab's
     "every service is a compose file in the control repo" convention, puts
     state outside git, and needs the schedule maintained in two places;
  2. a scheduler container with the Docker socket mounted (ofelia et al.) —
     the socket is root-equivalent, a large privilege grant for one daily job;
  3. **this**: a normal long-running container that sleeps until the next
     06:30 Europe/London, runs one cycle, and loops.

Option 3 is the pattern the box already uses for `tennisbot-drop` (chosen
2026-07-22 for exactly the "no host cron, no Docker socket" reasons), so the
deploy, the logs and the restart semantics all look like everything else here.
Cost: ~15 MB of RSS asleep for 23h59m a day. That is the whole price.

WHAT IT REACTS TO
-----------------
`python -m coach_sync build` distinguishes its failures by exit code, and each
one means something different to a human:

    0  ok
    2  refused to write — new data had less in it than the file on disk
    3  nothing dated parsed at all
    4  data is stale — newest record older than MAX_DATA_AGE_DAYS

`fetch` exits non-zero when credentials fail. Every non-zero exit becomes a
Telegram message (see notify.py) *and* flips the container's healthcheck, so
the failure is visible both by push and by `docker ps`. Silent staleness is the
one failure this project exists to prevent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from . import notify
from .clock import CAMPAIGN_TZ, assert_local_timezone, now

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INPUT_DIR = ROOT / "input"
STATE_DIR = DATA_DIR / "state"
HEARTBEAT = STATE_DIR / "heartbeat.json"
# Same resolution campaign.py uses (COACH_SYNC_CAMPAIGN_CONFIG or ./campaign.toml),
# read here WITHOUT importing campaign — importing it would execute its module
# level _load() and turn a missing config into a stack trace before preflight
# ever gets to explain the problem.
CAMPAIGN_CONFIG = Path(os.environ.get("COACH_SYNC_CAMPAIGN_CONFIG")
                       or ROOT / "campaign.toml")

# What each `build` exit code means, in words a phone notification can carry.
EXIT_MEANING = {
    0: "ok",
    2: ("REFUSED TO WRITE — the new data had LESS in it than the file on disk. "
        "A fetch probably succeeded partially. The existing CSVs are untouched "
        "and still good; re-run fetch. If the loss is real, run build with "
        "--allow-shrink."),
    3: ("NOTHING PARSED — no dated records at all. Either every raw file is "
        "missing, or the API response shape changed and the parser no longer "
        "matches it. Run `inspect` against the raw JSON."),
    4: ("STALE DATA — the newest record is more than 2 days old. The CSVs were "
        "written but they describe the past. Almost always an expired Google "
        "OAuth refresh token: that failure takes out the Hevy fetch too."),
}

RAW_NAME = re.compile(r"^(?P<prefix>.+)_\d{8}T\d{6}Z\.json$")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def log(msg: str) -> None:
    """One line, timestamped in London, unbuffered (PYTHONUNBUFFERED=1) so it
    shows up in `docker logs` / Dozzle immediately rather than when a 4 KB
    buffer fills — which for a job that speaks twice a day is never."""
    print("{} [coach-sync] {}".format(now().strftime("%Y-%m-%d %H:%M:%S %Z"), msg),
          flush=True)


# ------------------------------------------------------------------ preflight

def preflight() -> List[str]:
    """Return a list of FATAL problems. Warnings are logged, not returned."""
    fatal: List[str] = []

    try:
        assert_local_timezone()
    except Exception as exc:                      # TimezoneMismatch
        fatal.append(str(exc))

    # campaign.toml is personal health data: gitignored, never in the image,
    # mounted read-only at runtime. If it is missing, campaign.py would fall
    # back to campaign.example.toml's INVENTED targets — and every
    # delta_vs_target in the output would be wrong but plausible. The image
    # deliberately ships no example file, so the fallback cannot happen; this
    # check exists to say WHY rather than let it fail with a stack trace.
    if not CAMPAIGN_CONFIG.exists():
        fatal.append(
            "campaign.toml is missing at {}. Mount it read-only from the "
            "server (it holds the real targets and thresholds and is never "
            "baked into the image).".format(CAMPAIGN_CONFIG))

    missing = [name for name in
               ("GHEALTH_CLIENT_ID", "GHEALTH_CLIENT_SECRET",
                "GHEALTH_REFRESH_TOKEN")
               if not os.environ.get(name)]
    if missing:
        fatal.append(
            "missing credential env vars: {}. They come from the untracked "
            ".env on the server (env_file in the compose file)."
            .format(", ".join(missing)))

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        probe = STATE_DIR / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        fatal.append("data volume is not writable by this user (uid {}): {}"
                     .format(os.getuid(), exc))

    # Non-fatal, but say it loudly. Waist is hand-measured ground truth that no
    # API can regenerate; if the input mount is missing the waist columns come
    # out empty and nothing else complains.
    if not (INPUT_DIR / "manual.csv").exists():
        log("!! WARNING: input/manual.csv not found. Waist measurements will be "
            "blank. Check the read-only input bind mount.")
        notify.send("coach-sync: input/manual.csv is missing — waist columns "
                    "will be empty. Check the /app/input mount.",
                    key="manual-missing")
    if not os.environ.get("HEVY_API_KEY"):
        log("!! WARNING: HEVY_API_KEY unset — lifting data will not be fetched.")

    return fatal


# ------------------------------------------------------------- run a command

def run_step(args: List[str], timeout_s: int) -> Tuple[int, str]:
    """Run `python -m coach_sync <args>`, streaming output, returning
    (exit_code, tail). A wall-clock timeout is mandatory: a hung HTTP call in a
    process nobody is watching would freeze the daily loop indefinitely, and the
    symptom would be silence — the failure mode with the worst signal."""
    cmd = [sys.executable, "-m", "coach_sync"] + args
    log("run: {}".format(" ".join(args)))
    tail: deque = deque(maxlen=25)

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            print("    | " + line, flush=True)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        code = 124
        tail.append("!! killed after {}s wall-clock timeout".format(timeout_s))
        log("!! `{}` exceeded {}s and was killed".format(" ".join(args), timeout_s))
    reader.join(timeout=5)
    return code, "\n".join(tail)


# ------------------------------------------------------------------ retention

def prune_raw(keep: int) -> int:
    """Keep the newest `keep` raw files PER DATA TYPE; delete older ones.

    Raw JSON is the landing zone (extract.py): it is what makes re-parsing
    offline possible, so it is not disposable — but it also grows ~450 KB every
    cycle with no natural bound (~160 MB/year on a 232 GB SSD shared with
    everything else).

    Count-based, not age-based, on purpose: `find -mtime +30` would delete the
    ONLY copy of a data type that has not been fetched for a month, which is
    exactly the situation where you most want the last good file. Keeping N per
    type means `latest_raw`'s corrupt-file fallback always has something to fall
    back to.
    """
    if keep <= 0 or not RAW_DIR.exists():
        return 0
    groups: dict = {}
    for path in RAW_DIR.glob("*.json"):
        match = RAW_NAME.match(path.name)
        if match:
            groups.setdefault(match.group("prefix"), []).append(path)
    removed = 0
    for prefix, paths in groups.items():
        # Filenames embed a UTC timestamp, so lexical sort == chronological.
        for path in sorted(paths, reverse=True)[keep:]:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log("!! could not prune {}: {}".format(path.name, exc))
    if removed:
        log("pruned {} raw file(s), keeping the newest {} per data type"
            .format(removed, keep))
    return removed


# ------------------------------------------------------------------ heartbeat

def read_heartbeat() -> dict:
    try:
        return json.loads(HEARTBEAT.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_heartbeat(**fields) -> None:
    state = read_heartbeat()
    state.update(fields)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, HEARTBEAT)          # atomic; never a half-written file
    except OSError as exc:
        log("!! could not write heartbeat: {}".format(exc))


def healthcheck() -> int:
    """Exit 0 healthy / 1 unhealthy — called by Docker's HEALTHCHECK.

    A sleeping process is not evidence of a working job, so this asserts
    OUTCOMES rather than liveness: the last cycle succeeded, and a cycle
    happened recently enough. That turns `docker ps` into a second, pull-based
    alerting channel that works even if Telegram is down or the token is wrong.
    """
    state = read_heartbeat()
    if not state:
        # Never run yet — a fresh container legitimately has no result. Healthy
        # so the first hours after a deploy are not a false alarm; the missing
        # first run is caught by the catch-up run instead.
        return 0
    code = state.get("last_exit_code")
    if code not in (0, None):
        print("unhealthy: last run exited {} ({})".format(
            code, EXIT_MEANING.get(code, "see logs")[:80]))
        return 1
    last = state.get("last_attempt")
    if last:
        age_h = (now() - datetime.fromisoformat(last)).total_seconds() / 3600
        limit = _env_int("MAX_SILENCE_HOURS", 30)
        if age_h > limit:
            print("unhealthy: last run was {:.1f}h ago (limit {}h)"
                  .format(age_h, limit))
            return 1
    return 0


# ---------------------------------------------------------------- the cycle

def run_cycle() -> int:
    """One day's work. Returns the exit code that characterises the run."""
    started = now()
    write_heartbeat(last_attempt=started.isoformat())

    retries = _env_int("FETCH_RETRIES", 1)
    delay_min = _env_int("RETRY_DELAY_MINUTES", 10)
    code, tail = run_step(["fetch"], timeout_s=_env_int("FETCH_TIMEOUT_S", 900))
    attempt = 0
    while code != 0 and attempt < retries and not _stop.is_set():
        attempt += 1
        log("fetch failed (exit {}); retry {}/{} in {} min"
            .format(code, attempt, retries, delay_min))
        # Transient network blips and Google 5xx are common and self-healing.
        # Retrying before alerting keeps the channel worth reading.
        if _stop.wait(delay_min * 60):
            return 0
        code, tail = run_step(["fetch"], timeout_s=_env_int("FETCH_TIMEOUT_S", 900))

    if code != 0:
        alert("FETCH FAILED (exit {}).\n\n"
              "Most likely the Google OAuth refresh token is dead "
              "(invalid_grant) or the Hevy key was rotated. Re-consent and "
              "update the .env on the server, then:\n"
              "  docker compose restart coach-sync".format(code),
              tail, key="fetch-failed")
        write_heartbeat(last_exit_code=code, last_stage="fetch",
                        finished_at=now().isoformat())
        return code

    code, tail = run_step(["build"], timeout_s=_env_int("BUILD_TIMEOUT_S", 300))
    if code != 0:
        alert("BUILD exited {}.\n\n{}".format(
            code, EXIT_MEANING.get(code, "Unrecognised exit code — see logs.")),
            tail, key="build-{}".format(code))
        write_heartbeat(last_exit_code=code, last_stage="build",
                        finished_at=now().isoformat())
        return code

    prune_raw(_env_int("RAW_KEEP", 14))
    finished = now()
    write_heartbeat(last_exit_code=0, last_stage="build",
                    last_success=finished.isoformat(),
                    finished_at=finished.isoformat())
    log("cycle OK in {:.0f}s".format((finished - started).total_seconds()))
    if _env_bool("NOTIFY_ON_SUCCESS"):
        notify.send("coach-sync: daily sync OK ({}).".format(
            finished.strftime("%a %d %b %H:%M")))
    return 0


def alert(message: str, tail: str, key: str) -> None:
    log("!! " + message.replace("\n", " "))
    text = "⚠️ coach-sync\n\n" + message
    if _env_bool("ALERT_INCLUDE_OUTPUT"):
        # Off by default: build's stdout prints weights, lean mass and targets,
        # and Telegram is a third-party custodian (ARCHITECTURE.md section 7).
        text += "\n\n--- last lines ---\n" + tail[-1500:]
    else:
        text += "\n\nFull log on the box: docker logs --tail 50 coach-sync"
    notify.send(text, key=key)


# ------------------------------------------------------------------ schedule

def parse_hhmm(value: str) -> Tuple[int, int]:
    hour, _, minute = value.strip().partition(":")
    return int(hour), int(minute or 0)


def parse_schedule(value: str) -> List[Tuple[int, int]]:
    """"13:00,20:00" -> [(13, 0), (20, 0)], sorted and de-duplicated.

    Several run times per day, because a weigh-in reaches Google Health hours
    after the scale records it — `physicalTime` is when he stood on the scale,
    not when the reading became fetchable. A single slot either fires too early
    to see the morning's reading or too late to be useful during the day.
    Re-running is free: every run refetches the whole campaign and the writes
    are idempotent, so a second slot can only add data, never disturb it.
    """
    times = sorted({parse_hhmm(part) for part in value.split(",") if part.strip()})
    if not times:
        raise SystemExit("RUN_AT is empty — expected e.g. '13:00' or '13:00,20:00'")
    for hour, minute in times:
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise SystemExit("RUN_AT has an invalid time: {:02d}:{:02d}".format(
                hour, minute))
    return times


def _next_single(after: datetime, hour: int, minute: int) -> datetime:
    """Next occurrence of hour:minute in Europe/London, strictly after `after`.

    Built from a London-aware `datetime` rather than by adding 86400 seconds:
    on 25 Oct 2026 the London day is 25 hours long, and "+1 day" arithmetic on a
    UTC instant would drift the run time by an hour for the rest of the campaign.
    """
    candidate = after.astimezone(CAMPAIGN_TZ).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate = (candidate + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
    return candidate


def next_run(after: datetime, times: List[Tuple[int, int]]) -> datetime:
    """The soonest of all configured slots, strictly after `after`."""
    return min(_next_single(after, h, m) for h, m in times)


def seconds_until(target: datetime, from_: Optional[datetime] = None) -> float:
    """Real elapsed seconds to `target` — via UTC, deliberately.

    ⚠️ Python subtlety with teeth: subtracting two aware datetimes that share
    the SAME tzinfo object ignores the zone and does *wall-clock* arithmetic.
    On the night of 25 Oct 2026, 23:00 BST -> 06:30 GMT is 7.5 hours on the
    clock but 8.5 hours of actual time. Sleeping the wall-clock figure would
    wake the job an hour early on exactly the day this pipeline is most likely
    to attribute a record to the wrong date. Converting both sides to UTC
    forces a true-elapsed subtraction.
    """
    start = from_ or now()
    return (target.astimezone(timezone.utc)
            - start.astimezone(timezone.utc)).total_seconds()


def _previous_single(before: datetime, hour: int, minute: int) -> datetime:
    candidate = before.astimezone(CAMPAIGN_TZ).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > before:
        candidate = (candidate - timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
    return candidate


def previous_run(before: datetime, times: List[Tuple[int, int]]) -> datetime:
    """The most recent of all configured slots, at or before `before`."""
    return max(_previous_single(before, h, m) for h, m in times)


def missed_todays_run(times: List[Tuple[int, int]]) -> bool:
    """True if the most recent scheduled slot has no successful run.

    Makes the job reboot-safe: the box coming back up at 07:10 after a power cut
    should not mean that slot is skipped until tomorrow. Also means the very
    first `up -d` proves the deployment immediately instead of at the next slot.
    """
    state = read_heartbeat()
    last_success = state.get("last_success")
    slot = previous_run(now(), times)
    if not last_success:
        return True
    return datetime.fromisoformat(last_success) < slot


# ---------------------------------------------------------------------- main

_stop = threading.Event()


def _handle_signal(signum, _frame) -> None:
    log("received {} — shutting down".format(signal.Signals(signum).name))
    _stop.set()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="coach_sync.scheduler")
    parser.add_argument("--at", default=os.environ.get("RUN_AT", "13:00,20:00"),
                        help="run time(s), HH:MM Europe/London, comma-separated")
    parser.add_argument("--run-once", action="store_true",
                        help="run one cycle now and exit with its code")
    parser.add_argument("--healthcheck", action="store_true",
                        help="exit 0 if the last run succeeded recently")
    args = parser.parse_args(argv)

    if args.healthcheck:
        return healthcheck()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    times = parse_schedule(args.at)
    log("starting — daily at {} Europe/London".format(
        ", ".join("{:02d}:{:02d}".format(h, m) for h, m in times)))

    problems = preflight()
    if problems:
        for problem in problems:
            log("!! PREFLIGHT: " + problem)
        alert("REFUSING TO START — misconfigured:\n\n" +
              "\n\n".join("• " + p for p in problems), "", key="preflight")
        write_heartbeat(last_attempt=now().isoformat(), last_exit_code=78,
                        last_stage="preflight")
        # Sleep before dying so `restart: unless-stopped` cannot spin this into
        # a hot crash loop that spams Telegram and the log. Exit non-zero so the
        # failure is visible in `docker ps`.
        _stop.wait(_env_int("PREFLIGHT_BACKOFF_S", 300))
        return 78

    if args.run_once:
        return run_cycle()

    if _env_bool("CATCH_UP", True) and missed_todays_run(times):
        slot = previous_run(now(), times)
        log("no successful run since the {} slot — running now (catch-up)".format(
            slot.strftime("%H:%M")))
        run_cycle()

    while not _stop.is_set():
        target = next_run(now(), times)
        write_heartbeat(next_run=target.isoformat())
        seconds = seconds_until(target)
        log("sleeping {:.1f}h until {}".format(
            seconds / 3600, target.strftime("%Y-%m-%d %H:%M %Z")))
        # Event.wait, not time.sleep: it returns immediately on SIGTERM, so
        # `docker compose down` stops in milliseconds instead of waiting out the
        # 10s grace period and being SIGKILLed mid-write.
        if _stop.wait(max(seconds, 1)):
            break
        if _stop.is_set():
            break
        run_cycle()

    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
