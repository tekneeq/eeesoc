from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eeesoc import nogoal, nogoal_monitor
from eeesoc.nogoal import (
    HALF_END,
    backtest,
    build_model,
    evaluate_live,
    event_period,
    goals_after,
    league_factor,
    live_period,
    no_goal_probability,
    remaining_lambda,
    score_at,
    windows,
)


def _ev(minute: int, kind: str, team: str = "home", period: int | None = None, xg: float = 0.0) -> dict:
    return {"minute": minute, "kind": kind, "team": team, "period": period, "xg": xg}


def _rec(slug: str, events: list[dict]) -> dict:
    ft_h = sum(1 for e in events if e["kind"] in {"goal", "own_goal"} and e["team"] == "home")
    ft_a = sum(1 for e in events if e["kind"] in {"goal", "own_goal"} and e["team"] == "away")
    return {"league_slug": slug, "events": events, "ft_home": ft_h, "ft_away": ft_a}


def _records() -> list[dict]:
    recs = []
    # Quiet league: half the games goalless, goals late.
    for i in range(10):
        events = [_ev(12, "shot")]
        if i % 2 == 0:
            events.append(_ev(70, "goal", "away"))
        recs.append(_rec("quiet.1", events))
    # Busy league: two goals in every game, one per half.
    for _ in range(10):
        recs.append(_rec("busy.1", [_ev(20, "goal"), _ev(45, "goal", "away"), _ev(60, "goal"), _ev(90, "goal", "away")]))
    return recs


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def test_event_period_prefers_explicit_then_minute():
    assert event_period({"minute": 50, "period": 1}) == 1
    assert event_period({"minute": 45}) == 1
    assert event_period({"minute": 46}) == 2
    assert event_period({"period": "x"}) is None
    assert event_period({}) is None


def test_score_at_and_goals_after():
    events = [_ev(10, "goal"), _ev(45, "goal", "away"), _ev(60, "own_goal", "away"), _ev(88, "shot")]
    assert score_at(events, 1, 9) == (0, 0)
    assert score_at(events, 1, 10) == (1, 0)
    assert score_at(events, 1, 44) == (1, 0)
    assert score_at(events, 2, 50) == (1, 1)  # 1st-half goals carry over
    assert score_at(events, 2, 60) == (1, 2)
    assert goals_after(events, 1, 10) == 1  # the 45' stoppage goal
    assert goals_after(events, 1, 45) == 0
    assert goals_after(events, 2, 50) == 1
    assert goals_after(events, 2, 60) == 0


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_build_model_hazard_sums_to_goals_per_game():
    model = build_model(_records())
    assert model["archive_total"] == 20
    total = sum(model["hazard"][1]) + sum(model["hazard"][2])
    assert total == pytest.approx(model["goals_per_game"], abs=1e-3)
    # 45 goals over 20 games
    assert model["goals_per_game"] == pytest.approx(45 / 20, abs=1e-3)
    # stoppage-time share: 1H goals are 10 at 20' + 10 at 45' → half at 45'
    assert model["stoppage_share"][1] == pytest.approx(0.5, abs=1e-3)


def test_build_model_clamps_minutes_into_half():
    model = build_model([_rec("x.1", [_ev(0, "goal"), _ev(47, "goal", "away", period=1), _ev(40, "goal", period=2)])])
    assert model["hazard"][1][1] > 0  # minute 0 → 1
    assert model["hazard"][1][45] > 0  # 47' filed in period 1 → 45
    assert model["hazard"][2][46] > 0  # 40' filed in period 2 → 46
    assert sum(model["hazard"][1]) + sum(model["hazard"][2]) == pytest.approx(3.0)


def test_league_factor_shrinks_toward_global_and_can_leave_one_out():
    model = build_model(_records())
    gpg = model["goals_per_game"]
    quiet = model["leagues"]["quiet.1"]
    busy = model["leagues"]["busy.1"]
    assert quiet["goals_per_game"] == 0.5
    assert busy["goals_per_game"] == 4.0
    assert quiet["factor"] < 1.0 < busy["factor"]
    # shrinkage: 10 games of 0.5 blended with 20 games at the global rate
    expected = ((5 + 20 * gpg) / 30) / gpg
    assert quiet["factor"] == pytest.approx(expected, abs=1e-3)
    assert league_factor(model, "quiet.1") == pytest.approx(quiet["factor"])
    assert league_factor(model, "nowhere.1") == 1.0
    loo = league_factor(model, "quiet.1", exclude=(1, 1))
    assert loo == pytest.approx(((4 + 20 * gpg) / 29) / gpg, abs=1e-6)


