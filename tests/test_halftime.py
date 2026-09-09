"""0-0 first-half archive: features, profile bands, live lookalikes, backfill."""

from __future__ import annotations

from datetime import date

import pytest

from eeesoc import halftime
from eeesoc.halftime import (
    archive_timeline,
    backfill,
    feature_distance,
    is_zero_zero_first_half,
    live_cut_minute,
    load_records,
    match_pct,
    profile_zero_zero,
    record_from_timeline,
    side_features,
    similar_zero_zero,
    zero_zero_board,
    zero_zero_records,
)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    halftime.clear_record_cache()
    yield
    halftime.clear_record_cache()


def _ev(minute, kind, team, *, xg=None, period=None, player=None, clock=None):
    return {
        "minute": minute,
        "kind": kind,
        "team": team,
        "xg": xg,
        "period": period,
        "player": player,
        "clock": clock or f"{minute}'",
        "text": kind,
    }


def _timeline(events, *, home_score=0, away_score=0, final=True, clock="FT", minute=90, event_id="1"):
    xg_pts = {"home": [], "away": []}
    for e in events:
        if e.get("xg") is not None and e["team"] in xg_pts and e["kind"] != "own_goal":
            xg_pts[e["team"]].append((e["minute"], e["xg"]))
    series = {}
    for side, pts in xg_pts.items():
        total = 0.0
        rows = [{"minute": 0, "xg": 0.0, "cumulative": 0.0}]
        for m, x in sorted(pts):
            total = round(total + x, 4)
            rows.append({"minute": m, "xg": x, "cumulative": total})
        series[side] = rows
    return {
        "event_id": event_id,
        "league_slug": "eng.1",
        "home": "Home FC",
        "away": "Away FC",
        "clock": clock,
        "minute": minute,
        "final": final,
        "frozen": final,
        "home_score": home_score,
        "away_score": away_score,
        "events": events,
        "xg": {
            "home": series["home"],
            "away": series["away"],
            "home_total": series["home"][-1]["cumulative"],
            "away_total": series["away"][-1]["cumulative"],
        },
    }


def _quiet_half(extra=()):
    """A 0-0 first half: 7 shots, 1 SOT, low xG."""
    return [
        _ev(4, "corner", "home"),
        _ev(9, "shot", "home", xg=0.05),
        _ev(14, "shot", "away", xg=0.04),
        _ev(22, "blocked", "home", xg=0.03),
        _ev(27, "shot_on", "away", xg=0.12),
        _ev(33, "corner", "away"),
        _ev(38, "shot", "home", xg=0.06),
        _ev(41, "blocked", "away", xg=0.02),
        _ev(45, "shot", "away", xg=0.05, period=1, clock="45'+2'"),
        *extra,
    ]


def test_side_features_cut_respects_period_and_minute():
    events = _quiet_half(
        extra=[
            # Stoppage-time first-half shot labelled 45' but period 1 → counted at HT.
            _ev(45, "shot_on", "home", xg=0.2, period=1, clock="45'+3'"),
            # Second-half minute 46 → excluded from the half.
            _ev(46, "goal", "away", xg=0.4, period=2),
        ]
    )
    ht = side_features(events, minute=45)
    assert ht["home"]["shots"] == 4
    assert ht["home"]["sot"] == 1
    assert ht["home"]["blocked"] == 1
    assert ht["home"]["corners"] == 1
    assert ht["away"]["shots"] == 4
    assert ht["away"]["sot"] == 1
    assert ht["away"]["goals"] == 0
    assert ht["total"]["goals"] == 0
    assert ht["total"]["xg"] == pytest.approx(0.57, abs=1e-6)
    assert ht["home"]["xg_at"]["15"] == pytest.approx(0.05)
    assert ht["home"]["xg_at"]["30"] == pytest.approx(0.08)
    assert ht["home"]["xg_at"]["45"] == pytest.approx(0.34)

    # Cut at 27' — only checkpoints reached so far, only events up to 27'.
    early = side_features(events, minute=27)
    assert early["minute"] == 27
    assert early["home"]["shots"] == 2
    assert early["away"]["shots"] == 2
    assert early["away"]["sot"] == 1
    assert list(early["home"]["xg_at"]) == ["15"]


