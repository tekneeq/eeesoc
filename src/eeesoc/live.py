"""Live match scoreboard via ESPN CDN (no API key)."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

USER_AGENT = "eeesoc/0.1 (+https://github.com/tekneeq/eeesoc)"
CDN_SCOREBOARD = "https://cdn.espn.com/core/soccer/scoreboard?xhr=1&league={league}"
SITE_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"

# ESPN often leaves Saturday 15:00 EPL kickoffs as `pre` for several minutes.
# Treat kickoff-passed (and imminent) fixtures as live so chiclets appear at KO.
_KO_AHEAD_S = 45
_KO_STALE_PRE_S = 3 * 3600
_DEAD_STATUS = ("postponed", "canceled", "cancelled", "suspended", "abandoned", "forfeit")

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

_CACHE_TTL_S = 6.0
_board_cache: dict[tuple[bool, int], tuple[float, dict[str, Any]]] = {}


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


def parse_start(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def _scoreboard_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize CDN (`content.sbData`) and site.api (top-level events) payloads."""
    content = payload.get("content")
    if isinstance(content, dict) and isinstance(content.get("sbData"), dict):
        return content["sbData"]
    if payload.get("events") is not None or payload.get("leagues") is not None:
        return payload
    return {}


def _status_blob(match: LiveMatch) -> str:
    return f"{match.detail} {match.clock} {match.state}".lower()


def match_is_live(match: LiveMatch, now: datetime | None = None) -> bool:
    """True when ESPN says in-play, or kickoff has arrived while state is still `pre`."""
    if match.state == "in":
        return True
    if match.state != "pre":
        return False
    if any(word in _status_blob(match) for word in _DEAD_STATUS):
        return False
    start = parse_start(match.start)
    if start is None:
        return False
    now = now or datetime.now(timezone.utc)
    elapsed = (now - start).total_seconds()
    return -_KO_AHEAD_S <= elapsed <= _KO_STALE_PRE_S


_CLOCK_RE = re.compile(r"^\d{1,3}(\+\d+)?'?$")


def _usable_clock(clock: str) -> bool:
    c = (clock or "").strip()
    if not c or c in {"0'", "0"}:
        return False
    return bool(_CLOCK_RE.match(c) or c.upper() in {"HT", "FT", "LIVE"})


def promote_started_match(match: LiveMatch, now: datetime | None = None) -> LiveMatch:
    """Flip a kickoff-passed `pre` row to `in` and synthesize a running clock."""
    now = now or datetime.now(timezone.utc)
    if match.state == "in" or not match_is_live(match, now):
        return match
    start = parse_start(match.start)
    elapsed = max(0, int((now - start).total_seconds())) if start else 0
    clock = match.clock if _usable_clock(match.clock) else f"{elapsed // 60}'"
    clock_seconds = match.clock_seconds if match.clock_seconds is not None else elapsed
    return replace(match, state="in", clock=clock, clock_seconds=clock_seconds, detail=clock)


def parse_scoreboard(payload: dict[str, Any], league_slug: str, chiclet: str) -> list[LiveMatch]:
    sb = _scoreboard_body(payload)
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


def _scoreboard_dates(now: datetime | None = None, *, days_back: int = 0) -> str:
    """UTC date range: reach back for late finishes, ahead for tonight's remaining kickoffs."""
    now = now or datetime.now(timezone.utc)
    back = max(int(days_back), 1 if now.hour < 8 else 0)
    start = now - timedelta(days=back)
    # After 20:00 UTC, or whenever we already widened the window for Finished,
    # include tomorrow so evening-local "today" games are not dropped.
    ahead = 1 if now.hour >= 20 or int(days_back) > 0 else 0
    end = now + timedelta(days=ahead)
    if start.date() == end.date():
        return start.strftime("%Y%m%d")
    return f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"


def _scoreboard_urls(league_slug: str, *, days_back: int = 0) -> list[str]:
    quoted = urllib.parse.quote(league_slug, safe=".")
    dates = _scoreboard_dates(days_back=days_back)
    return [
        f"{SITE_SCOREBOARD.format(league=quoted)}?dates={dates}",
        CDN_SCOREBOARD.format(league=quoted),
    ]


