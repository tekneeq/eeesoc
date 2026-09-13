# eeesoc

Soccer **Matches + Similar** dashboard with a dark Revenant look.

Freeze an in-play snapshot (cut minute, goal times, shots/SOT) and rank last-season lookalikes. History is cached under `~/.eeesoc/cache` (or `./data/cache` on EC2).

The **Live** tab shows in-play matches as Revenant-style **match chiclets**, grouped by league.
Tap a chiclet for a live pitch graphic: last 15′ of the ball (home attacking right), fading older
touches, a heat map of where it has been, pass trails, and shot markers (ESPN play coordinates).
Each chiclet also has a rolling 15′ pressure bar (who is in the other side's final third right now),
a pressure graph of that same window at every minute, possession and duel graphs with three lines
(home, away, and 50%) over that same last-15′ window at every minute, and a territory heat of
where the ball has actually been in those last 15′ — so a first-half siege that flipped after
the break does not collapse to "even".

Under the pitch graphic sits the **formation board** (`/api/live/lineups`, from the ESPN match summary):
both teams' formations with everyone currently on the pitch in his slot. Each player carries a **power**
number — a FIFA-flavoured 40–99 match-performance score computed from ESPN's live per-player stats
(goals +12, assists +8, shots on target +4, other shots +1.5, fouls drawn +1; fouls −1.5, offsides −1,
yellow −4, red −12; keepers get +3 a save, −3 a goal conceded; 65 = quiet, tidy game) — and a
**freshness bar** that drains with minutes on the pitch (ESPN publishes no distance-run/GPS data, so
minutes are the honest proxy; substitutes come on full). Substitutes show **⇄ and the minute** they came
on, inherit the formation slot of the player they replaced, and the hover line names who came off.
Lineups appear about an hour before kickoff; goals and cards are marked on the player dot.

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
**🥅 Last 5** is those same five games as a strip: who they faced, then the **1H** and **2H**
score from that club's view (scored–allowed). Hover a game for the full-time score. Games
without a half-time score show dashes for the halves.
**🔮 Potential** is underlying strength with finishing luck stripped out: the geometric mean of the club's
chance-creation index (xG created vs the league) and its defence power, so 100 = a league-typical side on
chance quality both ways. The same index on actual goals is shown as *results*; the tag is *upside* when
results lag the underlying numbers by 5+ (they should improve), *overachieving* when they run ahead, else
*steady*.
**🧤 Clean sheets** counts games without conceding from the archived final scores: the season total (ranked
within the league, ties to the club that needed fewer games), how many in the last five, and the share of
games kept clean, coloured *tight* / *porous* against the league's share.
**🥅 Goals per half** follows the clock, as three rows: *goals* (both sides combined), *scored* (the club's
own) and *allowed*. Before the break (and before kickoff) each side shows how often that club's first halves
this season produced 0 / 1 / 2 / 3+ of each. From half time on, the rows become *2H goals* / *2H scored* /
*2H allowed*, and each side is tagged with **its own** score at the break: a 2–0 home lead looks up the
home club's games when they led 2–0 and the away club's games when they trailed 0–2 — never the leader's
sample on the trailer. How often those second halves went on to produce 0 / 1 / 2 / 3+ goals in total,
scored and allowed. The most common bucket is
highlighted (green total, orange scored, blue allowed) and the small number is how many of the club's own
games the split comes from. Fewer than 3 matching games is flagged as a thin sample (dashed cells; the
tooltip adds the league-wide split for the same half-time score for context); only when the club has no
matching game at all does the row show the league-wide split instead (amber *lg* count, oriented so the away
side's *scored* is the league's home-allowed).

The **0-0 HT** tab archives every full-time game the dashboard sees (plus a background ESPN backfill,
`EEESOC_HT_BACKFILL_DAYS`, default 60, `0` to disable) under `<cache>/halftime/`. **Archive** lists the games
that were 0-0 at the break by league, each cut at 45′ with the same event strip and xG chart, plus an
80% band (p10–p90) profile of shots / on-target / corners / xG and what happened after the restart.
**Similar live** scores today's goalless first halves against that archive minute for minute and shows
the closest lookalikes with a match % and how they finished.

### No-more-goals-this-half signal

Every live chiclet leads with a **🚫 no goal to HT / FT** row: the probability that nobody scores again
before the break (or the final whistle), the decimal odds you need to break even (`1 / p`), and the trigger
state. The model is fitted from the finished-match archive and refits as it grows (`eeesoc.nogoal`):

- **Clock.** The empirical goal hazard by minute for each half (the goals ESPN files at 45′ / 90′ are the
  stoppage-time goals; they carry ~9% of first-half and ~15% of second-half goals) gives the goals still
  expected this half, `λ`; `P(no goal) = exp(−λ)`.
- **League.** `λ` is scaled by the league's goals per game against the global rate, shrunk toward par over
  20 games — Liga Profesional runs ~0.77, the Bundesliga ~1.18.
- **Score state.** A fitted multiplier below 1 while the game is still 0–0, above 1 once it is not.
- **Passes into the penalty box — the edge.** Working backwards from every archived half that finished
  0–0: they had the same passes, final-third passes, corners, fouls and cards as the halves with a goal,
  but fewer completed passes *into the box* (6.0 vs 7.0 per half). The dashboard keeps every box entry
  (minute and side) from the ESPN play-by-play — for live games, for every new archive record, and by a
  one-off backfill for records that predate it (`EEESOC_BOX_BACKFILL=0` disables). Two factors are fitted
  from them, both as `rate × rel^β` on top of clock × league × score so nothing is counted twice:
  - *box habit* — how often these two clubs' halves usually reach the box (for and against, shrunk toward
    the league over 4 games; leave-one-out in the backtest). Quiet fixtures that were 0–0 at 15′ held to HT
    49% of the time, busy ones 35%.
  - *entries so far* — this half's entries against the archive pace for the minute. At 25′, games with 0–3
    entries held 61–65%, games with 6+ held 42%.
  Within any given minute the clock+league model cannot tell one 0–0 from another (AUC 0.50); with the
  box factors it can (0.55–0.57), and picking the most confident third of goalless games at 20′ lifted
  the held rate from 49% to 57% on forward-validated data.