def test_record_from_timeline_flags_0_0_half_and_second_half_goals():
    events = _quiet_half(
        extra=[
            _ev(57, "goal", "home", xg=0.3, period=2, player="A. Striker"),
            _ev(82, "own_goal", "away", period=2, player="B. Defender"),
        ]
    )
    rec = record_from_timeline(_timeline(events, home_score=1, away_score=1), {"start": "2026-09-05T14:00Z"})
    assert rec is not None
    assert rec["ht_goals"] == 0
    assert (rec["ht_home"], rec["ht_away"]) == (0, 0)
    assert (rec["ft_home"], rec["ft_away"]) == (1, 1)
    assert rec["date"] == "2026-09-05"
    assert [(g["minute"], g["team"], g["kind"]) for g in rec["second_half_goals"]] == [
        (57, "home", "goal"),
        (82, "away", "own_goal"),
    ]
    assert rec["events"][0]["period"] is None  # trimmed row keeps the period slot

    # First-half goal → not a 0-0 half.
    goal_half = record_from_timeline(_timeline(_quiet_half(extra=[_ev(30, "goal", "home", xg=0.5)]), home_score=1))
    assert goal_half["ht_goals"] == 1

    # Empty play feed is missing data, not a quiet game.
    assert record_from_timeline(_timeline([_ev(3, "corner", "home")])) is None


def test_archive_timeline_only_stores_finals_once():
    tl = _timeline(_quiet_half(), event_id="900")
    assert archive_timeline({**tl, "final": False}) is None
    first = archive_timeline(tl, {"league_chiclet": "EPL", "league_name": "Premier League"})
    assert first is not None
    assert first["league_chiclet"] == "EPL"
    assert archive_timeline(tl) is None  # already stored
    rows = load_records()
    assert len(rows) == 1
    assert rows[0]["event_id"] == "900"
    assert zero_zero_records(rows)[0]["ht_goals"] == 0


def _seed_archive():
    """Four archived games: three 0-0 at HT (varying tempo/outcomes), one 1-0 at HT."""
    quiet = _timeline(_quiet_half(extra=[_ev(70, "goal", "home", xg=0.3, period=2)]), home_score=1, event_id="a")
    busy_events = _quiet_half(
        extra=[
            _ev(11, "shot_on", "home", xg=0.3),
            _ev(19, "shot_on", "home", xg=0.25),
            _ev(24, "shot", "away", xg=0.1),
            _ev(31, "corner", "home"),
            _ev(36, "shot_on", "away", xg=0.2),
            _ev(43, "blocked", "home", xg=0.08),
            _ev(50, "goal", "away", xg=0.5, period=2),
            _ev(88, "goal", "away", xg=0.2, period=2),
        ]
    )
    busy = _timeline(busy_events, home_score=0, away_score=2, event_id="b")
    dead = _timeline(_quiet_half(), event_id="c")  # stayed 0-0
    lead = _timeline(_quiet_half(extra=[_ev(20, "goal", "home", xg=0.6)]), home_score=1, event_id="d")
    for tl, chiclet in ((quiet, "EPL"), (busy, "EPL"), (dead, "EPL"), (lead, "EPL")):
        assert archive_timeline(tl, {"league_chiclet": chiclet, "league_name": "Premier League", "start": "2026-09-01T14:00Z"})
    laliga = {**_timeline(_quiet_half(), event_id="e"), "league_slug": "esp.1"}
    assert archive_timeline(laliga, {"league_chiclet": "La Liga", "league_name": "LaLiga", "start": "2026-09-02T19:00Z"})


def test_profile_and_board_group_0_0_halves_by_league():
    _seed_archive()
    rows = load_records()
    assert len(rows) == 5
    zeros = zero_zero_records(rows)
    assert sorted(r["event_id"] for r in zeros) == ["a", "b", "c", "e"]

    prof = profile_zero_zero(zeros)
    assert prof["n"] == 4
    assert prof["bands"]["shots"]["p10"] <= prof["bands"]["shots"]["p50"] <= prof["bands"]["shots"]["p90"]
    assert prof["bands"]["shots"]["p90"] > prof["bands"]["shots"]["p10"]  # busy half widens the band
    assert [c["minute"] for c in prof["xg_curve"]] == [15, 30, 45]
    o = prof["outcomes"]
    assert o["ended_0_0_pct"] == 50
    assert o["any_2h_goal_pct"] == 50
    assert o["home_win_pct"] == 25 and o["away_win_pct"] == 25 and o["draw_pct"] == 50
    assert o["over_1_5_pct"] == 25
    assert o["first_2h_goal_pct"] == {"46-60": 25, "61-75": 25, "76-90+": 0}

    board = zero_zero_board(rows, leagues=[("eng.1", "EPL"), ("esp.1", "La Liga"), ("ger.1", "Bundesliga")])
    assert board["archive_total"] == 5
    assert board["zero_total"] == 4
    by_slug = {c["slug"]: c for c in board["chiclets"]}
    assert by_slug["eng.1"]["count"] == 3 and by_slug["eng.1"]["archived"] == 4
    assert by_slug["esp.1"]["count"] == 1
    assert by_slug["ger.1"]["count"] == 0
    assert [g["slug"] for g in board["leagues"]] == ["eng.1", "esp.1"]
    epl = board["leagues"][0]
    assert epl["profile"]["n"] == 3
    match = epl["matches"][0]
    tl = match["timeline"]
    assert tl["view_max_minute"] == 45
    assert tl["frozen"] and tl["final"]
    assert all(ev["minute"] <= 45 for ev in tl["events"])
    assert all(p["minute"] <= 45 for p in tl["xg"]["home"] + tl["xg"]["away"])
    assert (match["ht_home"], match["ht_away"]) == (0, 0)

    filtered = zero_zero_board(rows, leagues=[("eng.1", "EPL"), ("esp.1", "La Liga")], league_filter={"esp.1"})
    assert [g["slug"] for g in filtered["leagues"]] == ["esp.1"]
    assert filtered["profile"]["n"] == 1
    assert filtered["selected_total"] == 1


