"""Tests for the WinProb model, backtest record, and fixtures board."""

from __future__ import annotations

from datetime import date

from eeesoc.models import Match
from eeesoc.winprob import (
    backtest_predictions,
    build_fixture_detail,
    build_ratings,
    build_winprob_board,
    clear_winprob_cache,
    fetch_scheduled_fixtures,
    outcome_probs,
    parse_fd_date,
    parse_site_scoreboard,
    predict_fixture,
    summarize_record,
    team_form,
)


def mk(home, away, hg, ag, day, season="EPL:2025", idx=0):
    return Match(
        match_id=f"{season}:{day}:{home}:{away}:{idx}",
        season=season,
        date=day,
        home=home,
        away=away,
        home_goals_ft=hg,
        away_goals_ft=ag,
        home_shots_ft=0,
        away_shots_ft=0,
        home_sot_ft=0,
        away_sot_ft=0,
    )


def test_parse_fd_date():
    assert parse_fd_date("15/08/2025") == date(2025, 8, 15)
    assert parse_fd_date("15/08/25") == date(2025, 8, 15)
    assert parse_fd_date("2025-08-15") is None
    assert parse_fd_date("") is None


def test_outcome_probs_sum_to_one_and_favor_stronger_side():
    probs = outcome_probs(2.2, 0.7)
    assert abs(probs["home"] + probs["draw"] + probs["away"] - 1.0) < 1e-3
    assert probs["home"] > probs["away"]
    flipped = outcome_probs(0.7, 2.2)
    assert flipped["away"] > flipped["home"]


def test_build_ratings_strong_team_gets_high_attack_low_defence():
    matches = []
    for i in range(10):
        matches.append(mk("Giants", "Minnows", 3, 0, f"{i + 1:02d}/08/2025", idx=i))
        matches.append(mk("Minnows", "Giants", 0, 3, f"{i + 1:02d}/09/2025", idx=100 + i))
    ratings = build_ratings([(m, 1.0) for m in matches])
    assert ratings.attack["Giants"] > 1.0 > ratings.attack["Minnows"]
    assert ratings.defence["Giants"] < 1.0 < ratings.defence["Minnows"]
    pred = predict_fixture(ratings, "Giants", "Minnows")
    assert pred["pick"] == "home"
    assert pred["probs"]["home"] > 0.6


def test_predict_unknown_teams_defaults_to_home_edge():
    ratings = build_ratings([(mk("A", "B", 2, 1, "01/08/2025"), 1.0)])
    pred = predict_fixture(ratings, "X", "Y")
    assert pred["known_home"] is False
    assert abs(pred["probs"]["home"] + pred["probs"]["draw"] + pred["probs"]["away"] - 1.0) < 1e-3


def test_backtest_grades_every_match_and_excludes_presets():
    history = [mk("A", "B", 2, 0, "01/03/2025", season="EPL:2024", idx=i) for i in range(4)]
    current = [
        mk("A", "B", 3, 0, "01/08/2026", idx=0),
        mk("B", "A", 0, 2, "10/08/2026", idx=1),
        mk("A", "B", 1, 1, "20/08/2026", idx=2),
    ]
    preset = mk("Everton", "Demo United", 2, 0, "01/01/2026", idx=9)
    preset.match_id = "EPL:2025:preset:Everton:53"
    rows = backtest_predictions([*current, preset], history)
    assert len(rows) == 3
    assert rows[0]["date"] == "2026-08-01"
    assert {"pick", "actual", "correct", "probs", "ft"} <= set(rows[0])
    # Rows are chronological
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)


def test_summarize_record_splits_last30_and_season():
    rows = [
        {"date": "2026-08-01", "correct": True},
        {"date": "2026-08-20", "correct": False},
        {"date": "2026-09-01", "correct": True},
    ]
    rec = summarize_record(rows, today=date(2026, 9, 5), window_days=30)
    assert rec["season"]["total"] == 3
    assert rec["season"]["correct"] == 2
    assert rec["last30"]["total"] == 2  # cutoff 2026-08-06 drops the Aug 1 game
    assert rec["last30"]["correct"] == 1
    assert rec["last30"]["wrong"] == 1
    assert rec["window_days"] == 30
    assert rec["anchor"] == "2026-09-05"


def test_summarize_record_anchors_to_latest_match_when_window_empty():
    rows = [
        {"date": "2026-05-10", "correct": True},
        {"date": "2026-05-24", "correct": False},
        {"date": "2026-04-01", "correct": True},
    ]
    rec = summarize_record(rows, today=date(2026, 9, 5), window_days=30)
    assert rec["anchor"] == "2026-05-24"
    assert rec["last30"]["total"] == 2  # the two May games within 30d of the anchor
    assert rec["season"]["total"] == 3


