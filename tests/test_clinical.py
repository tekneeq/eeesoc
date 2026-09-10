"""Clinical power: goals per 100 xG per club, ranked within the league."""

from __future__ import annotations

import pytest

from eeesoc import clinical, halftime
from eeesoc.clinical import PRIOR_XG, build_clinical_board, clinical_board, lookup_team, team_key


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    halftime.clear_record_cache()
    clinical.clear_clinical_cache()
    yield
    halftime.clear_record_cache()
    clinical.clear_clinical_cache()


def _ev(minute, kind, team, xg=None):
    return {"minute": minute, "kind": kind, "team": team, "xg": xg, "period": 1 if minute <= 45 else 2}


def _rec(eid, home, away, home_id, away_id, events, *, league="eng.1", chiclet="EPL"):
    return {
        "event_id": eid,
        "league_slug": league,
        "league_chiclet": chiclet,
        "home": home,
        "away": away,
        "home_id": home_id,
        "away_id": away_id,
        "has_xg": any((e.get("xg") or 0) > 0 for e in events),
        "events": events,
    }


def _records():
    # Sharp FC: 3 goals from 1.0 xG. Blunt Town: 0 goals from 2.0 xG. Par United: 1 goal from 1.0 xG.
    return [
        _rec(
            "1",
            "Sharp FC",
            "Blunt Town",
            "10",
            "20",
            [
                _ev(5, "shot_on", "home", 0.2),
                _ev(15, "goal", "home", 0.3),
                _ev(30, "goal", "home", 0.3),
                _ev(60, "goal", "home", 0.2),
                _ev(20, "shot_on", "away", 0.5),
                _ev(50, "shot_on", "away", 0.5),
                _ev(70, "shot", "away", 0.5),
                _ev(80, "blocked", "away", 0.5),
                _ev(88, "own_goal", "away"),  # credited to Blunt on the board, not to their finishing
            ],
        ),
        _rec(
            "2",
            "Par United",
            "Sharp FC",
            "30",
            "10",
            [
                _ev(10, "goal", "home", 0.6),
                _ev(40, "shot_on", "home", 0.4),
                _ev(75, "shot", "away", 0.0),
            ],
        ),
    ]


def test_power_ranks_finishing_over_xg_with_prior():
    board = build_clinical_board(_records())
    epl = board["leagues"]["eng.1"]
    assert epl["basis"] == "xg"
    by = {t["team"]: t for t in epl["teams"]}

    sharp = by["Sharp FC"]
    assert sharp["games"] == 2 and sharp["goals"] == 3 and sharp["xg"] == pytest.approx(1.0)
    assert sharp["shots"] == 5 and sharp["sot"] == 4
    assert sharp["conversion_pct"] == 75
    # League ratio: 4 goals / 4.0 xG = 1.0 → prior adds 2 goals per 2 xG.
    assert sharp["power"] == round(100 * (3 + PRIOR_XG) / (1.0 + PRIOR_XG))
    assert sharp["clinical"] is True
    assert sharp["rank"] == 1

    blunt = by["Blunt Town"]
    assert blunt["goals"] == 0 and blunt["xg"] == pytest.approx(2.0)
    assert blunt["power"] == 50
    assert blunt["clinical"] is False
    # The own goal credited to Blunt is not their finishing, but Sharp still conceded it.
    assert sharp["conceded"] == 2
    # One game → unranked until MIN_GAMES.
    assert blunt["ranked"] is False and blunt["rank"] is None

    par = by["Par United"]
    assert par["power"] == 100
    assert par["clinical"] is False  # exactly par is not "more than they do not"
    assert par["rank"] is None  # single game
    assert epl["teams_ranked"] == 1
    assert [t["team"] for t in epl["teams"]][0] == "Sharp FC"


def test_league_without_xg_falls_back_to_sot_conversion():
    events_a = [_ev(10, "goal", "home"), _ev(20, "goal", "home"), _ev(30, "shot_on", "home"), _ev(40, "shot_on", "away"), _ev(50, "shot_on", "away")]
    events_b = [_ev(10, "shot_on", "home"), _ev(20, "goal", "away"), _ev(30, "shot_on", "away")]
    recs = [
        _rec("a", "Ajax", "Twente", "1", "2", events_a, league="ned.1", chiclet="Eredivisie"),
        _rec("b", "Twente", "Ajax", "2", "1", events_b, league="ned.1", chiclet="Eredivisie"),
    ]
    board = build_clinical_board(recs)
    ned = board["leagues"]["ned.1"]
    assert ned["basis"] == "sot"
    by = {t["team"]: t for t in ned["teams"]}
    # League: 3 goals / 8 SOT → par 38%. Ajax 3/5 SOT (60%), Twente 0/3.
    assert ned["par_conversion_pct"] == 38
    assert by["Ajax"]["conversion_pct"] == 60
    assert by["Ajax"]["power"] > 100 and by["Ajax"]["clinical"]
    assert by["Twente"]["power"] < 100 and not by["Twente"]["clinical"]
    assert by["Ajax"]["rank"] == 1 and by["Twente"]["rank"] == 2
    assert by["Ajax"]["basis"] == "sot"


def test_lookup_by_id_then_name_and_cache_tracks_archive():
    recs = _records()
    for r in recs:
        halftime.write_json(halftime.record_path(r["league_slug"], r["event_id"]), {**r, "ht_goals": 0, "n_events": 9, "ft_home": 0, "ft_away": 0, "first_half": {}})
    board = clinical_board()
    assert board["archive_total"] == 2
    assert lookup_team(board, "eng.1", team_id="10")["team"] == "Sharp FC"
    assert lookup_team(board, "eng.1", name="sharp fc")["team"] == "Sharp FC"
    assert lookup_team(board, "eng.1", team_id="999", name="Nobody") is None
    assert lookup_team(board, "esp.1", team_id="10") is None
    assert team_key("Brighton & Hove Albion") == "brighton hove albion"

    # Cached until the archive grows.
    assert clinical_board() is board
    extra = _rec("3", "Par United", "Blunt Town", "30", "20", [_ev(9, "goal", "home", 0.5)])
    halftime.write_json(halftime.record_path("eng.1", "3"), {**extra, "ht_goals": 1, "n_events": 1, "ft_home": 1, "ft_away": 0, "first_half": {}})
    fresh = clinical_board()
    assert fresh is not board
    assert fresh["archive_total"] == 3
    assert lookup_team(fresh, "eng.1", team_id="30")["games"] == 2
