"""Penalty-box entries: the edge found working backwards from every 0-0 half."""
from __future__ import annotations

import json
import math
import random

import pytest

from eeesoc import halftime, nogoal, nogoal_monitor
from eeesoc.live import box_entries_from_plays, build_event_timeline
from eeesoc.nogoal import (
    MIN_BOX_RECORDS,
    box_so_far,
    build_model,
    evaluate_live,
    expected_box,
    habit_factor,
    has_box_data,
    inplay_factor,
)


def _play(ptype: str, minute: int, team: str, x: float | None, y: float | None, period: int | None = 1) -> dict:
    p = {
        "type": {"type": ptype, "text": ptype},
        "clock": {"displayValue": f"{minute}'", "value": minute * 60},
        "team": {"$ref": f"http://x/teams/{team}?lang=en"},
    }
    if period is not None:
        p["period"] = {"number": period}
    if x is not None:
        p["fieldPositionX"] = x
    if y is not None:
        p["fieldPositionY"] = y
    return p


def test_box_entries_from_plays_filters_and_sides():
    plays = [
        _play("pass", 3, "10", 90.0, 50.0),  # home, in the box
        _play("pass", 5, "20", 84.0, 21.0),  # away, corner of the box (inclusive)
        _play("pass", 6, "10", 82.9, 50.0),  # just short of the box
        _play("pass", 7, "10", 95.0, 80.0),  # too wide
        _play("cross", 8, "10", 95.0, 50.0),  # crosses are a different play type
        _play("shot-on-target", 9, "10", 95.0, 50.0),  # shots are not entries
        _play("pass", 12, "10", None, None),  # no coordinates
        _play("pass", 50, "20", 90.0, 40.0, period=2),
        _play("pass", 47, "10", 90.0, 40.0, period=None),  # period inferred from the minute
        _play("pass", 30, "99", 90.0, 40.0),  # unknown team
    ]
    out = box_entries_from_plays(plays, home_id="10", away_id="20")
    assert out == {"1": {"home": [3], "away": [5]}, "2": {"home": [47], "away": [50]}}


def test_timeline_and_record_carry_box_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    plays = [
        _play("pass", 3, "10", 90.0, 50.0),
        _play("shot-on-target", 9, "10", 95.0, 50.0),
        _play("shot-off-target", 20, "20", 85.0, 50.0),
        _play("corner-awarded", 22, "20", None, None),
        _play("shot-blocked", 30, "10", 88.0, 50.0),
        _play("shot-on-target", 40, "20", 90.0, 50.0),
        _play("shot-off-target", 44, "10", 80.0, 50.0),
        _play("pass", 60, "20", 90.0, 40.0, period=2),
        _play("goal", 70, "20", 95.0, 50.0, period=2),
    ]
    plays[-1]["scoringPlay"] = True

    def fetcher(url):
        return {"items": plays, "pageCount": 1}

    tl = build_event_timeline("t.1", "1", home="A", away="B", home_id="10", away_id="20", clock="FT", fetcher=fetcher, use_cache=False)
    assert tl["box_entries"] == {"1": {"home": [3], "away": []}, "2": {"home": [], "away": [60]}}
    rec = halftime.record_from_timeline(tl, {"start": "2026-08-01T18:00Z", "home_id": "10", "away_id": "20"})
    assert rec is not None
    assert rec["box_entries"] == tl["box_entries"]
    assert has_box_data(rec)


def test_box_backfill_enriches_old_records(monkeypatch, tmp_path):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    halftime.clear_record_cache()
    old = {
        "event_id": "77", "league_slug": "t.1", "home": "A", "away": "B", "home_id": "10", "away_id": "20",
        "date": "2026-08-01", "start": "2026-08-01T18:00Z", "ft_home": 0, "ft_away": 0, "ht_home": 0, "ht_away": 0,
        "events": [{"minute": 10, "kind": "shot", "team": "home", "period": 1, "xg": None}] * 6, "has_xg": False,
    }
    path = halftime.record_path("t.1", "77")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(old))
    assert [r["event_id"] for r in halftime.records_missing_box_entries()] == ["77"]

    urls = []

    def fetcher(url):
        urls.append(url)
        return {"items": [_play("pass", 3, "10", 90.0, 50.0), _play("pass", 50, "20", 90.0, 50.0, period=2)], "pageCount": 1}

    status = halftime.backfill_box_entries(fetcher=fetcher, pause_s=0)
    assert status["todo"] == 1 and status["done"] == 1 and status["failed"] == 0
    assert "limit=1000" in urls[0]
    assert halftime.records_missing_box_entries() == []
    stored = json.loads(path.read_text())
    assert stored["box_entries"] == {"1": {"home": [3], "away": []}, "2": {"home": [], "away": [50]}}
    # a feed without coordinates is stored as empty, and treated as missing data by the model
    assert not has_box_data({"box_entries": {"1": {"home": [], "away": []}, "2": {"home": [], "away": []}}})


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def _ev(minute: int, kind: str, team: str = "home", period: int | None = None) -> dict:
    return {"minute": minute, "kind": kind, "team": team, "period": period if period is not None else (1 if minute <= 45 else 2), "xg": None}


