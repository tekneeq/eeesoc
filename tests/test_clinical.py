"""Clinical power: goals per 100 xG per club, ranked within the league."""

from __future__ import annotations

import pytest

from eeesoc import clinical, halftime
from eeesoc.clinical import (
    OFFENSE_WEIGHTS_SOT,
    OFFENSE_WEIGHTS_XG,
    PRIOR_GAMES,
    PRIOR_XG,
    build_clinical_board,
    clinical_board,
    lookup_team,
    team_key,
)


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


def _expected_offense(row, par, weights):
    score = 0.0
    for key, w in weights.items():
        games = row["games_xg"] if key == "xg" else row["games"]
        if par[key] <= 0:
            score += w
            continue
        rate = (row[key] + PRIOR_GAMES * par[key]) / (games + PRIOR_GAMES)
        score += w * rate / par[key]
    return round(100 * score)


def test_offense_power_blends_creation_volume_and_goals_vs_league():
    board = build_clinical_board(_records())
    epl = board["leagues"]["eng.1"]
    assert epl["offense_weights"] == OFFENSE_WEIGHTS_XG
    # 4 team-games: 4.0 xG, 11 shots, 8 SOT, 0 corners, 4 goals.
    assert epl["par_rates"] == {"xg": 1.0, "shots": 2.75, "sot": 2.0, "corners": 0.0, "goals": 1.0}
    par = epl["par_rates"]
    by = {t["team"]: t for t in epl["teams"]}

    sharp = by["Sharp FC"]
    assert sharp["shots_per_game"] == 2.5 and sharp["sot_per_game"] == 2.0 and sharp["corners_per_game"] == 0.0
    # Most clinical club in the league, but creates less than par per game → blunt attack.
    assert sharp["offense_power"] == _expected_offense(sharp, par, OFFENSE_WEIGHTS_XG) == 93
    assert sharp["potent"] is False
    assert sharp["rank"] == 1 and sharp["offense_rank"] == 1  # only ranked club

    blunt = by["Blunt Town"]
    # Wasteful in front of goal (0 from 2.0 xG) yet creates well above par → potent.
    assert blunt["clinical"] is False
    assert blunt["offense_power"] == _expected_offense(blunt, par, OFFENSE_WEIGHTS_XG) == 111
    assert blunt["potent"] is True
    assert blunt["offense_rank"] is None  # single game

    par_utd = by["Par United"]
    assert par_utd["offense_power"] == 98 and par_utd["potent"] is False


def test_offense_rank_counts_corners_as_pressure():
    base = [_ev(10, "shot_on", "home", 0.3), _ev(20, "shot_on", "away", 0.3)]
    recs = [
        _rec("1", "Calm FC", "Press Town", "1", "2", base + [_ev(30, "corner", "away"), _ev(40, "corner", "away"), _ev(70, "corner", "away")]),
        _rec("2", "Press Town", "Calm FC", "2", "1", base + [_ev(50, "corner", "home"), _ev(60, "corner", "home")]),
    ]
    board = build_clinical_board(recs)
    epl = board["leagues"]["eng.1"]
    assert epl["par_rates"]["corners"] == 1.25  # 5 corners over 4 team-games
    by = {t["team"]: t for t in epl["teams"]}
    # Identical shooting; only the corner count separates the two attacks.
    assert by["Press Town"]["corners"] == 5 and by["Calm FC"]["corners"] == 0
    assert by["Press Town"]["power"] == by["Calm FC"]["power"]
    assert by["Press Town"]["offense_power"] > 100 > by["Calm FC"]["offense_power"]
    assert by["Press Town"]["potent"] and not by["Calm FC"]["potent"]
    assert by["Press Town"]["offense_rank"] == 1 and by["Calm FC"]["offense_rank"] == 2


def test_defense_power_rewards_allowing_fewer_chances():
    board = build_clinical_board(_records())
    epl = board["leagues"]["eng.1"]
    assert epl["par_xga_per_game"] == pytest.approx(1.0)  # 4.0 xG over 4 team-games
    by = {t["team"]: t for t in epl["teams"]}

    sharp = by["Sharp FC"]
    # Faced 2.0 xG in game 1 and 1.0 in game 2 → 1.5 per game; shrunk: (3 + 2·1)/(2 + 2) = 1.25.
    assert sharp["xg_against"] == pytest.approx(3.0)
    assert sharp["xga_per_game"] == pytest.approx(1.5)
    assert sharp["shots_against"] == 6 and sharp["sot_against"] == 4
    assert sharp["defense_power"] == 80
    assert sharp["solid"] is False
    assert sharp["conceded_per_game"] == 1.0
    assert sharp["defense_rank"] == 1  # only ranked club

    blunt = by["Blunt Town"]
    # Faced 1.0 xG in one game; shrunk: (1 + 2)/(1 + 2) = 1.0 → par.
    assert blunt["defense_power"] == 100 and blunt["solid"] is False
    assert blunt["defense_rank"] is None

    par = by["Par United"]
    # Faced 0.0 xG; shrunk: (0 + 2)/(1 + 2) = 0.67 → 150.
    assert par["defense_power"] == 150 and par["solid"] is True


def test_defense_rank_orders_ranked_clubs_independently_of_attack():
    recs = _records() + [
        _rec("3", "Blunt Town", "Par United", "20", "30", [_ev(10, "shot_on", "away", 0.4), _ev(70, "shot", "home", 0.1)]),
    ]
    board = build_clinical_board(recs)
    by = {t["team"]: t for t in board["leagues"]["eng.1"]["teams"]}
    assert board["leagues"]["eng.1"]["teams_ranked"] == 3
    # Attack: Sharp first. Defence: Par (allowed 0.0 + 0.1) ahead of Sharp (3.0) and Blunt (1.0 + 0.4).
    assert by["Sharp FC"]["rank"] == 1
    assert by["Par United"]["defense_rank"] == 1
    assert by["Blunt Town"]["defense_rank"] == 2
    assert by["Sharp FC"]["defense_rank"] == 3
    assert by["Par United"]["defense_power"] > by["Blunt Town"]["defense_power"] > by["Sharp FC"]["defense_power"]


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
    # Defence falls back to SOT allowed per game: league 8 SOT / 4 team-games = 2.0.
    assert ned["par_sot_against_per_game"] == 2.0
    assert by["Ajax"]["sot_against"] == 3 and by["Twente"]["sot_against"] == 5
    assert by["Ajax"]["defense_power"] > 100 and by["Ajax"]["solid"]
    assert by["Twente"]["defense_power"] < 100 and not by["Twente"]["solid"]
    assert by["Ajax"]["defense_rank"] == 1 and by["Twente"]["defense_rank"] == 2
    # Offence drops the xG term without an xG feed: shots / SOT / corners / goals only.
    assert ned["offense_weights"] == OFFENSE_WEIGHTS_SOT
    assert ned["par_rates"]["xg"] == 0.0
    assert by["Ajax"]["offense_power"] == _expected_offense(by["Ajax"], ned["par_rates"], OFFENSE_WEIGHTS_SOT)
    assert by["Ajax"]["offense_power"] > 100 > by["Twente"]["offense_power"]
    assert by["Ajax"]["offense_rank"] == 1 and by["Twente"]["offense_rank"] == 2


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