def test_state_multipliers_quiet_when_goalless():
    model = build_model(_records())
    sm = model["state_mult"]
    # In this archive a game at 0-0 in the 2nd half is a quiet-league game, and any
    # game already scoring is a busy-league one.
    assert sm[2]["zero"] < 1.0 < sm[2]["scoring"]
    assert sm[1]["scoring"] == 1.0 or sm[1]["scoring"] > 0


def test_remaining_lambda_and_probability_monotone_in_time():
    model = build_model(_records())
    p15 = no_goal_probability(model, period=1, minute=15, league_slug="quiet.1", zero_zero=True)
    p25 = no_goal_probability(model, period=1, minute=25, league_slug="quiet.1", zero_zero=True)
    p35 = no_goal_probability(model, period=1, minute=35, league_slug="quiet.1", zero_zero=True)
    p45 = no_goal_probability(model, period=1, minute=45, league_slug="quiet.1", zero_zero=True)
    # goals only land at 20' and 45' in this archive, so 25' and 35' price the same
    assert p15 < p25 == p35 < p45 == 1.0
    parts = remaining_lambda(model, period=2, minute=60, league_slug="busy.1", zero_zero=False)
    assert parts["minutes_left"] == 30
    assert parts["lambda"] == pytest.approx(parts["base"] * parts["league_factor"] * parts["state_factor"])
    with pytest.raises(ValueError):
        remaining_lambda(model, period=3, minute=1, league_slug="x", zero_zero=True)


def test_league_scale_override_and_clamping():
    model = build_model(_records())
    a = remaining_lambda(model, period=1, minute=-5, league_slug="busy.1", zero_zero=True, league_scale=1.0)
    b = remaining_lambda(model, period=1, minute=0, league_slug="busy.1", zero_zero=True, league_scale=1.0)
    assert a == b
    assert a["league_factor"] == 1.0
    assert a["base"] == pytest.approx(sum(model["hazard"][1]))


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def test_backtest_shape_and_calibration():
    recs = _records()
    model = build_model(recs)
    bt = backtest(model, recs)
    assert bt["archive_total"] == 20
    assert bt["samples"] == 20 * (len(nogoal.FIT_MINUTES[1]) + len(nogoal.FIT_MINUTES[2]))
    assert bt["log_loss"] is not None and bt["log_loss_time_only"] is not None
    assert bt["windows"] == {"1": [10, 35], "2": [50, 78]}
    assert [row["threshold"] for row in bt["policy"]] == list(nogoal.BACKTEST_THRESHOLDS)
    for row in bt["policy"]:
        assert row["break_even_odds"] == pytest.approx(1 / row["threshold"], abs=0.01)
        for key in ("h1", "h2"):
            cell = row[key]
            assert 0 <= cell["fired"] <= 20
            assert cell["held"] <= cell["fired"]
            if cell["fired"]:
                assert 10 <= cell["avg_minute"] <= 78
    for cell in bt["calibration"]:
        assert cell["n"] > 0
        lo = int(cell["bucket"].split("-")[0])
        assert lo <= cell["predicted_pct"] <= lo + 10
    # the model must at least match the time-only baseline on its own training data
    assert bt["log_loss"] <= bt["log_loss_time_only"] + 1e-9


def test_backtest_quiet_start_counts():
    recs = _records()
    bt = backtest(build_model(recs), recs)
    q = bt["quiet_start"]
    # every game is 0-0 at 15' with a single shot → all quiet
    assert q["all_n"] == 20
    assert q["quiet_n"] == 20
    # quiet-league 1st halves all held, busy-league ones never did
    assert q["quiet_held_pct"] == 50
    assert q["all_held_pct"] == 50


