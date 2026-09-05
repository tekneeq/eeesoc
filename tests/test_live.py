"""Tests for the Live scoreboard parser / board assembly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    assert terr["total"] == 16
    # All home passes at x=80 land in the home-attacking third; away passes
    # mirror to x=20 (home-defensive third).
    assert terr["thirds"]["home_att"] == round(12 / 16, 3)
    assert terr["thirds"]["home_def"] == round(4 / 16, 3)
    assert terr["ball_share"]["home"] == round(12 / 16, 3)
    # Grid: home passes at (80,50) → col 4, row 2; away mirrored (20,70) → col 1, row 2
    assert terr["cells"][2][4] == 12
    assert terr["cells"][2][1] == 4
    assert terr["label"] == "home_attacking"


def test_territory_midfield_battle_label():
    from eeesoc.live import _build_territory

    pts = [(50.0, 50.0, "home") for _ in range(10)] + [(55.0, 40.0, "away") for _ in range(10)]
    terr = _build_territory(pts)
    assert terr["label"] == "midfield"
    assert terr["thirds"]["mid"] == 1.0


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
