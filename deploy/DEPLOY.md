# DEPLOY — coach-sync on the homelab

Runbook for the daily Google Health + Hevy ETL job. Written for the homelab
session (the one that owns `~/Development/homelab` and `ssh homelab`).

**Nothing in this file has been executed against the server.** The bundle was
prepared and verified locally; the deploy itself is a human-gated action.

---

## 0 · Shape of the service, in one paragraph

A single Python container with **no published ports**. It wakes at **06:30
Europe/London**, runs `fetch` (Google Health + Hevy → raw JSON) then `build`
(raw → four CSVs), prunes old raw captures, and goes back to sleep. Failures
go to **Telegram** and flip the container's **healthcheck**. Persistent state
lives in **host bind mounts under `/srv/coach-sync`**, not in Docker volumes.
Steady-state cost: ~15 MB RSS asleep, a few seconds of CPU a day, ~6 MB of disk.

| | |
|---|---|
| Image | `ghcr.io/ignacio-montero/coach-sync:0.1.0` (private, amd64, pinned) |
| Ports | **none** — outbound HTTPS only |
| RAM | `mem_limit: 192m` (measured peak < 64 MB) |
| Disk | ~6–7 MB steady state under `/srv/coach-sync` |
| Secrets | `~/homelab/services/coach-sync/.env` on the server (untracked) |
| Data | `/srv/coach-sync/{data,input,config}` bind mounts |
| Alerting | Telegram (`sendMessage` only) + Docker healthcheck |

---

## 1 · Publish the image

The box **pulls**; it never builds. Building Python wheels on a 4-core N95 is
minutes of its life for a byte-identical result, and a pull is reproducible.

### Option A — GitHub Actions (preferred)

Tagging the repo builds a **native amd64** image on GitHub's runners and pushes
it to GHCR. No QEMU, no dependence on the Mac being awake.

```bash
cd ~/Development/Personal-trainer/pipeline
git tag v0.1.0 && git push origin v0.1.0        # triggers .github/workflows/release.yml
```

> **First publish only:** the package is created public (it inherits the repo's
> visibility). Set it to **private** at
> `github.com/users/ignacio-montero/packages/container/coach-sync/settings`, to
> match `plaque-hunter` and `legobot`. The box is already `docker login`'d to
> ghcr.io, so a private package needs no extra setup — but confirm the token
> still has `read:packages` before the first pull.

### Option B — from the Mac

⚠️ The Mac is arm64 and the box is amd64. `--platform linux/amd64` is not
optional; without it the box refuses the image with `exec format error`.
Cross-building runs under QEMU emulation and is slow (minutes).

```bash
cd ~/Development/Personal-trainer/pipeline
docker buildx build --platform linux/amd64 \
  --build-arg VERSION=0.1.0 \
  -t ghcr.io/ignacio-montero/coach-sync:0.1.0 --push .
```

**Never `:latest`.** A pinned tag is what makes rollback a one-line edit and
`docker ps` an honest statement about which code is running.

---

## 2 · Prepare the server (once)

### 2.1 Persistent directories

```bash
ssh homelab '
  sudo mkdir -p /srv/coach-sync/data/raw /srv/coach-sync/data/state \
                /srv/coach-sync/input /srv/coach-sync/config
  sudo chown -R 1000:1000 /srv/coach-sync
  sudo chmod 750 /srv/coach-sync/input /srv/coach-sync/config
  id -un 1000    # sanity: should print nacho
'
```

The container runs as uid 1000. **Bind mounts pass host ownership straight
through — there is no uid translation** — so 1000 must own these paths or every
write fails. If `id -un 1000` is not `nacho`, say so before continuing; the
rsync step in §6 assumes it is.

### 2.2 The two files that are NOT in any repo

Both are personal health data, gitignored in the project and absent from the
image. Copy them over SSH — never through git, never through a chat window.

```bash
# real targets, thresholds, checkpoint ladder
scp ~/Development/Personal-trainer/pipeline/campaign.toml \
    homelab:/tmp/campaign.toml
# hand-measured waist history — IRREPLACEABLE, see section 5
scp ~/Development/Personal-trainer/pipeline/input/manual.csv \
    homelab:/tmp/manual.csv

ssh homelab '
  sudo mv /tmp/campaign.toml /srv/coach-sync/config/campaign.toml
  sudo mv /tmp/manual.csv    /srv/coach-sync/input/manual.csv
  sudo chown 1000:1000 /srv/coach-sync/config/campaign.toml /srv/coach-sync/input/manual.csv
  sudo chmod 640 /srv/coach-sync/config/campaign.toml /srv/coach-sync/input/manual.csv
'
```

