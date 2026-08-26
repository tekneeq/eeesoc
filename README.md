# eeesoc

Soccer **Matches + Similar** dashboard with a dark Revenant look.

Freeze an in-play snapshot (cut minute, goal times, shots/SOT) and rank last-season lookalikes. History is cached under `~/.eeesoc/cache` (or `./data/cache` on EC2).

## Quick start

```bash
uv sync --extra dev
uv run eeesoc --dashboard --port 8081 --warm EPL:2025
```

Open http://127.0.0.1:8081 — use **Everton 53′** for the demo preset (`42'/53' · 12/4 vs 6/1`).

## CLI

```bash
# Warm current + previous season into ~/.eeesoc/cache
uv run eeesoc --warm EPL:2025

# Similar lookalikes for a match at minute 53
uv run eeesoc --similar Everton --minute 53

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
chmod +x deploy.sh restart.sh scripts/*.sh

# If `docker` is not on PATH (fresh Amazon Linux):
./scripts/install-docker-amazon-linux.sh
# re-login (or: newgrp docker), then:
./deploy.sh
```

Optional nginx reverse proxy (julia already owns `:80`, so this sample listens on `:8080`):

```bash
sudo cp scripts/nginx-eeesoc-dashboard.conf /etc/nginx/conf.d/eeesoc-dashboard.conf
sudo nginx -t && sudo systemctl reload nginx
```

Open security group inbound for `8081` (direct) and/or `8080` (nginx).

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
- The Everton 53′ fixture is injected as an explicit demo snapshot for Similar.
- Docker binds `0.0.0.0:8081` and mounts `./data/cache` so warm data survives rebuilds.
