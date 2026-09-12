"""Tests for the Live scoreboard parser / board assembly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from eeesoc.live import (
    build_pitch_track,
    clear_live_cache,
    clear_track_cache,
    fetch_live_board,
    match_is_live,
    parse_scoreboard,
    promote_started_match,
)


SAMPLE_SB = {
    "content": {
        "sbData": {
            "leagues": [{"name": "Spanish LALIGA", "slug": "esp.1"}],
            "events": [
                {
                    "id": "1",
                    "date": "2026-08-26T19:00Z",
                    "competitions": [
                        {
                            "status": {
                                "displayClock": "72'",
                                "clock": 4335.0,
                                "type": {
                                    "state": "in",
                                    "detail": "72'",
                                    "shortDetail": "72'",
                                },
                            },
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "score": "3",
                                    "team": {"id": "86", "displayName": "Real Madrid"},
                                },
                                {
                                    "homeAway": "away",
                                    "score": "1",
                                    "team": {"id": "89", "displayName": "Real Sociedad"},
                                },
                            ],
                        }
                    ],
                },
                {
                    "id": "2",
                    "date": "2026-08-26T21:00Z",
                    "competitions": [
                        {
                            "status": {
                                "displayClock": "0'",
                                "type": {
                                    "state": "pre",
                                    "detail": "Scheduled",
                                    "shortDetail": "Scheduled",
                                },
                            },
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "score": "0",
                                    "team": {"displayName": "A"},
                                },
                                {
                                    "homeAway": "away",
                                    "score": "0",
                                    "team": {"displayName": "B"},
                                },
                            ],
                        }
                    ],
                },
            ],
        }
    }
}


def test_parse_scoreboard_extracts_live_and_pre():
    matches = parse_scoreboard(SAMPLE_SB, "esp.1", "La Liga")
    assert len(matches) == 2
    live = matches[0]
    assert live.state == "in"
    assert live.home == "Real Madrid"
    assert live.away == "Real Sociedad"
    assert live.home_score == 3
    assert live.away_score == 1
    assert live.clock == "72'"
    assert live.league_chiclet == "La Liga"
    assert live.home_id == "86"
    assert live.away_id == "89"
    assert live.clock_seconds == 4335
    assert matches[1].clock_seconds is None


def test_fetch_live_board_filters_and_groups():
    clear_live_cache()

    def fake_fetch(url: str):
        if "esp.1" in url:
            return SAMPLE_SB
        return {"content": {"sbData": {"leagues": [{"name": "X"}], "events": []}}}

    board = fetch_live_board(
        live_only=True,
        leagues=[("esp.1", "La Liga"), ("eng.1", "EPL")],
        fetcher=fake_fetch,
        use_cache=False,
    )
    assert board["live_total"] == 1
    assert board["total"] == 1
    assert len(board["leagues"]) == 1
    assert board["leagues"][0]["chiclet"] == "La Liga"
    assert board["leagues"][0]["matches"][0]["home"] == "Real Madrid"
    chic = {c["slug"]: c["live_count"] for c in board["chiclets"]}
    assert chic["esp.1"] == 1
    assert chic["eng.1"] == 0


def _event(eid: str, state: str, start: str, *, clock: str = "0'", detail: str = "Scheduled") -> dict:
    return {
        "id": eid,
        "date": start,
        "competitions": [
            {
                "status": {
                    "displayClock": clock,
                    "type": {"state": state, "detail": detail, "shortDetail": detail},
                },
                "competitors": [
                    {"homeAway": "home", "score": "0", "team": {"displayName": "Home FC"}},
                    {"homeAway": "away", "score": "0", "team": {"displayName": "Away FC"}},
                ],
            }
        ],
    }


def test_parse_site_api_scoreboard_shape():
    payload = {
        "leagues": [{"name": "English Premier League", "slug": "eng.1"}],
        "events": [_event("7", "in", "2026-09-05T14:00Z", clock="8'", detail="8'")],
    }
    matches = parse_scoreboard(payload, "eng.1", "EPL")
    assert len(matches) == 1
    assert matches[0].league_name == "English Premier League"
    assert matches[0].state == "in"
    assert matches[0].clock == "8'"


def test_kickoff_passed_pre_match_counts_as_live():
    now = datetime(2026, 9, 5, 14, 10, tzinfo=timezone.utc)
    payload = {
        "leagues": [{"name": "English Premier League"}],
        "events": [
            _event("ko-live", "pre", "2026-09-05T14:00Z", detail="Sat, September 5th at 10:00 AM EDT"),
            _event("later", "pre", "2026-09-05T16:30Z", detail="Sat, September 5th at 12:30 PM EDT"),
            _event("done", "post", "2026-09-05T11:30Z", clock="FT", detail="FT"),
            _event("ppd", "pre", "2026-09-05T14:00Z", detail="Postponed"),
        ],
    }
    matches = parse_scoreboard(payload, "eng.1", "EPL")
    by_id = {m.event_id: m for m in matches}
    assert match_is_live(by_id["ko-live"], now) is True
    assert match_is_live(by_id["later"], now) is False
    assert match_is_live(by_id["done"], now) is False
    assert match_is_live(by_id["ppd"], now) is False

    promoted = promote_started_match(by_id["ko-live"], now)
    assert promoted.state == "in"
    assert promoted.clock == "10'"
    assert promoted.clock_seconds == 600
    assert promote_started_match(by_id["later"], now).state == "pre"


def test_fetch_live_board_keeps_finished_games_when_not_live_only():
    clear_live_cache()
    payload = {
        "content": {
            "sbData": {
                "leagues": [{"name": "Italian Serie A"}],
                "events": [
                    {
                        "id": "1",
                        "date": "2026-09-07T18:45Z",
                        "competitions": [
                            {
                                "status": {"type": {"state": "post", "detail": "FT", "shortDetail": "FT"}},
                                "competitors": [
                                    {"homeAway": "home", "score": "1", "team": {"displayName": "Udinese", "id": "118"}},
                                    {"homeAway": "away", "score": "2", "team": {"displayName": "Lazio", "id": "112"}},
                                ],
                            }
                        ],
                    },
                    {
                        "id": "2",
                        "date": "2026-09-07T20:45Z",
                        "competitions": [
                            {
                                "status": {"type": {"state": "in", "detail": "60'"}, "displayClock": "60'"},
                                "competitors": [
                                    {"homeAway": "home", "score": "0", "team": {"displayName": "A"}},
                                    {"homeAway": "away", "score": "0", "team": {"displayName": "B"}},
                                ],
                            }
                        ],
                    },
                    {
                        "id": "3",
                        "date": "2026-09-07T22:00Z",
                        "competitions": [
                            {
                                "status": {
                                    "type": {"state": "pre", "detail": "Scheduled", "shortDetail": "Scheduled"},
                                    "displayClock": "0'",
                                },
                                "competitors": [
                                    {"homeAway": "home", "score": "0", "team": {"displayName": "Roma"}},
                                    {"homeAway": "away", "score": "0", "team": {"displayName": "Napoli"}},
                                ],
                            }
                        ],
                    },
                ],
            }
        }
    }
    seen_urls: list[str] = []

    def fake_fetch(url: str):
        seen_urls.append(url)
        return payload

    live = fetch_live_board(live_only=True, leagues=[("ita.1", "Serie A")], fetcher=fake_fetch, use_cache=False)
    assert live["total"] == 1 and live["post_total"] == 0 and live["pre_total"] == 0

    board = fetch_live_board(
        live_only=False, days_back=1, leagues=[("ita.1", "Serie A")], fetcher=fake_fetch, use_cache=False
    )
    assert board["total"] == 3
    assert board["live_total"] == 1
    assert board["post_total"] == 1
    assert board["pre_total"] == 1
    assert board["chiclets"][0]["post_count"] == 1
    assert board["chiclets"][0]["pre_count"] == 1
    states = {m["home"]: m["state"] for m in board["leagues"][0]["matches"]}
    assert states == {"Udinese": "post", "A": "in", "Roma": "pre"}
    # days_back widens the scoreboard window to a range
    assert any("dates=" in u and "-" in u.split("dates=")[-1] for u in seen_urls[-2:])


def test_scoreboard_dates_days_back():
    from eeesoc.live import _scoreboard_dates

    noon = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert _scoreboard_dates(noon) == "20260907"
    # days_back also reaches one day ahead so tonight's remaining kickoffs stay on the board
    assert _scoreboard_dates(noon, days_back=1) == "20260906-20260908"
    late = datetime(2026, 9, 7, 21, 0, tzinfo=timezone.utc)
    assert _scoreboard_dates(late) == "20260907-20260908"
    assert _scoreboard_dates(late, days_back=1) == "20260906-20260908"


def test_timeline_marks_final_and_caches_longer():
    from eeesoc.live import _timeline_cache, build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    items = [
        {"type": {"type": "pass"}, "clock": {"displayValue": "88'"}, "team": {"$ref": ".../teams/1"},
         "fieldPositionX": 50.0, "fieldPositionY": 50.0},
    ]
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        return {"pageCount": 1, "items": items}

    tl = build_event_timeline("ita.1", "77", home="A", away="B", home_id="1", away_id="2", clock="FT", fetcher=fetch)
    assert tl["final"] is True and tl["frozen"] is True
    # Age the entry past the live TTL but inside the full-time TTL — still served from cache.
    ts, payload = _timeline_cache["tl:ita.1:77"]
    _timeline_cache["tl:ita.1:77"] = (ts - 60, payload)
    build_event_timeline("ita.1", "77", home="A", away="B", home_id="1", away_id="2", clock="FT", fetcher=fetch)
    assert calls["n"] == 1
    clear_timeline_cache()


def test_fetch_live_board_includes_kickoff_passed_pre_epl():
    clear_live_cache()
    now = datetime.now(timezone.utc)
    started = (now.replace(microsecond=0) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = (now.replace(microsecond=0) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "content": {
            "sbData": {
                "leagues": [{"name": "English Premier League"}],
                "events": [
                    _event("epl-1", "pre", started, detail="Scheduled"),
                    _event("epl-later", "pre", later, detail="Scheduled"),
                ],
            }
        }
    }

    def fake_fetch(url: str):
        if "eng.1" in url:
            return payload
        return {"content": {"sbData": {"leagues": [{"name": "X"}], "events": []}}}

    board = fetch_live_board(
        live_only=True,
        leagues=[("eng.1", "EPL")],
        fetcher=fake_fetch,
        use_cache=False,
    )
    assert board["live_total"] == 1
    assert board["leagues"][0]["matches"][0]["home"] == "Home FC"
    assert board["leagues"][0]["matches"][0]["state"] == "in"
    assert board["chiclets"][0]["live_count"] == 1


SAMPLE_PLAYS = {
    "count": 3,
    "pageIndex": 1,
    "pageSize": 100,
    "pageCount": 1,
    "items": [
        {
            "id": "1",
            "type": {"type": "pass", "text": "Pass"},
            "shortText": "Player A Pass",
            "clock": {"displayValue": "10'"},
            "fieldPositionX": 20,
            "fieldPositionY": 50,
            "fieldPosition2X": 40,
            "fieldPosition2Y": 55,
            "team": {"$ref": "http://sports.core.api.espn.com/v2/sports/soccer/leagues/eng.1/seasons/2026/teams/364"},
        },
        {
            "id": "2",
            "type": {"type": "shot-on-target", "text": "Shot On Target"},
            "shortText": "Player B Shot On Target",
            "text": "Player B (Liverpool) Shot On Target at 12'",
            "clock": {"displayValue": "12'"},
            "fieldPositionX": 88,
            "fieldPositionY": 48,
            "team": {"$ref": "http://sports.core.api.espn.com/v2/sports/soccer/leagues/eng.1/seasons/2026/teams/364"},
        },
        {
            "id": "3",
            "type": {"type": "shot-off-target", "text": "Shot Off Target"},
            "shortText": "Player C Shot Off Target",
            "text": "Player C (Nottingham Forest) Shot Off Target at 20'",
            "clock": {"displayValue": "20'"},
            "fieldPositionX": 20,
            "fieldPositionY": 40,
            "team": {"$ref": "http://sports.core.api.espn.com/v2/sports/soccer/leagues/eng.1/seasons/2026/teams/393"},
        },
        {
            "id": "4",
            "type": {"type": "goal", "text": "Goal"},
            "shortText": "Dan Ndoye Goal",
            "text": "Dan Ndoye (Nottingham Forest) Goal at 24'",
            "clock": {"displayValue": "24'"},
            "scoringPlay": True,
            "fieldPositionX": 95,
            "fieldPositionY": 50,
            "team": {"$ref": "http://sports.core.api.espn.com/v2/sports/soccer/leagues/eng.1/seasons/2026/teams/393"},
        },
    ],
}


def test_build_pitch_track_ball_passes_shots():
    clear_track_cache()

    def fake_fetch(url: str):
        return SAMPLE_PLAYS

    track = build_pitch_track(
        "esp.1",
        "99",
        home="Home",
        away="Away",
        fetcher=fake_fetch,
        use_cache=False,
    )
    assert track["counts"]["passes"] == 1
    assert track["counts"]["shots"] == 3
    assert track["counts"]["goals"] == 1
    assert len(track["passes"]) == 1
    assert track["passes"][0]["x2"] == 40
    assert len(track["shots"]) == 3
    assert track["ball"]["type"] == "goal"
    assert track["ball"]["x"] == 95
    assert track["ball"]["y"] == 50
    assert track["window"] == 15
    assert track["from_minute"] == 10
    assert track["to_minute"] == 24
    assert all(p.get("age") is not None for p in track["passes"])


def test_pitch_track_last15_drops_old_plays_and_mirrors_away():
    clear_track_cache()

    def fake_fetch(url: str):
        return SAMPLE_PLAYS

    late = build_pitch_track(
        "esp.1",
        "99",
        home="Liverpool",
        away="Nottingham Forest",
        home_id="364",
        away_id="393",
        clock="90'",
        fetcher=fake_fetch,
        use_cache=False,
    )
    assert late["from_minute"] == 76
    assert late["passes"] == []
    assert late["shots"] == []
    assert late["ball"] is None

    now = build_pitch_track(
        "esp.1",
        "99",
        home="Liverpool",
        away="Nottingham Forest",
        home_id="364",
        away_id="393",
        clock="24'",
        fetcher=fake_fetch,
        use_cache=False,
    )
    # Away goal at team-relative (95, 50) → home-absolute (5, 50).
    assert now["ball"]["type"] == "goal"
    assert now["ball"]["x"] == 5
    assert now["ball"]["y"] == 50
    assert now["ball"]["side"] == "away"
    away_shot = next(s for s in now["shots"] if s["type"] == "shot-off-target")
    assert away_shot["x"] == 80 and away_shot["y"] == 60
    home_pass = now["passes"][0]
    assert home_pass["x"] == 20 and home_pass["x2"] == 40
    assert home_pass["side"] == "home"
    assert now["territory"]["total"] == 4


def test_build_live_situation_goal_and_team_shots():
    from eeesoc.live import build_live_situation, clear_situation_cache, parse_clock_minute

    clear_situation_cache()
    assert parse_clock_minute("24'") == 24
    assert parse_clock_minute("45'+2") == 45

    def fake_fetch(url: str):
        return SAMPLE_PLAYS

    sit = build_live_situation(
        "eng.1",
        "401879314",
        home="Liverpool",
        away="Nottingham Forest",
        home_score=0,
        away_score=1,
        clock="33'",
        home_id="364",
        away_id="393",
        fetcher=fake_fetch,
        use_cache=False,
    )
    assert sit["minute"] == 33
    snap = sit["snapshot"]
    assert snap["home_shots"] == 1
    assert snap["home_sot"] == 1
    assert snap["away_shots"] == 2  # off-target + goal
    assert snap["away_sot"] == 1
    assert snap["away_goals"] == 1
    assert sit["latest_goal"]["minute"] == 24
    assert sit["latest_goal"]["team"] == "away"
    assert sit["latest_goal"]["conceded_by_name"] == "Liverpool"
    assert sit["latest_goal"]["my_shots"] == 1
    assert sit["latest_goal"]["my_sot"] == 1
    assert sit["latest_goal"]["scorer_shots"] == 2
    assert sit["latest_goal"]["scorer_sot"] == 1


def test_build_event_timeline_kinds():
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    plays = {
        "count": 6,
        "pageIndex": 1,
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "corner-awarded"},
                "shortText": "Corner",
                "clock": {"displayValue": "11'"},
                "team": {"$ref": ".../teams/393"},
            },
            {
                "type": {"type": "shot-blocked"},
                "shortText": "Shot Blocked",
                "clock": {"displayValue": "12'"},
                "expectedGoals": 0.08,
                "team": {"$ref": ".../teams/393"},
            },
            {
                "type": {"type": "shot-off-target"},
                "shortText": "Shot Off",
                "clock": {"displayValue": "19'"},
                "expectedGoals": 0.12,
                "team": {"$ref": ".../teams/393"},
            },
            {
                "type": {"type": "shot-on-target"},
                "shortText": "Shot On",
                "clock": {"displayValue": "39'"},
                "expectedGoals": 0.2,
                "team": {"$ref": ".../teams/364"},
            },
            {
                "type": {"type": "goal---header"},
                "shortText": "Header Goal",
                "text": "Dan Ndoye (Nottingham Forest) Goal at 24'",
                "clock": {"displayValue": "24'"},
                "scoringPlay": True,
                "expectedGoals": 0.35,
                "team": {"$ref": ".../teams/393"},
            },
            {
                "type": {"type": "pass"},
                "shortText": "Pass",
                "clock": {"displayValue": "10'"},
                "team": {"$ref": ".../teams/364"},
            },
        ],
    }

    def fake_fetch(url: str):
        return plays

    tl = build_event_timeline(
        "eng.1",
        "401879314",
        home="Liverpool",
        away="Nottingham Forest",
        home_id="364",
        away_id="393",
        clock="85'",
        fetcher=fake_fetch,
        use_cache=False,
    )
    assert tl["minute"] == 85
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds == ["corner", "blocked", "shot", "goal", "shot_on"]
    assert tl["counts"]["corner"] == 1
    assert tl["counts"]["blocked"] == 1
    assert tl["counts"]["shot"] == 1
    assert tl["counts"]["goal"] == 1
    assert tl["counts"]["shot_on"] == 1
    assert tl["events"][3]["team"] == "away"
    assert tl["events"][4]["team"] == "home"
    assert tl["xg"]["away_total"] == 0.55  # 0.08+0.12+0.35
    assert tl["xg"]["home_total"] == 0.2
    assert tl["xg"]["away"][-1]["minute"] == 24
    assert tl["xg"]["home"][-1]["cumulative"] == 0.2
    # Scoreboard can still say 0-0 after a play-by-play goal
    assert tl["play_away_score"] == 1
    assert tl["play_home_score"] == 0
    assert tl["home_score"] == 0
    assert tl["away_score"] == 1
    # No period on these plays and every clock is ≤45′ — the 1H bucket matches the full row.
    assert tl["counts_by_half"]["1h"]["away_goal"] == 1
    assert tl["counts_by_half"]["1h"]["home_shot_on"] == 1
    assert tl["counts_by_half"]["1h"]["away_xg"] == 0.55
    assert tl["counts_by_half"]["2h"]["away_goal"] == 0
    assert tl["counts_by_half"]["2h"]["home_xg"] == 0.0


def test_timeline_counts_split_by_half():
    """1H / 2H buckets follow ESPN period; stoppage in period 1 stays first-half."""
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    plays = {
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "shot-off-target"},
                "clock": {"displayValue": "12'"},
                "period": {"number": 1},
                "expectedGoals": 0.1,
                "team": {"$ref": ".../teams/1"},
            },
            {
                "type": {"type": "corner-awarded"},
                "clock": {"displayValue": "40'"},
                "period": {"number": 1},
                "team": {"$ref": ".../teams/2"},
            },
            {
                "type": {"type": "foul"},
                "clock": {"displayValue": "45'+2'"},
                "period": {"number": 1},
                "team": {"$ref": ".../teams/1"},
            },
            {
                "type": {"type": "shot-on-target"},
                "clock": {"displayValue": "52'"},
                "period": {"number": 2},
                "expectedGoals": 0.25,
                "team": {"$ref": ".../teams/2"},
            },
            {
                "type": {"type": "goal"},
                "clock": {"displayValue": "70'"},
                "period": {"number": 2},
                "scoringPlay": True,
                "expectedGoals": 0.4,
                "team": {"$ref": ".../teams/1"},
            },
            {
                "type": {"type": "foul"},
                "clock": {"displayValue": "77'"},
                "period": {"number": 2},
                "team": {"$ref": ".../teams/2"},
            },
        ],
    }
    tl = build_event_timeline(
        "eng.1",
        "half-split",
        home="Home",
        away="Away",
        home_id="1",
        away_id="2",
        clock="80'",
        fetcher=lambda url: plays,
        use_cache=False,
    )
    h1, h2 = tl["counts_by_half"]["1h"], tl["counts_by_half"]["2h"]
    assert h1["home_shot"] == 1 and h1["away_corner"] == 1 and h1["home_foul"] == 1
    assert h1["home_xg"] == 0.1 and h1["away_xg"] == 0.0
    assert h1["home_goal"] == 0 and h2["home_goal"] == 1
    assert h2["away_shot_on"] == 1 and h2["away_foul"] == 1
    assert h2["home_xg"] == 0.4 and h2["away_xg"] == 0.25
    assert tl["counts"]["home_goal"] == 1
    assert tl["counts"]["home_foul"] == 1
    assert tl["counts"]["away_foul"] == 1


def test_chiclet_territory_and_pitch_use_last15():
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "function renderPitchHeat" in js
    assert "function pitchFade" in js
    assert "last ${win} minutes" in js
    assert "Ball last ${win}" in js
    assert ".pitch-heat-cell" in css
    assert "Territory · last 15′" in html
    assert "where the ball has actually been in those last 15′" in html


def test_chiclet_stats_keep_full_row_and_add_halves():
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    assert "function chicletStatChips" in js
    assert 'row("1H"' in js and 'row("2H"' in js
    assert "counts_by_half" in js
    assert ".mc-stats-half" in css
    assert ".mc-stat-period" in css


def test_live_chiclets_show_last5_goals_total_and_first_half():
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "function last5GoalsRowsHtml" in js
    assert "function last5GoalsSideHtml" in js
    assert "function last5GoalSeq" in js
    assert 'label: "🥅 last 5 scored"' in js
    assert 'label: "🥅 last 5 allowed"' in js
    assert "g.ht_gf" in js and "g.ht_ga" in js
    assert "rows.splice(afterMomentum + 1, 0, ...last5)" in js
    assert 'class="mc-form-g"' in js
    assert "metric === \"scored\" ? g.gf : g.ga" in js
    assert ".mc-form-g" in css and ".mc-form-games" in css
    assert "each of those five games as its own number" in html


def test_timeline_fouls_and_territory():
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    items = [
        # Home fouls twice, away once
        {"type": {"type": "foul"}, "clock": {"displayValue": "5'"}, "team": {"$ref": ".../teams/1"}},
        {"type": {"type": "foul"}, "clock": {"displayValue": "9'"}, "team": {"$ref": ".../teams/1"}},
        {"type": {"type": "foul"}, "clock": {"displayValue": "12'"}, "team": {"$ref": ".../teams/2"}},
    ]
    # Home plays in its attacking third (team-relative x≈80);
    # away plays also team-relative x≈80 — mirrored to home-defensive third.
    for i in range(12):
        items.append(
            {
                "type": {"type": "pass"},
                "clock": {"displayValue": f"{15 + i}'"},
                "team": {"$ref": ".../teams/1"},
                "fieldPositionX": 80.0,
                "fieldPositionY": 50.0,
            }
        )
    for i in range(4):
        items.append(
            {
                "type": {"type": "pass"},
                "clock": {"displayValue": f"{30 + i}'"},
                "team": {"$ref": ".../teams/2"},
                "fieldPositionX": 80.0,
                "fieldPositionY": 30.0,
            }
        )
    plays = {"pageCount": 1, "items": items}

    tl = build_event_timeline(
        "ger.1",
        "7",
        home="Stuttgart",
        away="Cologne",
        home_id="1",
        away_id="2",
        clock="40'",
        fetcher=lambda url: plays,
        use_cache=False,
    )
    assert tl["counts"]["home_foul"] == 2
    assert tl["counts"]["away_foul"] == 1
    terr = tl["territory"]
    # Last 15′ at 40′ is 26–40′: one late home pass (26′) plus four away passes (30–33′).
    # The earlier home siege is on the pressure graph, not this heat map.
    assert terr["window"] == 15
    assert terr["from_minute"] == 26 and terr["to_minute"] == 40
    assert terr["total"] == 5
    assert terr["thirds"]["home_att"] == round(1 / 5, 3)
    assert terr["thirds"]["home_def"] == round(4 / 5, 3)
    assert terr["ball_share"]["home"] == round(1 / 5, 3)
    assert terr["cells"][2][4] == 1
    assert terr["cells"][2][1] == 4
    # Five actions is still a thin sample — the tilt is in the thirds, not the tag.
    assert terr["label"] == "warming_up"


def test_territory_midfield_battle_label():
    from eeesoc.live import _build_territory

    pts = [(50.0, 50.0, "home") for _ in range(10)] + [(55.0, 40.0, "away") for _ in range(10)]
    terr = _build_territory(pts)
    assert terr["label"] == "midfield"
    assert terr["thirds"]["mid"] == 1.0


def test_territory_last15_shows_the_late_siege():
    from eeesoc.live import PRESSURE_WINDOW_MIN, _build_territory

    # First half: home camps forward. Second half: away returns the favour.
    pts = [(80.0, 50.0, "home", m) for m in range(5, 40)]
    pts += [(80.0, 50.0, "away", m) for m in range(60, 89)]
    full = _build_territory([(x, y, s) for x, y, s, _ in pts])
    assert full["window"] is None
    # Whole match is a wash. Last 15′ at 88′ is only the away siege (mirrored
    # onto the home-defensive third).
    late = _build_territory(pts, now_minute=88)
    assert late["window"] == PRESSURE_WINDOW_MIN
    assert late["from_minute"] == 74
    assert late["label"] == "away_attacking"
    assert late["thirds"]["home_def"] == 1.0
    assert late["total"] == 15


def test_territory_attacking_tilt_beats_midfield_label():
    from eeesoc.live import _build_territory

    # Middle third still holds most actions, but the away side owns the final third:
    # 12 mid, 9 away-attacking (mirrored to home-defensive), 3 home-attacking.
    pts = [(50.0, 50.0, "home") for _ in range(12)]
    pts += [(85.0, 50.0, "away") for _ in range(9)]
    pts += [(85.0, 50.0, "home") for _ in range(3)]
    terr = _build_territory(pts)
    assert terr["thirds"]["mid"] == 0.5
    assert terr["label"] == "away_attacking"


def test_pressure_window_flags_late_siege_the_territory_map_hides():
    from eeesoc.live import _build_pressure

    # First half: home camps in the away final third. Second half: away returns the favour.
    pts = [(m, 85.0, 50.0, "home", None) for m in range(5, 40)]
    pts += [(m, 40.0, 50.0, "away", None) for m in range(5, 40)]
    pts += [(m, 40.0, 50.0, "home", None) for m in range(60, 89)]
    pts += [(m, 85.0, 50.0, "away", "shot" if m % 7 == 0 else None) for m in range(60, 89)]
    pts += [(m, 95.0, 50.0, "away", "corner") for m in range(80, 88, 2)]

    early = _build_pressure(pts, now_minute=30)
    assert early["from_minute"] == 16
    assert early["leader"] == "home"
    assert early["share"]["home"] == 1.0

    late = _build_pressure(pts, now_minute=88)
    assert late["window"] == 15
    assert late["from_minute"] == 74
    assert late["leader"] == "away"
    assert late["share"]["away"] == 1.0
    assert late["away"]["final_third"] == 15 + 4
    assert late["away"]["box"] == 15 + 4
    assert late["away"]["corners"] == 4
    assert late["away"]["shots"] == 2  # minutes 77 and 84
    assert late["home"]["final_third"] == 0


def test_pressure_series_keeps_the_first_half_siege_after_the_flip():
    from eeesoc.live import _build_pressure, _build_pressure_series

    pts = [(m, 85.0, 50.0, "home", None) for m in range(5, 40)]
    pts += [(m, 40.0, 50.0, "away", None) for m in range(5, 40)]
    pts += [(m, 40.0, 50.0, "home", None) for m in range(60, 89)]
    pts += [(m, 85.0, 50.0, "away", None) for m in range(60, 89)]

    series = _build_pressure_series(pts, now_minute=88)
    assert [p["minute"] for p in series] == list(range(1, 89))
    # Too few actions in the opening window — gap, not a fake 50-50.
    assert series[4]["label"] == "quiet"
    assert series[4]["home"] is None

    at_30 = next(p for p in series if p["minute"] == 30)
    assert at_30["leader"] == "home"
    assert at_30["home"] == 1.0
    assert at_30["final_third"] == {"home": 15, "away": 0}

    at_88 = series[-1]
    assert at_88["leader"] == "away"
    assert at_88["away"] == 1.0
    assert at_88["final_third"]["home"] == 0

    # The snapshot at 88' still agrees with the last series point, but the
    # first-half siege is still on the series — that is the graph's job.
    snap = _build_pressure(pts, now_minute=88)
    assert snap["leader"] == "away"
    assert snap["series"][29]["leader"] == "home"
    assert snap["series"][-1]["leader"] == "away"


def test_pressure_quiet_when_too_little_data():
    from eeesoc.live import _build_pressure

    pts = [(m, 85.0, 50.0, "away", None) for m in range(80, 85)]
    p = _build_pressure(pts, now_minute=85)
    assert p["label"] == "quiet"
    assert p["leader"] is None


def test_timeline_payload_includes_pressure():
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    items = []
    for i in range(30):
        items.append(
            {
                "type": {"type": "pass"},
                "clock": {"displayValue": f"{61 + i // 2}'"},
                "team": {"$ref": ".../teams/2"},
                "fieldPositionX": 88.0,
                "fieldPositionY": 50.0,
            }
        )
    for i in range(10):
        items.append(
            {
                "type": {"type": "pass"},
                "clock": {"displayValue": f"{60 + i}'"},
                "team": {"$ref": ".../teams/1"},
                "fieldPositionX": 30.0,
                "fieldPositionY": 50.0,
            }
        )
    tl = build_event_timeline(
        "ita.1",
        "9",
        home="Udinese",
        away="Lazio",
        home_id="1",
        away_id="2",
        clock="75'",
        fetcher=lambda url: {"pageCount": 1, "items": items},
        use_cache=False,
    )
    p = tl["pressure"]
    assert p["to_minute"] == 75
    assert p["leader"] == "away"
    assert p["away"]["final_third"] == 30
    assert p["home"]["final_third"] == 0
    assert p["series"][-1]["leader"] == "away"
    assert p["series"][-1]["minute"] == 75
    assert len(p["series"]) == 75


def test_own_goal_is_not_a_regular_goal_tick():
    """ESPN own-goal plays must not render as the green goal marker."""
    from eeesoc.live import (
        _event_kind,
        _is_own_goal,
        _normalize_play_type,
        _side_for_own_goal,
        build_event_timeline,
        build_live_situation,
        build_pitch_track,
        clear_situation_cache,
        clear_timeline_cache,
        clear_track_cache,
    )

    assert _is_own_goal("own-goal", {"ownGoal": True})
    assert _is_own_goal("goal---own-goal")
    assert _normalize_play_type("goal---own-goal") == "own-goal"
    assert _normalize_play_type("own-goal") == "own-goal"
    assert _event_kind("own-goal", scoring=True) == "own_goal"
    assert _event_kind("goal", scoring=True) == "goal"

    # ESPN team $ref is the benefiting side (Bournemouth), not the scorer.
    og_play = {
        "type": {"id": "97", "text": "Own Goal", "type": "own-goal"},
        "ownGoal": True,
        "scoringPlay": True,
        "shortText": "Malick Thiaw Own Goal",
        "text": "Own Goal by Malick Thiaw, Newcastle United. Newcastle United 0, Bournemouth 2.",
        "clock": {"displayValue": "35'"},
        "team": {"$ref": ".../teams/349"},
        "fieldPositionX": 95.8,
        "fieldPositionY": 56.7,
    }
    assert (
        _side_for_own_goal(
            "349",
            home_id="361",
            away_id="349",
            home="Newcastle United",
            away="Bournemouth",
            play=og_play,
        )
        == "away"
    )
    # Without a team id, "Own Goal by X, Newcastle United" is the conceding club.
    assert (
        _side_for_own_goal(
            None,
            home_id="361",
            away_id="349",
            home="Newcastle United",
            away="Bournemouth",
            play=og_play,
        )
        == "away"
    )

    clear_timeline_cache()
    plays = {
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "goal"},
                "scoringPlay": True,
                "ownGoal": False,
                "shortText": "Marcus Tavernier Goal",
                "text": "Marcus Tavernier (Bournemouth) Goal at 12'",
                "clock": {"displayValue": "12'"},
                "team": {"$ref": ".../teams/349"},
            },
            og_play,
            {
                "type": {"type": "shot-on-target"},
                "shortText": "Home SOT",
                "clock": {"displayValue": "20'"},
                "team": {"$ref": ".../teams/361"},
            },
        ],
    }
    tl = build_event_timeline(
        "eng.1",
        "401879286",
        home="Newcastle United",
        away="Bournemouth",
        home_id="361",
        away_id="349",
        clock="40'",
        home_score=0,
        away_score=2,
        fetcher=lambda url: plays,
        use_cache=False,
    )
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds == ["goal", "shot_on", "own_goal"]
    og = next(e for e in tl["events"] if e["kind"] == "own_goal")
    assert og["team"] == "away"
    assert og["type"] == "own-goal"
    assert tl["counts"]["goal"] == 1
    assert tl["counts"]["own_goal"] == 1
    assert tl["counts"]["away_goal"] == 1
    assert tl["counts"]["away_own_goal"] == 1
    assert tl["counts"]["home_goal"] == 0
    assert tl["play_away_score"] == 2
    assert tl["away_score"] == 2
    # Own goals are not shots / SOT for either side
    assert tl["counts"]["home_shot_on"] == 1
    assert tl["counts"]["away_shot_on"] == 0

    clear_situation_cache()
    sit = build_live_situation(
        "eng.1",
        "401879286",
        home="Newcastle United",
        away="Bournemouth",
        home_score=0,
        away_score=2,
        clock="40'",
        home_id="361",
        away_id="349",
        fetcher=lambda url: plays,
        use_cache=False,
    )
    assert sit["snapshot"]["away_goals"] == 2
    assert sit["snapshot"]["home_goals"] == 0
    assert sit["goals"][1]["own_goal"] is True
    assert sit["goals"][1]["team"] == "away"
    assert sit["snapshot"]["away_sot"] == 1  # Tavernier goal only; OG is not SOT
    assert sit["snapshot"]["away_shots"] == 1

    clear_track_cache()
    track = build_pitch_track(
        "eng.1",
        "401879286",
        home="Newcastle United",
        away="Bournemouth",
        fetcher=lambda url: plays,
        use_cache=False,
    )
    assert track["counts"]["goals"] == 2
    assert any(s.get("own_goal") for s in track["shots"])
    assert any(s.get("type") == "own-goal" for s in track["shots"])

    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    assert 'ev.kind === "own_goal"' in js
    assert "tl-og" in js
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "Own goal" in html


def test_typed_goal_with_own_goal_copy_is_not_a_green_tick():
    """Some feeds send type=goal plus 'Own Goal' text — still an OG, not a goal tick."""
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    plays = {
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "goal"},
                "scoringPlay": True,
                "shortText": "João Pedro Own Goal",
                "text": "Own Goal by João Pedro, Chelsea. Chelsea 3, Brighton and Hove Albion 2.",
                "clock": {"displayValue": "71'"},
                "team": {"$ref": ".../teams/331"},
            }
        ],
    }
    tl = build_event_timeline(
        "eng.1",
        "2",
        home="Chelsea",
        away="Brighton and Hove Albion",
        home_id="363",
        away_id="331",
        clock="75'",
        home_score=3,
        away_score=2,
        fetcher=lambda url: plays,
        use_cache=False,
    )
    assert [e["kind"] for e in tl["events"]] == ["own_goal"]
    assert tl["events"][0]["team"] == "away"
    assert tl["counts"]["goal"] == 0
    assert tl["play_away_score"] == 1


def test_bulletin_lists_scorers_and_substitutions():
    from eeesoc.live import (
        _player_name_from_goal,
        _sub_players,
        build_event_timeline,
        clear_timeline_cache,
    )

    assert (
        _player_name_from_goal(
            {
                "shortText": "Bryan Mbeumo Goal",
                "text": "Goal! Everton 0, Manchester United 1. Bryan Mbeumo (Manchester United) left footed shot.",
            }
        )
        == "Bryan Mbeumo"
    )
    assert (
        _player_name_from_goal(
            {"shortText": "Serhou Guirassy Penalty - Scored", "text": ""},
        )
        == "Serhou Guirassy"
    )
    assert (
        _player_name_from_goal(
            {
                "shortText": "Renato Veiga Own Goal",
                "text": "Own Goal by Renato Veiga, Villarreal. Borussia Dortmund 1, Villarreal 0.",
            },
            own=True,
        )
        == "Renato Veiga"
    )
    assert _sub_players(
        {
            "shortText": "Jack Grealish Substitution",
            "text": "Substitution, Everton. Jack Grealish replaces Brennan Johnson.",
        }
    ) == ("Jack Grealish", "Brennan Johnson")
    assert _sub_players(
        {
            "shortText": "Ainsley Maitland-Niles Substitution",
            "text": "Substitution, Everton. Ainsley Maitland-Niles replaces James Garner because of an injury.",
        }
    ) == ("Ainsley Maitland-Niles", "James Garner")

    clear_timeline_cache()
    plays = {
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "goal"},
                "scoringPlay": True,
                "shortText": "Bryan Mbeumo Goal",
                "text": "Goal! Everton 0, Manchester United 1. Bryan Mbeumo (Manchester United) left footed shot.",
                "clock": {"displayValue": "46'", "value": 2760.0},
                "team": {"$ref": ".../teams/360"},
            },
            {
                "type": {"type": "substitution"},
                "substitution": True,
                "shortText": "Jack Grealish Substitution",
                "text": "Substitution, Everton. Jack Grealish replaces Brennan Johnson.",
                "clock": {"displayValue": "66'", "value": 3960.0},
                "team": {"$ref": ".../teams/368"},
            },
            {
                "type": {"type": "goal---header"},
                "scoringPlay": True,
                "shortText": "Benjamin Sesko Goal - Header",
                "text": "Goal! Everton 1, Manchester United 2. Benjamin Sesko (Manchester United) header.",
                "clock": {"displayValue": "88'", "value": 5280.0},
                "team": {"$ref": ".../teams/360"},
            },
            {
                "type": {"type": "penalty---scored"},
                "scoringPlay": True,
                "shortText": "Serhou Guirassy Penalty - Scored",
                "text": "Goal! Home 1, Away 0. Serhou Guirassy (Everton) converts the penalty.",
                "clock": {"displayValue": "85'", "value": 5100.0},
                "team": {"$ref": ".../teams/368"},
            },
        ],
    }
    tl = build_event_timeline(
        "eng.1",
        "401879291",
        home="Everton",
        away="Manchester United",
        home_id="368",
        away_id="360",
        clock="90'",
        home_score=1,
        away_score=2,
        fetcher=lambda url: plays,
        use_cache=False,
    )
    kinds = [e["kind"] for e in tl["bulletin"]]
    assert kinds == ["goal", "sub", "goal", "goal"]
    # 90'+1 before 90'+6 even if ESPN elapsed is messy
    late = build_event_timeline(
        "eng.1",
        "late",
        home="Everton",
        away="Manchester United",
        home_id="368",
        away_id="360",
        clock="FT",
        fetcher=lambda url: {
            "pageCount": 1,
            "items": [
                {
                    "type": {"type": "goal"},
                    "scoringPlay": True,
                    "shortText": "Late Goal",
                    "text": "Goal! Everton 2, Manchester United 2. Ainsley Maitland-Niles (Everton) shot.",
                    "clock": {"displayValue": "90'+6'", "value": 5400.0},
                    "team": {"$ref": ".../teams/368"},
                },
                {
                    "type": {"type": "substitution"},
                    "substitution": True,
                    "shortText": "Noussair Mazraoui Substitution",
                    "text": "Substitution, Manchester United. Noussair Mazraoui replaces Luke Shaw.",
                    "clock": {"displayValue": "90'+1'", "value": 5460.0},
                    "team": {"$ref": ".../teams/360"},
                },
            ],
        },
        use_cache=False,
    )
    assert [e["clock"] for e in late["bulletin"]] == ["90'+1'", "90'+6'"]
    by_clock = {e["clock"]: e for e in tl["bulletin"]}
    assert by_clock["46'"]["player"] == "Bryan Mbeumo"
    assert by_clock["46'"]["team"] == "away"
    assert by_clock["66'"]["kind"] == "sub"
    assert by_clock["66'"]["player_on"] == "Jack Grealish"
    assert by_clock["66'"]["player_off"] == "Brennan Johnson"
    assert by_clock["66'"]["team"] == "home"
    assert by_clock["88'"]["player"] == "Benjamin Sesko"
    assert by_clock["85'"]["player"] == "Serhou Guirassy"
    assert by_clock["85'"]["penalty"] is True
    assert by_clock["85'"]["team"] == "home"
    # Subs stay off the shot/goal strip
    assert "sub" not in [e["kind"] for e in tl["events"]]
    assert tl["events"][0]["player"] == "Bryan Mbeumo"

    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    assert "function chicletBulletinHtml" in js
    assert "data-bulletin-for" in js


def test_timeline_score_prefers_plays_over_stale_board():
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    plays = {
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "goal"},
                "scoringPlay": True,
                "shortText": "Stuttgart Goal",
                "text": "Undav (Stuttgart) Goal at 12'",
                "clock": {"displayValue": "12'"},
                "team": {"$ref": ".../teams/134"},
            }
        ],
    }
    tl = build_event_timeline(
        "ger.1",
        "1",
        home="Stuttgart",
        away="Cologne",
        home_id="134",
        away_id="122",
        clock="14'",
        home_score=0,
        away_score=0,
        fetcher=lambda url: plays,
        use_cache=False,
    )
    assert tl["counts"]["home_goal"] == 1
    assert tl["board_home_score"] == 0
    assert tl["home_score"] == 1
    assert tl["away_score"] == 0


def test_timeline_now_follows_latest_play_not_stale_board_clock():
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    plays = {
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "shot-off-target"},
                "shortText": "Late shot",
                "clock": {"displayValue": "35'", "value": 2049.0},
                "team": {"$ref": ".../teams/2950"},
            },
            {
                "type": {"type": "corner-awarded"},
                "shortText": "Corner",
                "clock": {"displayValue": "36'", "value": 2133.0},
                "team": {"$ref": ".../teams/2950"},
            },
        ],
    }

    def fake_fetch(url: str):
        return plays

    # Scoreboard chiclet still says 26' while plays already reach 36'
    tl = build_event_timeline(
        "ger.1",
        "401884812",
        home="Mainz",
        away="Paderborn",
        home_id="2950",
        away_id="3307",
        clock="26'",
        fetcher=fake_fetch,
        use_cache=False,
    )
    assert tl["board_minute"] == 26
    assert tl["play_minute"] == 36
    assert tl["minute"] == 36
    assert max(e["minute"] for e in tl["events"]) <= tl["minute"]
    # elapsed follows the freshest play clock (2133s), not the stale board clock
    assert tl["elapsed_seconds"] == 2133
    assert tl["frozen"] is False


def test_timeline_elapsed_prefers_board_clock_seconds_and_freezes_at_ht():
    from eeesoc.live import build_event_timeline, clear_timeline_cache

    clear_timeline_cache()
    plays = {
        "pageCount": 1,
        "items": [
            {
                "type": {"type": "shot-on-target"},
                "shortText": "Early shot",
                "clock": {"displayValue": "12'", "value": 700.0},
                "team": {"$ref": ".../teams/86"},
            }
        ],
    }

    tl = build_event_timeline(
        "esp.1",
        "9",
        home="Real Madrid",
        away="Real Sociedad",
        home_id="86",
        away_id="89",
        clock="45'",
        clock_seconds=2700,
        fetcher=lambda url: plays,
        use_cache=False,
    )
    assert tl["elapsed_seconds"] == 2700

    clear_timeline_cache()
    ht = build_event_timeline(
        "esp.1",
        "9",
        home="Real Madrid",
        away="Real Sociedad",
        home_id="86",
        away_id="89",
        clock="HT",
        fetcher=lambda url: plays,
        use_cache=False,
    )
    assert ht["frozen"] is True


def test_opponent_scored_context_averages():
    from eeesoc.models import GoalEvent, Match
    from eeesoc.similar import opponent_scored_context

    peers = [
        Match(
            match_id="a",
            season="EPL:2024",
            date="01/01/2024",
            home="A",
            away="B",
            home_goals_ft=1,
            away_goals_ft=2,
            home_shots_ft=8,
            away_shots_ft=10,
            home_sot_ft=2,
            away_sot_ft=4,
            goals=[GoalEvent(24, "away"), GoalEvent(55, "home"), GoalEvent(78, "away")],
            home_shots_by_min=[0] + [3] * 90,
            away_shots_by_min=[0] + [5] * 90,
            home_sot_by_min=[0] + [1] * 90,
            away_sot_by_min=[0] + [2] * 90,
        ),
        Match(
            match_id="b",
            season="EPL:2024",
            date="02/01/2024",
            home="C",
            away="D",
            home_goals_ft=0,
            away_goals_ft=1,
            home_shots_ft=6,
            away_shots_ft=9,
            home_sot_ft=1,
            away_sot_ft=3,
            goals=[GoalEvent(22, "away")],
            home_shots_by_min=[0] + [5] * 90,
            away_shots_by_min=[0] + [7] * 90,
            home_sot_by_min=[0] + [2] * 90,
            away_sot_by_min=[0] + [3] * 90,
        ),
    ]
    ctx = opponent_scored_context(peers, goal_minute=24, scored_by="away", window=5)
    assert ctx["count"] == 2
    assert ctx["conceded_by"] == "home"
    assert ctx["avg_my_shots"] == 4.0
    assert ctx["avg_my_sot"] == 1.5
    assert ctx["peers"][0]["conceded_by_name"] == "A"
    assert ctx["peers"][0]["more_goals"] == 2
    assert ctx["peers"][0]["more_goals_2h"] == 2
    assert ctx["peers"][0]["my_2h"] == 1
    assert ctx["peers"][0]["opp_2h"] == 1
    assert ctx["peers"][0]["after_label"] == "55'm · 78'o"
    assert ctx["peers"][0]["second_half_label"] == "55'm · 78'o"
    assert ctx["peers"][1]["more_goals"] == 0
    assert ctx["peers"][1]["second_half_label"] == "no 2H goals"
    assert ctx["avg_more_goals"] == 1.0
    assert ctx["avg_more_goals_2h"] == 1.0
    assert ctx["pct_equalized"] == 0.5
    assert ctx["pct_any_2h_goals"] == 0.5
    assert any(w["bucket"] == "46-60" and w["side"] == "my" for w in ctx["when_2h"])
    assert any(w["bucket"] == "76-90" and w["side"] == "opp" for w in ctx["when_2h"])


def test_half_goals_row_tags_each_side_with_its_own_ht_score():
    """A 2–0 home lead must look up 2–0 for home and 0–2 for away — the label says so."""
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    assert "function ownHtScore" in js
    assert 'side === "home" ? `${hp.htHome}–${hp.htAway}` : `${hp.htAway}–${hp.htHome}`' in js
    assert "mc-half-ht" in js
    assert "never the leader's sample on the trailer" in js


def test_live_chiclets_use_pointer_drag():
    """Live reorder must not rely on HTML5 DnD on <button> (broken in Firefox / SVG)."""
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    assert "function placeChicletAtY" in js
    assert 'addEventListener("pointerdown"' in js
    assert "btn.draggable = true" not in js
    assert "makeChicletDropZone" not in js


def test_live_league_chips_toggle_instead_of_replacing():
    """Live / Upcoming / Finished league chips stack; a second click adds, not swaps."""
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "function toggleLeagueFilter" in js
    assert "function pruneLeagueFilter" in js
    assert "state[filterKey] = toggleLeagueFilter(state[filterKey], c.slug)" in js
    assert "state[filterKey] = new Set([c.slug])" not in js
    assert "click more than one" in html

    # Exercise the toggle itself (extracted from app.js) so ALL → A → A+B → B → ALL.
    script = r"""