def test_backtest_fires_once_per_half_inside_window():
    recs = _records()
    model = build_model(recs)
    bt = backtest(model, recs, thresholds=(0.0,), windows={1: (10, 12), 2: (50, 50)})
    row = bt["policy"][0]
    assert row["break_even_odds"] is None
    assert row["h1"]["fired"] == 20 and row["h1"]["avg_minute"] == 10.0
    assert row["h2"]["fired"] == 20 and row["h2"]["avg_minute"] == 50.0
    # quiet-league games (10) held in the 1st half; busy ones scored at 20'
    assert row["h1"]["held"] == 10
    assert row["h1"]["hit_pct"] == 50


# ---------------------------------------------------------------------------
# Config + live evaluation
# ---------------------------------------------------------------------------


def test_threshold_and_windows_from_env(monkeypatch):
    monkeypatch.delenv("EEESOC_NOGOAL_THRESHOLD", raising=False)
    assert nogoal.threshold() == nogoal.DEFAULT_THRESHOLD
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.72")
    assert nogoal.threshold() == 0.72
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.1")
    assert nogoal.threshold() == 0.5
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "junk")
    assert nogoal.threshold() == nogoal.DEFAULT_THRESHOLD

    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_1H", raising=False)
    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_2H", raising=False)
    assert windows() == nogoal.DEFAULT_WINDOWS
    monkeypatch.setenv("EEESOC_NOGOAL_WINDOW_1H", "12-30")
    monkeypatch.setenv("EEESOC_NOGOAL_WINDOW_2H", "40-99")
    w = windows()
    assert w[1] == (12, 30)
    assert w[2] == (46, 90)  # clamped into the half
    monkeypatch.setenv("EEESOC_NOGOAL_WINDOW_1H", "nonsense")
    assert windows()[1] == nogoal.DEFAULT_WINDOWS[1]


def test_live_period_phases():
    assert live_period({"minute": 23, "events": []}) == (1, 23, "1h")
    assert live_period({"minute": 45, "clock": "HT", "events": []}) == (2, 45, "ht")
    assert live_period({"minute": 30, "events": [_ev(48, "shot", period=2)]}) == (2, 46, "2h")
    assert live_period({"minute": 61, "events": []}) == (2, 61, "2h")
    assert live_period({"minute": 95, "final": True, "events": []}) == (2, 90, "ft")
    assert live_period({"minute": 99, "events": []}) == (2, 90, "2h")


def test_evaluate_live_windows_and_ready(monkeypatch):
    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_1H", raising=False)
    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_2H", raising=False)
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.5")
    model = build_model(_records())

    early = evaluate_live(model, {"minute": 5, "events": [], "home_score": 0, "away_score": 0}, league_slug="quiet.1")
    assert early["window"] == "before" and early["ready"] is False and early["phase"] == "1h"

    mid = evaluate_live(model, {"minute": 30, "events": [], "home_score": 0, "away_score": 0}, league_slug="quiet.1")
    assert mid["window"] == "open"
    assert mid["zero_zero"] is True
    assert mid["p_no_goal"] == pytest.approx(math.exp(-mid["lambda"]), abs=0.002)
    assert mid["break_even_odds"] == pytest.approx(1 / mid["p_no_goal"], abs=0.02)
    assert mid["minutes_left"] == 15
    assert mid["window_minutes"] == [10, 35]
    assert mid["threshold"] == 0.5
    assert mid["ready"] is (mid["p_no_goal"] >= 0.5)

    late = evaluate_live(model, {"minute": 40, "events": [], "home_score": 0, "away_score": 0}, league_slug="quiet.1")
    assert late["window"] == "after" and late["ready"] is False

    ht = evaluate_live(model, {"minute": 45, "clock": "HT", "events": [], "home_score": 1, "away_score": 0}, league_slug="quiet.1")
    assert ht["phase"] == "ht" and ht["window"] == "before" and ht["zero_zero"] is False and ht["period"] == 2

    ft = evaluate_live(model, {"minute": 90, "final": True, "events": []}, league_slug="quiet.1")
    assert ft["window"] == "closed" and ft["p_no_goal"] == 1.0 and ft["ready"] is False


def test_nogoal_model_cache(monkeypatch):
    recs = _records()
    monkeypatch.setattr(nogoal, "load_records", lambda: recs)
    nogoal.clear_nogoal_cache()
    m1, bt1 = nogoal.nogoal_model()
    assert bt1 is None
    m2, bt2 = nogoal.nogoal_model(with_backtest=True)
    assert m2 is m1
    assert bt2 is not None and bt2["archive_total"] == 20
    m3, bt3 = nogoal.nogoal_model()
    assert m3 is m1 and bt3 is bt2  # backtest retained once computed
    nogoal.clear_nogoal_cache()


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