def test_similar_ranks_identical_half_first_and_cuts_at_live_minute():
    _seed_archive()
    rows = load_records()
    # Live game: the "quiet" archived half, still 0-0 at 41'.
    live_events = [e for e in _quiet_half() if e["minute"] <= 41]
    live = _timeline(live_events, final=False, clock="41'", minute=41)
    assert is_zero_zero_first_half(live)
    assert live_cut_minute(live) == 41

    out = similar_zero_zero(live, rows, limit=4)
    assert out["minute"] == 41
    assert out["archive_n"] == 4
    assert out["is_zero_zero_first_half"]
    ids = [r["event_id"] for r in out["lookalikes"]]
    assert sorted(ids[:3]) == ["a", "c", "e"]  # identical quiet halves tie at 100%
    top = out["lookalikes"][0]
    assert top["match_pct"] == 100
    assert top["distance"] == 0
    assert top["cut"]["minute"] == 41
    # The busy half is the worst fit of the pool.
    assert ids[-1] == "b"
    assert out["lookalikes"][-1]["match_pct"] < 100
    assert similar_zero_zero(live, rows, limit=2)["lookalikes"][-1]["match_pct"] == 100
    assert out["lookalike_outcomes"] is not None
    assert 0 <= out["rank"]["shots_pct"] <= 100
    assert out["population"]["n"] == 4 and out["population"]["minute"] == 41

    # Same-league scope drops the La Liga row.
    scoped = similar_zero_zero(live, rows, limit=10, league_filter={"eng.1"})
    assert scoped["archive_n"] == 3
    assert all(r["league_slug"] == "eng.1" for r in scoped["lookalikes"])


def test_zero_zero_first_half_detection():
    assert not is_zero_zero_first_half(_timeline(_quiet_half(), home_score=1, final=False, clock="30'", minute=30))
    assert not is_zero_zero_first_half(_timeline(_quiet_half(extra=[_ev(12, "goal", "home")]), final=False, clock="30'", minute=30))
    assert not is_zero_zero_first_half(_timeline(_quiet_half(), final=False, clock="52'", minute=52))
    ht = _timeline(_quiet_half(), final=False, clock="HT", minute=45)
    assert is_zero_zero_first_half(ht)
    assert live_cut_minute(ht) == 45
    assert not is_zero_zero_first_half(_timeline(_quiet_half(), final=True))


def test_feature_distance_and_match_pct_scale():
    a = side_features(_quiet_half())
    b = side_features(_quiet_half(extra=[_ev(30, "shot_on", "home", xg=0.4)]))
    assert feature_distance(a, a) == 0
    assert feature_distance(a, b) > 0
    assert match_pct(0) == 100
    assert match_pct(feature_distance(a, b)) < 100
    assert match_pct(50) == 0


def test_leagues_without_xg_feed_do_not_read_as_quiet():
    with_xg = side_features(_quiet_half())
    no_xg_events = [{**e, "xg": None} for e in _quiet_half()]
    without_xg = side_features(no_xg_events)
    assert with_xg["has_xg"] and not without_xg["has_xg"]
    # Same shot pattern → identical once xG terms are dropped, not penalised for missing xG.
    assert feature_distance(with_xg, without_xg) == 0
    busier = side_features([{**e, "xg": None} for e in _quiet_half(extra=[_ev(30, "shot_on", "home"), _ev(31, "shot_on", "home")])])
    assert feature_distance(without_xg, busier) > 0

    rec_xg = record_from_timeline(_timeline(_quiet_half(), event_id="x"), {"start": "2026-09-01T14:00Z"})
    rec_no = record_from_timeline(_timeline(no_xg_events, event_id="y"), {"start": "2026-09-01T14:00Z"})
    assert rec_xg["has_xg"] and not rec_no["has_xg"]
    prof = profile_zero_zero([rec_xg, rec_no])
    assert prof["n"] == 2 and prof["n_xg"] == 1
    assert prof["bands"]["xg"]["p50"] == pytest.approx(rec_xg["first_half"]["total"]["xg"], abs=0.01)
    assert prof["sides"]["home"]["xg"] == pytest.approx(rec_xg["first_half"]["home"]["xg"], abs=0.01)
    # Records archived before the flag existed fall back to scanning events.
    legacy = {k: v for k, v in rec_no.items() if k != "has_xg"}
    assert profile_zero_zero([rec_xg, legacy])["n_xg"] == 1

    live = _timeline(no_xg_events[:5], final=False, clock="30'", minute=30)
    out = similar_zero_zero(live, [rec_xg, rec_no], limit=2)
    assert out["rank"]["xg_pct"] is None
    assert out["rank"]["xg_n"] == 1
    assert not out["live"]["has_xg"]


