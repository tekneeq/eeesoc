"""Live match scoreboard via ESPN CDN (no API key)."""

from __future__ import annotations

import json
import re
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
    home_id: str = ""
    away_id: str = ""
    clock_seconds: int | None = None

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
        clock_seconds: int | None = None
        raw_clock = status.get("clock")
        if raw_clock is not None:
            try:
                clock_seconds = max(0, int(float(raw_clock)))
            except (TypeError, ValueError):
                clock_seconds = None

        home = away = "?"
        home_id = away_id = ""
        home_score = away_score = 0
        for team in comp.get("competitors") or []:
            tmeta = team.get("team") or {}
            name = tmeta.get("displayName") or tmeta.get("shortDisplayName") or "?"
            tid = str(tmeta.get("id") or team.get("id") or "")
            score = _safe_int(team.get("score"), 0)
            if team.get("homeAway") == "home":
                home, home_score, home_id = name, score, tid
            elif team.get("homeAway") == "away":
                away, away_score, away_id = name, score, tid

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
                home_id=home_id,
                away_id=away_id,
                clock_seconds=clock_seconds,
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


# —— Live situation for Similar (goals + per-team shots/SOT) ——

_TEAM_ID_RE = re.compile(r"/teams/(\d+)")
_CLOCK_MIN_RE = re.compile(r"(\d+)")
_sit_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_SIT_TTL_S = 12.0


def parse_clock_minute(clock: str | None, *, default: int = 1) -> int:
    """Parse ESPN display clocks like \"24'\" / \"45'+2\" / \"90'+4\" into a 1–90 minute."""
    if not clock:
        return default
    m = _CLOCK_MIN_RE.search(str(clock))
    if not m:
        return default
    return max(1, min(90, int(m.group(1))))


def _team_id_from_play(play: dict[str, Any]) -> str | None:
    team = play.get("team") or {}
    ref = str(team.get("$ref") or "")
    m = _TEAM_ID_RE.search(ref)
    if m:
        return m.group(1)
    for part in play.get("participants") or []:
        tref = str((part.get("team") or {}).get("$ref") or "")
        m = _TEAM_ID_RE.search(tref)
        if m:
            return m.group(1)
    return None