def _board(matches: list[dict]) -> dict:
    return {"leagues": [{"slug": "quiet.1", "chiclet": "QUI", "name": "Quiet League", "matches": matches}]}


def _match(event_id: str = "1001", **over) -> dict:
    base = {"event_id": event_id, "state": "in", "home": "Alpha", "away": "Beta", "home_score": 0, "away_score": 0, "clock": "27'"}
    base.update(over)
    return base


def _tl(minute: int, events: list[dict] | None = None, **over) -> dict:
    tl = {"minute": minute, "clock": f"{minute}'", "events": events or [], "home_score": 0, "away_score": 0, "final": False}
    tl.update(over)
    return tl


@pytest.fixture
def quiet_env(monkeypatch):
    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_1H", raising=False)
    monkeypatch.delenv("EEESOC_NOGOAL_WINDOW_2H", raising=False)
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.5")
    return build_model(_records())


def test_flatten_live_skips_dead_and_not_in_play():
    board = _board([_match("1"), _match("2", state="pre"), _match("3", detail="Postponed"), _match("", state="in")])
    rows = nogoal_monitor.flatten_live(board)
    assert list(rows) == ["1"]
    assert rows["1"]["league_slug"] == "quiet.1"
    assert rows["1"]["league_chiclet"] == "QUI"


def test_half_helpers():
    tl = _tl(50, [_ev(10, "shot"), _ev(20, "goal"), _ev(45, "goal", "away"), _ev(48, "shot_on", period=2, xg=0.3)])
    assert nogoal_monitor._half_goals(tl, 1) == 2
    assert nogoal_monitor._half_goals(tl, 2) == 0
    assert nogoal_monitor._half_over(tl, 1) is True  # 2nd-half events exist
    assert nogoal_monitor._half_over(tl, 2) is False
    assert nogoal_monitor._half_over(_tl(45, clock="HT"), 1) is True
    assert nogoal_monitor._half_over(_tl(30), 1) is False
    assert nogoal_monitor._half_over(_tl(90, final=True), 2) is True
    tempo = nogoal_monitor._shots_so_far(tl, 1)
    assert tempo == {"shots": 3, "sot": 2, "xg": 0.0}
    assert nogoal_monitor._shots_so_far(tl, 2) == {"shots": 1, "sot": 1, "xg": 0.3}


def test_poll_fires_once_then_busts(quiet_env):
    model = quiet_env
    state = {"signals": {}}
    timelines = {"1001": _tl(27)}

    msgs, store, evals = nogoal_monitor.poll(board=_board([_match()]), model=model, state=state, fetch_timeline=lambda m: timelines[m["event_id"]], now=1000.0)
    assert len(msgs) == 1
    assert "No more goals this half?" in msgs[0]
    assert "Alpha 0–0 Beta" in msgs[0]
    assert "QUI" in msgs[0]
    sig = store["signals"]["1001:1"]
    assert sig["status"] == "open" and sig["fired_minute"] == 27 and sig["half_goals_at_fire"] == 0
    assert sig["home_id"] == "" and sig["league_name"] == "Quiet League"
    assert evals["1001"]["signal"] == {"status": "open", "fired_minute": 27}
    assert evals["1001"]["ready"] is True

    # Same half again: no second trigger.
    timelines["1001"] = _tl(31)
    msgs, store, evals = nogoal_monitor.poll(board=_board([_match()]), model=model, state=store, fetch_timeline=lambda m: timelines[m["event_id"]], now=1240.0)
    assert msgs == []
    assert evals["1001"]["signal"]["status"] == "open"

    # Goal at 39' → busted with the scorer noted.
    timelines["1001"] = _tl(40, [_ev(39, "goal", "away")], home_score=0, away_score=1)
    msgs, store, evals = nogoal_monitor.poll(board=_board([_match(home_score=0, away_score=1)]), model=model, state=store, fetch_timeline=lambda m: timelines[m["event_id"]], now=1800.0)
    assert len(msgs) == 1
    assert msgs[0].startswith("❌ **Busted**")
    assert "at 39′ (Beta)" in msgs[0]
    assert "Record: 0/1 held (0%)" in msgs[0]
    sig = store["signals"]["1001:1"]
    assert sig["status"] == "busted" and sig["resolved_minute"] == 39
    assert sig["final_score"] == [0, 1]
    assert evals["1001"]["signal"]["status"] == "busted"

    # Nothing further once resolved.
    msgs, _, _ = nogoal_monitor.poll(board=_board([_match()]), model=model, state=store, fetch_timeline=lambda m: timelines[m["event_id"]], now=1900.0)
    assert msgs == []


