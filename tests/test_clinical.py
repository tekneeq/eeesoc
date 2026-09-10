"""Clinical power: goals per 100 xG per club, ranked within the league."""

from __future__ import annotations

import pytest

from eeesoc import clinical, halftime
from eeesoc.clinical import (
    FORM_GAMES,
    HALF_GOALS_MIN_SAMPLE,
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


def _rec(eid, home, away, home_id, away_id, events, *, league="eng.1", chiclet="EPL", start=""):
    # Scoreboard goals: an own_goal event is filed under the side it counts for.
    def goals(side, upto=999):
        return sum(1 for e in events if e["team"] == side and e["kind"] in {"goal", "own_goal"} and e["minute"] <= upto)

    return {
        "ht_home": goals("home", 45),
        "ht_away": goals("away", 45),
        "event_id": eid,
        "league_slug": league,
        "league_chiclet": chiclet,
        "home": home,
        "away": away,
        "home_id": home_id,
        "away_id": away_id,
        "start": start,
        "ft_home": goals("home"),
        "ft_away": goals("away"),
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


def test_momentum_is_recency_weighted_points_vs_league():
    board = build_clinical_board(_records())
    epl = board["leagues"]["eng.1"]
    # Sharp W 3-1 then L 0-1; Blunt L; Par W → 6 points over 4 team-games.
    assert epl["par_points_per_game"] == 1.5
    assert epl["par_goals_per_game"] == 1.25
    assert epl["form_games"] == FORM_GAMES
    by = {t["team"]: t for t in epl["teams"]}

    sharp = by["Sharp FC"]
    assert sharp["form"] == "WL" and sharp["points"] == 3 and sharp["scored"] == 3
    assert sharp["recent_points"] == 3 and sharp["recent_scored"] == 3 and sharp["recent_allowed"] == 2
    assert sharp["points_per_game"] == 1.5 and sharp["scored_per_game"] == 1.5
    # Weights 1 (older W) and 2 (newer L), scaled to 2 games: 3·2/3 = 2 pts; shrunk (2 + 2·1.5)/4 = 1.25 → 83.
    assert sharp["momentum"] == 83 and sharp["rising"] is False
    assert sharp["momentum_rank"] == 1  # only ranked club
    assert [g["letter"] for g in sharp["recent"]] == ["W", "L"]
    assert sharp["recent"][1]["opponent"] == "Par United" and sharp["recent"][1]["venue"] == "away"

    # Same two results in the other order: the win is now the newest → rising.
    recs = _records()
    recs[0]["start"], recs[1]["start"] = "2026-09-08T15:00Z", "2026-09-01T15:00Z"
    flipped = {t["team"]: t for t in build_clinical_board(recs)["leagues"]["eng.1"]["teams"]}["Sharp FC"]
    assert flipped["form"] == "LW"
    assert flipped["momentum"] == 117 and flipped["rising"] is True

    assert by["Blunt Town"]["form"] == "L" and by["Blunt Town"]["momentum"] < 100
    assert by["Par United"]["form"] == "W" and by["Par United"]["momentum"] > 100
    assert by["Blunt Town"]["momentum_rank"] is None  # single game


def test_momentum_only_looks_at_the_last_form_games():
    recs = []
    # Six straight wins, then a loss: only the last five (WWWWL) count.
    for i in range(6):
        recs.append(_rec(f"w{i}", "Hot FC", f"Foe {i}", "1", f"9{i}", [_ev(10, "goal", "home", 0.5)], start=f"2026-08-{i + 1:02d}"))
    recs.append(_rec("l", "Foe X", "Hot FC", "99", "1", [_ev(10, "goal", "home", 0.5)], start="2026-08-20"))
    hot = {t["team"]: t for t in build_clinical_board(recs)["leagues"]["eng.1"]["teams"]}["Hot FC"]
    assert hot["games"] == 7 and hot["points"] == 18
    assert hot["form"] == "WWWWL" and len(hot["recent"]) == FORM_GAMES
    assert hot["recent_points"] == 12


def test_potential_strips_finishing_luck_and_tags_results_gap():
    board = build_clinical_board(_records())
    by = {t["team"]: t for t in board["leagues"]["eng.1"]["teams"]}

    sharp = by["Sharp FC"]
    # Creates 0.75 of par (shrunk) and defends at 80 → √(0.75·0.8) = 77; scores 3 from 1.0 xG so results (111) run ahead.
    assert sharp["potential"] == 77
    assert sharp["results_power"] == 111
    assert sharp["potential_tag"] == "overachieving"
    assert sharp["potential_rank"] == 1  # only ranked club

    blunt = by["Blunt Town"]
    # Creates 1.33 of par at par defence → 115, but 1 goal from 2.0 xG leaves results at 80 → upside.
    assert blunt["potential"] == 115
    assert blunt["results_power"] == 80
    assert blunt["potential_tag"] == "upside"
    assert blunt["potential_rank"] is None

    par = by["Par United"]
    # Par creation, 150 defence → 122; results 118 → within the gap → steady.
    assert par["potential"] == 122 and par["results_power"] == 118
    assert par["potential_tag"] == "steady"


def test_potential_and_momentum_rank_among_ranked_clubs():
    recs = _records() + [
        _rec("3", "Blunt Town", "Par United", "20", "30", [_ev(10, "shot_on", "away", 0.4), _ev(70, "shot", "home", 0.1)]),
    ]
    by = {t["team"]: t for t in build_clinical_board(recs)["leagues"]["eng.1"]["teams"]}
    ranks = {name: by[name]["potential_rank"] for name in ("Sharp FC", "Blunt Town", "Par United")}
    assert sorted(ranks.values()) == [1, 2, 3]
    assert by["Par United"]["potential_rank"] == 1  # best defence, par creation
    mranks = {name: by[name]["momentum_rank"] for name in ("Sharp FC", "Blunt Town", "Par United")}
    assert sorted(mranks.values()) == [1, 2, 3]
    # Par: W then D → most recent form; Blunt: L then D; Sharp: W then L.
    assert by["Par United"]["form"] == "WD" and by["Par United"]["momentum_rank"] == 1
    assert by["Blunt Town"]["form"] == "LD"


def test_clean_sheets_total_recent_and_rank():
    board = build_clinical_board(_records())
    epl = board["leagues"]["eng.1"]
    # Only Par United's 1-0 was a shutout: 1 of 4 team-games.
    assert epl["par_clean_sheet_pct"] == 25
    by = {t["team"]: t for t in epl["teams"]}

    sharp = by["Sharp FC"]  # 3-1 then 0-1: conceded in both
    assert sharp["clean_sheets"] == 0 and sharp["clean_sheet_pct"] == 0
    assert sharp["recent_clean_sheets"] == 0
    assert sharp["tight"] is False
    assert sharp["clean_sheet_rank"] == 1  # only ranked club

    par = by["Par United"]
    assert par["clean_sheets"] == 1 and par["clean_sheet_pct"] == 100
    assert par["recent_clean_sheets"] == 1
    assert par["tight"] is True
    assert par["clean_sheet_rank"] is None  # single game

    assert by["Blunt Town"]["clean_sheets"] == 0

    # Add a 0-0: both sides keep a clean sheet and all three clubs become ranked.
    recs = _records() + [_rec("3", "Blunt Town", "Par United", "20", "30", [_ev(10, "shot_on", "away", 0.4)])]
    by3 = {t["team"]: t for t in build_clinical_board(recs)["leagues"]["eng.1"]["teams"]}
    assert by3["Par United"]["clean_sheets"] == 2 and by3["Par United"]["clean_sheet_rank"] == 1
    assert by3["Blunt Town"]["clean_sheets"] == 1 and by3["Blunt Town"]["clean_sheet_rank"] == 2
    assert by3["Sharp FC"]["clean_sheets"] == 0 and by3["Sharp FC"]["clean_sheet_rank"] == 3


def test_clean_sheet_ties_go_to_fewer_games_and_recent_counts_last_five():
    recs = []
    # Wall FC: six 1-0 wins then a 0-1 loss → 6 clean sheets, 4 in the last five.
    for i in range(6):
        recs.append(_rec(f"w{i}", "Wall FC", f"Foe {i}", "1", f"9{i}", [_ev(10, "goal", "home", 0.5)], start=f"2026-08-{i + 1:02d}"))
    recs.append(_rec("l", "Foe X", "Wall FC", "99", "1", [_ev(10, "goal", "home", 0.5)], start="2026-08-20"))
    # Foe 0 and Foe 1 each also play a 0-0 against each other → 1 clean sheet in 2 games each.
    recs.append(_rec("d", "Foe 0", "Foe 1", "90", "91", [_ev(10, "shot_on", "home", 0.2)], start="2026-08-21"))
    by = {t["team"]: t for t in build_clinical_board(recs)["leagues"]["eng.1"]["teams"]}
    wall = by["Wall FC"]
    assert wall["clean_sheets"] == 6 and wall["games"] == 7
    assert wall["clean_sheet_pct"] == 86
    assert wall["form"] == "WWWWL" and wall["recent_clean_sheets"] == 4
    assert wall["clean_sheet_rank"] == 1
    # Foe 0 and Foe 1 tie on 1 clean sheet from 2 games; Foe X (1 game) is unranked.
    assert {by["Foe 0"]["clean_sheet_rank"], by["Foe 1"]["clean_sheet_rank"]} == {2, 3}
    assert by["Foe X"]["clean_sheet_rank"] is None and by["Foe X"]["clean_sheets"] == 1


def test_half_goals_split_first_half_and_second_half_by_ht_score():
    board = build_clinical_board(_records())
    epl = board["leagues"]["eng.1"]
    assert epl["goal_buckets"] == ["0", "1", "2", "3+"]
    assert epl["half_goals_min_sample"] == HALF_GOALS_MIN_SAMPLE
    by = {t["team"]: t for t in epl["teams"]}

    def dist(*counts):
        return {"n": sum(counts), "counts": list(counts)}

    # Game 1: Sharp 2-0 Blunt at HT (goals 15', 30'), 3-1 FT → 2H: Sharp scored 1, allowed 1.
    # Game 2: Par 1-0 Sharp at HT (goal 10'), 1-0 FT → 2H: nothing.
    sharp = by["Sharp FC"]["half_goals"]
    assert sharp["first"] == {"total": dist(0, 1, 1, 0), "scored": dist(1, 0, 1, 0), "allowed": dist(1, 1, 0, 0)}
    assert sharp["second_by_ht"] == {
        "0-1": {"total": dist(1, 0, 0, 0), "scored": dist(1, 0, 0, 0), "allowed": dist(1, 0, 0, 0)},
        "2-0": {"total": dist(0, 0, 1, 0), "scored": dist(0, 1, 0, 0), "allowed": dist(0, 1, 0, 0)},
    }
    # The same games from the other side of the pitch flip the half-time key and swap scored / allowed.
    blunt = by["Blunt Town"]["half_goals"]
    assert blunt["first"] == {"total": dist(0, 0, 1, 0), "scored": dist(1, 0, 0, 0), "allowed": dist(0, 0, 1, 0)}
    assert blunt["second_by_ht"] == {"0-2": {"total": dist(0, 0, 1, 0), "scored": dist(0, 1, 0, 0), "allowed": dist(0, 1, 0, 0)}}
    assert by["Par United"]["half_goals"]["second_by_ht"] == {"1-0": {"total": dist(1, 0, 0, 0), "scored": dist(1, 0, 0, 0), "allowed": dist(1, 0, 0, 0)}}

    # League table: each game once, home-away view (scored = the home side's goals).
    assert epl["half_goals"]["first"] == {"total": dist(0, 1, 1, 0), "scored": dist(0, 1, 1, 0), "allowed": dist(2, 0, 0, 0)}
    assert epl["half_goals"]["second_by_ht"] == {
        "1-0": {"total": dist(1, 0, 0, 0), "scored": dist(1, 0, 0, 0), "allowed": dist(1, 0, 0, 0)},
        "2-0": {"total": dist(0, 0, 1, 0), "scored": dist(0, 1, 0, 0), "allowed": dist(0, 1, 0, 0)},
    }


def test_half_goals_buckets_three_plus_and_skips_games_without_ht_score():
    events = [_ev(5, "goal", "home", 0.3), _ev(20, "goal", "away", 0.3), _ev(40, "goal", "home", 0.3), _ev(44, "goal", "away", 0.3)]
    events += [_ev(50, "goal", "home", 0.3), _ev(60, "goal", "home", 0.3), _ev(75, "goal", "away", 0.3)]
    goalfest = _rec("g", "Sharp FC", "Blunt Town", "10", "20", events)  # 2-2 HT, 4-3 FT
    assert goalfest["ht_home"] == 2 and goalfest["ht_away"] == 2
    unknown = _rec("u", "Sharp FC", "Par United", "10", "30", [_ev(10, "goal", "home", 0.5)])
    unknown.pop("ht_home")
    unknown.pop("ht_away")
    by = {t["team"]: t for t in build_clinical_board([goalfest, unknown])["leagues"]["eng.1"]["teams"]}
    hg = by["Sharp FC"]["half_goals"]
    assert by["Sharp FC"]["games"] == 2
    # 4 first-half goals → 3+ (2 scored, 2 allowed); the HT-less game is skipped.
    assert hg["first"]["total"] == {"n": 1, "counts": [0, 0, 0, 1]}
    assert hg["first"]["scored"] == {"n": 1, "counts": [0, 0, 1, 0]}
    assert hg["first"]["allowed"] == {"n": 1, "counts": [0, 0, 1, 0]}
    # 3 second-half goals: Sharp scored 2, allowed 1.
    assert hg["second_by_ht"] == {
        "2-2": {"total": {"n": 1, "counts": [0, 0, 0, 1]}, "scored": {"n": 1, "counts": [0, 0, 1, 0]}, "allowed": {"n": 1, "counts": [0, 1, 0, 0]}}
    }


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
