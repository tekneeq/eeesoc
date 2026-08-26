"""CLI entrypoint: warm cache and serve the dashboard."""

from __future__ import annotations

import argparse
import json
import sys

from eeesoc import __version__
from eeesoc.cache import cache_root
from eeesoc.data import list_cached_seasons, load_season, parse_warm_spec, previous_season_label, warm
from eeesoc.models import MatchSnapshot
from eeesoc.similar import find_similar


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eeesoc", description="Matches + Similar soccer dashboard")
    p.add_argument("--version", action="version", version=f"eeesoc {__version__}")
    p.add_argument("--dashboard", action="store_true", help="Serve the web dashboard")
    p.add_argument("--port", type=int, default=8081, help="Dashboard port (default 8081)")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Dashboard bind address (default 127.0.0.1; use 0.0.0.0 in Docker)",
    )
    p.add_argument(
        "--warm",
        metavar="SPEC",
        help="Warm cache for a season, e.g. EPL:2025 (also pulls previous season)",
    )
    p.add_argument("--season", default=None, help="Season label for queries (default: last warmed)")
    p.add_argument("--similar", metavar="MATCH_ID", help="Print similar lookalikes for a match")
    p.add_argument("--minute", type=int, default=53, help="Cut minute for --similar")
    p.add_argument("--json", action="store_true", help="JSON output for CLI queries")
    return p


def _default_season() -> str | None:
    seasons = list_cached_seasons()
    return seasons[-1] if seasons else None


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.warm:
        counts = warm(args.warm)
        print(f"Cache root: {cache_root()}")
        for label, n in counts.items():
            print(f"Warmed {label}: {n} matches")

    if args.similar:
        season = args.season or _default_season()
        if not season:
            print("No cached season; run with --warm EPL:2025 first", file=sys.stderr)
            raise SystemExit(2)
        matches = {m.match_id: m for m in load_season(season)}
        # Also search previous season corpus
        corpus = list(load_season(season))
        try:
            prev = previous_season_label(season)
            corpus.extend(load_season(prev))
        except ValueError:
            pass
        match = matches.get(args.similar)
        if match is None:
            # allow partial / home name lookup
            lowered = args.similar.lower()
            for m in matches.values():
                if lowered in m.match_id.lower() or lowered == m.home.lower():
                    match = m
                    break
        if match is None:
            print(f"Match not found: {args.similar}", file=sys.stderr)
            raise SystemExit(1)
        snap = match.snapshot_at(args.minute)
        hits = find_similar(snap, corpus, exclude_ids={match.match_id})
        if args.json:
            print(
                json.dumps(
                    {"query": snap.to_dict(), "label": snap.label(), "hits": [h.to_dict() for h in hits]},
                    indent=2,
                )
            )
        else:
            print(f"{match.home} vs {match.away} @ {args.minute}' — {snap.label()}")
            for h in hits:
                print(
                    f"  {h.score:0.3f}  {h.match.date}  {h.match.home} vs {h.match.away}  "
                    f"[{h.snapshot.label()}]  FT {h.match.home_goals_ft}-{h.match.away_goals_ft}"
                )

    if args.dashboard:
        season = args.season
        if not season and args.warm:
            season, _ = parse_warm_spec(args.warm)
        season = season or _default_season()
        if not season:
            print("No cached season; pass --warm EPL:2025", file=sys.stderr)
            raise SystemExit(2)
        from eeesoc.dashboard import serve

        print(f"Serving dashboard on http://{args.host}:{args.port}  season={season}")
        print(f"Cache: {cache_root()}")
        serve(port=args.port, season=season, host=args.host)

    if not args.warm and not args.dashboard and not args.similar:
        build_parser().print_help()
        raise SystemExit(0)


# Re-export for typing convenience
Snapshot = MatchSnapshot