def _play_minute(play: dict[str, Any]) -> int | None:
    clock = play.get("clock") or {}
    if isinstance(clock, dict):
        if clock.get("displayValue"):
            return parse_clock_minute(str(clock["displayValue"]), default=0) or None
        val = clock.get("value")
        if val is not None:
            try:
                # ESPN clock.value is seconds elapsed in the match
                return max(1, min(90, int(float(val) // 60) + 1))
            except (TypeError, ValueError):
                return None
    return None


def fetch_all_plays(
    league_slug: str,
    event_id: str,
    *,
    page_size: int = 100,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch every ESPN play page for an event (needed for accurate shot totals)."""
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

    page_count = max(1, int(first.get("pageCount") or 1))
    plays: list[dict[str, Any]] = list(first.get("items") or [])
    for page in range(2, page_count + 1):
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


def _side_for_team(
    team_id: str | None,
    *,
    home_id: str,
    away_id: str,
    home: str,
    away: str,
    play: dict[str, Any],
) -> str | None:
    if team_id and home_id and team_id == home_id:
        return "home"
    if team_id and away_id and team_id == away_id:
        return "away"
    text = str(play.get("text") or "")
    # "Dan Ndoye (Nottingham Forest) Goal at 24'"
    if home and f"({home})" in text:
        return "home"
    if away and f"({away})" in text:
        return "away"
    return None


def build_live_situation(
    league_slug: str,
    event_id: str,
    *,
    home: str = "",
    away: str = "",
    home_score: int = 0,
    away_score: int = 0,
    clock: str = "",
    league_chiclet: str = "",
    home_id: str = "",
    away_id: str = "",
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Freeze a live match into a Similar-ready snapshot plus goal-event context.

    Counts shots / SOT / goals from the full ESPN play feed, attributed home/away.
    For each goal, records each team's cumulative shots/SOT at that minute.
    """
    cache_key = f"sit:{league_slug}:{event_id}"
    if use_cache and cache_key in _sit_cache:
        ts, payload = _sit_cache[cache_key]
        if time.time() - ts < _SIT_TTL_S:
            return payload

    plays = fetch_all_plays(league_slug, event_id, fetcher=fetcher)
    minute = parse_clock_minute(clock, default=1)

    home_shots = away_shots = home_sot = away_sot = 0
    home_goals = away_goals = 0
    goals: list[dict[str, Any]] = []
    goal_minutes: list[int] = []

    # Running tallies so each goal can snapshot "what each team had when this happened"
    for play in plays:
        ptype = _play_type(play)
        tid = _team_id_from_play(play)
        side = _side_for_team(
            tid, home_id=home_id, away_id=away_id, home=home, away=away, play=play
        )
        pmin = _play_minute(play) or minute

        is_shot = ptype in SHOT_TYPES
        is_sot = ptype in {"shot-on-target", "goal", "penalty-goal"}
        is_goal = ptype in {"goal", "penalty-goal"} or bool(play.get("scoringPlay"))

        if is_shot and side == "home":
            home_shots += 1
            if is_sot:
                home_sot += 1
        elif is_shot and side == "away":
            away_shots += 1
            if is_sot:
                away_sot += 1

        if is_goal and side in {"home", "away"}:
            if side == "home":
                home_goals += 1
            else:
                away_goals += 1
            goal_minutes.append(pmin)
            scorer = str(play.get("shortText") or play.get("text") or "Goal")
            goals.append(
                {
                    "minute": pmin,
                    "team": side,
                    "team_name": home if side == "home" else away,
                    "opponent_name": away if side == "home" else home,
                    "text": scorer,
                    "home_shots": home_shots,
                    "away_shots": away_shots,
                    "home_sot": home_sot,
                    "away_sot": away_sot,
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    # Focal "my team" when opponent scored = the conceding side
                    "conceded_by": "away" if side == "home" else "home",
                    "conceded_by_name": away if side == "home" else home,
                    "my_shots": away_shots if side == "home" else home_shots,
                    "my_sot": away_sot if side == "home" else home_sot,
                    "scorer_shots": home_shots if side == "home" else away_shots,
                    "scorer_sot": home_sot if side == "home" else away_sot,
                }
            )

    # Prefer live scoreboard totals when plays missed attribution
    if home_goals + away_goals < home_score + away_score:
        home_goals, away_goals = home_score, away_score

    snapshot = {
        "minute": minute,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "home_shots": home_shots,
        "away_shots": away_shots,
        "home_sot": home_sot,
        "away_sot": away_sot,
        "goal_minutes": tuple(goal_minutes),
    }

    latest = goals[-1] if goals else None
    goals_label = "/".join(f"{m}'" for m in goal_minutes) or "—"
    payload = {
        "event_id": str(event_id),
        "league_slug": league_slug,
        "league_chiclet": league_chiclet,
        "home": home,
        "away": away,
        "home_id": home_id,
        "away_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "clock": clock,
        "minute": minute,
        "snapshot": snapshot,
        "label": f"{goals_label} · {home_shots}/{home_sot} vs {away_shots}/{away_sot}",
        "goals": goals,
        "latest_goal": latest,
        "fetched_at": time.time(),
    }
    if use_cache:
        _sit_cache[cache_key] = (time.time(), payload)
    return payload


def clear_situation_cache() -> None:
    _sit_cache.clear()


# —— Match minute timeline (shots / SOT / goals / corners) ——

CORNER_TYPES = {"corner-awarded", "corner"}
GOAL_TYPES = {"goal", "penalty-goal", "penalty---scored"}
SHOT_ON_TYPES = {"shot-on-target", "goal", "penalty-goal", "penalty---scored"}
SHOT_OFF_TYPES = {
    "shot",
    "shot-off-target",
    "shot-blocked",
    "miss",
    "woodwork",
}

_timeline_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_TIMELINE_TTL_S = 12.0


def _event_kind(ptype: str, *, scoring: bool) -> str | None:
    """Most specific marker: goal > shot_on > shot > corner."""
    if ptype in GOAL_TYPES or (scoring and ptype in SHOT_ON_TYPES):
        return "goal"
    if ptype in SHOT_ON_TYPES:
        return "shot_on"
    if ptype in SHOT_OFF_TYPES or ptype in SHOT_TYPES:
        return "shot"
    if ptype in CORNER_TYPES:
        return "corner"
    return None


def _play_elapsed_seconds(play: dict[str, Any]) -> int | None:
    clock = play.get("clock") or {}
    if isinstance(clock, dict) and clock.get("value") is not None:
        try:
            return max(0, int(float(clock["value"])))
        except (TypeError, ValueError):
            return None
    minute = _play_minute(play)
    if minute is None:
        return None
    return max(0, (minute - 1) * 60)


def build_event_timeline(
    league_slug: str,
    event_id: str,
    *,
    home: str = "",
    away: str = "",
    home_id: str = "",
    away_id: str = "",
    clock: str = "",
    clock_seconds: int | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Build a 0–90' event strip: shots, shots on target, goals, corners.

    Home events sit above the axis, away below. ``elapsed_seconds`` is the best
    live match clock for a client-side 1s tick of the now cursor.
    """
    cache_key = f"tl:{league_slug}:{event_id}"
    if use_cache and cache_key in _timeline_cache:
        ts, payload = _timeline_cache[cache_key]
        if time.time() - ts < _TIMELINE_TTL_S:
            return payload

    plays = fetch_all_plays(league_slug, event_id, fetcher=fetcher)
    board_minute = parse_clock_minute(clock, default=1)
    play_seconds = [s for p in plays if (s := _play_elapsed_seconds(p)) is not None]
    latest_play_seconds = max(play_seconds) if play_seconds else None
    latest_play = (latest_play_seconds // 60) + 1 if latest_play_seconds is not None else board_minute
    minute = max(1, min(90, max(board_minute, latest_play)))

    board_seconds = clock_seconds
    if board_seconds is None:
        board_seconds = max(0, (board_minute - 1) * 60)
    elapsed_seconds = max(board_seconds, latest_play_seconds or 0)
    # Soft-cap near full time; still allow a bit of stoppage headroom for the tick
    elapsed_seconds = min(elapsed_seconds, 99 * 60)

    clock_l = clock.lower()
    frozen = any(tok in clock_l for tok in ("ht", "half", "ft", "end", "sched", "postpon"))

    events: list[dict[str, Any]] = []
    counts = {
        "shot": 0,
        "shot_on": 0,
        "goal": 0,
        "corner": 0,
        "home_shot": 0,
        "away_shot": 0,
        "home_shot_on": 0,
        "away_shot_on": 0,
        "home_goal": 0,
        "away_goal": 0,
        "home_corner": 0,
        "away_corner": 0,
    }

    for play in plays:
        ptype = _play_type(play)
        # Normalise ESPN's odd penalty slug
        if ptype.startswith("penalty") and "scor" in ptype:
            ptype = "penalty---scored"
        kind = _event_kind(ptype, scoring=bool(play.get("scoringPlay")))
        if not kind:
            continue
        pmin = _play_minute(play)
        if pmin is None:
            continue
        tid = _team_id_from_play(play)
        side = _side_for_team(
            tid, home_id=home_id, away_id=away_id, home=home, away=away, play=play
        )
        text = str(play.get("shortText") or play.get("text") or kind)
        events.append(
            {
                "minute": pmin,
                "kind": kind,
                "type": ptype,
                "team": side,
                "text": text,
                "clock": _clock_label(play),
            }
        )
        counts[kind] += 1
        if side in {"home", "away"}:
            counts[f"{side}_{kind}"] = counts.get(f"{side}_{kind}", 0) + 1

    events.sort(key=lambda e: (e["minute"], e["kind"]))
    payload = {
        "event_id": str(event_id),
        "league_slug": league_slug,
        "home": home,
        "away": away,
        "clock": clock,
        "minute": minute,
        "board_minute": board_minute,
        "play_minute": latest_play,
        "elapsed_seconds": elapsed_seconds,
        "frozen": frozen,
        "max_minute": 90,
        "events": events,
        "counts": counts,
        "fetched_at": time.time(),
    }
    if use_cache:
        _timeline_cache[cache_key] = (time.time(), payload)
    return payload


def clear_timeline_cache() -> None:
    _timeline_cache.clear()
