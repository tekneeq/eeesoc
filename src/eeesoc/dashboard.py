"""Local HTTP dashboard: Matches + Similar."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from eeesoc.data import load_season, previous_season_label
from eeesoc.models import Match, MatchSnapshot
from eeesoc.similar import find_similar

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
                return self._send(
                    200,
                    _json_bytes({"query": snap.to_dict(), "label": snap.label(), "hits": [h.to_dict() for h in hits]}),
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
