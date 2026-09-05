"""Pre-match win probabilities: Poisson model, backtested record, scheduled fixtures."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from eeesoc.models import Match
from eeesoc.teams import resolve_team

USER_AGENT = "eeesoc/0.1 (+https://github.com/tekneeq/eeesoc)"
SITE_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={date}"
)
DEFAULT_LEAGUE_SLUG = "eng.1"
DEFAULT_LEAGUE_CHICLET = "EPL"

MAX_GOALS = 10
PREV_SEASON_WEIGHT = 0.5
# Shrink small-sample attack/defence multipliers toward league average.
SHRINK_GAMES = 5.0
RECORD_WINDOW_DAYS = 30


def _fetch_json(url: str, timeout: float = 12.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def parse_fd_date(raw: str | None) -> date | None:
    """football-data dates are DD/MM/YYYY (older files DD/MM/YY)."""
    text = (raw or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _is_model_match(m: Match) -> bool:
    return ":preset:" not in m.match_id and parse_fd_date(m.date) is not None


# —— Poisson attack/defence ratings ——


@dataclass(frozen=True)
class Ratings:
    home_goals_avg: float  # league avg goals by the home side
    away_goals_avg: float  # league avg goals by the away side
    attack: dict[str, float]
    defence: dict[str, float]
    games: dict[str, float]  # weighted matches per team


def build_ratings(weighted: list[tuple[Match, float]]) -> Ratings:
    """Weighted attack/defence multipliers vs league average, with shrinkage."""
    played: dict[str, float] = {}
    scored: dict[str, float] = {}
    conceded: dict[str, float] = {}
    total_w = 0.0
    home_goals = 0.0
    away_goals = 0.0

    for m, w in weighted:
        if w <= 0:
            continue
        total_w += w
        home_goals += w * m.home_goals_ft
        away_goals += w * m.away_goals_ft
        for team, gf, ga in (
            (m.home, m.home_goals_ft, m.away_goals_ft),
            (m.away, m.away_goals_ft, m.home_goals_ft),
        ):
            played[team] = played.get(team, 0.0) + w
            scored[team] = scored.get(team, 0.0) + w * gf
            conceded[team] = conceded.get(team, 0.0) + w * ga

    if total_w <= 0:
        return Ratings(1.4, 1.1, {}, {}, {})

    home_avg = home_goals / total_w
    away_avg = away_goals / total_w
    league_rate = (home_goals + away_goals) / (2 * total_w) or 1.0

    attack: dict[str, float] = {}
    defence: dict[str, float] = {}
    for team, n in played.items():
        raw_att = (scored[team] / n) / league_rate if league_rate else 1.0
        raw_def = (conceded[team] / n) / league_rate if league_rate else 1.0
        blend = n / (n + SHRINK_GAMES)
        attack[team] = _clamp(1.0 + (raw_att - 1.0) * blend, 0.25, 4.0)
        defence[team] = _clamp(1.0 + (raw_def - 1.0) * blend, 0.25, 4.0)

    return Ratings(home_avg, away_avg, attack, defence, played)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def expected_goals(ratings: Ratings, home: str, away: str) -> tuple[float, float]:
    lam_home = ratings.home_goals_avg * ratings.attack.get(home, 1.0) * ratings.defence.get(away, 1.0)
    lam_away = ratings.away_goals_avg * ratings.attack.get(away, 1.0) * ratings.defence.get(home, 1.0)
    return max(0.05, lam_home), max(0.05, lam_away)


def _poisson_row(lam: float, max_goals: int = MAX_GOALS) -> list[float]:
    return [math.exp(-lam) * lam**k / math.factorial(k) for k in range(max_goals + 1)]


def outcome_probs(lam_home: float, lam_away: float) -> dict[str, float]:
    """P(home win / draw / away win) from independent Poisson goal counts."""
    hp = _poisson_row(lam_home)
    ap = _poisson_row(lam_away)
    p_home = p_draw = p_away = 0.0
    for h, ph in enumerate(hp):
        for a, pa in enumerate(ap):
            p = ph * pa
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
    total = p_home + p_draw + p_away
    return {
        "home": round(p_home / total, 4),
        "draw": round(p_draw / total, 4),
        "away": round(p_away / total, 4),
    }


def predict_fixture(ratings: Ratings, home: str, away: str) -> dict[str, Any]:
    lam_h, lam_a = expected_goals(ratings, home, away)
    probs = outcome_probs(lam_h, lam_a)
    pick = max(probs, key=probs.get)
    return {
        "probs": probs,
        "pick": pick,
        "pick_prob": probs[pick],
        "lambda_home": round(lam_h, 3),
        "lambda_away": round(lam_a, 3),
        "known_home": home in ratings.games,
        "known_away": away in ratings.games,
    }


# —— Backtest: grade the model on every completed match ——


def _actual_outcome(m: Match) -> str:
    if m.home_goals_ft > m.away_goals_ft:
        return "home"
    if m.home_goals_ft < m.away_goals_ft:
        return "away"
    return "draw"


def backtest_predictions(current: list[Match], history: list[Match]) -> list[dict[str, Any]]:
    """
    Walk the current season chronologically; predict each matchday using only
    matches played before it (previous season down-weighted as a prior).
    """
    playable = [m for m in current if _is_model_match(m)]
    playable.sort(key=lambda m: parse_fd_date(m.date))  # type: ignore[arg-type]

    weighted: list[tuple[Match, float]] = [
        (m, PREV_SEASON_WEIGHT) for m in history if _is_model_match(m)
    ]
    rows: list[dict[str, Any]] = []

    i = 0
    while i < len(playable):
        day = parse_fd_date(playable[i].date)
        group: list[Match] = []
        while i < len(playable) and parse_fd_date(playable[i].date) == day:
            group.append(playable[i])
            i += 1

        ratings = build_ratings(weighted)
        for m in group:
            pred = predict_fixture(ratings, m.home, m.away)
            actual = _actual_outcome(m)
            pick = pred["pick"]
            rows.append(
                {
                    "date": day.isoformat() if day else "",
                    "home": m.home,
                    "away": m.away,
                    "ft": f"{m.home_goals_ft}-{m.away_goals_ft}",
                    "probs": pred["probs"],
                    "pick": pick,
                    "pick_team": m.home if pick == "home" else (m.away if pick == "away" else "Draw"),
                    "pick_prob": pred["pick_prob"],
                    "actual": actual,
                    "correct": pick == actual,
                }
            )
        weighted.extend((m, 1.0) for m in group)

    return rows


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    return {
        "correct": correct,
        "wrong": total - correct,
        "total": total,
        "pct": round(correct / total, 3) if total else None,
    }


def summarize_record(
    rows: list[dict[str, Any]], *, today: date, window_days: int = RECORD_WINDOW_DAYS
) -> dict[str, Any]:
    cutoff = (today - timedelta(days=window_days)).isoformat()
    recent = [r for r in rows if r["date"] >= cutoff]
    return {
        "window_days": window_days,
        "last30": _summarize(recent),
        "season": _summarize(rows),
    }


# —— Scheduled fixtures via ESPN site scoreboard ——


def parse_site_scoreboard(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        comps = event.get("competitions") or []
        comp = comps[0] if comps else {}
        status = comp.get("status") or event.get("status") or {}
        stype = status.get("type") or {}
        state = str(stype.get("state") or "pre")

        home = away = "?"
        home_id = away_id = ""
        for team in comp.get("competitors") or []:
            tmeta = team.get("team") or {}
            name = tmeta.get("displayName") or tmeta.get("shortDisplayName") or "?"
            tid = str(tmeta.get("id") or team.get("id") or "")
            if team.get("homeAway") == "home":
                home, home_id = name, tid
            elif team.get("homeAway") == "away":
                away, away_id = name, tid

        out.append(
            {
                "event_id": str(event.get("id") or comp.get("id") or f"{home}:{away}"),
                "start": str(event.get("date") or comp.get("date") or ""),
                "state": state,
                "detail": str(stype.get("detail") or stype.get("shortDetail") or ""),
                "home": home,
                "away": away,
                "home_id": home_id,
                "away_id": away_id,
            }
        )
    return out


def fetch_scheduled_fixtures(
    *,
    days: int = 8,
    league: str = DEFAULT_LEAGUE_SLUG,
    today: date | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Scheduled (state == pre) fixtures for today .. today+days-1, deduped."""
    fetch = fetcher or _fetch_json
    start = today or date.today()
    seen: dict[str, dict[str, Any]] = {}
    for offset in range(max(1, days)):
        d = start + timedelta(days=offset)
        url = SITE_SCOREBOARD.format(
            league=urllib.parse.quote(league, safe="."), date=d.strftime("%Y%m%d")
        )
        try:
            payload = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        for fx in parse_site_scoreboard(payload):
            if fx["state"] != "pre":
                continue
            seen.setdefault(fx["event_id"], fx)
    return sorted(seen.values(), key=lambda f: (f["start"], f["home"]))


