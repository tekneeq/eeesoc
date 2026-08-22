# eeesoc

Soccer **Matches + Similar** dashboard with a dark Revenant look.

Freeze an in-play snapshot (cut minute, goal times, shots/SOT) and rank last-season lookalikes. History is cached under `~/.eeesoc/cache`.

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

## Notes

- Season data is loaded from [football-data.co.uk](https://www.football-data.co.uk/) EPL CSVs.
- Minute-level shot ramps are reconstructed from full-time box scores (deterministic per match).
- The Everton 53′ fixture is injected as an explicit demo snapshot for Similar.
