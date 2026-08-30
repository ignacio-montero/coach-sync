# syntax=docker/dockerfile:1
#
# coach-sync — production image for the homelab (Intel N95, linux/amd64).
#
# MULTI-STAGE BUILD. The first stage has uv, compilers and the full dependency
# resolution machinery; the final stage gets only the resulting virtualenv and
# the application. Nothing that was needed to BUILD the thing ships in the thing
# — smaller image, faster pulls on a slow box, less attack surface.
#
# Rejected: a single-stage `pip install -r requirements.txt` image. It works,
# but bakes pip's cache and build tooling into the layers and gives no lockfile
# guarantee.

# ---------------------------------------------------------------- 0. tooling
# uv comes from its own pinned image rather than `pip install uv`, so the
# resolver version is reproducible too. Pinned deliberately: an unpinned
# builder tool is an unpinned build.
FROM ghcr.io/astral-sh/uv:0.11.23 AS uv

# ---------------------------------------------------------------- 1. builder
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# IMAGE LAYER CACHING: copy the manifest + lockfile FIRST and install from them
# in their own layer. Docker caches layers by the checksum of their inputs, so
# editing coach_sync/*.py leaves this layer untouched and the dependency install
# is skipped entirely. Copying the source first would invalidate it on every
# code change and re-download httpx every build.
COPY pyproject.toml uv.lock ./

# --frozen: install EXACTLY what uv.lock says, and fail if the lock disagrees
# with pyproject.toml rather than quietly re-resolving. This is the line that
# makes "rebuild in six months" produce the same dependency tree as today.
# --no-dev: pytest and friends stay out of the runtime image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------- 2. runtime
FROM python:3.12-slim-bookworm AS runtime

ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="coach-sync" \
      org.opencontainers.image.description="Daily Google Health + Hevy ETL job" \
      org.opencontainers.image.source="https://github.com/ignacio-montero/coach-sync" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    TZ=Europe/London

# NON-ROOT. uid/gid 1000 is not arbitrary: the container writes to a HOST BIND
# MOUNT (/srv/coach-sync/data), and a bind mount carries the host's ownership
# straight through — there is no uid translation. 1000 is the first human user
# on Ubuntu (`nacho`), so the files the container writes are the files the Mac's
# rsync can read, with no chown dance and no root-owned output.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --chown=1000:1000 coach_sync ./coach_sync
COPY --chown=1000:1000 README.md LICENSE ./

# Mount points, pre-created so an empty volume inherits the right ownership.
#   data/   — CSVs + raw landing zone (read-write bind mount)
#   input/  — hand-entered ground truth (READ-ONLY bind mount)
#   config/ — campaign.toml (READ-ONLY bind mount of a DIRECTORY, not a file:
#             a single-file bind mount whose host path is missing makes Docker
#             create an empty directory in its place, which fails obscurely)
RUN mkdir -p /app/data/raw /app/data/state /app/input /app/config \
 && chown -R 1000:1000 /app/data /app/input /app/config

# NOT COPIED, ON PURPOSE:
#   .env             — secrets; injected at runtime via compose env_file
#   campaign.toml    — real targets/thresholds; mounted read-only at runtime
#                    at /app/config/campaign.toml (COACH_SYNC_CAMPAIGN_CONFIG)
#   campaign.example.toml — omitting it is load-bearing. campaign.py falls back
#                    to the example's INVENTED numbers when campaign.toml is
#                    missing, which would produce plausible-but-wrong targets.
#                    With no example present that fallback cannot happen and the
#                    job refuses to start instead (scheduler.preflight).
#   data/, input/    — personal health data. See .dockerignore, which is
#                    deny-by-default so this stays true as the repo grows.

USER 1000:1000

# No EXPOSE and no ports: this service only makes OUTBOUND HTTPS calls
# (health.googleapis.com, api.hevyapp.com, api.telegram.org). Nothing listens,
# so there is nothing to bind, no UFW rule, and no Docker-bypasses-UFW exposure.

# The healthcheck asserts an OUTCOME (last cycle exited 0, recently) rather than
# liveness. A daily job spends 23h59m asleep, so "the process exists" proves
# almost nothing — a crashed-but-restarted loop that never fetches would pass it.
HEALTHCHECK --interval=5m --timeout=15s --start-period=1m --retries=2 \
  CMD ["python", "-m", "coach_sync.scheduler", "--healthcheck"]

# ENTRYPOINT holds the command; extra `docker run` args are APPENDED to it, so
# `docker run <image> --run-once` works. To run a different subcommand by hand,
# override it: `docker compose run --rm --entrypoint python coach-sync -m coach_sync build`.
ENTRYPOINT ["python", "-m", "coach_sync.scheduler"]
