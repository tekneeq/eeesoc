"""Command parsing and reply formatting for the !soc Discord bot."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from eeesoc.discorder import (
    HELP_TEXT,
    format_board,
    format_fixture_detail,
    format_similar,
    format_winprob,
    handle_command,
    is_upcoming_match,
    parse_command,
)
from eeesoc.similar import SimilarHit
from eeesoc.models import Match, MatchSnapshot


TZ = ZoneInfo("America/New_York")


def _iso(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _board(now: datetime) -> dict:
    later = now + timedelta(hours=3)
    earlier = now - timedelta(hours=2)
    tomorrow = now + timedelta(days=1)
    return {
        "live_total": 1,
        "post_total": 1,
        "pre_total": 2,
        "leagues": [
            {
                "slug": "eng.1",
                "name": "English Premier League",
                "chiclet": "EPL",
                "matches": [
                    {
                        "event_id": "1",
                        "home": "Arsenal",
                        "away": "Chelsea",
                        "home_score": 1,
                        "away_score": 0,
                        "state": "in",
                        "clock": "67'",
                        "detail": "2nd Half",
                        "start": _iso(earlier),
                    },
                    {
                        "event_id": "2",
                        "home": "Liverpool",
                        "away": "Everton",
                        "home_score": 0,
                        "away_score": 0,
                        "state": "pre",
                        "clock": "0'",
                        "detail": "Scheduled",
                        "start": _iso(later),
                    },
                    {
                        "event_id": "3",
                        "home": "Tottenham",
                        "away": "West Ham",
                        "home_score": 2,
                        "away_score": 1,
                        "state": "post",
                        "clock": "FT",
                        "detail": "FT",
                        "start": _iso(earlier),
                    },
                    {
                        "event_id": "4",
                        "home": "Postponed United",
                        "away": "Cancelled City",
                        "home_score": 0,
                        "away_score": 0,
                        "state": "pre",
                        "clock": "0'",
                        "detail": "Postponed",
                        "start": _iso(later),
                    },
                ],
            },
            {
                "slug": "esp.1",
                "name": "Spanish La Liga",
                "chiclet": "La Liga",
                "matches": [
                    {
                        "event_id": "5",
                        "home": "Barcelona",
                        "away": "Valencia",
                        "home_score": 0,
                        "away_score": 0,
                        "state": "pre",
                        "clock": "0'",
                        "detail": "Scheduled",
                        "start": _iso(tomorrow),
                    }
                ],
            },
        ],
    }


def test_parse_command_prefix_and_help():
    assert parse_command("hello") is None
    assert parse_command("!soc") == ("help", [])
    assert parse_command("!eee live epl") == ("live", ["epl"])
    assert parse_command("!soc fixture Arsenal vs Chelsea") == (
        "fixture",
        ["Arsenal", "vs", "Chelsea"],
    )


def test_handle_ignores_other_messages():
    assert handle_command("!lia help") is None
    assert handle_command("soc live") is None


def test_handle_help_and_unknown():
    help_text = handle_command("!soc help")
    assert help_text == HELP_TEXT
    assert "Unknown" in (handle_command("!soc nope") or "")
    assert "help" in (handle_command("!soc nope") or "")


def test_live_upcoming_finished_scopes():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=TZ)
    board = _board(now)
    live = format_board(board, "live", now=now)
    upcoming = format_board(board, "upcoming", now=now)
    finished = format_board(board, "finished", now=now)

    assert "Arsenal" in live and "1–0" in live and "67'" in live
    assert "Liverpool" not in live
    assert "Liverpool" in upcoming and "Everton" in upcoming
    assert "Barcelona" not in upcoming
    assert "Postponed United" not in upcoming
    assert "Tottenham" in finished and "2–1" in finished
    assert "Arsenal" not in finished


def test_league_filter_and_empty_scope():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=TZ)
    board = _board(now)
    epl = format_board(board, "live", "epl", now=now)
    uala = format_board(board, "live", "laliga", now=now)
    assert "Arsenal" in epl
    assert "No live matches" in uala


def test_upcoming_helper_skips_tomorrow_and_dead():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=TZ)
    later = {
        "state": "pre",
        "detail": "Scheduled",
        "start": _iso(now + timedelta(hours=2)),
    }
    tomorrow = {
        "state": "pre",
        "detail": "Scheduled",
        "start": _iso(now + timedelta(days=1)),
    }
    dead = {
        "state": "pre",
        "detail": "Postponed",
        "start": _iso(now + timedelta(hours=2)),
    }
    assert is_upcoming_match(later, now)
    assert not is_upcoming_match(tomorrow, now)
    assert not is_upcoming_match(dead, now)


def test_handle_live_uses_injected_board():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=TZ)
    board = _board(now)
    reply = handle_command("!soc live", board=board, now=now)
    assert reply and "Arsenal" in reply
    filtered = handle_command("!soc upcoming epl", board=board, now=now)
    assert filtered and "Liverpool" in filtered
    assert "Barcelona" not in filtered


def test_format_winprob_and_fixture():
    wp = format_winprob(
        {
            "record": {
                "window_days": 30,
                "last30": {"correct": 12, "wrong": 8, "total": 20, "pct": 0.6},
                "season": {"correct": 40, "wrong": 30, "total": 70, "pct": 0.571},
            },
            "fixtures": [
                {
                    "home": "Arsenal",
                    "away": "Chelsea",
                    "home_fd": "Arsenal",
                    "away_fd": "Chelsea",
                    "start": "2026-09-08T19:00:00Z",
                    "pick": "home",
                    "pick_team": "Arsenal",
                    "pick_prob": 0.58,
                    "bucket": "50-55",
                }
            ],
        }
    )
    assert "12–8 (60%)" in wp
    assert "Arsenal" in wp and "58%" in wp

    detail = format_fixture_detail(
        {
            "home_fd": "Arsenal",
            "away_fd": "Chelsea",
            "prediction": {
                "pick": "home",
                "pick_team": "Arsenal",
                "pick_prob": 0.58,
                "probs": {"home": 0.58, "draw": 0.24, "away": 0.18},
            },
            "home_form": {"season": {"wins": 6, "draws": 2, "losses": 1}},
            "away_form": {"season": {"wins": 4, "draws": 3, "losses": 2}},
            "h2h": [{"home": "Arsenal", "away": "Chelsea", "ft": "2-1"}],
        }
    )
    assert "Pick **Arsenal** 58%" in detail
    assert "H2H" in detail
    assert format_fixture_detail({"error": "team not mapped"}) != ""


def test_format_similar_and_handle_similar_inject():
    snap = MatchSnapshot(minute=53, home_goals=1, away_goals=0, home_shots=8, away_shots=4, home_sot=3, away_sot=1)
    match = Match(
        match_id="EPL:2025:x",
        season="EPL:2025",
        date="01/09/2025",
        home="A",
        away="B",
        home_goals_ft=2,
        away_goals_ft=1,
        home_shots_ft=10,
        away_shots_ft=6,
        home_sot_ft=4,
        away_sot_ft=2,
    )
    hit = SimilarHit(match=match, snapshot=snap, distance=1.2, score=0.812)
    text = format_similar("Everton @ 53'", [hit])
    assert "0.812" in text and "A vs B" in text
    handled = handle_command("!soc similar Everton 53", similar_hits=[hit])
    assert handled and "0.812" in handled


def test_handle_winprob_and_fixture_injected():
    reply = handle_command(
        "!soc winprob",
        winprob={
            "record": {"window_days": 30, "last30": {"correct": 1, "wrong": 0, "total": 1, "pct": 1.0}, "season": {}},
            "fixtures": [],
        },
    )
    assert reply and "WinProb" in reply
    pair = handle_command(
        "!soc fixture Manchester City vs Arsenal",
        fixture_detail={"home_fd": "Man City", "away_fd": "Arsenal", "prediction": {"pick_team": "Man City", "pick_prob": 0.51, "probs": {}}},
    )
    assert pair and "Man City" in pair