⚠️ **`campaign.toml` must exist before the first `up -d`.** The image ships no
`campaign.example.toml`, deliberately: with the example present the code would
silently fall back to *invented* targets and every `delta_vs_target` in the
output would be plausible and wrong. Without it, the container refuses to start
and says why.

### 2.3 Seed the history (optional, recommended)

The first run fetches from the campaign start date, so no seeding is strictly
required. If you want the box to start from what the Mac already has:

```bash
rsync -av ~/Development/Personal-trainer/pipeline/data/ homelab:/tmp/coach-data/
ssh homelab 'sudo rsync -a /tmp/coach-data/ /srv/coach-sync/data/ \
             && sudo chown -R 1000:1000 /srv/coach-sync/data && rm -rf /tmp/coach-data'
```

This also arms the **shrink guard**: `build` refuses to replace a CSV with one
containing less data, so a partial fetch cannot quietly delete history.

### 2.4 The service definition and its secrets

```bash
mkdir -p ~/Development/homelab/services/coach-sync
cp ~/Development/Personal-trainer/pipeline/deploy/docker-compose.yml \
   ~/Development/homelab/services/coach-sync/docker-compose.yml
# then add to ~/Development/homelab/compose.yaml:
#   - services/coach-sync/docker-compose.yml
```

Secrets go in an **untracked `.env` on the server**, next to the compose file:

```bash
scp ~/Development/Personal-trainer/pipeline/deploy/.env.example \
    homelab:/tmp/coach-sync.env
ssh homelab 'mkdir -p ~/homelab/services/coach-sync && \
             mv /tmp/coach-sync.env ~/homelab/services/coach-sync/.env && \
             chmod 600 ~/homelab/services/coach-sync/.env'
ssh -t homelab 'nano ~/homelab/services/coach-sync/.env'    # fill in the values
```

Required: `GHEALTH_CLIENT_ID`, `GHEALTH_CLIENT_SECRET`, `GHEALTH_REFRESH_TOKEN`,
`HEVY_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

> **Concept — env injection.** `env_file:` hands variables to the container's
> process at start time; they exist in RAM, never in an image layer and never in
> git. This is why a rotated credential needs only an edit + `up -d`, and why
> `docker history` on the published image reveals nothing.

---

## 3 · Deploy

```bash
cd ~/Development/homelab
git add services/coach-sync compose.yaml && git commit -m "feat: add coach-sync daily ETL"
git push
ssh homelab 'cd ~/homelab && git pull && docker compose pull coach-sync && docker compose up -d coach-sync'
```

Nothing here touches networking, the firewall, SSH or disks beyond creating
`/srv/coach-sync`. **Rollback for the whole thing:**
`ssh homelab 'cd ~/homelab && docker compose down coach-sync'` — which leaves
`/srv/coach-sync` untouched, so no data is lost.

---

## 4 · Verify (do all five)

```bash
# 1. It started, and CATCH_UP fired an immediate first run.
ssh homelab 'docker logs --tail 40 coach-sync'
#    expect: "starting — daily at 06:30 Europe/London"
#            "no successful run since the last 06:30 slot — running now (catch-up)"
#            a run: fetch ... build ... "cycle OK in Ns"
#            "sleeping N.Nh until <tomorrow> 06:30 BST"

# 2. The timestamps say BST/GMT, not UTC. If they say UTC, TZ was dropped and
#    the container should have refused to start — investigate before continuing.

# 3. Healthy, and running the tag you think it is.
ssh homelab 'docker ps --filter name=coach-sync --format "{{.Image}}  {{.Status}}"'
#    expect: ghcr.io/ignacio-montero/coach-sync:0.1.0   Up N minutes (healthy)
#    (healthy can take up to ~5 min — the healthcheck interval)