def test_poll_held_at_halftime_and_second_half_is_separate_signal(quiet_env):
    model = quiet_env
    state = {"signals": {}}
    tls = {"1001": _tl(30)}
    fetch = lambda m: tls[m["event_id"]]  # noqa: E731

    msgs, store, _ = nogoal_monitor.poll(board=_board([_match()]), model=model, state=state, fetch_timeline=fetch, now=1.0)
    assert len(msgs) == 1

    tls["1001"] = _tl(45, clock="HT")
    msgs, store, evals = nogoal_monitor.poll(board=_board([_match(clock="HT")]), model=model, state=store, fetch_timeline=fetch, now=2.0)
    assert len(msgs) == 1
    assert msgs[0].startswith("✅ **Held**")
    assert "Record: 1/1 held (100%)" in msgs[0]
    assert store["signals"]["1001:1"]["status"] == "held"
    assert store["signals"]["1001:1"]["resolved_minute"] == 45
    # At HT the evaluation already looks at period 2, so the held 1H signal is not attached.
    assert evals["1001"]["period"] == 2 and evals["1001"]["signal"] is None

    # 2nd half at 0-0: evaluates period 2 independently; the key differs.
    tls["1001"] = _tl(60, [_ev(47, "shot", period=2)])
    msgs, store, evals = nogoal_monitor.poll(board=_board([_match(clock="60'")]), model=model, state=store, fetch_timeline=fetch, now=3.0)
    assert evals["1001"]["period"] == 2
    if evals["1001"]["ready"]:
        assert "1001:2" in store["signals"]
        assert len(msgs) == 1
    else:
        assert "1001:2" not in store["signals"]


def test_poll_no_trigger_outside_window_or_below_threshold(quiet_env, monkeypatch):
    model = quiet_env
    fetch = lambda m: _tl(27)  # noqa: E731
    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.95")
    msgs, store, evals = nogoal_monitor.poll(board=_board([_match()]), model=model, state={"signals": {}}, fetch_timeline=fetch, now=1.0)
    assert msgs == [] and store["signals"] == {}
    assert evals["1001"]["ready"] is False and evals["1001"]["signal"] is None

    monkeypatch.setenv("EEESOC_NOGOAL_THRESHOLD", "0.5")
    msgs, store, _ = nogoal_monitor.poll(board=_board([_match()]), model=model, state={"signals": {}}, fetch_timeline=lambda m: _tl(5), now=1.0)
    assert msgs == [] and store["signals"] == {}


def test_poll_voids_signal_when_game_vanishes(quiet_env):
    model = quiet_env
    fetch = lambda m: _tl(27)  # noqa: E731
    msgs, store, _ = nogoal_monitor.poll(board=_board([_match()]), model=model, state={"signals": {}}, fetch_timeline=fetch, now=0.0)
    assert len(msgs) == 1
    # Gone from the board, but not for long enough.
    msgs, store, _ = nogoal_monitor.poll(board=_board([]), model=model, state=store, fetch_timeline=fetch, now=600.0)
    assert msgs == [] and store["signals"]["1001:1"]["status"] == "open"
    msgs, store, _ = nogoal_monitor.poll(board=_board([]), model=model, state=store, fetch_timeline=fetch, now=3 * 3600.0)
    assert msgs == [] and store["signals"]["1001:1"]["status"] == "open"
    msgs, store, _ = nogoal_monitor.poll(board=_board([]), model=model, state=store, fetch_timeline=fetch, now=4 * 3600.0)
    assert len(msgs) == 1 and msgs[0].startswith("⚪ **Void**")
    assert store["signals"]["1001:1"]["status"] == "void"


