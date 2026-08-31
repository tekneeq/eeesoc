"""Local HTTP dashboard: Matches + Similar."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from eeesoc.data import load_season, previous_season_label
from eeesoc.live import build_event_timeline, build_live_situation, build_pitch_track, fetch_live_board
from eeesoc.models import Match, MatchSnapshot
from eeesoc.scorelines import build_live_scoreline_eval, score_path
from eeesoc.similar import find_similar, opponent_scored_context

STATIC_DIR = Path(__file__).resolve().parent / "static"

EVERTON_PRESET_RE = re.compile(r"preset:Everton:53")


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
                            "match_count": len(state.matches),
                            "history_count": len(state.history),
                            "everton_preset_id": preset.match_id if preset else None,
                        }
                    ),
                    "application/json",
                )

            if path == "/health":
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")

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
                timeline = build_event_timeline(
                    league,
                    event_id,
                    home=(qs.get("home") or [""])[0],
                    away=(qs.get("away") or [""])[0],
                    home_id=(qs.get("home_id") or [""])[0],
                    away_id=(qs.get("away_id") or [""])[0],
                    clock=(qs.get("clock") or [""])[0],
                    clock_seconds=clock_seconds,
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
                prev_h = prev_a = None
                goals = situation.get("goals") or []
                if goals:
                    last = goals[-1]
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
                rows = [
                    {
                        "match_id": m.match_id,
                        "date": m.date,
                        "home": m.home,
                        "away": m.away,
                        "ft": f"{m.home_goals_ft}-{m.away_goals_ft}",
                        "shots": f"{m.home_shots_ft}/{m.home_sot_ft} vs {m.away_shots_ft}/{m.away_sot_ft}",
                        "is_preset": bool(EVERTON_PRESET_RE.search(m.match_id)),
                    }
                    for m in state.matches
                ]
                return self._send(200, _json_bytes({"matches": rows}), "application/json")

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
                    _, hs, aws = visits[-1]
                    prev_h = prev_a = None
                    if len(visits) >= 2:
                        _, prev_h, prev_a = visits[-2]
                    scorelines = build_live_scoreline_eval(
                        state.corpus,
                        home_name=match.home,
                        away_name=match.away,
                        home_score=hs,
                        away_score=aws,
                        prev_home=prev_h,
                        prev_away=prev_a,
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