def _scoreboard_payload(events):
    return {"leagues": [{"name": "English Premier League"}], "events": events}


def _finished_event(eid, day, hs, aws):
    return {
        "id": eid,
        "date": f"{day}T14:00Z",
        "competitions": [
            {
                "status": {"displayClock": "90'+4'", "type": {"state": "post", "detail": "FT", "shortDetail": "FT"}},
                "competitors": [
                    {"homeAway": "home", "score": str(hs), "team": {"id": "1", "displayName": "Home FC"}},
                    {"homeAway": "away", "score": str(aws), "team": {"id": "2", "displayName": "Away FC"}},
                ],
            }
        ],
    }


def _play(minute, ptype, team_id, *, period=1, xg=None, scoring=False):
    row = {
        "type": {"type": ptype},
        "clock": {"displayValue": f"{minute}'"},
        "period": {"number": period},
        "team": {"$ref": f".../teams/{team_id}"},
        "shortText": ptype,
    }
    if xg is not None:
        row["expectedGoals"] = xg
    if scoring:
        row["scoringPlay"] = True
    return row


def test_backfill_archives_finished_games_and_skips_existing():
    plays_by_event = {
        "10": [
            _play(5, "corner", "1"),
            _play(12, "shot-on-target", "1", xg=0.1),
            _play(20, "shot-off-target", "2", xg=0.05),
            _play(28, "shot-blocked", "2", xg=0.04),
            _play(35, "shot-off-target", "1", xg=0.07),
            _play(44, "corner", "2"),
            _play(63, "goal", "1", period=2, xg=0.5, scoring=True),
        ],
        "11": [
            _play(8, "goal", "2", xg=0.3, scoring=True),
            _play(15, "shot-on-target", "1", xg=0.1),
            _play(25, "corner", "1"),
            _play(31, "shot-off-target", "2", xg=0.05),
            _play(40, "corner", "2"),
            _play(44, "shot-blocked", "1", xg=0.02),
        ],
    }
    calls = {"scoreboard": 0, "plays": 0}

    def fetcher(url):
        if "scoreboard" in url:
            calls["scoreboard"] += 1
            if "dates=20260905" in url:
                return _scoreboard_payload([_finished_event("10", "2026-09-05", 1, 0)])
            if "dates=20260904" in url:
                return _scoreboard_payload([_finished_event("11", "2026-09-04", 0, 1)])
            return _scoreboard_payload([])
        calls["plays"] += 1
        eid = url.split("/events/")[1].split("/")[0]
        return {"pageCount": 1, "items": plays_by_event[eid]}

    status = backfill(days_back=2, leagues=[("eng.1", "EPL")], fetcher=fetcher, workers=2, today=date(2026, 9, 5))
    assert status["running"] is False
    assert status["scanned"] == 2
    assert status["archived"] == 2
    assert status["errors"] == []
    assert calls["scoreboard"] == 3  # today, -1, -2

    rows = load_records()
    assert sorted(r["event_id"] for r in rows) == ["10", "11"]
    ten = next(r for r in rows if r["event_id"] == "10")
    assert ten["ht_goals"] == 0
    assert (ten["ft_home"], ten["ft_away"]) == (1, 0)
    assert ten["league_chiclet"] == "EPL"
    assert ten["league_name"] == "English Premier League"
    assert ten["date"] == "2026-09-05"
    assert [(g["minute"], g["team"]) for g in ten["second_half_goals"]] == [(63, "home")]
    assert ten["events"][0]["period"] == 1
    eleven = next(r for r in rows if r["event_id"] == "11")
    assert eleven["ht_goals"] == 1

    # Second sweep: same games already on disk → nothing refetched.
    plays_before = calls["plays"]
    again = backfill(days_back=2, leagues=[("eng.1", "EPL")], fetcher=fetcher, workers=2, today=date(2026, 9, 5))
    assert again["archived"] == 0
    assert again["skipped"] == 2
    assert calls["plays"] == plays_before

    board = zero_zero_board(rows, leagues=[("eng.1", "EPL")])
    assert board["zero_total"] == 1
    assert board["leagues"][0]["matches"][0]["event_id"] == "10"