- **League by league.** Leagues differ a lot (Liga Profesional 2.1 goals a game and 0–0 at the break 44%
  of the time; Eredivisie 4.0 and 20%), and the league factor carries that. The box edge was re-tested per
  league: measuring quiet/busy against each league's own pace, fitting the exponents per league, and a
  league goals-per-entry conversion were all worse out of sample — a busy MLS fixture really is more
  dangerous than a quiet Liga ARG one, and 20–140 games per league is too few to fit separately. The same
  pooled model runs everywhere; the Signals tab's league table shows, per league, the base rates, what the
  trigger would have done at the current threshold, and whether the model separates that league's own
  goalless games (held rate of its more-confident half vs its less-confident half). `EEESOC_NOGOAL_LEAGUES`
  (comma-separated slugs) restricts firing to the leagues you trust.
- **Not used, on purpose.** Shots / xG so far this half, the last ten minutes' activity and club
  attack/defence strength were all tried and did not lower the log loss, so they are shown as context only.
  The classic "0–0 with under four shots at 15′" read is measured directly in the backtest: in the current
  archive those halves stayed goalless 46% of the time versus 44% for every half that was 0–0 at 15′.
  Shots are a noisy by-product of box entries; once entries are in they add nothing out of sample.

The **Bets** tab is today’s slip: every no-more-goals alarm that has fired, grouped
by league, with a won / lost / still-live count at the top and on each league. **Won**
means the half stayed goalless after the alarm (held); **lost** means a goal landed
(busted). Live bets are still waiting on half-time or full-time.

The **Signals** tab replays the archive minute by minute: what 0–0 halves look like against the rest and the
held rates by box habit and entries so far, calibration (does 70% mean 70%?), log loss against a
time-only baseline, and the **trigger policy** table — for each confidence threshold, how many games would
have fired inside the betting window, at what average minute and how often the half really stayed goalless
— plus the live record. Fires are stacked as a **daily bar** (green held, red busted); click a day to
open those game chiclets with the same strip / xG / territory as the Live tab. The table under the
chart is the same log, newest first.

The monitor runs as a background thread inside the dashboard, fires **once per game per half** when
`P(no goal)` clears the threshold inside the window, tracks the signal to **held** / **busted** (with the
goal minute and scorer) / **void**, and posts each trigger and outcome to Discord:

```bash
EEESOC_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...   # unset = evaluate only, no posts
# EEESOC_NOGOAL_THRESHOLD=0.65     # P(no goal) needed to fire; pick from the Signals policy table
# EEESOC_NOGOAL_WINDOW_1H=10-35    # minutes the 1st-half trigger may fire
# EEESOC_NOGOAL_WINDOW_2H=50-78    # same for the 2nd half
# EEESOC_NOGOAL_LEAGUES=arg.1,eng.2 # only fire in these leagues (unset = all)
# EEESOC_NOGOAL_POLL_S=20          # seconds between polls
# EEESOC_NOGOAL_MONITOR=0          # disable the background thread
```

Signals persist in `<cache>/nogoal-signals.json`; `/api/nogoal/live`, `/api/nogoal/backtest` and
`/api/nogoal/signals` expose the same data.

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
DISCORD_CHANNEL_ID=...         # required for the ready greeting, live alerts, and 9:30 AM ET slate
# Optional: timezone for "today" kickoffs (default America/New_York)
# EEESOC_TZ=America/New_York
# Optional: live-alert poll interval in seconds (default 20)
# EEESOC_DISCORD_POLL_S=20
# Optional: morning slate time (default 9:30 America/New_York)
# EEESOC_DIGEST_HOUR=9
# EEESOC_DIGEST_MINUTE=30
```

`./restart.sh` loads `.env` into the container. `./deploy.sh` starts the bot afterward. If `DISCORD_BOT_TOKEN` is unset, deploy skips the bot and continues.

```bash
./restart-discord-bot.sh           # start / restart
./restart-discord-bot.sh --status
./restart-discord-bot.sh --logs
./restart-discord-bot.sh --stop
```

With `DISCORD_CHANNEL_ID` set, the bot also **posts on its own**:

- **9:30 AM ET** every day — today's scheduled matches in MLS, Premier League, Championship, Ligue 1, La Liga, Bundesliga, Serie A, Champions League, Europa League, and Conference League (kickoff times in ET). If the bot starts later the same morning, it still posts once. `!soc today` reprints that slate.
- Kickoff (`pre` → `in`) and every goal, with the live score path (`0-0 → 1-0 → …`) and each club’s Similar “from here” branches (what usually happens next from that scoreline at that minute).

The bot **also** responds to `!soc …` (or `!eee …`) messages:

```
!soc help
!soc live [league]         # in-play scoreboard
!soc upcoming [league]     # today's remaining kickoffs
!soc finished [league]     # today's / yesterday's full-time
!soc today                 # today's slate (same as the 9:30 AM ET post)
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
