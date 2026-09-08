"""Kickoff / goal Discord alerts and Similar path formatting."""

from __future__ import annotations

from eeesoc.discord_alerts import (
    detect_alerts,
    flatten_board,
    format_alert,
    format_similar_paths,
    poll_alerts,
    scoreline_for_alert,
)
from eeesoc.models import GoalEvent, Match


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


def _row(**kwargs):
    base = {
        "event_id": "1",
        "state": "in",
        "home": "Liverpool",
        "away": "Chelsea",
        "home_score": 0,
        "away_score": 0,
        "clock": "1'",
        "detail": "",
        "home_id": "364",
        "away_id": "363",
        "league_slug": "eng.1",
        "league_chiclet": "EPL",
    }
    base.update(kwargs)
    return base


def _board(*matches):
    return {"leagues": [{"slug": "eng.1", "name": "EPL", "chiclet": "EPL", "matches": list(matches)}]}


def test_flatten_skips_postponed():
    board = _board(
        _row(event_id="1"),
        _row(event_id="2", state="pre", detail="Postponed", home="A", away="B"),
    )
    rows = flatten_board(board)
    assert "1" in rows
    assert "2" not in rows


def test_first_poll_seeds_without_posting():
    current = {"1": _row(state="in", home_score=1)}
    alerts, nxt = detect_alerts({}, current, seeded=False)
    assert alerts == []
    assert nxt["1"]["home_score"] == 1
    assert nxt["1"]["path"][-1] == "1-0"


def test_kickoff_when_pre_becomes_in():
    prev = {"1": {"state": "pre", "home_score": 0, "away_score": 0, "path": ["0-0"]}}
    current = {"1": _row(state="in", clock="1'")}
    alerts, _nxt = detect_alerts(prev, current, seeded=True)
    kinds = [a.kind for a in alerts]
    assert kinds == ["kickoff"]
    assert "Kickoff" in format_alert(alerts[0])
    assert "Liverpool vs Chelsea" in format_alert(alerts[0])


def test_goal_posts_current_score_and_path():
    prev = {"1": {"state": "in", "home_score": 0, "away_score": 0, "path": ["0-0"]}}
    current = {"1": _row(state="in", home_score=1, away_score=0, clock="24'")}
    alerts, nxt = detect_alerts(prev, current, seeded=True)
    assert [a.kind for a in alerts] == ["goal"]
    text = format_alert(alerts[0])
    assert "Goal" in text
    assert "Liverpool 1–0 Chelsea" in text
    assert "0-0 → 1-0" in text
    assert nxt["1"]["path"] == ["0-0", "1-0"]


def test_already_in_play_is_not_a_kickoff():
    current = {"1": _row(state="in", home_score=1, clock="67'")}
    alerts, _nxt = detect_alerts({}, current, seeded=True)
    assert alerts == []


def test_no_alert_when_score_unchanged():
    prev = {"1": {"state": "in", "home_score": 1, "away_score": 0, "path": ["0-0", "1-0"]}}
    current = {"1": _row(home_score=1, away_score=0, clock="30'")}
    alerts, _nxt = detect_alerts(prev, current, seeded=True)
    assert alerts == []


def test_poll_attaches_similar_paths_for_both_clubs():
    corpus = [
        _match("a", "Liverpool", "Chelsea", [(20, "home"), (30, "away"), (40, "home"), (50, "away")], (2, 2)),
        _match("b", "Chelsea", "Liverpool", [(10, "home"), (20, "away")], (1, 1)),
        _match("c", "Arsenal", "Everton", [(15, "home")], (1, 0)),
    ]
    board = _board(_row(state="in", home_score=0, away_score=0, clock="1'"))
    # seed
    _msgs, seeded = poll_alerts(board=board, state={"seeded": False, "matches": {}}, corpus=corpus)
    assert _msgs == []
    # kickoff
    live = _board(_row(state="in", home_score=0, away_score=0, clock="1'"))
    # Pretend previous was pre so kickoff fires
    seeded["matches"]["1"]["state"] = "pre"
    messages, _ = poll_alerts(board=live, state=seeded, corpus=corpus)
    assert len(messages) == 1
    text = messages[0]
    assert "Kickoff" in text
    assert "From here" in text
    assert "Liverpool:" in text
    assert "Chelsea:" in text


def test_scoreline_for_goal_uses_team_perspective():
    corpus = [
        _match("a", "Liverpool", "Chelsea", [(20, "home")], (1, 0)),
        _match("b", "Liverpool", "Arsenal", [(15, "home"), (70, "away")], (1, 1)),
    ]
    from eeesoc.discord_alerts import Alert

    alert = Alert(
        kind="goal",
        match=_row(home_score=1, away_score=0, clock="24'"),
        prev_home=0,
        prev_away=0,
        path=["0-0", "1-0"],
    )
    sl = scoreline_for_alert(alert, corpus)
    assert sl is not None
    assert sl["scoreline"] == "1-0"
    text = format_similar_paths(sl)
    assert "Liverpool:" in text and "Chelsea:" in text
    assert "1-0" in text
