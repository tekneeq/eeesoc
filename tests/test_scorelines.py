"""Tests for team aliases and scoreline trees."""

from __future__ import annotations

from eeesoc.models import GoalEvent, Match
from eeesoc.scorelines import (
    build_live_scoreline_eval,
    score_path,
    scoreline_outcomes,
    team_score_path,
    transition_tree,
)
from eeesoc.teams import resolve_team


def _match(mid: str, home: str, away: str, goals: list[tuple[int, str]], ft: tuple[int, int]) -> Match:
    events = [GoalEvent(minute=m, team=t) for m, t in goals]
    return Match(
        match_id=mid,
        season="EPL:2024",
        date="01/01/2024",
        home=home,
        away=away,
        home_goals_ft=ft[0],
        away_goals_ft=ft[1],
        home_shots_ft=10,
        away_shots_ft=8,
        home_sot_ft=4,
        away_sot_ft=3,
        goals=events,
        home_shots_by_min=[0] + [10] * 90,
        away_shots_by_min=[0] + [8] * 90,
        home_sot_by_min=[0] + [4] * 90,
        away_sot_by_min=[0] + [3] * 90,
    )


def test_resolve_team_espn_to_fd():
    assert resolve_team("Nottingham Forest") == "Nott'm Forest"
    assert resolve_team("Nottm Forest") == "Nott'm Forest"
    assert resolve_team(espn_id="393") == "Nott'm Forest"
    assert resolve_team("Liverpool") == "Liverpool"
    assert resolve_team(espn_id="364") == "Liverpool"
    assert resolve_team("Tottenham Hotspur") == "Tottenham"
    assert resolve_team("Unknown FC") is None


def test_score_paths():
    m = _match("x", "Liverpool", "Nott'm Forest", [(24, "away"), (55, "home"), (70, "home")], (2, 1))
    assert score_path(m) == [(0, 0, 0), (24, 0, 1), (55, 1, 1), (70, 2, 1)]
    # Forest away perspective: for/against
    assert team_score_path(m, "away") == [(0, 0, 0), (24, 1, 0), (55, 1, 1), (70, 1, 2)]


def test_scoreline_outcomes_team_and_league():
    corpus = [
        # Liverpool home: hit 2-2, ended 2-2
        _match("a", "Liverpool", "Chelsea", [(20, "home"), (30, "away"), (40, "home"), (50, "away")], (2, 2)),
        # Liverpool away: hit 2-2 for/against (absolute 1-2 then 2-2 from LFC view = scored 2nd)
        # Path away: home Chelsea scores, Liverpool scores twice, Chelsea once → LFC for/against hits 2-2
        _match(
            "b",
            "Chelsea",
            "Liverpool",
            [(10, "home"), (20, "away"), (35, "away"), (60, "home"), (80, "away")],
            (2, 3),
        ),
        # Unrelated
        _match("c", "Arsenal", "Everton", [(15, "home")], (1, 0)),
    ]
    liv = scoreline_outcomes(corpus, for_goals=2, against_goals=2, team="Liverpool")
    assert liv["count"] == 2
    assert liv["pct_ended_same"] == 0.5  # match a ended 2-2; b ended 3-2 for LFC
    assert any(r["score"] == "2-2" for r in liv["ft_distribution"])

    league = scoreline_outcomes(corpus, for_goals=2, against_goals=2, team=None)
    # Matches a and b both visit absolute 2-2
    assert league["count"] == 2
    assert league["pct_ended_same"] == 0.5  # a ended 2-2; b ended 2-3


def test_scoreline_outcomes_at_minute_conditions_on_time():
    corpus = [
        # 0-1 at 60': conceded 20', still 0-1 at 60', equalized 75' → FT 1-1
        _match("late-eq", "Liverpool", "Chelsea", [(20, "away"), (75, "home")], (1, 1)),
        # 0-1 at 60': conceded 55', stayed 0-1 → FT 0-1
        _match("stay", "Liverpool", "Arsenal", [(55, "away")], (0, 1)),
        # Hit 0-1 early but was 1-1 by 40' → NOT 0-1 at 60', must be excluded
        _match("early", "Liverpool", "Everton", [(10, "away"), (40, "home")], (1, 1)),
        # Never behind
        _match("cruise", "Liverpool", "Fulham", [(30, "home")], (1, 0)),
    ]

    # Old behavior: every game that ever hit 0-1 counts (3 of them)
    anytime = scoreline_outcomes(corpus, for_goals=0, against_goals=1, team="Liverpool")
    assert anytime["count"] == 3

    # Time-aware: only games that were 0-1 AT the 60th minute
    at60 = scoreline_outcomes(
        corpus, for_goals=0, against_goals=1, team="Liverpool", at_minute=60
    )
    assert at60["count"] == 2
    ids = {p["match_id"] for p in at60["peers"]}
    assert ids == {"late-eq", "stay"}
    assert at60["at_minute"] == 60
    assert at60["pct_ended_same"] == 0.5
    # Branches: one went 1-1 (after 60'), one reached FT unchanged
    next_scores = {r["score"]: r["count"] for r in at60["next_distribution"]}
    assert next_scores == {"1-1": 1, "FT": 1}


