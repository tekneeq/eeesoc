"""Totals bet parser, archive hit rates, and !soc bets replies."""

from __future__ import annotations

from eeesoc.bets import (
    USAGE,
    evaluate_bet,
    find_fixture,
    format_bet,
    name_matches,
    parse_bets_args,
    period_goals,
    sample_rate,
)
from eeesoc.discorder import handle_command


def test_parse_bets_args_defaults_under_and_full_time():
    assert parse_bets_args(["Liverpool", "1.5", "1h"]) == {
        "team": "Liverpool",
        "line": 1.5,
        "side": "under",
        "period": "1h",
    }
    assert parse_bets_args(["Liverpool", "1.5"])["period"] == "ft"
    assert parse_bets_args(["Liverpool", "over", "2.5"]) == {
        "team": "Liverpool",
        "line": 2.5,
        "side": "over",
        "period": "ft",
    }
    assert parse_bets_args(["Manchester", "United", "under", "1.5", "2h"])["team"] == "Manchester United"
    assert parse_bets_args(["Liverpool"]) is None
    assert parse_bets_args(["1.5", "1h"]) is None


def test_name_matches_and_find_prefers_live():
    assert name_matches("Liverpool", "liverpool")
    assert name_matches("Manchester United", "man united")
    assert not name_matches("Everton", "liverpool")
    board = {
        "leagues": [
            {
                "slug": "eng.1",
                "chiclet": "EPL",
                "name": "EPL",
                "matches": [
                    {"home": "Liverpool", "away": "Everton", "state": "pre", "start": "2026-09-11T18:00Z"},
                    {"home": "Liverpool", "away": "Arsenal", "state": "in", "start": "2026-09-11T15:00Z", "clock": "33'"},
                ],
            }
        ]
    }
    hit = find_fixture(board, "Liverpool")
    assert hit and hit["away"] == "Arsenal" and hit["state"] == "in"


def test_period_goals_and_under_1_5_first_half():
    rec = {"ht_home": 0, "ht_away": 1, "ft_home": 2, "ft_away": 1}
    assert period_goals(rec, "1h") == 1
    assert period_goals(rec, "2h") == 2
    assert period_goals(rec, "ft") == 3
    assert period_goals({"ft_home": 1, "ft_away": 0}, "1h") is None

    archive = [
        {"home": "Liverpool", "away": "A", "league_slug": "eng.1", "ht_home": 0, "ht_away": 0, "ft_home": 1, "ft_away": 0},
        {"home": "B", "away": "Liverpool", "league_slug": "eng.1", "ht_home": 1, "ht_away": 0, "ft_home": 1, "ft_away": 1},
        {"home": "Liverpool", "away": "C", "league_slug": "eng.1", "ht_home": 2, "ht_away": 0, "ft_home": 2, "ft_away": 1},
        {"home": "Other", "away": "Side", "league_slug": "eng.1", "ht_home": 0, "ht_away": 0, "ft_home": 0, "ft_away": 0},
    ]
    rate = sample_rate(archive, line=1.5, side="under", period="1h", team="Liverpool", league_slug="eng.1")
    assert rate["n"] == 3 and rate["wins"] == 2 and rate["hit_pct"] == 67
    assert rate["fair_odds"] == 1.5  # 1 / (2/3)


def test_evaluate_and_handle_command():
    spec = parse_bets_args(["Liverpool", "1.5", "1h"])
    match = {
        "home": "Liverpool",
        "away": "Everton",
        "league_slug": "eng.1",
        "league_chiclet": "EPL",
        "state": "pre",
        "clock": "KO",
    }
    archive = [
        {"home": "Liverpool", "away": "A", "league_slug": "eng.1", "ht_home": 0, "ht_away": 0, "ft_home": 0, "ft_away": 1},
        {"home": "Everton", "away": "B", "league_slug": "eng.1", "ht_home": 0, "ht_away": 1, "ft_home": 1, "ft_away": 1},
        {"home": "C", "away": "D", "league_slug": "eng.1", "ht_home": 0, "ht_away": 0, "ft_home": 0, "ft_away": 0},
    ]
    payload = evaluate_bet(spec, match=match, records=archive)
    text = format_bet(payload)
    assert "Under 1.5 1st half" in text
    assert "Liverpool vs Everton" in text
    assert "Liverpool:" in text and "Everton:" in text and "League:" in text
    assert "fair" in text

    board = {"leagues": [{"slug": "eng.1", "chiclet": "EPL", "matches": [match]}]}
    reply = handle_command("!soc bets Liverpool 1.5 1h", board=board, records=archive)
    assert reply and "Under 1.5 1st half" in reply
    assert handle_command("!soc bets", board=board, records=[]) == USAGE

    live = {
        **match,
        "state": "in",
        "clock": "28'",
        "detail": "1st Half",
        "home_score": 2,
        "away_score": 0,
    }
    busted = evaluate_bet(spec, match=live, records=archive)
    assert busted["so_far"] == 2
    assert "under is dead" in format_bet(busted)

    late = {**live, "clock": "67'", "detail": "2nd Half"}
    late_1h = evaluate_bet(spec, match=late, records=archive)
    assert late_1h["so_far"] is None
    spec_2h = parse_bets_args(["Liverpool", "1.5", "2h"])
    late_2h = evaluate_bet(spec_2h, match=late, records=archive)
    assert late_2h["so_far"] is None
    late_2h_known = evaluate_bet(
        spec_2h,
        match={**late, "ht_home": 2, "ht_away": 0},
        records=archive,
    )
    assert late_2h_known["so_far"] == 0