def _box_records(seed: int = 7) -> list[dict]:
    """Quiet clubs rarely reach the box and rarely score; busy clubs do both — in one league."""
    rng = random.Random(seed)
    recs = []
    clubs = {f"q{i}": "quiet" for i in range(6)} | {f"b{i}": "busy" for i in range(6)}
    ids = list(clubs)
    for n in range(120):
        home, away = rng.sample(ids, 2)
        busy = (clubs[home] == "busy") + (clubs[away] == "busy")
        # Game-level tempo: the same clubs can produce a slow or a frantic night, and that
        # shows up in the entries already seen as well as in the goals still to come.
        tempo = rng.choice((0.4, 1.0, 1.8))
        events = []
        box = {"1": {"home": [], "away": []}, "2": {"home": [], "away": []}}
        for p, (lo, hi) in ((1, (1, 45)), (2, (46, 90))):
            for side, club in (("home", home), ("away", away)):
                base = rng.randint(0, 2) if clubs[club] == "quiet" else rng.randint(4, 9)
                entries = int(round(base * tempo))
                if p == 1 and side == "home":
                    entries = max(1, entries)  # a game with no entry at all would read as missing data
                box[str(p)][side] = sorted(rng.randint(lo, hi) for _ in range(entries))
                # roughly one goal per 6 entries
                for m in box[str(p)][side]:
                    if rng.random() < 0.16:
                        events.append(_ev(m, "goal", side, p))
                for _ in range(rng.randint(1, 4)):
                    events.append(_ev(rng.randint(lo, hi), "shot", side, p))
        events.sort(key=lambda e: e["minute"])
        g1h = [e for e in events if e["kind"] == "goal" and e["period"] == 1]
        recs.append(
            {
                "event_id": str(1000 + n), "league_slug": "one.1", "home_id": home, "away_id": away, "home": home, "away": away,
                "events": events, "box_entries": box,
                "ft_home": sum(1 for e in events if e["kind"] == "goal" and e["team"] == "home"),
                "ft_away": sum(1 for e in events if e["kind"] == "goal" and e["team"] == "away"),
                "ht_home": sum(1 for e in g1h if e["team"] == "home"), "ht_away": sum(1 for e in g1h if e["team"] == "away"),
                "has_xg": False, "busy": busy,
            }
        )
    return recs


def test_box_so_far_counts_up_to_minute():
    rec = {"box_entries": {"1": {"home": [3, 20, 44], "away": [10]}, "2": {"home": [50], "away": [88, 90]}}}
    assert box_so_far(rec, 1, 15) == (1, 1)
    assert box_so_far(rec, 1, 45) == (3, 1)
    assert box_so_far(rec, 2, 60) == (1, 0)
    assert box_so_far(rec, 2, 90) == (1, 2)
    assert box_so_far({}, 1, 30) == (0, 0)
    # integer period keys are accepted too
    assert box_so_far({"box_entries": {1: {"home": [5], "away": []}}}, 1, 10) == (1, 0)


def test_fit_beta_recovers_exponent():
    rng = random.Random(1)
    samples = []
    for _ in range(4000):
        r = rng.uniform(0.4, 2.0)
        e = 0.6
        lam = e * r**0.7
        # Poisson draw
        k, p, L = 0, 1.0, math.exp(-lam)
        while True:
            p *= rng.random()
            if p <= L:
                break
            k += 1
        samples.append((k, e, r))
    assert nogoal._fit_beta(samples) == pytest.approx(0.7, abs=0.12)
    assert nogoal._fit_beta([]) == 0.0
    assert nogoal._power_factor(None, 1.0) == 1.0
    assert nogoal._power_factor(0.01, 1.0) == 0.5  # clamped
    assert nogoal._power_factor(9.0, 1.0) == 1.8


