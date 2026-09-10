# eeesoc

Soccer **Matches + Similar** dashboard with a dark Revenant look.

Freeze an in-play snapshot (cut minute, goal times, shots/SOT) and rank last-season lookalikes. History is cached under `~/.eeesoc/cache` (or `./data/cache` on EC2).

The **Live** tab shows in-play matches as Revenant-style **match chiclets**, grouped by league.
Tap a chiclet for a live pitch graphic: ball position, pass trails, and shot markers (ESPN play coordinates).

Every chiclet also carries each club's **⚡ clinical power** (`/api/clinical`): goals per 100 xG over the
archived finished games this season, shrunk toward league par for thin samples. 100 means the club scores
exactly what its chances are worth; above 100 is tagged *clinical*, below *wasteful*; the rank is its place
in the league's power table (clubs with 2+ archived games). Leagues without an ESPN xG feed use
shots-on-target conversion against the league's own rate on the same 100 = par scale.
**🎯 Offence power** is the superset of clinical — everything that leads to goals: the club's per-game xG
created, shots, shots on target, corners (a proxy for sustained pressure) and goals are each divided by the
league's rate, shrunk toward par, and blended (xG 40%, shots 20%, SOT 15%, corners 10%, goals 15%; without
an xG feed: shots 35%, SOT 25%, corners 15%, goals 25%). 100 = a league-typical attack, above 100 is
*potent*, below *blunt*, ranked within the league, shown with goals scored per game.
**🛡 Defence power** is the mirror: league-average xG allowed per game over the club's own (shrunk toward
par), so 100 = allows the league's typical chances, above 100 is *solid*, below *leaky*, ranked the same way
(shots on target allowed where there is no xG feed), shown with goals allowed per game.
**📈 Momentum** is recent form: points per game over the last five results, weighted toward the newest and
shrunk toward the league's points per game; 100 = par form, above is *rising*, below *fading*, with the
W/D/L letters (oldest → newest) alongside.
**🔮 Potential** is underlying strength with finishing luck stripped out: the geometric mean of the club's
chance-creation index (xG created vs the league) and its defence power, so 100 = a league-typical side on
chance quality both ways. The same index on actual goals is shown as *results*; the tag is *upside* when
results lag the underlying numbers by 5+ (they should improve), *overachieving* when they run ahead, else
*steady*.
**🧤 Clean sheets** counts games without conceding from the archived final scores: the season total (ranked
within the league, ties to the club that needed fewer games), how many in the last five, and the share of
games kept clean, coloured *tight* / *porous* against the league's share.
**🥅 Goals per half** follows the clock. Before the break (and before kickoff) each side shows how often that
club's first halves this season produced 0 / 1 / 2 / 3+ total goals. From half time on, the row becomes
*2H after h–a*: given this game's half-time score (from each club's own side, so 0–1 = trailing), how often
that club's second halves went on to produce 0 / 1 / 2 / 3+ goals. The most common bucket is highlighted and
the small number is the sample; clubs with fewer than 3 matching games fall back to the league-wide split
for the same situation (marked *lg*).

The **0-0 HT** tab archives every full-time game the dashboard sees (plus a background ESPN backfill,
`EEESOC_HT_BACKFILL_DAYS`, default 60, `0` to disable) under `<cache>/halftime/`. **Archive** lists the games
that were 0-0 at the break by league, each cut at 45′ with the same event strip and xG chart, plus an
80% band (p10–p90) profile of shots / on-target / corners / xG and what happened after the restart.
**Similar live** scores today's goalless first halves against that archive minute for minute and shows
the closest lookalikes with a match % and how they finished.

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
DISCORD_CHANNEL_ID=...         # required for the ready greeting and live alerts
# Optional: timezone for "today" kickoffs (default America/New_York)
# EEESOC_TZ=America/New_York
# Optional: live-alert poll interval in seconds (default 20)
# EEESOC_DISCORD_POLL_S=20
```

`./restart.sh` loads `.env` into the container. `./deploy.sh` starts the bot afterward. If `DISCORD_BOT_TOKEN` is unset, deploy skips the bot and continues.

```bash
./restart-discord-bot.sh           # start / restart
./restart-discord-bot.sh --status
./restart-discord-bot.sh --logs
./restart-discord-bot.sh --stop
```

With `DISCORD_CHANNEL_ID` set, the bot also **posts on its own**: kickoff (`pre` → `in`) and every goal, with the live score path (`0-0 → 1-0 → …`) and each club’s Similar “from here” branches (what usually happens next from that scoreline at that minute).

The bot **also** responds to `!soc …` (or `!eee …`) messages:

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