def test_poll_survives_bad_timeline(quiet_env):
    def boom(_m):
        raise RuntimeError("feed down")

    msgs, store, evals = nogoal_monitor.poll(board=_board([_match()]), model=quiet_env, state={"signals": {}}, fetch_timeline=boom, now=1.0)
    assert msgs == [] and evals == {} and store["signals"] == {}


def test_format_trigger_pluralises_shots(quiet_env):
    model = quiet_env
    tl = _tl(27, [_ev(10, "shot", xg=0.05)])
    msgs, store, _ = nogoal_monitor.poll(board=_board([_match()]), model=model, state={"signals": {}}, fetch_timeline=lambda m: tl, now=1.0)
    assert "1 shot ·" in msgs[0]
    assert "1 shots" not in msgs[0]
    sig = store["signals"]["1001:1"]
    txt = nogoal_monitor.format_trigger({**sig, "tempo": {"shots": 3, "sot": 1, "xg": 0.2}})
    assert "3 shots" in txt
    assert f"**{sig['eval']['p_no_goal_pct']}%**" in txt
    assert "break-even odds" in txt


def test_state_roundtrip_and_signal_log(tmp_path: Path):
    path = tmp_path / "signals.json"
    assert nogoal_monitor.load_state(path) == {"signals": {}}
    state = {
        "signals": {
            "1:1": {"status": "held", "fired_at": 10.0},
            "2:1": {"status": "busted", "fired_at": 20.0},
            "3:1": {"status": "open", "fired_at": 30.0},
            "4:1": {"status": "void", "fired_at": 40.0},
        }
    }
    nogoal_monitor.save_state(state, path)
    loaded = nogoal_monitor.load_state(path)
    assert set(loaded["signals"]) == {"1:1", "2:1", "3:1", "4:1"}
    assert json.loads(path.read_text())["updated_at"] > 0
    log = nogoal_monitor.signal_log(path)
    assert [s["fired_at"] for s in log["signals"]] == [40.0, 30.0, 20.0, 10.0]
    assert log["held"] == 1 and log["resolved"] == 2 and log["open"] == 1 and log["hit_pct"] == 50
    assert "daily" in log

    path.write_text("not json")
    assert nogoal_monitor.load_state(path) == {"signals": {}}


def test_daily_record_buckets_by_utc_day():
    t_wed = datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc).timestamp()
    t_thu = datetime(2026, 9, 10, 16, 30, tzinfo=timezone.utc).timestamp()
    signals = {
        "roma:2": {
            "key": "roma:2",
            "status": "busted",
            "fired_at": t_thu,
            "home": "Roma",
            "away": "Inter",
        },
        "a:1": {"key": "a:1", "status": "held", "fired_at": t_wed},
        "b:1": {"key": "b:1", "status": "held", "fired_at": t_thu},
        "c:1": {"key": "c:1", "status": "open", "fired_at": t_thu},
    }
    days = nogoal_monitor.daily_record(signals)
    assert [d["date"] for d in days] == ["2026-09-09", "2026-09-10"]
    assert days[0] == {
        "date": "2026-09-09",
        "held": 1,
        "busted": 0,
        "open": 0,
        "void": 0,
        "fired": 1,
        "keys": ["a:1"],
        "hit_pct": 100,
    }
    assert days[1]["held"] == 1 and days[1]["busted"] == 1 and days[1]["open"] == 1
    assert days[1]["fired"] == 3 and days[1]["hit_pct"] == 50
    assert "roma:2" in days[1]["keys"]


def test_bets_tally_and_group_by_league():
    signals = {
        "epl-a:1": {
            "status": "held",
            "fired_at": 10.0,
            "league_slug": "eng.1",
            "league_chiclet": "EPL",
            "league_name": "English Premier League",
        },
        "epl-b:2": {
            "status": "busted",
            "fired_at": 30.0,
            "league_slug": "eng.1",
            "league_chiclet": "EPL",
        },
        "epl-c:1": {
            "status": "open",
            "fired_at": 40.0,
            "league_slug": "eng.1",
            "league_chiclet": "EPL",
        },
        "ll-a:1": {
            "status": "held",
            "fired_at": 20.0,
            "league_slug": "esp.1",
            "league_chiclet": "La Liga",
            "league_name": "Spanish LALIGA",
        },
    }
    tally = nogoal_monitor.tally_bets(signals)
    assert tally == {
        "fired": 4,
        "won": 2,
        "lost": 1,
        "live": 1,
        "void": 0,
        "settled": 3,
        "hit_pct": 67,
    }
    leagues = nogoal_monitor.bets_by_league(signals)
    assert [g["league_chiclet"] for g in leagues] == ["EPL", "La Liga"]
    epl = leagues[0]
    assert epl["fired"] == 3 and epl["won"] == 1 and epl["lost"] == 1 and epl["live"] == 1
    assert epl["hit_pct"] == 50
    assert [s["fired_at"] for s in epl["signals"]] == [40.0, 30.0, 10.0]
    liga = leagues[1]
    assert liga["won"] == 1 and liga["settled"] == 1 and liga["hit_pct"] == 100


