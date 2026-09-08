# eeesoc

Soccer **Matches + Similar** dashboard with a dark Revenant look.

Freeze an in-play snapshot (cut minute, goal times, shots/SOT) and rank last-season lookalikes. History is cached under `~/.eeesoc/cache` (or `./data/cache` on EC2).

The **Live** tab shows in-play matches as Revenant-style **match chiclets**, grouped by league.
Tap a chiclet for a live pitch graphic: ball position, pass trails, and shot markers (ESPN play coordinates).

## Quick start

```bash
uv sync --extra dev
uv run eeesoc --dashboard --port 8081 --warm EPL:2025
```

Open http://127.0.0.1:8081 — the **Matches** tab lists today's scheduled fixtures first.

## CLI

```bash
# Warm current + previous season into ~/.eeesoc/cache
uv run eeesoc --warm EPL:2025

# Similar lookalikes for a match at minute 53
uv run eeesoc --similar Everton --minute 53

# Discord !soc bot (needs DISCORD_BOT_TOKEN in .env)
uv run eeesoc --discord

uv run pytest
```

## Deploy on EC2 (same pattern as `tekneeq/julia`)

Flow on every push/merge to `main`:

1. GitHub Actions workflow `.github/workflows/deploy-ec2.yml` SSHes into the box
2. `git pull --ff-only origin main`
3. `./deploy.sh` → `./restart.sh` (docker rebuild + `docker run --restart unless-stopped`)
4. Container entrypoint warms cache (no-op when already warm) and serves `:8081`

### One-time bootstrap on the EC2 host

```bash
# Docker + git (same box as julia is fine — install Docker if missing)
git clone https://github.com/tekneeq/eeesoc.git ~/eeesoc
cd ~/eeesoc
chmod +x deploy.sh restart.sh restart-discord-bot.sh scripts/*.sh

# If `docker` is not on PATH (fresh Amazon Linux):
./scripts/install-docker-amazon-linux.sh
# re-login (or: newgrp docker), then:
./deploy.sh
```

Optional nginx reverse proxy (`eeesoc.com` / `www.eeesoc.com` `:80`/`:443` → app `:8081`):

```bash
cd ~/eeesoc
./scripts/install-nginx-amazon-linux.sh          # HTTP + ACME webroot
# DNS A records for eeesoc.com (+ www if you want it) → this instance; SG: TCP 80 and 443
CERTBOT_EMAIL=you@example.com ./scripts/install-https-letsencrypt.sh
# Apex only (if www has no DNS yet):
# CERTBOT_DOMAINS="eeesoc.com" CERTBOT_EMAIL=you@example.com ./scripts/install-https-letsencrypt.sh
```

Then browse `https://eeesoc.com/`. HTTP redirects to HTTPS. Domains without DNS are skipped automatically; re-run after adding `www` to expand the cert. Certs auto-renew via certbot’s timer.

### Discord `!soc` bot (optional)

Same lifecycle as [tekneeq/julia](https://github.com/tekneeq/julia)’s `!lia` bot: a Discord application talks to the dashboard container, started detached after deploy.

Create a **new** Discord application at https://discord.com/developers/applications (do not reuse another bot's token), then:

1. **Bot → Privileged Gateway Intents** → enable **Message Content Intent** (required)
2. Invite the bot (OAuth2 → URL Generator: scope `bot`, Send Messages / Read History)
3. Add to `.env` on the eeesoc EC2 host:

```bash
DISCORD_BOT_TOKEN=...          # required to start the bot
DISCORD_CHANNEL_ID=...         # optional — channel for the ready greeting
# Optional: timezone for "today" kickoffs (default America/New_York)
# EEESOC_TZ=America/New_York
```

`./restart.sh` loads `.env` into the container. `./deploy.sh` starts the bot afterward. If `DISCORD_BOT_TOKEN` is unset, deploy skips the bot and continues.

```bash
./restart-discord-bot.sh           # start / restart
./restart-discord-bot.sh --status
./restart-discord-bot.sh --logs
./restart-discord-bot.sh --stop
```

The bot **only** responds to `!soc …` (or `!eee …`) messages:

```
!soc help
!soc live [league]         # in-play scoreboard
!soc upcoming [league]     # today's remaining kickoffs
!soc finished [league]     # today's / yesterday's full-time
!soc winprob               # EPL picks + 30-day record
!soc fixture HOME AWAY     # WinProb detail
!soc similar TEAM [minute] # historical lookalikes (needs warmed cache)
```

### Auto-deploy on push

Pushes to `main` trigger `.github/workflows/deploy-ec2.yml`, which SSHes in and runs `./deploy.sh`.

One-time GitHub setup (repo → **Settings → Secrets and variables → Actions**):

| Secret | Example | Notes |
| --- | --- | --- |
| `EC2_HOST` | `54.91.65.71` | Same host as julia is fine |
| `EC2_USER` | `ec2-user` | |
| `EC2_SSH_PRIVATE_KEY` | full `.pem` contents | Include `BEGIN`/`END` lines |
| `EC2_SSH_PORT` | `22` | Optional |
| `EC2_APP_DIR` | `/home/ec2-user/eeesoc` | Optional; **must differ** from julia’s `~/julia` |

Manual redeploy / diagnostics: Actions → **Deploy to EC2** / **EC2 status** → Run workflow.

Local-on-box redeploy anytime:

```bash
cd ~/eeesoc && ./deploy.sh
```

## Notes

- Season data is loaded from [football-data.co.uk](https://www.football-data.co.uk/) EPL CSVs.
- Minute-level shot ramps are reconstructed from full-time box scores (deterministic per match).
- An Everton 53′ fixture is still injected into the season cache for CLI `--similar` demos; it is not shown in the dashboard header.
- Docker binds `0.0.0.0:8081` and mounts `./data/cache` so warm data survives rebuilds.
