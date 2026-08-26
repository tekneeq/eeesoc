"""Tests for eeesoc Matches + Similar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eeesoc.cache import cache_root, read_json, write_json
from eeesoc.data import (
    inject_everton_preset,
    matches_from_csv,
    parse_warm_spec,
    previous_season_label,
    save_season,
    load_season,
)
from eeesoc.models import Match, MatchSnapshot
from eeesoc.similar import find_similar, snapshot_distance
from eeesoc.timeline import build_timelines, cumulative_shots, place_goals


SAMPLE_CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,HS,AS,HST,AST
E0,16/08/2024,20:00,Man United,Fulham,1,0,H,0,0,D,R Jones,14,10,5,2
E0,17/08/2024,12:30,Ipswich,Liverpool,0,2,A,0,0,D,T Robinson,7,18,2,5
"""


def test_parse_warm_spec_epl_2025():
    label, code = parse_warm_spec("EPL:2025")
    assert label == "EPL:2025"
    assert code == "2526"


def test_previous_season_label():
    assert previous_season_label("EPL:2025") == "EPL:2024"


def test_place_goals_matches_ht_ft():
    goals = place_goals(home_ht=1, away_ht=0, home_ft=2, away_ft=1, seed=42)
    assert sum(1 for g in goals if g.team == "home" and g.minute <= 45) == 1
    assert sum(1 for g in goals if g.team == "away" and g.minute <= 45) == 0
    assert sum(1 for g in goals if g.team == "home") == 2
    assert sum(1 for g in goals if g.team == "away") == 1


def test_cumulative_shots_end_totals():
    shots, sot = cumulative_shots(12, 4, seed=7)
    assert shots[90] == 12
    assert sot[90] == 4
    assert shots[53] <= 12
    assert sot[53] <= shots[53]


def test_everton_53_preset_label(tmp_path, monkeypatch):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    matches = inject_everton_preset([], "EPL:2025")
    everton = matches[0]
    snap = everton.snapshot_at(53)
    assert snap.home_shots == 12
    assert snap.home_sot == 4
    assert snap.away_shots == 6
    assert snap.away_sot == 1
    assert snap.goal_minutes == (42, 53)
    assert snap.label() == "42'/53' · 12/4 vs 6/1"


def test_snapshot_distance_identical_is_zero():
    a = MatchSnapshot(53, 2, 0, 12, 6, 4, 1, (42, 53))
    assert snapshot_distance(a, a) == 0.0


def test_find_similar_ranks_closer_first():
    query = MatchSnapshot(53, 2, 0, 12, 6, 4, 1, (42, 53))
    near = Match(
        match_id="near",
        season="EPL:2024",
        date="01/01/2024",
        home="A",
        away="B",
        home_goals_ft=2,
        away_goals_ft=0,
        home_shots_ft=13,
        away_shots_ft=6,
        home_sot_ft=4,
        away_sot_ft=1,
        goals=[],
        home_shots_by_min=[0] + [12] * 90,
        away_shots_by_min=[0] + [6] * 90,
        home_sot_by_min=[0] + [4] * 90,
        away_sot_by_min=[0] + [1] * 90,
    )
    # force goals into snapshot via mutating goals list
    from eeesoc.models import GoalEvent

    near.goals = [GoalEvent(42, "home"), GoalEvent(53, "home")]
    far = Match(
        match_id="far",
        season="EPL:2024",
        date="02/01/2024",
        home="C",
        away="D",
        home_goals_ft=0,
        away_goals_ft=3,
        home_shots_ft=2,
        away_shots_ft=20,
        home_sot_ft=0,
        away_sot_ft=9,
        goals=[GoalEvent(10, "away"), GoalEvent(20, "away"), GoalEvent(30, "away")],
        home_shots_by_min=[0] + [2] * 90,
        away_shots_by_min=[0] + [20] * 90,
        home_sot_by_min=[0] + [0] * 90,
        away_sot_by_min=[0] + [9] * 90,
    )
    hits = find_similar(query, [far, near], limit=2)
    assert hits[0].match.match_id == "near"
    assert hits[0].distance < hits[1].distance


def test_matches_from_csv_and_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    matches = matches_from_csv(SAMPLE_CSV, "EPL:2024")
    assert len(matches) == 2
    assert matches[0].home == "Man United"
    tl = build_timelines("x", 0, 0, 1, 0, 14, 10, 5, 2)
    assert len(tl["goals"]) == 1
    save_season("EPL:2024", matches)
    loaded = load_season("EPL:2024")
    assert len(loaded) == 2
    assert loaded[0].snapshot_at(90).home_shots == matches[0].home_shots_ft


def test_cache_json_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    root = cache_root()
    assert root == tmp_path
    path = root / "hello.json"
    write_json(path, {"ok": True})
    assert read_json(path) == {"ok": True}
    assert path.read_text().endswith("\n")


def test_cli_host_flag_defaults():
    from eeesoc.cli import build_parser

    ns = build_parser().parse_args(["--dashboard", "--host", "0.0.0.0", "--port", "8081"])
    assert ns.host == "0.0.0.0"
    assert ns.port == 8081