def fetch_league(
    league_slug: str,
    chiclet: str,
    *,
    days_back: int = 0,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> list[LiveMatch]:
    fetch = fetcher or _fetch_json
    for url in _scoreboard_urls(league_slug, days_back=days_back):
        try:
            payload = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        matches = parse_scoreboard(payload, league_slug, chiclet)
        if matches or fetcher is not None:
            return matches
    return []


def fetch_live_board(
    *,
    live_only: bool = True,
    days_back: int = 0,
    leagues: list[tuple[str, str]] | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Fan out across leagues and return matches grouped for the Live tab.

    ``live_only=False`` keeps finished (``post``) and upcoming (``pre``) rows
    so the client can show full-time and not-yet-started chiclets; ``days_back``
    widens the scoreboard window to pick up yesterday's finished games and
    tonight's remaining kickoffs.

    Cached briefly so the dashboard can poll without hammering ESPN.
    """
    days_back = max(0, min(int(days_back), 3))
    cache_key = (bool(live_only), days_back)
    if use_cache and cache_key in _board_cache:
        ts, payload = _board_cache[cache_key]
        if time.time() - ts < _CACHE_TTL_S:
            return payload

    league_list = leagues or LEAGUES
    matches: list[LiveMatch] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(8, len(league_list) or 1)) as pool:
        futures = {
            pool.submit(fetch_league, slug, label, days_back=days_back, fetcher=fetcher): (slug, label)
            for slug, label in league_list
        }
        for fut in as_completed(futures):
            slug, label = futures[fut]
            try:
                matches.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 — surface per-league failure
                errors.append(f"{label}: {exc}")

    now = datetime.now(timezone.utc)
    matches = [promote_started_match(m, now) for m in matches]
    if live_only:
        matches = [m for m in matches if match_is_live(m, now)]

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
            "post_count": sum(1 for m in matches if m.league_slug == slug and m.state == "post"),
            "pre_count": sum(
                1
                for m in matches
                if m.league_slug == slug and m.state == "pre" and not any(w in _status_blob(m) for w in _DEAD_STATUS)
            ),
            "count": sum(1 for m in matches if m.league_slug == slug),
        }
        for slug, label in league_list
    ]

    payload = {
        "live_only": live_only,
        "days_back": days_back,
        "fetched_at": time.time(),
        "total": len(matches),
        "live_total": sum(1 for m in matches if m.state == "in"),
        "post_total": sum(1 for m in matches if m.state == "post"),
        "pre_total": sum(
            1 for m in matches if m.state == "pre" and not any(w in _status_blob(m) for w in _DEAD_STATUS)
        ),
        "chiclets": chiclet_meta,
        "leagues": grouped,
        "errors": errors,
    }
    if use_cache:
        _board_cache[cache_key] = (time.time(), payload)
    return payload


def clear_live_cache() -> None:
    _board_cache.clear()


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
    "own-goal",
}

_track_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_TRACK_TTL_S = 5.0


def _play_type(play: dict[str, Any]) -> str:
    t = play.get("type") or {}
    return str(t.get("type") or t.get("text") or "").lower()


_OWN_GOAL_BY_RE = re.compile(r"own\s+goal\s+by\s+.+?,\s*([^.\[]+)", re.I)
_OWN_GOAL_PAREN_RE = re.compile(r"\(([^)]+)\)\s*own\s+goal", re.I)


def _is_own_goal(ptype: str, play: dict[str, Any] | None = None) -> bool:
    """ESPN own goals: type ``own-goal``, ``ownGoal: true``, or 'Own Goal' copy."""
    if play is not None and play.get("ownGoal") is True:
        return True
    blob = (ptype or "").lower().replace("_", "-").replace(" ", "-")
    if "own-goal" in blob:
        return True
    if play:
        text = " ".join(
            str(play.get(k) or "") for k in ("text", "shortText", "alternativeText")
        ).lower()
        if "own goal" in text or "own-goal" in text:
            return True
    return False


def _name_in_text(name: str, haystack: str) -> bool:
    if not name or not haystack:
        return False
    h = haystack.lower()
    n = name.lower()
    if n in h:
        return True
    token = name.split()[-1]
    return len(token) > 3 and token.lower() in h


def _side_for_own_goal(
    team_id: str | None,
    *,
    home_id: str,
    away_id: str,
    home: str,
    away: str,
    play: dict[str, Any],
) -> str | None:
    """
    Side that *benefits* from the own goal.

    ESPN's ``team`` $ref is the scoring (benefiting) side, not the player
    who put it in. Text fallbacks name the conceding club ("Own Goal by
    X, Newcastle United") so those must be flipped.
    """
    if team_id and home_id and team_id == home_id:
        return "home"
    if team_id and away_id and team_id == away_id:
        return "away"
    text = " ".join(str(play.get(k) or "") for k in ("text", "shortText", "alternativeText"))
    scorer = ""
    matched = _OWN_GOAL_BY_RE.search(text)
    if matched:
        scorer = matched.group(1).strip()
    else:
        matched = _OWN_GOAL_PAREN_RE.search(text)
        if matched:
            scorer = matched.group(1).strip()
    if scorer:
        if _name_in_text(home, scorer):
            return "away"
        if _name_in_text(away, scorer):
            return "home"
    return None


def _clock_label(play: dict[str, Any]) -> str:
    clock = play.get("clock") or {}
    if isinstance(clock, dict) and clock.get("displayValue"):
        return str(clock["displayValue"])
    return ""


_GENERIC_PLAYER = {
    "goal",
    "header",
    "header goal",
    "penalty",
    "penalty goal",
    "own goal",
    "substitution",
}
_GOAL_SHORT_RE = re.compile(
    r"^(?P<name>.+?)\s+(?:Own Goal|Goal(?:\s*-\s*\S+)?|Penalty\s*-\s*Scored)\s*$",
    re.I,
)
_GOAL_BANG_RE = re.compile(
    r"Goal!\s*.+?\.\s*(?P<name>.+?)\s*\([^)]+\)",
    re.I,
)
_GOAL_PAREN_RE = re.compile(
    r"^(?P<name>.+?)\s*\([^)]+\)\s*(?:Goal|Own Goal|Penalty)\b",
    re.I,
)
_OG_PLAYER_RE = re.compile(r"Own Goal by\s+(?P<name>.+?)\s*,", re.I)
_SUB_TEXT_RE = re.compile(
    r"Substitution,\s*(?P<team>.+?)\.\s*(?P<on>.+?)\s+replaces\s+(?P<off>.+?)(?:\s+because|\.|$)",
    re.I,
)
_SUB_SHORT_RE = re.compile(r"^(?P<on>.+?)\s+Substitut", re.I)


def _clean_player(name: str | None) -> str | None:
    text = re.sub(r"\s+", " ", (name or "").strip().strip(".,;"))
    if not text or text.lower() in _GENERIC_PLAYER:
        return None
    return text


def _is_substitution(ptype: str, play: dict[str, Any] | None = None) -> bool:
    if play is not None and play.get("substitution") is True:
        return True
    return "substitut" in (ptype or "").lower()


def _is_penalty_goal(ptype: str, play: dict[str, Any] | None = None) -> bool:
    blob = (ptype or "").lower()
    if "penalty" in blob and ("scor" in blob or blob in {"penalty-goal", "penalty"}):
        return True
    if play:
        short = str(play.get("shortText") or "").lower()
        if "penalty" in short and "scor" in short:
            return True
    return False


def _player_name_from_goal(play: dict[str, Any], *, own: bool = False) -> str | None:
    short = str(play.get("shortText") or "")
    text = str(play.get("text") or play.get("alternativeText") or "")
    if own:
        m = _OG_PLAYER_RE.search(text)
        named = _clean_player(m.group("name") if m else None)
        if named:
            return named
        m = _GOAL_SHORT_RE.search(short)
        return _clean_player(m.group("name") if m else None)
    for raw, rx in (
        (text, _GOAL_BANG_RE),
        (text, _GOAL_PAREN_RE),
        (short, _GOAL_PAREN_RE),
        (short, _GOAL_SHORT_RE),
    ):
        m = rx.search(raw)
        named = _clean_player(m.group("name") if m else None)
        if named:
            return named
    return None


_STOPPAGE_RE = re.compile(r"(\d+)\s*'?\s*\+\s*(\d+)")


def _bulletin_sort_key(row: dict[str, Any]) -> tuple:
    """Order by clock, including 90'+n stoppage, then elapsed seconds."""
    clock = str(row.get("clock") or "")
    added = 0
    base = int(row.get("minute") or 0)
    hit = _STOPPAGE_RE.search(clock)
    if hit:
        base = int(hit.group(1))
        added = int(hit.group(2))
    elapsed = row.get("elapsed")
    return (base, added, elapsed if elapsed is not None else 10**6, str(row.get("kind") or ""))


def _sub_players(play: dict[str, Any]) -> tuple[str | None, str | None]:
    text = str(play.get("text") or play.get("alternativeText") or "")
    short = str(play.get("shortText") or "")
    m = _SUB_TEXT_RE.search(text)
    if m:
        return _clean_player(m.group("on")), _clean_player(m.group("off"))
    m = _SUB_SHORT_RE.search(short)
    return (_clean_player(m.group("on") if m else None), None)


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
        own = _is_own_goal(ptype, play)
        if own:
            ptype = "own-goal"
        if ptype in PASS_TYPES:
            counts["passes"] += 1
        if ptype in SHOT_TYPES:
            counts["shots"] += 1
        if ptype in {"shot-on-target", "goal", "penalty-goal"}:
            counts["shots_on"] += 1
        if ptype in {"goal", "penalty-goal"} or own:
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
            "own_goal": own,
        }

        if ptype in PASS_TYPES and x is not None and y is not None:
            passes.append(entry)
        if (ptype in SHOT_TYPES or own) and x is not None and y is not None:
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


def _play_period(play: dict[str, Any]) -> int | None:
    """ESPN period number (1 = first half, 2 = second half); None when absent."""
    period = play.get("period")
    if isinstance(period, dict):
        period = period.get("number")
    try:
        n = int(period)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


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
    text = " ".join(
        str(play.get(k) or "") for k in ("text", "shortText", "alternativeText")
    )
    # "Dan Ndoye (Nottingham Forest) Goal at 24'"
    if home and f"({home})" in text:
        return "home"
    if away and f"({away})" in text:
        return "away"
    for side, name in (("home", home), ("away", away)):
        if name and name in text:
            return side
        token = name.split()[-1] if name else ""
        if token and len(token) > 3 and token in text:
            return side
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
        own = _is_own_goal(ptype, play)
        tid = _team_id_from_play(play)
        side = (
            _side_for_own_goal(
                tid, home_id=home_id, away_id=away_id, home=home, away=away, play=play
            )
            if own
            else _side_for_team(
                tid, home_id=home_id, away_id=away_id, home=home, away=away, play=play
            )
        )
        pmin = _play_minute(play) or minute

        is_shot = (not own) and ptype in SHOT_TYPES
        is_sot = (not own) and ptype in {"shot-on-target", "goal", "penalty-goal"}
        is_goal = own or ptype in {"goal", "penalty-goal"} or bool(play.get("scoringPlay"))

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
                    "own_goal": own,
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


# —— Match minute timeline (shots / SOT / goals / corners / blocked) ——

CORNER_TYPES = {"corner-awarded", "corner"}
GOAL_TYPES = {"goal", "penalty-goal", "penalty---scored"}
SHOT_ON_TYPES = {"shot-on-target", "goal", "penalty-goal", "penalty---scored"}
BLOCKED_TYPES = {"shot-blocked"}
SHOT_OFF_TYPES = {
    "shot",
    "shot-off-target",
    "miss",
    "woodwork",
}

_timeline_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_TIMELINE_TTL_S = 5.0
# Full-time play-by-play never changes; finished chiclets should not re-page ESPN every poll.
_TIMELINE_FINAL_TTL_S = 15 * 60.0
_FINAL_CLOCK_TOKENS = ("ft", "full", "final", "aet", "pen")


def _is_final_clock(clock: str) -> bool:
    c = (clock or "").lower()
    return any(tok in c for tok in _FINAL_CLOCK_TOKENS)


# A completed pass received inside the penalty area, in the passer's attacking
# frame (ESPN normalises fieldPositionX so 100 is always the goal being attacked).
BOX_X_MIN = 83.0
BOX_Y_MIN, BOX_Y_MAX = 21.0, 79.0


def _is_box_entry(ptype: str, play: dict[str, Any]) -> bool:
    if ptype != "pass":
        return False
    px = _coord(play, "fieldPositionX")
    py = _coord(play, "fieldPositionY")
    return px is not None and py is not None and px >= BOX_X_MIN and BOX_Y_MIN <= py <= BOX_Y_MAX


def box_entries_from_plays(
    plays: list[dict[str, Any]],
    *,
    home_id: str = "",
    away_id: str = "",
    home: str = "",
    away: str = "",
) -> dict[str, dict[str, list[int]]]:
    """
    Minutes of every penalty-box entry per half and side:
    ``{"1": {"home": [...], "away": [...]}, "2": {...}}``.

    Box entries — not shots — are what separates goalless halves from the rest
    in the archive (see ``eeesoc.nogoal``), so they are kept at minute level.
    """
    out: dict[str, dict[str, list[int]]] = {"1": {"home": [], "away": []}, "2": {"home": [], "away": []}}
    for play in plays:
        ptype = _play_type(play)
        if not _is_box_entry(ptype, play):
            continue
        pmin = _play_minute(play)
        period = _play_period(play)
        if pmin is None:
            continue
        if period is None:
            period = 1 if pmin <= 45 else 2
        if period not in (1, 2):
            continue
        side = _side_for_team(
            _team_id_from_play(play), home_id=home_id, away_id=away_id, home=home, away=away, play=play
        )
        if side not in {"home", "away"}:
            continue
        out[str(period)][side].append(int(pmin))
    for half in out.values():
        for side in half.values():
            side.sort()
    return out


def _normalize_play_type(ptype: str) -> str:
    if _is_own_goal(ptype):
        return "own-goal"
    if ptype.startswith("penalty") and "scor" in ptype:
        return "penalty---scored"
    # ESPN variants: goal---header, goal---volley — but not goal-kick
    if ptype == "goal" or ptype.startswith("goal---"):
        return "goal"
    return ptype


def _event_kind(ptype: str, *, scoring: bool) -> str | None:
    """Most specific marker: own_goal > goal > shot_on > blocked > shot > corner."""
    if ptype == "own-goal" or _is_own_goal(ptype):
        return "own_goal"
    if ptype in GOAL_TYPES or (scoring and ptype in SHOT_ON_TYPES):
        return "goal"
    if ptype in SHOT_ON_TYPES:
        return "shot_on"
    if ptype in BLOCKED_TYPES:
        return "blocked"
    if ptype in SHOT_OFF_TYPES or ptype in SHOT_TYPES:
        return "shot"
    if ptype in CORNER_TYPES:
        return "corner"
    return None


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
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


_FROZEN_CLOCK_TOKENS = ("ht", "half", "ft", "end", "sched", "postpon", "delay", "abandon")

_TERRITORY_COLS = 6
_TERRITORY_ROWS = 4


def _build_territory(points: list[tuple[float, float, str]]) -> dict[str, Any]:
    """
    Aggregate positioned plays into a pitch-occupancy map.

    ESPN play coordinates are team-relative (each side attacks x→100), so away
    events are mirrored onto one absolute pitch with HOME attacking right.
    ``points`` is (x, y, side) with side in {"home", "away"}.
    """
    cells = [[0] * _TERRITORY_COLS for _ in range(_TERRITORY_ROWS)]
    thirds = [0, 0, 0]  # home-defensive | middle | home-attacking
    side_counts = {"home": 0, "away": 0}

    for x, y, side in points:
        if side == "away":
            x, y = 100.0 - x, 100.0 - y
        x = min(99.99, max(0.0, x))
        y = min(99.99, max(0.0, y))
        col = int(x / 100.0 * _TERRITORY_COLS)
        row = int(y / 100.0 * _TERRITORY_ROWS)
        cells[row][col] += 1
        thirds[0 if x < 100 / 3 else (1 if x < 200 / 3 else 2)] += 1
        side_counts[side] += 1

    total = len(points)
    max_cell = max((max(r) for r in cells), default=0)
    pct = [round(t / total, 3) if total else None for t in thirds]

    # Most of any match is played in the middle third, so test for an
    # attacking tilt first — otherwise "midfield" swallows every game.
    label = "balanced"
    if total < 15:
        label = "warming_up"
    elif pct[2] >= 0.30 and pct[2] - pct[0] >= 0.10:
        label = "home_attacking"
    elif pct[0] >= 0.30 and pct[0] - pct[2] >= 0.10:
        label = "away_attacking"
    elif pct[1] >= 0.42:
        label = "midfield"

    ball_total = side_counts["home"] + side_counts["away"]
    return {
        "cols": _TERRITORY_COLS,
        "rows": _TERRITORY_ROWS,
        "cells": cells,
        "max": max_cell,
        "total": total,
        "thirds": {"home_def": pct[0], "mid": pct[1], "home_att": pct[2]},
        "ball_share": {
            "home": round(side_counts["home"] / ball_total, 3) if ball_total else None,
            "away": round(side_counts["away"] / ball_total, 3) if ball_total else None,
        },
        "label": label,
    }


PRESSURE_WINDOW_MIN = 15
_ATT_THIRD_X = 200.0 / 3.0
# ESPN team-relative box: roughly the last 17m of the pitch, central 58% of width.
_BOX_X = 83.0
_BOX_Y_LO, _BOX_Y_HI = 21.0, 79.0
_PRESSURE_MIN_ACTIONS = 20
_PRESSURE_MIN_FINAL_THIRD = 6
_PRESSURE_TILT = 0.62

_PRESSURE_SHOT_KINDS = {"shot", "shot_on", "blocked", "goal"}


def _pressure_verdict(
    *,
    actions: int,
    final_third: int,
    share_home: float | None,
) -> tuple[str, str | None]:
    """Who is pinning whom, or quiet / even when the window is too thin."""
    if actions < _PRESSURE_MIN_ACTIONS or final_third < _PRESSURE_MIN_FINAL_THIRD:
        return "quiet", None
    if share_home is not None and share_home >= _PRESSURE_TILT:
        return "home", "home"
    if share_home is not None and (1.0 - share_home) >= _PRESSURE_TILT:
        return "away", "away"
    return "even", None


def _build_pressure(
    points: list[tuple[int, float, float, str, str | None]],
    *,
    now_minute: int,
    window: int = PRESSURE_WINDOW_MIN,
) -> dict[str, Any]:
    """
    Rolling-window pressure: who keeps getting into the other side's final third.

    ``points`` is (minute, x, y, side, kind) with team-relative coordinates
    (each side attacks x→100). Territory alone hides this — a first-half
    Udinese siege and a second-half Lazio siege sum to "even". ``series`` is
    the same window evaluated at every minute so the chiclet can show how
    the squeeze has shifted (what it looked like 10′ ago, not just now).
    """
    lo = max(1, now_minute - window + 1)
    stats: dict[str, dict[str, int]] = {
        side: {"actions": 0, "final_third": 0, "box": 0, "shots": 0, "corners": 0}
        for side in ("home", "away")
    }
    for minute, x, y, side, kind in points:
        if side not in stats or minute < lo or minute > now_minute:
            continue
        s = stats[side]
        s["actions"] += 1
        if x >= _ATT_THIRD_X:
            s["final_third"] += 1
        if x >= _BOX_X and _BOX_Y_LO <= y <= _BOX_Y_HI:
            s["box"] += 1
        if kind in _PRESSURE_SHOT_KINDS:
            s["shots"] += 1
        elif kind == "corner":
            s["corners"] += 1

    actions = stats["home"]["actions"] + stats["away"]["actions"]
    final_third = stats["home"]["final_third"] + stats["away"]["final_third"]
    share_home = stats["home"]["final_third"] / final_third if final_third else None
    share_away = 1.0 - share_home if share_home is not None else None
    label, leader = _pressure_verdict(
        actions=actions, final_third=final_third, share_home=share_home
    )

    return {
        "window": window,
        "from_minute": lo,
        "to_minute": now_minute,
        "home": stats["home"],
        "away": stats["away"],
        "share": {
            "home": round(share_home, 3) if share_home is not None else None,
            "away": round(share_away, 3) if share_away is not None else None,
        },
        "label": label,
        "leader": leader,
        "series": _build_pressure_series(points, now_minute=now_minute, window=window),
    }


def _build_pressure_series(
    points: list[tuple[int, float, float, str, str | None]],
    *,
    now_minute: int,
    window: int = PRESSURE_WINDOW_MIN,
) -> list[dict[str, Any]]:
    """
    One rolling-window snapshot per minute from 1′ to ``now_minute``.

    Quiet minutes (too few actions) keep ``home``/``away`` null so the
    graph draws a gap instead of inventing a 50-50.
    """
    now_minute = max(1, min(90, int(now_minute)))
    n = now_minute + 1
    home_ft = [0] * n
    away_ft = [0] * n
    home_act = [0] * n
    away_act = [0] * n
    for minute, x, _y, side, _kind in points:
        if minute < 1 or minute > now_minute:
            continue
        if side == "home":
            home_act[minute] += 1
            if x >= _ATT_THIRD_X:
                home_ft[minute] += 1
        elif side == "away":
            away_act[minute] += 1
            if x >= _ATT_THIRD_X:
                away_ft[minute] += 1

    def _prefix(arr: list[int]) -> list[int]:
        out = [0] * n
        running = 0
        for i in range(1, n):
            running += arr[i]
            out[i] = running
        return out

    h_ft = _prefix(home_ft)
    a_ft = _prefix(away_ft)
    h_act = _prefix(home_act)
    a_act = _prefix(away_act)

    series: list[dict[str, Any]] = []
    for t in range(1, now_minute + 1):
        lo = max(1, t - window + 1)
        prev = lo - 1
        home_final = h_ft[t] - h_ft[prev]
        away_final = a_ft[t] - a_ft[prev]
        actions = (h_act[t] - h_act[prev]) + (a_act[t] - a_act[prev])
        final_third = home_final + away_final
        share_home = home_final / final_third if final_third else None
        label, leader = _pressure_verdict(
            actions=actions, final_third=final_third, share_home=share_home
        )
        readable = label != "quiet" and share_home is not None
        series.append(
            {
                "minute": t,
                "home": round(share_home, 3) if readable else None,
                "away": round(1.0 - share_home, 3) if readable else None,
                "label": label,
                "leader": leader,
                "final_third": {"home": home_final, "away": away_final},
            }
        )
    return series


def _cumulative_xg_series(
    points: list[tuple[int, float]],
) -> list[dict[str, Any]]:
    """Build step series from (minute, xg) shot points, starting at 0'."""
    series: list[dict[str, Any]] = [{"minute": 0, "xg": 0.0, "cumulative": 0.0}]
    total = 0.0
    for minute, xg in sorted(points, key=lambda t: t[0]):
        total = round(total + xg, 4)
        series.append({"minute": minute, "xg": round(xg, 4), "cumulative": total})
    return series


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
    home_score: int = 0,
    away_score: int = 0,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Build a 0–90' event strip + cumulative xG series.

    Markers: shots, blocked, shots on target, goals, corners (home above / away below).
    ``xg`` holds per-side cumulative expected-goals vs minute for the chart.
    ``elapsed_seconds`` is the best live clock for a client-side 1s cursor tick;
    ``frozen`` flags HT/FT-style clocks where the tick should pause.
    """
    cache_key = f"tl:{league_slug}:{event_id}"
    if use_cache and cache_key in _timeline_cache:
        ts, payload = _timeline_cache[cache_key]
        ttl = _TIMELINE_FINAL_TTL_S if payload.get("final") else _TIMELINE_TTL_S
        if time.time() - ts < ttl:
            return payload

    plays = fetch_all_plays(league_slug, event_id, fetcher=fetcher)
    board_minute = parse_clock_minute(clock, default=1)
    # Play clocks often lead the scoreboard displayClock by a minute or more —
    # never draw the "now" cursor behind events we are already plotting.
    play_seconds = [s for p in plays if (s := _play_elapsed_seconds(p)) is not None]
    latest_play_seconds = max(play_seconds) if play_seconds else None
    latest_play = (
        (latest_play_seconds // 60) + 1 if latest_play_seconds is not None else board_minute
    )
    minute = max(1, min(90, max(board_minute, latest_play)))

    board_seconds = clock_seconds
    if board_seconds is None:
        board_seconds = max(0, (board_minute - 1) * 60)
    elapsed_seconds = min(max(board_seconds, latest_play_seconds or 0), 99 * 60)

    clock_l = clock.lower()
    frozen = any(tok in clock_l for tok in _FROZEN_CLOCK_TOKENS)
    final = _is_final_clock(clock)
    events: list[dict[str, Any]] = []
    counts = {
        "shot": 0,
        "shot_on": 0,
        "blocked": 0,
        "goal": 0,
        "own_goal": 0,
        "corner": 0,
        "home_shot": 0,
        "away_shot": 0,
        "home_shot_on": 0,
        "away_shot_on": 0,
        "home_blocked": 0,
        "away_blocked": 0,
        "home_goal": 0,
        "away_goal": 0,
        "home_own_goal": 0,
        "away_own_goal": 0,
        "home_corner": 0,
        "away_corner": 0,
        "foul": 0,
        "home_foul": 0,
        "away_foul": 0,
    }
    home_xg_pts: list[tuple[int, float]] = []
    away_xg_pts: list[tuple[int, float]] = []
    territory_pts: list[tuple[float, float, str]] = []
    pressure_pts: list[tuple[int, float, float, str, str | None]] = []
    bulletin: list[dict[str, Any]] = []

    for play in plays:
        ptype = _normalize_play_type(_play_type(play))
        own = _is_own_goal(ptype, play)
        if own:
            ptype = "own-goal"
        pmin = _play_minute(play)
        tid = _team_id_from_play(play)
        side = (
            _side_for_own_goal(
                tid, home_id=home_id, away_id=away_id, home=home, away=away, play=play
            )
            if own
            else _side_for_team(
                tid, home_id=home_id, away_id=away_id, home=home, away=away, play=play
            )
        )
        xg = _safe_float(play.get("expectedGoals"))
        if (
            xg is not None
            and pmin is not None
            and side in {"home", "away"}
            and xg > 0
            and not own
        ):
            (home_xg_pts if side == "home" else away_xg_pts).append((pmin, xg))

        kind = _event_kind(ptype, scoring=bool(play.get("scoringPlay")))
        if own:
            kind = "own_goal"

        if side in {"home", "away"}:
            px = _coord(play, "fieldPositionX")
            py = _coord(play, "fieldPositionY")
            if px is not None and py is not None and (px or py):
                territory_pts.append((px, py, side))
                if pmin is not None:
                    pressure_pts.append((pmin, px, py, side, kind))

        if "foul" in ptype and side in {"home", "away"}:
            counts["foul"] += 1
            counts[f"{side}_foul"] += 1

        clock = _clock_label(play)
        elapsed = _play_elapsed_seconds(play)
        period = _play_period(play)
        if _is_substitution(ptype, play) and pmin is not None and side in {"home", "away"}:
            player_on, player_off = _sub_players(play)
            bulletin.append(
                {
                    "minute": pmin,
                    "elapsed": elapsed,
                    "clock": clock or f"{pmin}'",
                    "kind": "sub",
                    "team": side,
                    "player": player_on,
                    "player_on": player_on,
                    "player_off": player_off,
                    "penalty": False,
                    "text": str(play.get("shortText") or play.get("text") or "Substitution"),
                }
            )

        if not kind or pmin is None:
            continue
        text = str(play.get("shortText") or play.get("text") or kind)
        player = _player_name_from_goal(play, own=own) if kind in {"goal", "own_goal"} else None
        events.append(
            {
                "minute": pmin,
                "kind": kind,
                "type": ptype,
                "team": side,
                "text": text,
                "clock": clock,
                "xg": xg,
                "player": player,
                "period": period,
            }
        )
        if kind in {"goal", "own_goal"} and side in {"home", "away"}:
            bulletin.append(
                {
                    "minute": pmin,
                    "elapsed": elapsed,
                    "clock": clock or f"{pmin}'",
                    "kind": kind,
                    "team": side,
                    "player": player,
                    "player_on": None,
                    "player_off": None,
                    "penalty": _is_penalty_goal(ptype, play),
                    "text": text,
                }
            )
        counts[kind] += 1
        if side in {"home", "away"}:
            counts[f"{side}_{kind}"] = counts.get(f"{side}_{kind}", 0) + 1

    events.sort(key=lambda e: (e["minute"], e["kind"]))
    bulletin.sort(key=_bulletin_sort_key)
    play_home = int(counts.get("home_goal") or 0) + int(counts.get("home_own_goal") or 0)
    play_away = int(counts.get("away_goal") or 0) + int(counts.get("away_own_goal") or 0)
    # Plays often land a goal before the scoreboard tick — never show a
    # chiclet score behind bars already drawn on the strip.
    resolved_home = max(int(home_score or 0), play_home)
    resolved_away = max(int(away_score or 0), play_away)
    home_series = _cumulative_xg_series(home_xg_pts)
    away_series = _cumulative_xg_series(away_xg_pts)
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
        "final": final,
        "max_minute": 90,
        "board_home_score": int(home_score or 0),
        "board_away_score": int(away_score or 0),
        "play_home_score": play_home,
        "play_away_score": play_away,
        "home_score": resolved_home,
        "away_score": resolved_away,
        "events": events,
        "bulletin": bulletin,
        "counts": counts,
        "territory": _build_territory(territory_pts),
        "pressure": _build_pressure(pressure_pts, now_minute=minute),
        "box_entries": box_entries_from_plays(plays, home_id=home_id, away_id=away_id, home=home, away=away),
        "xg": {
            "home": home_series,
            "away": away_series,
            "home_total": home_series[-1]["cumulative"] if home_series else 0.0,
            "away_total": away_series[-1]["cumulative"] if away_series else 0.0,
        },
        "fetched_at": time.time(),
    }
    if use_cache:
        _timeline_cache[cache_key] = (time.time(), payload)
    return payload


def clear_timeline_cache() -> None:
    _timeline_cache.clear()


# —— Lineups: formation, XI, subs and a match power number ——

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/summary?event={event_id}"

_lineup_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_LINEUP_TTL_S = 60.0
_LINEUP_FINAL_TTL_S = 15 * 60.0

# Stats ESPN files per player in the match summary (name → payload key).
_LINEUP_STATS = {
    "totalGoals": "goals",
    "goalAssists": "assists",
    "totalShots": "shots",
    "shotsOnTarget": "sot",
    "saves": "saves",
    "goalsConceded": "goals_conceded",
    "foulsCommitted": "fouls",
    "foulsSuffered": "fouls_suffered",
    "yellowCards": "yellow",
    "redCards": "red",
    "offsides": "offsides",
}


def _lineup_stat_values(entry: dict[str, Any]) -> dict[str, int]:
    out = {v: 0 for v in _LINEUP_STATS.values()}
    for stat in entry.get("stats") or []:
        key = _LINEUP_STATS.get(str(stat.get("name") or ""))
        if key is None:
            continue
        try:
            out[key] = int(float(stat.get("value") or 0))
        except (TypeError, ValueError):
            continue
    return out


def player_power(stats: dict[str, int], *, is_keeper: bool) -> int:
    """
    FIFA-flavoured match power on a 40–99 scale, from ESPN's per-player match
    stats (no tracking data exists in the feed): 65 = quiet-but-fine game.
    """
    score = 65.0
    score += 12.0 * stats.get("goals", 0)
    score += 8.0 * stats.get("assists", 0)
    sot = stats.get("sot", 0)
    score += 4.0 * sot
    score += 1.5 * max(0, stats.get("shots", 0) - sot)
    score += 1.0 * stats.get("fouls_suffered", 0)
    score -= 1.5 * stats.get("fouls", 0)
    score -= 1.0 * stats.get("offsides", 0)
    score -= 4.0 * stats.get("yellow", 0)
    score -= 12.0 * stats.get("red", 0)
    if is_keeper:
        score += 3.0 * stats.get("saves", 0)
        score -= 3.0 * stats.get("goals_conceded", 0)
    return int(round(max(40.0, min(99.0, score))))


def _sub_minute(entry: dict[str, Any]) -> int | None:
    for play in entry.get("plays") or []:
        if play.get("substitution"):
            clock = play.get("clock") or {}
            label = str(clock.get("displayValue") or "") if isinstance(clock, dict) else str(clock)
            if label:
                return parse_clock_minute(label, default=0) or None
    return None


def _short_athlete(entry: dict[str, Any]) -> str:
    athlete = entry.get("athlete") or {}
    return str(athlete.get("shortName") or athlete.get("displayName") or "")


def _parse_side_roster(side: dict[str, Any]) -> dict[str, Any]:
    entries = side.get("roster") or []
    formation = str(side.get("formation") or "")
    # Starter places 1–11; substitutes inherit the place of whoever they replaced
    # (chains resolve because each hop lands on a starter or an earlier sub).
    place_by_jersey: dict[str, int] = {}
    for e in entries:
        try:
            place = int(e.get("formationPlace") or 0)
        except (TypeError, ValueError):
            place = 0
        if place > 0:
            place_by_jersey[str(e.get("jersey") or "")] = place

    def resolve_place(entry: dict[str, Any], hops: int = 0) -> int:
        try:
            place = int(entry.get("formationPlace") or 0)
        except (TypeError, ValueError):
            place = 0
        if place > 0 or hops > 4:
            return place
        replaced = entry.get("subbedInFor") or {}
        jersey = str(replaced.get("jersey") or "")
        if jersey in place_by_jersey:
            return place_by_jersey[jersey]
        for other in entries:
            if str(other.get("jersey") or "") == jersey:
                return resolve_place(other, hops + 1)
        return 0

    players: list[dict[str, Any]] = []
    for e in entries:
        starter = bool(e.get("starter"))
        sub_in = bool(e.get("subbedIn"))
        sub_out = bool(e.get("subbedOut"))
        if not starter and not sub_in:
            continue  # unused bench player
        pos = e.get("position") or {}
        pos_abbr = str(pos.get("abbreviation") or "")
        is_keeper = pos_abbr.upper().startswith("G") or str(pos.get("name") or "").lower() == "goalkeeper"
        stats = _lineup_stat_values(e)
        minute = _sub_minute(e)
        place = resolve_place(e)
        if sub_in and place > 0:
            place_by_jersey.setdefault(str(e.get("jersey") or ""), place)
        players.append(
            {
                "name": str((e.get("athlete") or {}).get("displayName") or ""),
                "short": _short_athlete(e),
                "jersey": str(e.get("jersey") or ""),
                "pos": pos_abbr,
                "place": place,
                "starter": starter,
                "on_pitch": not sub_out,
                "in_minute": minute if sub_in else 0,
                "out_minute": minute if sub_out else None,
                "sub_for": _short_athlete(e.get("subbedInFor") or {}) if sub_in else "",
                "sub_by": _short_athlete(e.get("subbedOutFor") or {}) if sub_out else "",
                "stats": stats,
                "power": player_power(stats, is_keeper=is_keeper),
            }
        )
    return {
        "team": str((side.get("team") or {}).get("displayName") or ""),
        "formation": formation,
        "players": players,
    }


def build_lineups(
    league_slug: str,
    event_id: str,
    *,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Formation + XI per side from the ESPN match summary: who is on the pitch,
    where they line up, sub in/out minutes and replacements, per-player match
    stats and the derived power number. ``available`` is False before ESPN
    publishes lineups (roughly an hour before kickoff).
    """
    cache_key = f"lu:{league_slug}:{event_id}"
    if use_cache and cache_key in _lineup_cache:
        ts, payload = _lineup_cache[cache_key]
        ttl = _LINEUP_FINAL_TTL_S if payload.get("final") else _LINEUP_TTL_S
        if time.time() - ts < ttl:
            return payload

    fetch = fetcher or _fetch_json
    url = SUMMARY_URL.format(
        league=urllib.parse.quote(league_slug, safe="."),
        event_id=urllib.parse.quote(str(event_id), safe=""),
    )
    try:
        data = fetch(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        data = {}

    sides = {str(r.get("homeAway") or ""): r for r in data.get("rosters") or []}
    home = _parse_side_roster(sides.get("home") or {})
    away = _parse_side_roster(sides.get("away") or {})
    status = {}
    try:
        status = ((data.get("header") or {}).get("competitions") or [{}])[0].get("status") or {}
    except (AttributeError, IndexError, TypeError):
        status = {}
    final = str((status.get("type") or {}).get("state") or "") == "post"
    payload = {
        "event_id": str(event_id),
        "league_slug": league_slug,
        "available": bool(home["players"] or away["players"]),
        "final": final,
        "home": home,
        "away": away,
        "fetched_at": time.time(),
    }
    if use_cache and payload["available"]:
        _lineup_cache[cache_key] = (time.time(), payload)
    return payload


def clear_lineup_cache() -> None:
    _lineup_cache.clear()
