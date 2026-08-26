"""Live match scoreboard via ESPN CDN (no API key)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any, Callable

USER_AGENT = "eeesoc/0.1 (+https://github.com/tekneeq/eeesoc)"
CDN_SCOREBOARD = "https://cdn.espn.com/core/soccer/scoreboard?xhr=1&league={league}"

# Major leagues shown as chiclets (slug → short label).
LEAGUES: list[tuple[str, str]] = [
    ("eng.1", "EPL"),
    ("esp.1", "La Liga"),
    ("ita.1", "Serie A"),
    ("ger.1", "Bundesliga"),
    ("fra.1", "Ligue 1"),
    ("uefa.champions", "UCL"),
    ("uefa.europa", "UEL"),
    ("eng.2", "Championship"),
    ("usa.1", "MLS"),
    ("mex.1", "Liga MX"),
    ("ned.1", "Eredivisie"),
    ("por.1", "Primeira"),
    ("sco.1", "SPL"),
    ("bra.1", "Brasileirão"),
    ("arg.1", "Liga ARG"),
]

_CACHE_TTL_S = 20.0
_cache_lock_payload: tuple[float, dict[str, Any]] | None = None


@dataclass(frozen=True)
class LiveMatch:
    event_id: str
    league_slug: str
    league_name: str
    league_chiclet: str
    home: str
    away: str
    home_score: int
    away_score: int
    state: str  # pre | in | post
    clock: str
    detail: str
    start: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fetch_json(url: str, timeout: float = 12.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_scoreboard(payload: dict[str, Any], league_slug: str, chiclet: str) -> list[LiveMatch]:
    content = payload.get("content") or {}
    sb = content.get("sbData") or {}
    leagues = sb.get("leagues") or []
    league_name = chiclet
    if leagues and isinstance(leagues[0], dict):
        league_name = str(leagues[0].get("name") or chiclet)

    out: list[LiveMatch] = []
    for event in sb.get("events") or []:
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status = (comp.get("status") or event.get("status") or {})
        stype = status.get("type") or {}
        state = str(stype.get("state") or "pre")
        clock = str(status.get("displayClock") or stype.get("shortDetail") or "")
        detail = str(stype.get("detail") or stype.get("shortDetail") or "")

        home = away = "?"
        home_score = away_score = 0
        for team in comp.get("competitors") or []:
            name = (
                (team.get("team") or {}).get("displayName")
                or (team.get("team") or {}).get("shortDisplayName")
                or "?"
            )
            score = _safe_int(team.get("score"), 0)
            if team.get("homeAway") == "home":
                home, home_score = name, score
            elif team.get("homeAway") == "away":
                away, away_score = name, score

        out.append(
            LiveMatch(
                event_id=str(event.get("id") or comp.get("id") or f"{league_slug}:{home}:{away}"),
                league_slug=league_slug,
                league_name=league_name,
                league_chiclet=chiclet,
                home=home,
                away=away,
                home_score=home_score,
                away_score=away_score,
                state=state,
                clock=clock,
                detail=detail,
                start=str(event.get("date") or ""),
            )
        )
    return out


def fetch_league(
    league_slug: str,
    chiclet: str,
    *,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> list[LiveMatch]:
    fetch = fetcher or _fetch_json
    url = CDN_SCOREBOARD.format(league=urllib.parse.quote(league_slug, safe="."))
    try:
        payload = fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    return parse_scoreboard(payload, league_slug, chiclet)


def fetch_live_board(
    *,
    live_only: bool = True,
    leagues: list[tuple[str, str]] | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Fan out across leagues and return matches grouped for the Live tab.

    Cached briefly so the dashboard can poll without hammering ESPN.
    """
    global _cache_lock_payload

    cache_key_live = live_only
    if use_cache and _cache_lock_payload is not None:
        ts, payload = _cache_lock_payload
        if time.time() - ts < _CACHE_TTL_S and payload.get("live_only") is cache_key_live:
            return payload

    league_list = leagues or LEAGUES
    matches: list[LiveMatch] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(8, len(league_list) or 1)) as pool:
        futures = {
            pool.submit(fetch_league, slug, label, fetcher=fetcher): (slug, label)
            for slug, label in league_list
        }
        for fut in as_completed(futures):
            slug, label = futures[fut]
            try:
                matches.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 — surface per-league failure
                errors.append(f"{label}: {exc}")

    if live_only:
        matches = [m for m in matches if m.state == "in"]

    # Stable order: league chiclet order, then kickoff, then home.
    order = {slug: i for i, (slug, _) in enumerate(league_list)}
    matches.sort(key=lambda m: (order.get(m.league_slug, 999), m.start, m.home))

    groups: dict[str, dict[str, Any]] = {}
    for m in matches:
        bucket = groups.setdefault(
            m.league_slug,
            {
                "slug": m.league_slug,
                "name": m.league_name,
                "chiclet": m.league_chiclet,
                "matches": [],
            },
        )
        bucket["matches"].append(m.to_dict())

    grouped = sorted(groups.values(), key=lambda g: order.get(g["slug"], 999))

    chiclet_meta = [
        {
            "slug": slug,
            "label": label,
            "live_count": sum(1 for m in matches if m.league_slug == slug and m.state == "in"),
            "count": sum(1 for m in matches if m.league_slug == slug),
        }
        for slug, label in league_list
    ]

    payload = {
        "live_only": live_only,
        "fetched_at": time.time(),
        "total": len(matches),
        "live_total": sum(1 for m in matches if m.state == "in"),
        "chiclets": chiclet_meta,
        "leagues": grouped,
        "errors": errors,
    }
    if use_cache:
        _cache_lock_payload = (time.time(), payload)
    return payload


def clear_live_cache() -> None:
    global _cache_lock_payload
    _cache_lock_payload = None
