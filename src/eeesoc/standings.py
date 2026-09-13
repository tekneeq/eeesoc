"""Official league tables from ESPN standings (position, W-D-L, points)."""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from eeesoc.live import LEAGUES, _fetch_json

STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/soccer/{league}/standings"

_CACHE_TTL_S = 5 * 60
_cache: tuple[float, dict[str, Any]] | None = None


def _stat_int(stats: dict[str, dict[str, Any]], *names: str) -> int | None:
    for name in names:
        row = stats.get(name)
        if not row:
            continue
        raw = row.get("value")
        if raw is None or raw == "":
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _stat_display(stats: dict[str, dict[str, Any]], *names: str) -> str:
    for name in names:
        row = stats.get(name)
        if row and row.get("display"):
            return str(row["display"])
    return ""


def _entry_stats(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for stat in entry.get("stats") or []:
        name = str(stat.get("name") or "").lower()
        if not name:
            continue
        out[name] = {"value": stat.get("value"), "display": stat.get("displayValue")}
    return out


def parse_standings(payload: dict[str, Any], league_slug: str, chiclet: str) -> dict[str, Any]:
    """Flatten ESPN standings children (conferences / groups) into one team map."""
    children = [c for c in (payload.get("children") or []) if isinstance(c, dict)]
    multi = len(children) > 1
    teams: dict[str, dict[str, Any]] = {}
    season = str(payload.get("name") or chiclet)
    for child in children:
        group = str(child.get("name") or "").strip()
        entries = ((child.get("standings") or {}).get("entries") or [])
        group_n = len(entries)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            team = entry.get("team") or {}
            tid = str(team.get("id") or "")
            if not tid:
                continue
            stats = _entry_stats(entry)
            wins = _stat_int(stats, "wins") or 0
            draws = _stat_int(stats, "ties", "draws") or 0
            losses = _stat_int(stats, "losses") or 0
            record = _stat_display(stats, "overall", "total") or f"{wins}-{draws}-{losses}"
            note = entry.get("note") or {}
            teams[tid] = {
                "team_id": tid,
                "team": str(team.get("displayName") or team.get("shortDisplayName") or ""),
                "short": str(team.get("shortDisplayName") or team.get("abbreviation") or ""),
                "rank": _stat_int(stats, "rank"),
                "played": _stat_int(stats, "gamesplayed") or 0,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "gf": _stat_int(stats, "pointsfor") or 0,
                "ga": _stat_int(stats, "pointsagainst") or 0,
                "gd": _stat_int(stats, "pointdifferential") or 0,
                "points": _stat_int(stats, "points") or 0,
                "record": record,
                "group": group if multi else "",
                "group_n": group_n,
                "note": str(note.get("description") or "") if isinstance(note, dict) else "",
            }
    return {
        "slug": league_slug,
        "label": chiclet,
        "season": season,
        "teams_n": max((t.get("group_n") or 0) for t in teams.values()) if teams else 0,
        "teams": teams,
    }


def fetch_league_standings(
    league_slug: str,
    chiclet: str,
    *,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fetch = fetcher or _fetch_json
    quoted = urllib.parse.quote(league_slug, safe=".")
    payload = fetch(STANDINGS_URL.format(league=quoted))
    return parse_standings(payload, league_slug, chiclet)


def standings_board(
    *,
    leagues: list[tuple[str, str]] | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """All configured leagues' tables, cached a few minutes."""
    global _cache
    if use_cache and _cache is not None:
        ts, payload = _cache
        if time.time() - ts < _CACHE_TTL_S:
            return payload

    league_list = leagues or LEAGUES
    tables: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(8, len(league_list) or 1)) as pool:
        futures = {
            pool.submit(fetch_league_standings, slug, label, fetcher=fetcher): (slug, label)
            for slug, label in league_list
        }
        for fut in as_completed(futures):
            slug, label = futures[fut]
            try:
                tables[slug] = fut.result()
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError) as exc:
                errors.append(f"{label}: {exc}")

    payload = {
        "fetched_at": time.time(),
        "leagues": tables,
        "errors": errors,
    }
    if use_cache:
        _cache = (time.time(), payload)
    return payload


def clear_standings_cache() -> None:
    global _cache
    _cache = None


def lookup_team(board: dict[str, Any], league_slug: str, *, team_id: str = "", name: str = "") -> dict[str, Any] | None:
    league = (board.get("leagues") or {}).get(league_slug)
    if not league:
        return None
    teams = league.get("teams") or {}
    tid = str(team_id or "")
    if tid and tid in teams:
        return {**teams[tid], "_league": league}
    key = " ".join(str(name or "").lower().split())
    if not key:
        return None
    for row in teams.values():
        if " ".join(str(row.get("team") or "").lower().split()) == key:
            return {**row, "_league": league}
    return None