function toggleLeagueFilter(filter, slug) {
  const next = filter instanceof Set ? new Set(filter) : new Set();
  if (next.has(slug)) next.delete(slug);
  else next.add(slug);
  return next.size ? next : null;
}
let f = toggleLeagueFilter(null, "eng.1");
if (!(f instanceof Set) || f.size !== 1 || !f.has("eng.1")) process.exit(2);
f = toggleLeagueFilter(f, "esp.1");
if (f.size !== 2 || !f.has("eng.1") || !f.has("esp.1")) process.exit(3);
f = toggleLeagueFilter(f, "eng.1");
if (f.size !== 1 || !f.has("esp.1")) process.exit(4);
f = toggleLeagueFilter(f, "esp.1");
if (f !== null) process.exit(5);
"""
    import subprocess

    subprocess.run(["node", "-e", script], check=True)


# —— Lineups: formation, subs, power ——


def _lu_player(
    jersey,
    name,
    place,
    pos="M",
    *,
    starter=True,
    stats=None,
    subbed_in=False,
    subbed_out=False,
    sub_minute=None,
    in_for=None,
    out_for=None,
):
    entry = {
        "starter": starter,
        "jersey": jersey,
        "athlete": {"displayName": name, "shortName": name},
        "position": {"name": pos, "abbreviation": pos},
        "formationPlace": str(place),
        "subbedIn": subbed_in,
        "subbedOut": subbed_out,
        "stats": [{"name": k, "value": v} for k, v in (stats or {}).items()],
    }
    if sub_minute is not None:
        entry["plays"] = [{"substitution": True, "clock": {"displayValue": f"{sub_minute}'"}}]
    if in_for:
        entry["subbedInFor"] = {"jersey": in_for[0], "athlete": {"shortName": in_for[1]}}
    if out_for:
        entry["subbedOutFor"] = {"jersey": out_for[0], "athlete": {"shortName": out_for[1]}}
    return entry


def _lu_summary():
    home_roster = [
        _lu_player("1", "Keeper", 1, "G", stats={"saves": 3, "goalsConceded": 1}),
        _lu_player(
            "2", "Right Back", 2, "RB", subbed_out=True, sub_minute=57, out_for=("27", "New Legs")
        ),
        _lu_player("9", "Striker", 9, "F", stats={"totalGoals": 1, "totalShots": 2, "shotsOnTarget": 1}),
        _lu_player(
            "27", "New Legs", 0, "SUB", starter=False, subbed_in=True, sub_minute=57, in_for=("2", "Right Back")
        ),
        _lu_player("30", "Bench Only", 0, "SUB", starter=False),
    ]
    away_roster = [_lu_player("13", "Away Keeper", 1, "G")]
    return {
        "rosters": [
            {"homeAway": "home", "formation": "4-2-3-1", "team": {"displayName": "Alpha"}, "roster": home_roster},
            {"homeAway": "away", "formation": "4-4-2", "team": {"displayName": "Beta"}, "roster": away_roster},
        ],
        "header": {"competitions": [{"status": {"type": {"state": "in"}}}]},
    }


def test_build_lineups_formation_subs_and_power():
    from eeesoc.live import build_lineups, clear_lineup_cache

    clear_lineup_cache()
    payload = build_lineups("eng.1", "42", fetcher=lambda url: _lu_summary(), use_cache=False)
    assert payload["available"] is True
    assert payload["final"] is False
    home = payload["home"]
    assert home["team"] == "Alpha" and home["formation"] == "4-2-3-1"
    by_jersey = {p["jersey"]: p for p in home["players"]}
    assert set(by_jersey) == {"1", "2", "9", "27"}  # bench-only player dropped

    keeper = by_jersey["1"]
    assert keeper["place"] == 1 and keeper["on_pitch"] is True
    assert keeper["power"] == 65 + 3 * 3 - 3  # saves help, conceded hurts

    rb = by_jersey["2"]
    assert rb["on_pitch"] is False and rb["out_minute"] == 57 and rb["sub_by"] == "New Legs"

    sub = by_jersey["27"]
    assert sub["place"] == 2  # inherits the replaced player's formation place
    assert sub["in_minute"] == 57 and sub["sub_for"] == "Right Back" and sub["on_pitch"] is True

    striker = by_jersey["9"]
    assert striker["power"] == 65 + 12 + 4 + 1  # goal + SOT + off-target shot (int round of 1.5)

    assert payload["away"]["formation"] == "4-4-2"


def test_player_power_bounds():
    from eeesoc.live import player_power

    assert player_power({"goals": 3, "sot": 5, "assists": 2}, is_keeper=False) == 99
    assert player_power({"red": 2, "fouls": 6, "yellow": 2}, is_keeper=False) == 40
    assert player_power({}, is_keeper=False) == 65


def test_build_lineups_unavailable_and_cache():
    from eeesoc.live import build_lineups, clear_lineup_cache

    clear_lineup_cache()
    empty = build_lineups("eng.1", "43", fetcher=lambda url: {}, use_cache=True)
    assert empty["available"] is False and empty["home"]["players"] == []

    calls = []

    def fetcher(url):
        calls.append(url)
        return _lu_summary()

    clear_lineup_cache()
    first = build_lineups("eng.1", "44", fetcher=fetcher)
    second = build_lineups("eng.1", "44", fetcher=fetcher)
    assert first is second and len(calls) == 1
    assert "summary?event=44" in calls[0]
    clear_lineup_cache()