# 4. The four CSVs exist, are owned by 1000, and are non-trivial.
ssh homelab 'ls -la /srv/coach-sync/data/*.csv && wc -l /srv/coach-sync/data/*.csv'

# 5. It publishes nothing. This is the one that protects the health data.
ssh homelab 'docker port coach-sync || echo "no published ports — correct"'
ssh homelab 'sudo ss -tulnp | grep -c coach-sync || echo "not listening — correct"'
```

**Prove the alerting works before you trust the silence.** Temporarily break a
credential (or send a test message) and confirm a Telegram arrives:

```bash
ssh homelab 'docker exec coach-sync python -c "
from coach_sync import notify; print(notify.send(\"coach-sync: alert path test\"))"'
```

Then re-run the snapshot and log the change in the homelab repo:

```bash
cd ~/Development/homelab && ./scripts/snapshot.sh
# update docs/services.md (new row) and append a dated entry with rollback to docs/decisions.md
```

---

## 5 · Data, durability and retention

| Path (host) | Mount | Contents | If lost |
|---|---|---|---|
| `/srv/coach-sync/data` | `/app/data` rw | 4 CSVs + `raw/` landing zone + `state/heartbeat.json` | **Recoverable** — re-fetch and rebuild |
| `/srv/coach-sync/input` | `/app/input` **ro** | `manual.csv` — hand-measured waist | **GONE FOREVER** |
| `/srv/coach-sync/config` | `/app/config` **ro** | `campaign.toml` | Recoverable from the Mac |

### How `manual.csv` is protected — three independent layers

1. **It is not in a Docker volume.** `docker compose down -v`, `docker volume
   prune` and `docker volume rm` — the three commands that destroy volume data,
   all of which are normal things to type while debugging — have no reach into
   a host bind mount.
2. **It is mounted read-only.** The container cannot write to `/app/input` at
   all, so no bug, no partial write and no "clean rebuild" code path inside the
   app can truncate it.
3. **It is outside `data/`.** The application's own layout keeps derived,
   regenerable output in `data/` and irreplaceable input in `input/`, so a
   "wipe the data dir and re-fetch" recovery never touches it.

Back it up anyway — three layers of protection against software are zero
protection against a dead SSD:

```bash
ssh homelab 'sudo tar czf - /srv/coach-sync/input /srv/coach-sync/config' \
  > ~/Backups/coach-sync-inputs-$(date +%F).tgz
```

### Retention

Raw captures grow ~450 KB per cycle with no natural bound (~160 MB/year).
Every successful cycle keeps the **newest 14 files per data type** and deletes
older ones (`RAW_KEEP`), giving a steady state of ~6–7 MB and two weeks of
re-parseable history.

Count-based, not age-based, on purpose: `find -mtime +30` would delete the last
surviving capture of a source that has been failing for a month — exactly when
you most need it. Raise `RAW_KEEP` if you want a longer window; the constraint
is a 232 GB SSD shared with everything else, so this is cheap either way.

---

## 6 · Getting the CSVs onto the Mac

Pull-based rsync over Tailscale (ARCHITECTURE.md D-013 — chosen over a private
git repo to avoid a third custodian of body-composition data). Run **from the
Mac**; nothing needs to be installed on the box.

```bash
# at home (LAN alias) — use homelab-ts when away
rsync -av homelab:/srv/coach-sync/data/*.csv \
          ~/.claude/skills/marta-ibanez/references/data/
```

No `--delete`: the destination is the coach's skill directory and a stray flag
should not be able to empty it. Add it to a launchd job later if the manual pull
becomes tedious.

---

## 7 · Updating

Same loop as everything else on the box, scoped to one service.

```bash
# 1. change the code, bump the version, publish a NEW tag (never reuse one)
git tag v0.2.0 && git push origin v0.2.0
# 2. bump the tag in the compose file
sed -i '' 's|coach-sync:0.1.0|coach-sync:0.2.0|' \
  ~/Development/homelab/services/coach-sync/docker-compose.yml
cd ~/Development/homelab && git commit -am "chore: coach-sync 0.2.0" && git push
# 3. pull loop, scoped
ssh homelab 'cd ~/homelab && git pull && docker compose pull coach-sync && docker compose up -d coach-sync'
```

**Config- or credential-only change:** edit the server's `.env` (or the
`environment:` block) and `docker compose up -d coach-sync`. No new image.

### Is rollback safe?

**Yes, at 0.1.0 — and the reason is structural, not luck.** This service has no
database and runs no migrations. The CSVs are *derived*: every run rebuilds
them from the raw JSON, so an older image reading the same `/srv/coach-sync`
produces its own correct output. Recreating the container does not touch the
bind mounts.

```bash
sed -i '' 's|coach-sync:0.2.0|coach-sync:0.1.0|' \
  ~/Development/homelab/services/coach-sync/docker-compose.yml
cd ~/Development/homelab && git commit -am "revert: coach-sync back to 0.1.0" && git push
ssh homelab 'cd ~/homelab && git pull && docker compose up -d coach-sync'
```

⚠️ **The one thing that would change this:** a future version that **removes or
renames a CSV column**. Rolling back then meets a file the old code cannot
parse — and worse, the old code's rewrite may trip the shrink guard (exit 2) and
refuse to write at all, which is the safe failure but still an outage. If a
release changes the CSV schema, say so in its notes and treat rollback as
"revert the image **and** restore `data/` from backup".

---

## 8 · When it complains

The scheduler translates the CLI's exit codes into a message; here is the same
table for when you are reading logs directly.

| Exit | Meaning | What to do |
|---|---|---|
| `1` (fetch) | Credentials or network | Almost always `invalid_grant` — the Google refresh token died. Re-consent, update `.env`, `docker compose up -d coach-sync`. Check the consent screen is still "In production" (see OQ-5). |
| `2` (build) | **Refused to write** — new data had less in it than the file on disk | A partial fetch. The existing CSVs are untouched and still good. Re-run fetch. Only if the loss is genuine: `docker compose run --rm --entrypoint python coach-sync -m coach_sync build --allow-shrink` |
| `3` (build) | Nothing dated parsed at all | The API response shape changed. `--entrypoint python … -m coach_sync inspect` against the raw JSON and pin the parser. |
| `4` (build) | **Stale** — newest record older than 2 days | The CSVs are written but describe the past. Usually a failed fetch earlier in the chain. |
| `78` | Refused to start — misconfigured | The log names the problem: missing `campaign.toml`, missing credentials, wrong TZ, unwritable volume. The container backs off 5 min before exiting so it cannot hot-loop. |
| `124` | A step hit its wall-clock timeout and was killed | A hung HTTP call. Look for a Google/Hevy outage; the next cycle retries. |

Useful one-liners:

```bash
ssh homelab 'docker logs --tail 80 coach-sync'
ssh homelab 'docker inspect -f "{{.State.Health.Status}}" coach-sync'
ssh homelab 'cat /srv/coach-sync/data/state/heartbeat.json'   # last attempt/success/exit code
# force a run now, without waiting for 06:30 (safe: the job is idempotent)
ssh homelab 'docker exec coach-sync python -m coach_sync.scheduler --run-once'
```

> **Concept — ENTRYPOINT vs CMD.** The image's `ENTRYPOINT` is the scheduler, so
> arguments you pass are *appended* to it (`docker run … --run-once` works). To
> run something else entirely you must replace it:
> `docker compose run --rm --entrypoint python coach-sync -m coach_sync build`.
> Rule of thumb: ENTRYPOINT is "what this container **is**", CMD is "its default
> arguments".

---

## 9 · Notes for whoever reads this next

- **The image contains no personal data and no secrets.** `.dockerignore` is
  deny-by-default (allow-list), because image layers are immutable: a secret
  copied in and deleted in a later layer is still in the published image and
  recoverable with `docker history`/`docker save`.
- **`pipeline/.env.bak` on the Mac is a stale second copy of live credentials.**
  It will not be rotated when `.env` is, so it is a credential that outlives its
  own expiry policy. Delete it (it is gitignored and was never committed —
  verify with `git log --all --full-history -- .env.bak`).
- **The 7-day refresh-token expiry (OQ-5) is the likeliest cause of any
  unexplained outage.** It is now loud rather than silent, which was the point.
- The container has no shell entrypoint, runs as uid 1000, drops all
  capabilities and mounts its root filesystem read-only. If a future version
  needs to write somewhere new, it will fail with `EROFS` — add a tmpfs or a
  mount rather than removing `read_only: true`.