def test_transition_tree_marks_live_branch():
    corpus = [
        _match("a", "Liverpool", "Chelsea", [(20, "away"), (40, "home")], (1, 1)),  # 0-1 → 1-1
        _match("b", "Liverpool", "Arsenal", [(18, "away"), (50, "away")], (0, 2)),  # 0-1 → 0-2
    ]
    tree = transition_tree(
        corpus,
        from_for=0,
        from_against=1,
        team="Liverpool",
        live_to=(1, 1),
    )
    assert tree["count"] == 2
    live = [b for b in tree["branches"] if b["is_live_branch"]]
    assert len(live) == 1
    assert live[0]["score"] == "1-1"
    assert live[0]["pct"] == 0.5


def test_build_live_scoreline_eval_liv_nfo():
    corpus = [
        _match("a", "Liverpool", "Nott'm Forest", [(24, "away"), (60, "home")], (1, 1)),
        _match("b", "Nott'm Forest", "Everton", [(10, "home"), (20, "away"), (70, "home")], (2, 1)),
    ]
    ev = build_live_scoreline_eval(
        corpus,
        home_name="Liverpool",
        away_name="Nottingham Forest",
        home_score=1,
        away_score=1,
        home_id="364",
        away_id="393",
        prev_home=0,
        prev_away=1,
    )
    assert ev["home_fd"] == "Liverpool"
    assert ev["away_fd"] == "Nott'm Forest"
    assert ev["scoreline"] == "1-1"
    assert ev["prev_scoreline"] == "0-1"
    assert ev["home_history"]["count"] >= 1
    # Forward tree always present from current scoreline
    assert ev["trees"]["league"]["from"] == "1-1"
    assert ev["trees"]["league"]["count"] >= 1
    # Retrospective tree highlights the live branch from previous score
    assert ev["trees_from_prev"]["league"]["live_to"] == "1-1"
    assert any(b["is_live_branch"] for b in ev["trees_from_prev"]["league"]["branches"])


def test_build_live_scoreline_eval_time_aware():
    corpus = [
        _match("late-eq", "Liverpool", "Chelsea", [(20, "away"), (75, "home")], (1, 1)),
        _match("stay", "Liverpool", "Arsenal", [(55, "away")], (0, 1)),
        _match("early", "Liverpool", "Everton", [(10, "away"), (40, "home")], (1, 1)),
    ]
    # Live: Liverpool 0-1 down at the 60th (goal just scored at 60')
    ev = build_live_scoreline_eval(
        corpus,
        home_name="Liverpool",
        away_name="Chelsea",
        home_score=0,
        away_score=1,
        prev_home=0,
        prev_away=0,
        minute=60,
        prev_minute=59,
    )
    assert ev["minute"] == 60
    assert ev["home_history"]["at_minute"] == 60
    assert ev["home_history"]["count"] == 2  # 'early' was 1-1 by 60'
    assert ev["trees"]["home"]["at_minute"] == 60
    # Retrospective tree conditioned just before the goal (0-0 at 59')
    assert ev["trees_from_prev"]["home"]["at_minute"] == 59
    assert ev["trees_from_prev"]["home"]["from"] == "0-0"


def test_forward_tree_at_kickoff():
    corpus = [
        _match("a", "Liverpool", "Chelsea", [(20, "home")], (1, 0)),
        _match("b", "Arsenal", "Everton", [(15, "away")], (0, 1)),
    ]
    ev = build_live_scoreline_eval(
        corpus,
        home_name="Liverpool",
        away_name="Chelsea",
        home_score=0,
        away_score=0,
    )
    assert ev["prev_scoreline"] is None
    assert ev["trees_from_prev"] == {}
    assert ev["trees"]["league"]["from"] == "0-0"
    assert ev["trees"]["league"]["count"] >= 1
    assert any(b["score"] == "1-0" for b in ev["trees"]["league"]["branches"])
    assert any(b["score"] == "0-1" for b in ev["trees"]["league"]["branches"])