# —— Team form / detail ——


def _team_rows(team: str, matches: list[Match]) -> list[dict[str, Any]]:
    rows = []
    for m in matches:
        if not _is_model_match(m) or team not in (m.home, m.away):
            continue
        is_home = m.home == team
        gf = m.home_goals_ft if is_home else m.away_goals_ft
        ga = m.away_goals_ft if is_home else m.home_goals_ft
        rows.append(
            {
                "date": parse_fd_date(m.date).isoformat(),  # type: ignore[union-attr]
                "season": m.season,
                "home": m.home,
                "away": m.away,
                "venue": "H" if is_home else "A",
                "opponent": m.away if is_home else m.home,
                "ft": f"{m.home_goals_ft}-{m.away_goals_ft}",
                "result": "W" if gf > ga else ("L" if gf < ga else "D"),
                "gf": gf,
                "ga": ga,
            }
        )
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def team_form(
    team: str, current: list[Match], history: list[Match], *, limit: int = 5
) -> dict[str, Any]:
    """Last N games (current season first, padded from last season) + season record."""
    season_rows = _team_rows(team, current)
    last = season_rows[:limit]
    if len(last) < limit:
        last = last + _team_rows(team, history)[: limit - len(last)]

    wins = sum(1 for r in season_rows if r["result"] == "W")
    draws = sum(1 for r in season_rows if r["result"] == "D")
    losses = sum(1 for r in season_rows if r["result"] == "L")
    return {
        "team": team,
        "last5": last,
        "season": {
            "played": len(season_rows),
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "gf": sum(r["gf"] for r in season_rows),
            "ga": sum(r["ga"] for r in season_rows),
        },
    }