def test_model_without_box_data_is_neutral():
    recs = [{"league_slug": "x.1", "events": [_ev(20, "goal")], "ft_home": 1, "ft_away": 0} for _ in range(50)]
    model = build_model(recs)
    assert model["box"]["records"] == 0 and not model["box"]["teams"]
    assert habit_factor(model, 1, "a", "b", "x.1") == (1.0, None, None, None)
    assert inplay_factor(model, 1, 20, 3) == (1.0, None, None)
    ev = evaluate_live(model, {"minute": 20, "events": [], "box_entries": {"1": {"home": [3], "away": []}}}, league_slug="x.1", home_id="a", away_id="b")
    assert ev["habit_factor"] == 1.0 and ev["inplay_factor"] == 1.0
    assert ev["box_total"] == 1 and ev["habit_bucket"] is None


def test_box_fit_needs_minimum_records():
    recs = _box_records()[: MIN_BOX_RECORDS - 1]
    model = build_model(recs)
    assert model["box"]["records"] == MIN_BOX_RECORDS - 1
    assert not model["box"]["teams"]


def test_habit_and_inplay_exponents_are_positive_on_box_driven_archive():
    recs = _box_records()
    model = build_model(recs)
    box = model["box"]
    assert box["records"] == 120
    assert box["per_half"][1] > 0 and box["per_half"][2] > 0
    # goals scale with box entries in this archive → both exponents > 0 in both halves
    assert box["habit_beta"][1] > 0 and box["habit_beta"][2] > 0
    assert box["inplay_beta"][1] > 0 and box["inplay_beta"][2] > 0
    # the implied multipliers are monotone quiet < normal < busy, low < normal < high
    for p in (1, 2):
        hm, im = box["habit_mult"][p], box["inplay_mult"][p]
        assert hm["quiet"] < hm["normal"] < hm["busy"]
        assert im["low"] < im["normal"] < im["high"]
    # a quiet-vs-quiet fixture is expected to reach the box far less than busy-vs-busy
    quiet = expected_box(model, 1, "q0", "q1", "one.1")
    busy = expected_box(model, 1, "b0", "b1", "one.1")
    assert quiet < box["per_half"][1] < busy
    qf, qrel, qb, _ = habit_factor(model, 1, "q0", "q1", "one.1")
    bf, brel, bb, _ = habit_factor(model, 1, "b0", "b1", "one.1")
    assert qf < 1.0 < bf and qrel < 1.0 < brel
    assert qb == "quiet" and bb == "busy"
    # unknown clubs sit at the league mean
    nf, nrel, nb, _ = habit_factor(model, 1, "zz", "yy", "one.1")
    assert nrel == pytest.approx(1.0, abs=0.05) and nb == "normal"
    # in-play: an empty box at 25' is a lower multiplier than a busy one
    lo, lrel, lb = inplay_factor(model, 1, 25, 0)
    hi, hrel, hb = inplay_factor(model, 1, 25, 9)
    assert lo < 1.0 < hi and lb == "low" and hb == "high"
    assert inplay_factor(model, 1, 0, 0) == (1.0, None, None)  # nothing elapsed yet


def test_expected_box_leave_one_out_removes_own_game():
    recs = _box_records()
    model = build_model(recs)
    rec = recs[0]
    with_game = expected_box(model, 1, rec["home_id"], rec["away_id"], "one.1")
    without = expected_box(model, 1, rec["home_id"], rec["away_id"], "one.1", exclude=rec)
    assert with_game != without
    # removing a game with many entries lowers the expectation, and vice versa
    own = sum(box_so_far(rec, 1, 45))
    assert (without < with_game) == (own > with_game)


def test_backtest_reports_box_edge_and_gain():
    recs = _box_records()
    model = build_model(recs)
    bt = nogoal.backtest(model, recs)
    assert bt["log_loss_no_box"] is not None
    assert bt["log_loss"] <= bt["log_loss_no_box"]
    edge = bt["box_edge"]
    assert edge["fitted"] is True and edge["records"] == 120
    assert set(edge["minutes"]) == {"15", "25"}
    m15 = edge["minutes"]["15"]
    assert m15["n"] > 0 and m15["held_pct"] is not None
    assert {r["bucket"] for r in m15["habit"]} <= {"quiet", "normal", "busy"}
    assert {r["bucket"] for r in m15["inplay"]} <= {"low", "normal", "high"}
    assert sum(r["n"] for r in m15["entries"]) == m15["n"]
    assert sum(g["n"] for g in m15["grid"]) == m15["n"]
    # quiet fixtures held more often than busy ones in this archive
    habit = {r["bucket"]: r["held_pct"] for r in m15["habit"]}
    assert habit["quiet"] > habit["busy"]
    prof = edge["profile"]
    assert prof["zero_n"] + prof["goal_n"] == 120
    stats = {r["stat"]: r for r in prof["rows"]}
    assert stats["box_entries"]["zero"] < stats["box_entries"]["goal"]
    assert "xg" not in stats  # no xG feed in this archive


