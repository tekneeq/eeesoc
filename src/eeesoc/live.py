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


# —— Live pitch tracking (passes / shots / ball) ——

PLAYS_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league}"
    "/events/{event_id}/competitions/{event_id}/plays?limit={limit}&page={page}"
)

SHOT_TYPES = {
    "shot",
    "shot-on-target",
    "shot-off-target",
    "shot-blocked",
    "goal",
    "penalty-goal",
    "miss",
    "woodwork",
}
PASS_TYPES = {"pass", "cross", "through-ball", "blocked-pass"}
BALL_TYPES = SHOT_TYPES | PASS_TYPES | {
    "ball-touch",
    "tackle",
    "clear",
    "take-on",
    "interception",
    "throw-in",
    "foul",
    "save",
    "claim",
    "aerial",
    "dispossessed",
    "free-kick",
    "corner-awarded",
    "goal-kick",
}

_track_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_TRACK_TTL_S = 8.0


def _play_type(play: dict[str, Any]) -> str:
    t = play.get("type") or {}
    return str(t.get("type") or t.get("text") or "").lower()


def _clock_label(play: dict[str, Any]) -> str:
    clock = play.get("clock") or {}
    if isinstance(clock, dict) and clock.get("displayValue"):
        return str(clock["displayValue"])
    return ""


def _coord(play: dict[str, Any], key: str) -> float | None:
    val = play.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def fetch_plays_pages(
    league_slug: str,
    event_id: str,
    *,
    pages: int = 3,
    page_size: int = 100,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch the newest N pages of ESPN plays (with pitch coordinates)."""
    fetch = fetcher or _fetch_json
    first_url = PLAYS_URL.format(
        league=urllib.parse.quote(league_slug, safe="."),
        event_id=urllib.parse.quote(str(event_id), safe=""),
        limit=page_size,
        page=1,
    )
    try:
        first = fetch(first_url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return []

    page_count = int(first.get("pageCount") or 1)
    start_page = max(1, page_count - pages + 1)
    plays: list[dict[str, Any]] = []
    for page in range(start_page, page_count + 1):
        if page == 1:
            payload = first
        else:
            url = PLAYS_URL.format(
                league=urllib.parse.quote(league_slug, safe="."),
                event_id=urllib.parse.quote(str(event_id), safe=""),
                limit=page_size,
                page=page,
            )
            try:
                payload = fetch(url)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
                continue
        plays.extend(payload.get("items") or [])
    return plays


def build_pitch_track(
    league_slug: str,
    event_id: str,
    *,
    home: str = "",
    away: str = "",
    home_score: int = 0,
    away_score: int = 0,
    clock: str = "",
    league_chiclet: str = "",
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Build a pitch graphic payload: ball position, recent passes, shots.

    Coordinates are ESPN fieldPosition* on a 0–100 pitch
    (X along length, Y across width).
    """
    cache_key = f"{league_slug}:{event_id}"
    if use_cache and cache_key in _track_cache:
        ts, payload = _track_cache[cache_key]
        if time.time() - ts < _TRACK_TTL_S:
            return payload

    plays = fetch_plays_pages(league_slug, event_id, pages=8, fetcher=fetcher)
    passes: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    ball: dict[str, Any] | None = None
    counts = {"passes": 0, "shots": 0, "shots_on": 0, "goals": 0, "fouls": 0}

    for play in plays:
        ptype = _play_type(play)
        if ptype in PASS_TYPES:
            counts["passes"] += 1
        if ptype in SHOT_TYPES:
            counts["shots"] += 1
        if ptype in {"shot-on-target", "goal", "penalty-goal"}:
            counts["shots_on"] += 1
        if ptype in {"goal", "penalty-goal"}:
            counts["goals"] += 1
        if ptype == "foul":
            counts["fouls"] += 1

        x = _coord(play, "fieldPositionX")
        y = _coord(play, "fieldPositionY")
        x2 = _coord(play, "fieldPosition2X")
        y2 = _coord(play, "fieldPosition2Y")
        label = str(play.get("shortText") or play.get("text") or ptype)
        entry = {
            "id": str(play.get("id") or ""),
            "type": ptype,
            "text": label,
            "clock": _clock_label(play),
            "x": x,
            "y": y,
            "x2": x2,
            "y2": y2,
            "scoring": bool(play.get("scoringPlay")),
        }

        if ptype in PASS_TYPES and x is not None and y is not None:
            passes.append(entry)
        if ptype in SHOT_TYPES and x is not None and y is not None:
            shots.append(entry)

        # Ball = end of latest positioned action
        if ptype in BALL_TYPES:
            bx = x2 if x2 is not None else x
            by = y2 if y2 is not None else y
            if bx is not None and by is not None:
                ball = {
                    "x": bx,
                    "y": by,
                    "type": ptype,
                    "text": label,
                    "clock": _clock_label(play),
                }

    # Keep the most recent trails for the graphic
    passes = passes[-40:]
    shots = shots[-25:]
    recent = []
    for play in plays[-30:]:
        recent.append(
            {
                "type": _play_type(play),
                "text": str(play.get("shortText") or play.get("text") or ""),
                "clock": _clock_label(play),
            }
        )
    recent = list(reversed(recent))

    payload = {
        "event_id": str(event_id),
        "league_slug": league_slug,
        "league_chiclet": league_chiclet,
        "home": home,
        "away": away,
        "home_score": home_score,
        "away_score": away_score,
        "clock": clock,
        "ball": ball,
        "passes": passes,
        "shots": shots,
        "recent": recent,
        "counts": counts,
        "fetched_at": time.time(),
    }
    if use_cache:
        _track_cache[cache_key] = (time.time(), payload)
    return payload


def clear_track_cache() -> None:
    _track_cache.clear()