def test_signal_log_includes_bets_board(tmp_path: Path):
    path = tmp_path / "signals.json"
    nogoal_monitor.save_state(
        {
            "signals": {
                "1:1": {
                    "status": "held",
                    "fired_at": 10.0,
                    "league_slug": "eng.1",
                    "league_chiclet": "EPL",
                },
                "2:1": {
                    "status": "busted",
                    "fired_at": 20.0,
                    "league_slug": "esp.1",
                    "league_chiclet": "La Liga",
                },
            }
        },
        path,
    )
    log = nogoal_monitor.signal_log(path)
    assert log["bets"]["won"] == 1 and log["bets"]["lost"] == 1 and log["bets"]["hit_pct"] == 50
    assert [g["league_chiclet"] for g in log["bets"]["leagues"]] == ["EPL", "La Liga"]


def test_save_state_trims_log(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(nogoal_monitor, "_MAX_LOG", 3)
    path = tmp_path / "s.json"
    state = {"signals": {f"{i}:1": {"status": "held", "fired_at": float(i)} for i in range(6)}}
    nogoal_monitor.save_state(state, path)
    kept = nogoal_monitor.load_state(path)["signals"]
    assert set(kept) == {"5:1", "4:1", "3:1"}


def test_post_discord_uses_webhook(monkeypatch):
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def opener(req, timeout):
        calls.append((req.full_url, json.loads(req.data.decode("utf-8")), timeout))
        return _Resp()

    assert nogoal_monitor.post_discord(["hello", "world"], "https://discord.test/hook", opener=opener) == 2
    assert calls[0][0] == "https://discord.test/hook"
    assert calls[0][1] == {"content": "hello", "username": "eeesoc no-goal"}
    assert calls[0][2] == 10

    monkeypatch.delenv("EEESOC_DISCORD_WEBHOOK", raising=False)
    assert nogoal_monitor.post_discord(["x"], opener=opener) == 0  # no webhook configured
    assert nogoal_monitor.post_discord([], "https://discord.test/hook", opener=opener) == 0

    def failing(req, timeout):
        raise OSError("nope")

    assert nogoal_monitor.post_discord(["x"], "https://discord.test/hook", opener=failing) == 0


def test_config_helpers(monkeypatch):
    monkeypatch.setenv("EEESOC_NOGOAL_POLL_S", "3")
    assert nogoal_monitor.poll_seconds() == 8.0
    monkeypatch.setenv("EEESOC_NOGOAL_POLL_S", "abc")
    assert nogoal_monitor.poll_seconds() == 20.0
    monkeypatch.setenv("EEESOC_NOGOAL_MONITOR", "0")
    assert nogoal_monitor.monitor_enabled() is False
    monkeypatch.delenv("EEESOC_NOGOAL_MONITOR", raising=False)
    assert nogoal_monitor.monitor_enabled() is True
    monkeypatch.setenv("EEESOC_NOGOAL_STATE", "/tmp/x/y.json")
    assert nogoal_monitor.state_path() == Path("/tmp/x/y.json")


def test_bets_tab_lists_todays_alarms_by_league():
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert 'data-tab="bets"' in html
    assert 'id="panel-bets"' in html
    assert "function refreshBets" in js
    assert "function betsByLeague" in js
    assert "function tallyBets" in js
    assert "function betCardHtml" in js
    assert "No more goals this half" in js
    assert "Won" in js and "Lost" in js and "Live" in js
    assert "if (name === \"bets\") refreshBets()" in js
    assert ".bets-hero" in css and ".bets-league" in css and ".bets-card.won" in css
    assert "split by league" in html
