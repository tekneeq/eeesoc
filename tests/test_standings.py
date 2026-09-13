"""Official ESPN standings parser / lookup."""

from __future__ import annotations

from eeesoc.standings import (
    clear_standings_cache,
    lookup_team,
    parse_standings,
    standings_board,
)

SAMPLE = {
    "name": "English Premier League",
    "children": [
        {
            "name": "2026-27 English Premier League",
            "standings": {
                "entries": [
                    {
                        "team": {"id": "359", "displayName": "Arsenal", "shortDisplayName": "Arsenal"},
                        "note": {"description": "Champions League"},
                        "stats": [
                            {"name": "gamesPlayed", "value": 4},
                            {"name": "wins", "value": 4},
                            {"name": "ties", "value": 0},
                            {"name": "losses", "value": 0},
                            {"name": "points", "value": 12},
                            {"name": "pointsFor", "value": 8},
                            {"name": "pointsAgainst", "value": 1},
                            {"name": "pointDifferential", "value": 7},
                            {"name": "rank", "value": 1},
                            {"name": "overall", "displayValue": "4-0-0"},
                        ],
                    },
                    {
                        "team": {"id": "364", "displayName": "Liverpool"},
                        "stats": [
                            {"name": "wins", "value": 2},
                            {"name": "ties", "value": 1},
                            {"name": "losses", "value": 1},
                            {"name": "points", "value": 7},
                            {"name": "rank", "value": 5},
                            {"name": "gamesPlayed", "value": 4},
                        ],
                    },
                ]
            },
        }
    ],
}

MLS = {
    "name": "MLS",
    "children": [
        {
            "name": "Eastern Conference",
            "standings": {
                "entries": [
                    {
                        "team": {"id": "202", "displayName": "Inter Miami CF"},
                        "stats": [
                            {"name": "rank", "value": 1},
                            {"name": "wins", "value": 8},
                            {"name": "ties", "value": 2},
                            {"name": "losses", "value": 1},
                            {"name": "points", "value": 26},
                            {"name": "overall", "displayValue": "8-2-1"},
                        ],
                    }
                ]
            },
        },
        {
            "name": "Western Conference",
            "standings": {
                "entries": [
                    {
                        "team": {"id": "191", "displayName": "LAFC"},
                        "stats": [
                            {"name": "rank", "value": 2},
                            {"name": "wins", "value": 6},
                            {"name": "ties", "value": 3},
                            {"name": "losses", "value": 2},
                            {"name": "points", "value": 21},
                            {"name": "overall", "displayValue": "6-3-2"},
                        ],
                    }
                ]
            },
        },
    ],
}


def test_parse_standings_reads_rank_record_and_points():
    table = parse_standings(SAMPLE, "eng.1", "EPL")
    assert table["slug"] == "eng.1" and table["label"] == "EPL"
    assert table["teams_n"] == 2
    ars = table["teams"]["359"]
    assert ars["rank"] == 1
    assert ars["record"] == "4-0-0"
    assert ars["wins"] == 4 and ars["draws"] == 0 and ars["losses"] == 0
    assert ars["points"] == 12 and ars["gf"] == 8 and ars["ga"] == 1 and ars["gd"] == 7
    assert ars["note"] == "Champions League"
    assert ars["group"] == ""
    liv = table["teams"]["364"]
    assert liv["rank"] == 5 and liv["record"] == "2-1-1" and liv["points"] == 7


def test_parse_standings_keeps_conference_group():
    table = parse_standings(MLS, "usa.1", "MLS")
    east = table["teams"]["202"]
    west = table["teams"]["191"]
    assert east["group"] == "Eastern Conference" and east["group_n"] == 1
    assert west["group"] == "Western Conference"
    assert east["rank"] == 1 and west["rank"] == 2


def test_standings_board_uses_fetcher_and_lookup():
    clear_standings_cache()
    board = standings_board(
        leagues=[("eng.1", "EPL")],
        fetcher=lambda url: SAMPLE,
        use_cache=False,
    )
    assert "eng.1" in board["leagues"]
    row = lookup_team(board, "eng.1", team_id="359")
    assert row is not None and row["points"] == 12 and row["_league"]["label"] == "EPL"
    by_name = lookup_team(board, "eng.1", name="Liverpool")
    assert by_name is not None and by_name["rank"] == 5
    assert lookup_team(board, "eng.1", team_id="nope") is None


def test_chiclet_shows_league_table_row():
    from pathlib import Path

    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "function standingsRowHtml" in js
    assert "function standingsFor" in js
    assert "/api/live/standings" in js
    assert "📊 table" in js
    assert "league table" in html.lower() or "league standing" in html.lower()
    assert ".mc-table-side" in css
