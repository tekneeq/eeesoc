"""Local HTTP dashboard: Matches + Similar."""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from eeesoc.data import load_season, previous_season_label
from eeesoc.live import build_event_timeline, build_live_situation, build_pitch_track, fetch_live_board
from eeesoc.models import Match, MatchSnapshot
from eeesoc.scorelines import build_live_scoreline_eval, score_path
from eeesoc.similar import find_similar, opponent_scored_context
from eeesoc.teams import resolve_team
from eeesoc.winprob import build_fixture_detail, build_winprob_board, fetch_scheduled_fixtures, parse_fd_date

STATIC_DIR = Path(__file__).resolve().parent / "static"

EVERTON_PRESET_RE = re.compile(r"preset:Everton:53")

_MATCHES_SCHED_TTL_S = 60.0
_matches_sched_cache: tuple[float, str, list[dict[str, Any]]] | None = None


def _is_preset(match_id: str) -> bool:
    return bool(EVERTON_PRESET_RE.search(match_id or ""))


def _fixture_day(start: str) -> date | None:
    text = (start or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _serialize_match(m: Match, *, today: date) -> dict[str, Any]:
    parsed = parse_fd_date(m.date)
    return {
        "match_id": m.match_id,
        "date": m.date,
        "iso_date": parsed.isoformat() if parsed else "",
        "home": m.home,
        "away": m.away,
        "ft": f"{m.home_goals_ft}-{m.away_goals_ft}",
        "shots": f"{m.home_shots_ft}/{m.home_sot_ft} vs {m.away_shots_ft}/{m.away_sot_ft}",
        "is_preset": _is_preset(m.match_id),
        "is_today": parsed == today if parsed else False,
        "scheduled": False,
        "start": "",
    }


def build_match_rows(
    matches: list[Match],
    *,
    today: date | None = None,
    scheduled: list[dict[str, Any]] | None = None,
    include_presets: bool = False,
) -> list[dict[str, Any]]:
    """Season fixtures with today's slate first, then newest remaining dates."""
    today = today or date.today()
    rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for m in matches:
        if not include_presets and _is_preset(m.match_id):
            continue
        row = _serialize_match(m, today=today)
        rows.append(row)
        if row["iso_date"]:
            seen_keys.add((row["home"].lower(), row["away"].lower(), row["iso_date"]))

    for fx in scheduled or []:
        start = str(fx.get("start") or "")
        home = str(fx.get("home_fd") or fx.get("home") or "")
        away = str(fx.get("away_fd") or fx.get("away") or "")
        if not home or not away:
            continue
        day = _fixture_day(start)
        if day is None or abs((day - today).days) > 1:
            continue
        iso = day.isoformat()
        key = (home.lower(), away.lower(), iso)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append(
            {
                "match_id": "",
                "date": day.strftime("%d/%m/%Y"),
                "iso_date": iso,
                "home": home,
                "away": away,
                "ft": "—",
                "shots": "scheduled",
                "is_preset": False,
                "is_today": day == today,
                "scheduled": True,
                "start": start,
            }
        )

    today_rows = [r for r in rows if r["is_today"]]
    other_rows = [r for r in rows if not r["is_today"]]
    today_rows.sort(key=lambda r: (r.get("start") or "", r.get("home") or ""))
    other_rows.sort(key=lambda r: (r.get("iso_date") or "", r.get("home") or ""), reverse=True)
    return today_rows + other_rows


def load_nearby_scheduled(today: date | None = None) -> list[dict[str, Any]]:
    """Yesterday..tomorrow scheduled EPL fixtures (timezone buffer), cached briefly."""
    global _matches_sched_cache
    today = today or date.today()
    key = today.isoformat()
    if _matches_sched_cache is not None:
        ts, cached_key, cached = _matches_sched_cache
        if cached_key == key and time.time() - ts < _MATCHES_SCHED_TTL_S:
            return cached
    out: list[dict[str, Any]] = []
    try:
        for fx in fetch_scheduled_fixtures(days=3, today=today - timedelta(days=1)):
            home_fd = resolve_team(fx["home"], espn_id=fx.get("home_id")) or fx["home"]
            away_fd = resolve_team(fx["away"], espn_id=fx.get("away_id")) or fx["away"]
            out.append({**fx, "home_fd": home_fd, "away_fd": away_fd})
    except Exception:  # noqa: BLE001 — Matches tab still lists the cached season
        out = []
    _matches_sched_cache = (time.time(), key, out)
    return out


class DashboardState:
    def __init__(self, season: str) -> None:
        self.season = season
        self.reload()

    def reload(self) -> None:
        self.matches: list[Match] = load_season(self.season)
        self.by_id = {m.match_id: m for m in self.matches}
        try:
            prev = previous_season_label(self.season)
            self.history: list[Match] = load_season(prev)
        except ValueError:
            self.history = []
        self.corpus = [*self.history, *self.matches]


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def make_handler(state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # quieter
            sys_stderr = __import__("sys").stderr
            sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                html = (STATIC_DIR / "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/static/app.css":
                return self._send(200, (STATIC_DIR / "app.css").read_bytes(), "text/css; charset=utf-8")
            if path == "/static/app.js":
                return self._send(
                    200, (STATIC_DIR / "app.js").read_bytes(), "application/javascript; charset=utf-8"
                )

            if path == "/api/meta":
                preset = next((m for m in state.matches if EVERTON_PRESET_RE.search(m.match_id)), None)
                return self._send(
                    200,
                    _json_bytes(
                        {
                            "season": state.season,
                            "match_count": len([m for m in state.matches if not _is_preset(m.match_id)]),
                            "history_count": len(state.history),
                            "everton_preset_id": preset.match_id if preset else None,
                        }
                    ),
                    "application/json",
                )

            if path == "/health":
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")

            if path == "/api/winprob":
                days = int((qs.get("days") or ["8"])[0])
                board = build_winprob_board(state.matches, state.history, days=days)
                return self._send(200, _json_bytes(board), "application/json")

            if path == "/api/winprob/detail":
                home = (qs.get("home") or [None])[0]
                away = (qs.get("away") or [None])[0]
                if not home or not away:
                    return self._send(
                        400, _json_bytes({"error": "home and away required"}), "application/json"
                    )
                detail = build_fixture_detail(
                    home,
                    away,
                    state.matches,
                    state.history,
                    home_id=(qs.get("home_id") or [""])[0],
                    away_id=(qs.get("away_id") or [""])[0],
                )
                code = 404 if detail.get("error") else 200
                return self._send(code, _json_bytes(detail), "application/json")

            if path == "/api/live":
                live_only = (qs.get("live_only") or ["1"])[0] not in ("0", "false", "no")
                board = fetch_live_board(live_only=live_only)
                return self._send(200, _json_bytes(board), "application/json")

            if path == "/api/live/track":
                league = (qs.get("league") or [None])[0]
                event_id = (qs.get("event_id") or [None])[0]
                if not league or not event_id:
                    return self._send(
                        400, _json_bytes({"error": "league and event_id required"}), "application/json"
                    )
                track = build_pitch_track(
                    league,
                    event_id,
                    home=(qs.get("home") or [""])[0],
                    away=(qs.get("away") or [""])[0],
                    home_score=int((qs.get("hs") or ["0"])[0] or 0),
                    away_score=int((qs.get("as") or ["0"])[0] or 0),
                    clock=(qs.get("clock") or [""])[0],
                    league_chiclet=(qs.get("chiclet") or [""])[0],
                )
                return self._send(200, _json_bytes(track), "application/json")

            if path == "/api/live/timeline":
                league = (qs.get("league") or [None])[0]
                event_id = (qs.get("event_id") or [None])[0]
                if not league or not event_id:
                    return self._send(
                        400, _json_bytes({"error": "league and event_id required"}), "application/json"
                    )
                clock_seconds = None
                raw_cs = (qs.get("clock_s") or [""])[0]
                if raw_cs.strip().isdigit():
                    clock_seconds = int(raw_cs)
                def _qint(key: str, default: int = 0) -> int:
                    raw = (qs.get(key) or [""])[0]
                    try:
                        return int(raw)
                    except (TypeError, ValueError):
                        return default

                timeline = build_event_timeline(
                    league,
                    event_id,
                    home=(qs.get("home") or [""])[0],
                    away=(qs.get("away") or [""])[0],
                    home_id=(qs.get("home_id") or [""])[0],
                    away_id=(qs.get("away_id") or [""])[0],
                    clock=(qs.get("clock") or [""])[0],
                    clock_seconds=clock_seconds,
                    home_score=_qint("hs"),
                    away_score=_qint("as"),
                )
                return self._send(200, _json_bytes(timeline), "application/json")

            if path == "/api/live/similar":
                league = (qs.get("league") or [None])[0]
                event_id = (qs.get("event_id") or [None])[0]
                if not league or not event_id:
                    return self._send(
                        400, _json_bytes({"error": "league and event_id required"}), "application/json"
                    )
                limit = int((qs.get("limit") or ["12"])[0])
                window = int((qs.get("window") or ["5"])[0])
                situation = build_live_situation(
                    league,
                    event_id,
                    home=(qs.get("home") or [""])[0],
                    away=(qs.get("away") or [""])[0],
                    home_score=int((qs.get("hs") or ["0"])[0] or 0),
                    away_score=int((qs.get("as") or ["0"])[0] or 0),
                    clock=(qs.get("clock") or [""])[0],
                    league_chiclet=(qs.get("chiclet") or [""])[0],
                    home_id=(qs.get("home_id") or [""])[0],
                    away_id=(qs.get("away_id") or [""])[0],
                )
                snap_data = situation["snapshot"]
                snap = MatchSnapshot(
                    minute=int(snap_data["minute"]),
                    home_goals=int(snap_data["home_goals"]),
                    away_goals=int(snap_data["away_goals"]),
                    home_shots=int(snap_data["home_shots"]),
                    away_shots=int(snap_data["away_shots"]),
                    home_sot=int(snap_data["home_sot"]),
                    away_sot=int(snap_data["away_sot"]),
                    goal_minutes=tuple(int(m) for m in snap_data.get("goal_minutes") or ()),
                )
                hits = find_similar(snap, state.corpus, limit=limit)
                latest = situation.get("latest_goal")
                concede = None
                if latest and latest.get("team") in {"home", "away"}:
                    concede = opponent_scored_context(
                        state.corpus,
                        goal_minute=int(latest["minute"]),
                        scored_by=str(latest["team"]),
                        window=window,
                        limit=limit,
                    )
                    concede = {
                        **concede,
                        "live_my_name": latest.get("conceded_by_name"),
                        "live_opp_name": latest.get("team_name"),
                        "live_my_shots": latest.get("my_shots"),
                        "live_my_sot": latest.get("my_sot"),
                        "live_opp_shots": latest.get("scorer_shots"),
                        "live_opp_sot": latest.get("scorer_sot"),
                        "live_goal_text": latest.get("text"),
                    }

                hs = int(situation.get("home_score", snap.home_goals) or 0)
                aws = int(situation.get("away_score", snap.away_goals) or 0)
                live_minute = int(situation.get("minute") or 1)
                prev_h = prev_a = None
                prev_minute = None
                goals = situation.get("goals") or []
                if goals:
                    last = goals[-1]
                    try:
                        prev_minute = max(0, int(last.get("minute")) - 1)
                    except (TypeError, ValueError):
                        prev_minute = None
                    # Prefer cumulative scores on the goal event (robust to board lag).
                    try:
                        gh = int(last.get("home_goals"))
                        ga = int(last.get("away_goals"))
                        if last.get("team") == "home":
                            prev_h, prev_a = max(0, gh - 1), ga
                        elif last.get("team") == "away":
                            prev_h, prev_a = gh, max(0, ga - 1)
                        # Align "now" with the last scored state when board disagrees.
                        if gh + ga >= hs + aws:
                            hs, aws = gh, ga
                    except (TypeError, ValueError):
                        if last.get("team") == "home":
                            prev_h, prev_a = max(0, hs - 1), aws
                        elif last.get("team") == "away":
                            prev_h, prev_a = hs, max(0, aws - 1)

                scorelines = build_live_scoreline_eval(
                    state.corpus,
                    home_name=str(situation.get("home") or ""),
                    away_name=str(situation.get("away") or ""),
                    home_score=hs,
                    away_score=aws,
                    home_id=str(situation.get("home_id") or (qs.get("home_id") or [""])[0]),
                    away_id=str(situation.get("away_id") or (qs.get("away_id") or [""])[0]),
                    prev_home=prev_h,
                    prev_away=prev_a,
                    minute=live_minute,
                    prev_minute=prev_minute,
                    limit_peers=min(8, limit),
                )
                return self._send(
                    200,
                    _json_bytes(
                        {
                            "situation": situation,
                            "query": snap.to_dict(),
                            "label": situation["label"],
                            "hits": [h.to_dict() for h in hits],
                            "opponent_scored": concede,
                            "scorelines": scorelines,
                        }
                    ),
                    "application/json",
                )
            if path == "/api/matches":
                today = date.today()
                rows = build_match_rows(
                    state.matches,
                    today=today,
                    scheduled=load_nearby_scheduled(today),
                )
                return self._send(
                    200,
                    _json_bytes({"matches": rows, "today": today.isoformat()}),
                    "application/json",
                )

            if path == "/api/snapshot":
                mid = (qs.get("match_id") or [None])[0]
                minute = int((qs.get("minute") or ["53"])[0])
                match = state.by_id.get(mid or "")
                if not match:
                    return self._send(404, _json_bytes({"error": "match not found"}), "application/json")
                snap = match.snapshot_at(minute)
                return self._send(
                    200,
                    _json_bytes(
                        {
                            "match_id": match.match_id,
                            "home": match.home,
                            "away": match.away,
                            "snapshot": snap.to_dict(),
                            "label": snap.label(),
                        }
                    ),
                    "application/json",
                )

            if path == "/api/similar":
                mid = (qs.get("match_id") or [None])[0]
                minute = int((qs.get("minute") or ["53"])[0])
                limit = int((qs.get("limit") or ["12"])[0])
                match = state.by_id.get(mid or "")
                if match:
                    snap = match.snapshot_at(minute)
                    exclude = {match.match_id}
                else:
                    # raw snapshot query
                    try:
                        snap = MatchSnapshot(
                            minute=minute,
                            home_goals=int((qs.get("hg") or ["0"])[0]),
                            away_goals=int((qs.get("ag") or ["0"])[0]),
                            home_shots=int((qs.get("hs") or ["0"])[0]),
                            away_shots=int((qs.get("as") or ["0"])[0]),
                            home_sot=int((qs.get("hst") or ["0"])[0]),
                            away_sot=int((qs.get("ast") or ["0"])[0]),
                            goal_minutes=tuple(
                                int(x) for x in ((qs.get("goals") or [""])[0].split(",") if (qs.get("goals") or [""])[0] else [])
                                if x.strip().isdigit()
                            ),
                        )
                        exclude = set()
                    except (TypeError, ValueError):
                        return self._send(400, _json_bytes({"error": "bad query"}), "application/json")
                hits = find_similar(snap, state.corpus, limit=limit, exclude_ids=exclude)
                scorelines = None
                situation = None
                if match:
                    visits = [(m, h, a) for m, h, a in score_path(match) if m <= minute]
                    if not visits:
                        visits = [(0, 0, 0)]
                    cur_start, hs, aws = visits[-1]
                    prev_h = prev_a = None
                    prev_minute = None
                    if len(visits) >= 2:
                        _, prev_h, prev_a = visits[-2]
                        prev_minute = max(0, cur_start - 1)
                    scorelines = build_live_scoreline_eval(
                        state.corpus,
                        home_name=match.home,
                        away_name=match.away,
                        home_score=hs,
                        away_score=aws,
                        prev_home=prev_h,
                        prev_away=prev_a,
                        minute=minute,
                        prev_minute=prev_minute,
                        limit_peers=min(8, limit),
                    )
                    goal_rows = []
                    running_h = running_a = 0
                    for g in sorted(match.goals, key=lambda x: (x.minute, x.team)):
                        if g.minute > minute:
                            break
                        if g.team == "home":
                            running_h += 1
                        else:
                            running_a += 1
                        goal_rows.append(
                            {
                                "minute": g.minute,
                                "team": g.team,
                                "team_name": match.home if g.team == "home" else match.away,
                                "home_goals": running_h,
                                "away_goals": running_a,
                            }
                        )
                    situation = {
                        "home": match.home,
                        "away": match.away,
                        "home_score": hs,
                        "away_score": aws,
                        "minute": minute,
                        "clock": f"{minute}'",
                        "goals": goal_rows,
                    }
                return self._send(
                    200,
                    _json_bytes(
                        {
                            "query": snap.to_dict(),
                            "label": snap.label(),
                            "hits": [h.to_dict() for h in hits],
                            "scorelines": scorelines,
                            "situation": situation,
                        }
                    ),
                    "application/json",
                )

            return self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def serve(*, port: int, season: str, host: str = "127.0.0.1") -> None:
    state = DashboardState(season)
    handler = make_handler(state)
    httpd = ThreadingHTTPServer((host, port), handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
