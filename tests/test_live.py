"""Tests for the Live scoreboard parser / board assembly."""

from __future__ import annotations

from eeesoc.live import (
    build_pitch_track,
    clear_live_cache,
    clear_track_cache,
    fetch_live_board,
    parse_scoreboard,
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
                                    "team": {"displayName": "Real Madrid"},
                                },
                                {
                                    "homeAway": "away",
                                    "score": "1",
                                    "team": {"displayName": "Real Sociedad"},
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
        },
        {
            "id": "2",
            "type": {"type": "shot-on-target", "text": "Shot On Target"},
            "shortText": "Player B Shot On Target",
            "clock": {"displayValue": "12'"},
            "fieldPositionX": 88,
            "fieldPositionY": 48,
        },
        {
            "id": "3",
            "type": {"type": "goal", "text": "Goal"},
            "shortText": "Player B Goal",
            "clock": {"displayValue": "13'"},
            "scoringPlay": True,
            "fieldPositionX": 95,
            "fieldPositionY": 50,
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
    assert track["counts"]["shots"] == 2
    assert track["counts"]["goals"] == 1
    assert len(track["passes"]) == 1
    assert track["passes"][0]["x2"] == 40
    assert len(track["shots"]) == 2
    assert track["ball"]["type"] == "goal"
    assert track["ball"]["x"] == 95
    assert track["ball"]["y"] == 50