def test_backtest_without_box_data_reports_unfitted():
    recs = [{"league_slug": "x.1", "events": [_ev(20, "goal")], "ft_home": 1, "ft_away": 0, "ht_home": 1, "ht_away": 0} for _ in range(50)]
    model = build_model(recs)
    bt = nogoal.backtest(model, recs)
    assert bt["log_loss_no_box"] == bt["log_loss"]
    assert bt["box_edge"]["fitted"] is False
    assert bt["box_edge"]["minutes"]["15"]["n"] == 0


def test_evaluate_live_box_fields(monkeypatch):
    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_1H", raising=False)
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.5")
    model = build_model(_box_records())
    tl = {"minute": 25, "clock": "25'", "events": [], "home_score": 0, "away_score": 0, "box_entries": {"1": {"home": [4, 30], "away": [12]}, "2": {"home": [], "away": []}}}
    ev = evaluate_live(model, tl, league_slug="one.1", home_id="q0", away_id="q1")
    assert (ev["box_home"], ev["box_away"], ev["box_total"]) == (1, 1, 2)  # the 30' entry has not happened yet
    assert ev["habit_bucket"] == "quiet" and ev["habit_factor"] < 1.0 and ev["habit_expected"] > 0
    assert ev["inplay_bucket"] in {"low", "normal", "high"} and ev["inplay_rel"] > 0
    assert ev["p_no_goal"] == pytest.approx(math.exp(-ev["lambda"]), abs=0.002)
    # without team ids the habit is neutral but in-play still applies
    ev2 = evaluate_live(model, tl, league_slug="one.1")
    assert ev2["habit_factor"] == 1.0 and ev2["habit_bucket"] is None
    assert ev2["inplay_factor"] == ev["inplay_factor"]
    # at half time nothing has been played in period 2 → in-play neutral, box counters reset
    ht = evaluate_live(model, {**tl, "minute": 45, "clock": "HT"}, league_slug="one.1", home_id="q0", away_id="q1")
    assert ht["period"] == 2 and ht["inplay_factor"] == 1.0 and ht["box_total"] == 0
    # a timeline without box entries reports None
    ev3 = evaluate_live(model, {"minute": 25, "events": []}, league_slug="one.1", home_id="q0", away_id="q1")
    assert ev3["box_total"] is None and ev3["inplay_factor"] == 1.0


def test_monitor_passes_team_ids_and_formats_edge(monkeypatch):
    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_1H", raising=False)
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.5")
    model = build_model(_box_records())
    board = {"leagues": [{"slug": "one.1", "chiclet": "ONE", "name": "One", "matches": [
        {"event_id": "5", "state": "in", "home": "Q Zero", "away": "Q One", "home_id": "q0", "away_id": "q1", "home_score": 0, "away_score": 0, "clock": "27'"},
    ]}]}
    tl = {"minute": 27, "clock": "27'", "events": [{"minute": 10, "kind": "shot", "team": "home", "period": 1, "xg": None}], "home_score": 0, "away_score": 0,
          "box_entries": {"1": {"home": [8], "away": [19]}, "2": {"home": [], "away": []}}}
    msgs, store, evals = nogoal_monitor.poll(board=board, model=model, state={"signals": {}}, fetch_timeline=lambda m: tl, now=1.0)
    ev = evals["5"]
    assert ev["habit_bucket"] == "quiet" and ev["box_total"] == 2
    assert len(msgs) == 1
    text = msgs[0]
    assert "Edge: Box entries so far **2** (Q Zero 1 · Q One 1)" in text
    assert "quiet fixture ×" in text
    assert "1 shot ·" in text
    # without box data in the timeline the edge line is skipped entirely
    sig = store["signals"]["5:1"]
    plain = nogoal_monitor.format_trigger({**sig, "eval": {**sig["eval"], "box_total": None, "habit_bucket": None}})
    assert "Edge:" not in plain