def head_to_head(
    home: str, away: str, current: list[Match], history: list[Match], *, limit: int = 5
) -> list[dict[str, Any]]:
    rows = []
    for m in [*current, *history]:
        if not _is_model_match(m):
            continue
        if {m.home, m.away} == {home, away}:
            rows.append(
                {
                    "date": parse_fd_date(m.date).isoformat(),  # type: ignore[union-attr]
                    "season": m.season,
                    "home": m.home,
                    "away": m.away,
                    "ft": f"{m.home_goals_ft}-{m.away_goals_ft}",
                }
            )
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:limit]


# —— Board assembly (what the WinProb tab polls) ——

_board_cache: tuple[float, str, dict[str, Any]] | None = None
_BOARD_TTL_S = 60.0


def _corpus_signature(current: list[Match], history: list[Match], today: date) -> str:
    return f"{len(current)}:{len(history)}:{today.isoformat()}"


def build_winprob_board(
    current: list[Match],
    history: list[Match],
    *,
    days: int = 8,
    today: date | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Scheduled fixtures + model probabilities + backtested record."""
    global _board_cache
    today = today or date.today()
    sig = _corpus_signature(current, history, today)
    if use_cache and _board_cache is not None:
        ts, cached_sig, payload = _board_cache
        if cached_sig == sig and time.time() - ts < _BOARD_TTL_S:
            return payload

    graded = backtest_predictions(current, history)
    record = summarize_record(graded, today=today)

    weighted = [(m, PREV_SEASON_WEIGHT) for m in history if _is_model_match(m)]
    weighted += [(m, 1.0) for m in current if _is_model_match(m)]
    ratings = build_ratings(weighted)

    fixtures = []
    for fx in fetch_scheduled_fixtures(days=days, today=today, fetcher=fetcher):
        home_fd = resolve_team(fx["home"], espn_id=fx["home_id"])
        away_fd = resolve_team(fx["away"], espn_id=fx["away_id"])
        entry: dict[str, Any] = {
            **fx,
            "league_chiclet": DEFAULT_LEAGUE_CHICLET,
            "home_fd": home_fd,
            "away_fd": away_fd,
        }
        if home_fd and away_fd:
            pred = predict_fixture(ratings, home_fd, away_fd)
            entry.update(pred)
            entry["pick_team"] = (
                home_fd if pred["pick"] == "home" else (away_fd if pred["pick"] == "away" else "Draw")
            )
        else:
            entry["probs"] = None
            entry["pick"] = None
        fixtures.append(entry)

    payload = {
        "fetched_at": time.time(),
        "league": DEFAULT_LEAGUE_CHICLET,
        "days": days,
        "record": record,
        "recent_results": list(reversed(graded[-20:])),
        "fixtures": fixtures,
    }
    if use_cache:
        _board_cache = (time.time(), sig, payload)
    return payload


def clear_winprob_cache() -> None:
    global _board_cache
    _board_cache = None


def build_fixture_detail(
    home_name: str,
    away_name: str,
    current: list[Match],
    history: list[Match],
    *,
    home_id: str = "",
    away_id: str = "",
) -> dict[str, Any]:
    """Double-click payload: last-5 form for both clubs, H2H, and the prediction."""
    home_fd = resolve_team(home_name, espn_id=home_id)
    away_fd = resolve_team(away_name, espn_id=away_id)
    if not home_fd or not away_fd:
        return {
            "error": "team not mapped to EPL history",
            "home_fd": home_fd,
            "away_fd": away_fd,
        }

    weighted = [(m, PREV_SEASON_WEIGHT) for m in history if _is_model_match(m)]
    weighted += [(m, 1.0) for m in current if _is_model_match(m)]
    ratings = build_ratings(weighted)
    pred = predict_fixture(ratings, home_fd, away_fd)

    return {
        "home_fd": home_fd,
        "away_fd": away_fd,
        "prediction": {
            **pred,
            "pick_team": (
                home_fd if pred["pick"] == "home" else (away_fd if pred["pick"] == "away" else "Draw")
            ),
        },
        "home_form": team_form(home_fd, current, history),
        "away_form": team_form(away_fd, current, history),
        "h2h": head_to_head(home_fd, away_fd, current, history),
    }