SITE_PAYLOAD = {
    "events": [
        {
            "id": "700001",
            "date": "2026-09-06T14:00Z",
            "competitions": [
                {
                    "status": {"type": {"state": "pre", "detail": "Scheduled"}},
                    "competitors": [
                        {"homeAway": "home", "team": {"id": "359", "displayName": "Arsenal"}},
                        {"homeAway": "away", "team": {"id": "363", "displayName": "Chelsea"}},
                    ],
                }
            ],
        },
        {
            "id": "700002",
            "date": "2026-09-05T11:30Z",
            "competitions": [
                {
                    "status": {"type": {"state": "in", "detail": "55'"}},
                    "competitors": [
                        {"homeAway": "home", "team": {"id": "364", "displayName": "Liverpool"}},
                        {"homeAway": "away", "team": {"id": "368", "displayName": "Everton"}},
                    ],
                }
            ],
        },
    ]
}


def test_parse_site_scoreboard():
    fixtures = parse_site_scoreboard(SITE_PAYLOAD)
    assert len(fixtures) == 2
    assert fixtures[0]["home"] == "Arsenal"
    assert fixtures[0]["home_id"] == "359"
    assert fixtures[0]["state"] == "pre"
    assert fixtures[1]["state"] == "in"


def test_fetch_scheduled_keeps_pre_only_and_dedupes():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return SITE_PAYLOAD

    fixtures = fetch_scheduled_fixtures(days=3, today=date(2026, 9, 5), fetcher=fake_fetch)
    assert len(calls) == 3
    assert len(fixtures) == 1  # same event on each day, in-play one dropped
    assert fixtures[0]["event_id"] == "700001"
    assert fixtures[0]["away"] == "Chelsea"


def _corpus():
    history = []
    current = []
    for i in range(6):
        history.append(mk("Arsenal", "Chelsea", 2, 0, f"{i + 1:02d}/10/2024", season="EPL:2024", idx=i))
        history.append(mk("Chelsea", "Arsenal", 1, 1, f"{i + 1:02d}/02/2025", season="EPL:2024", idx=50 + i))
    for i in range(4):
        current.append(mk("Arsenal", "Chelsea", 3, 1, f"{i + 1:02d}/08/2026", idx=i))
        current.append(mk("Chelsea", "Arsenal", 0, 1, f"{i + 10:02d}/08/2026", idx=20 + i))
    return current, history


def test_build_winprob_board():
    clear_winprob_cache()
    current, history = _corpus()

    def fake_fetch(url):
        return SITE_PAYLOAD

    board = build_winprob_board(
        current, history, days=2, today=date(2026, 9, 5), fetcher=fake_fetch, use_cache=False
    )
    assert board["record"]["season"]["total"] == len(current)
    assert board["record"]["last30"]["total"] <= board["record"]["season"]["total"]
    assert board["recent_results"]
    assert len(board["fixtures"]) == 1
    fx = board["fixtures"][0]
    assert fx["home_fd"] == "Arsenal"
    assert fx["away_fd"] == "Chelsea"
    assert fx["pick"] in {"home", "draw", "away"}
    probs = fx["probs"]
    assert abs(probs["home"] + probs["draw"] + probs["away"] - 1.0) < 1e-3
    # Arsenal dominate this synthetic corpus
    assert fx["pick_team"] == "Arsenal"


def test_team_form_last5_and_season_record():
    current, history = _corpus()
    form = team_form("Arsenal", current, history)
    assert len(form["last5"]) == 5
    assert form["season"]["played"] == 8
    assert form["season"]["wins"] == 8
    # newest first
    dates = [r["date"] for r in form["last5"]]
    assert dates == sorted(dates, reverse=True)


def test_build_fixture_detail_maps_espn_names():
    current, history = _corpus()
    detail = build_fixture_detail(
        "Arsenal", "Chelsea", current, history, home_id="359", away_id="363"
    )
    assert detail["home_fd"] == "Arsenal"
    assert detail["prediction"]["pick_team"] == "Arsenal"
    assert len(detail["home_form"]["last5"]) == 5
    assert detail["h2h"]
    assert detail["h2h"][0]["date"] >= detail["h2h"][-1]["date"]

    missing = build_fixture_detail("Unknown FC", "Chelsea", current, history)
    assert missing["error"]
