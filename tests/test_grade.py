"""Live grade: a side against what it usually has by the same minute."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from eeesoc import grade, halftime
from eeesoc.grade import (
    GradeIndex,
    count_percentile,
    grade_timeline,
    letter_for,
    metric_percentile,
    side_game,
    stamp_grade,
    usual_at,
)
from eeesoc.live import BOX_VERSION, build_event_timeline, clear_timeline_cache, touch_counts_from_plays


def _touches(home_per_min: int = 6, away_per_min: int = 4, upto: int = 90) -> dict[str, list[int]]:
    return {
        "home": [home_per_min if 1 <= m <= upto else 0 for m in range(91)],
        "away": [away_per_min if 1 <= m <= upto else 0 for m in range(91)],
    }


def _record(
    event_id: str,
    *,
    home_shots: list[int],
    away_shots: list[int] = (),
    home_goals: list[int] = (),
    home_box: list[int] = (),
    league: str = "t.1",
    home: str = "Alpha",
    away: str = "Beta",
    home_id: str = "10",
    away_id: str = "20",
    start: str = "2026-08-01T18:00Z",
    touches: dict | None = None,
    xg: float | None = 0.1,
) -> dict:
    events = [{"minute": m, "kind": "shot", "team": "home", "xg": xg, "period": 1 if m <= 45 else 2} for m in home_shots]
    events += [{"minute": m, "kind": "goal", "team": "home", "xg": xg, "period": 1 if m <= 45 else 2} for m in home_goals]
    events += [{"minute": m, "kind": "shot", "team": "away", "xg": xg, "period": 1 if m <= 45 else 2} for m in away_shots]
    return {
        "event_id": event_id, "league_slug": league, "home": home, "away": away, "home_id": home_id, "away_id": away_id,
        "start": start, "events": sorted(events, key=lambda e: e["minute"]), "has_xg": xg is not None,
        "box_entries": {"1": {"home": [m for m in home_box if m <= 45], "away": []}, "2": {"home": [m for m in home_box if m > 45], "away": []}},
        "box_version": BOX_VERSION,
        "touch_counts": touches if touches is not None else _touches(),
    }


# Alpha's habit: a chance every four minutes from the 4th, so five by 20' and eleven by the break.
STEADY = list(range(4, 90, 4))


def _history(n: int = 6) -> list[dict]:
    return [
        _record(f"h{i}", home_shots=STEADY, away_shots=[30, 60], home_box=list(range(2, 90, 2)), start=f"2026-08-{i + 1:02d}T18:00Z")
        for i in range(n)
    ]


def _live(minute: int, *, home_shots=(), away_shots=(), home_box=(), final=False, touches=None, event_id="live") -> dict:
    rec = _record(event_id, home_shots=list(home_shots), away_shots=list(away_shots), home_box=list(home_box), touches=touches or _touches(upto=minute))
    return {**rec, "minute": minute, "final": final, "frozen": final}


def test_count_percentile_is_centred_and_ordered():
    # A side exactly on its usual sits in the middle; a blank against a real expectation sits low.
    assert 0.4 < count_percentile(5, 5.0) < 0.6
    assert count_percentile(0, 5.0) < 0.01
    assert count_percentile(0, 0.3) > 0.35  # nothing expected yet → a blank says little
    assert count_percentile(0, 0.0) == 0.5 and count_percentile(2, 0.0) == 1.0
    steps = [count_percentile(k, 4.0, 2.0) for k in range(12)]
    assert steps == sorted(steps) and steps[0] < 0.1 and steps[-1] > 0.95
    # Bursty counts: the same shortfall is weaker evidence than under Poisson.
    assert count_percentile(1, 5.0, 3.5) > count_percentile(1, 5.0, 1.0)
    assert metric_percentile("xg", 0.5, 0.5, 30) == pytest.approx(0.5)
    # Possession is noisy early: +10 points reads stronger at 60' than at 8'.
    assert metric_percentile("possession", 60, 50, 60) > metric_percentile("possession", 60, 50, 8) > 0.5
    assert [letter_for(s) for s in (90, 75, 74, 60, 40, 39, 25, 24, 0)] == ["A", "A", "B", "B", "C", "D", "D", "F", "F"]


def test_no_chances_by_twenty_minutes_grades_low_against_the_clubs_own_usual():
    index = GradeIndex(_history())
    # 20' in, Alpha — normally five chances and ten box touches by now — has done nothing.
    blank = grade_timeline(_live(20), home_id="10", away_id="20", start="2026-09-01T18:00Z", index=index)
    home = blank["home"]
    parts = {p["key"]: p for p in home["parts"]}
    assert blank["minute"] == 20 and blank["final"] is False
    assert home["basis"] == "club" and home["games"] == 6
    assert parts["chances"]["actual"] == 0 and 3.5 < parts["chances"]["usual"] < 5.1
    assert parts["chances"]["score"] < 15 and parts["box"]["score"] < 15
    assert home["score"] < 30 and home["letter"] in {"D", "F"}
    assert set(parts) == {"goals", "chances", "xg", "box", "possession"}
    assert sum(p["weight"] for p in home["parts"]) == pytest.approx(1.0)

    # The same blank five minutes in costs far less: there was little to have done yet.
    early = grade_timeline(_live(5), home_id="10", away_id="20", start="2026-09-01T18:00Z", index=index)
    assert early["home"]["score"] > home["score"] + 10

    # On its usual pace it sits in the middle (a touch over 50: the usual is pulled toward the league's); well ahead of it, an A.
    usual = grade_timeline(
        _live(20, home_shots=[4, 8, 12, 16, 20], home_box=range(2, 21, 2)), home_id="10", away_id="20", start="2026-09-01T18:00Z", index=index
    )
    assert 45 <= usual["home"]["score"] <= 68 and usual["home"]["letter"] in {"B", "C"}
    hot = grade_timeline(
        _live(20, home_shots=range(2, 21, 2), home_box=range(1, 21), touches=_touches(8, 2, upto=20)),
        home_id="10", away_id="20", start="2026-09-01T18:00Z", index=index,
    )
    assert hot["home"]["score"] > usual["home"]["score"] + 15 and hot["home"]["letter"] == "A"


def test_usual_is_read_at_the_same_minute_and_shrunk_toward_the_league():
    # A league of quiet sides plus one loud club with a single game.
    quiet = [
        _record(f"q{i}", home_shots=[20, 60], away_shots=[40, 80], home="Q", away="R", home_id=f"3{i}", away_id=f"4{i}")
        for i in range(6)
    ]
    loud = _record("loud", home_shots=list(range(1, 41)), home="Loud", home_id="77", away="R", away_id="40")
    index = GradeIndex(quiet + [loud])
    games = index.history(league="t.1", team_id="77", name="Loud", event_id="x", start="2027-01-01T00:00Z")
    assert len(games) == 1
    value, n = usual_at(index, games, league="t.1", metric="chances", minute=20)
    league = index.league_mean("t.1", "chances", 20)[0]
    # One game of 20 chances by 20' against two league games' worth of prior.
    assert n == 1 and value == pytest.approx((20 + 2 * league) / 3)
    assert usual_at(index, games, league="t.1", metric="chances", minute=40)[0] > value
    # A club the archive has never seen is measured against the league alone.
    assert usual_at(index, [], league="t.1", metric="chances", minute=20) == (league, 0)
    assert usual_at(index, [], league="t.1", metric="possession", minute=20)[0] == 50.0
    unknown = grade_timeline(_live(30, home_shots=[5]), home_id="999", away_id="998", start="2027-01-01T00:00Z", index=index)
    assert unknown["home"]["basis"] == "league" and unknown["home"]["games"] == 0


def test_history_skips_this_game_later_games_and_thin_records():
    recs = _history(3)
    this_game = _record("live", home_shots=STEADY, start="2026-08-10T18:00Z")
    later = _record("later", home_shots=STEADY, start="2026-08-20T18:00Z")
    # Goals-only feed: a handful of on-ball plays all game — would read as a club that never shoots.
    thin = _record("thin", home_shots=[10, 20, 30, 40, 50, 60], start="2026-07-01T18:00Z", touches=_touches(0, 0))
    # Too few events to trust at all.
    stub = _record("stub", home_shots=[10, 20], start="2026-07-02T18:00Z")
    index = GradeIndex(recs + [this_game, later, thin, stub])
    games = index.history(league="t.1", team_id="10", name="Alpha", event_id="live", start="2026-08-10T18:00Z")
    assert sorted(g.event_id for g in games) == ["h0", "h1", "h2"]
    assert [g.event_id for g in games] == ["h2", "h1", "h0"]  # most recent first
    # No id on the board row: the club is found by name within the league.
    by_name = index.history(league="t.1", team_id="", name="alpha", event_id="live", start="2026-08-10T18:00Z")
    assert len(by_name) == 3


def test_side_game_reads_goals_own_goals_xg_and_possession():
    rec = _record("g", home_shots=[10, 30], home_goals=[50], away_shots=[20])
    rec["events"].append({"minute": 70, "kind": "own_goal", "team": "home", "xg": None, "period": 2})
    rec["events"].append({"minute": 75, "kind": "corner", "team": "home", "xg": None, "period": 2})
    game = side_game(rec, "home")
    assert game.at("goals", 49) == 0 and game.at("goals", 50) == 1 and game.at("goals", 90) == 2
    assert game.at("chances", 90) == 3  # the own goal is a goal for them, not a chance; corners are neither
    assert game.at("xg", 9) == 0 and game.at("xg", 30) == pytest.approx(0.2) and game.at("xg", 90) == pytest.approx(0.3)
    assert game.at("possession", 45) == pytest.approx(60.0)
    assert game.at("possession", 1) is None  # ten plays is not a sample
    # A record archived before the counts existed has no possession or box read, and no xG without values.
    old = {k: v for k, v in rec.items() if k not in {"touch_counts", "box_version"}}
    old["has_xg"] = False
    old_game = side_game(old, "home")
    assert old_game.at("possession", 45) is None and old_game.at("box", 45) is None and old_game.at("xg", 45) is None


def test_grade_waits_for_the_fifth_minute_and_ignores_goals_only_feeds():
    index = GradeIndex(_history())
    assert grade_timeline(_live(4), index=index) is None
    assert grade_timeline({}, index=index) is None
    # A live feed with no on-ball plays behind it has nothing to grade.
    assert grade_timeline(_live(30, touches=_touches(0, 0)), index=index) is None
    # Shots are landing but none carries an xG value → xG is left out and the rest re-weighted.
    no_xg = _live(40, home_shots=[5, 10, 15, 20])
    for ev in no_xg["events"]:
        ev["xg"] = None
    out = grade_timeline(no_xg, home_id="10", away_id="20", index=index)
    assert "xg" not in {p["key"] for p in out["home"]["parts"]}
    # Full time is graded against the whole game.
    done = grade_timeline(_live(90, home_shots=STEADY, home_box=range(2, 90, 2), final=True, touches=_touches()), home_id="10", away_id="20", index=index)
    assert done["final"] is True and done["minute"] == 90 and 45 <= done["home"]["score"] <= 68


def test_stamp_grade_never_fails_the_timeline(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("archive unreadable")

    monkeypatch.setattr(grade, "grade_timeline", boom)
    tl = {"minute": 30}
    assert stamp_grade(tl) is tl and tl["grade"] is None


def _play(ptype: str, minute: int, team: str) -> dict:
    return {
        "type": {"type": ptype, "text": ptype},
        "clock": {"displayValue": f"{minute}'", "value": minute * 60},
        "period": {"number": 1 if minute <= 45 else 2},
        "team": {"$ref": f"http://x/teams/{team}?lang=en"},
        "fieldPositionX": 50.0,
        "fieldPositionY": 50.0,
    }


def test_touch_counts_ride_the_timeline_and_the_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    halftime.clear_record_cache()
    clear_timeline_cache()
    plays = (
        [_play("pass", m, "10") for m in range(1, 31) for _ in range(3)]
        + [_play("pass", m, "20") for m in range(1, 31)]
        + [_play("foul", 12, "10"), _play("shot-off-target", 14, "20")]
        + [_play("shot-on-target", m, "10") for m in (5, 10, 15, 20, 25, 30)]
    )
    counts = touch_counts_from_plays(plays, home_id="10", away_id="20")
    assert len(counts["home"]) == 91 and len(counts["away"]) == 91
    assert counts["home"][5] == 4 and counts["home"][6] == 3  # three passes + the shot; fouls are not on-ball
    assert counts["away"][14] == 2 and counts["away"][40] == 0
    assert sum(counts["home"]) == 96 and sum(counts["away"]) == 31

    tl = build_event_timeline("t.1", "1", home="A", away="B", home_id="10", away_id="20", clock="FT", fetcher=lambda url: {"items": plays, "pageCount": 1}, use_cache=False)
    assert tl["touch_counts"] == counts
    rec = halftime.record_from_timeline(tl, {"start": "2026-08-01T18:00Z", "home_id": "10", "away_id": "20"})
    assert rec["touch_counts"] == counts and halftime.has_touch_counts(rec)

    # A record archived before the counts existed is picked up by the play-by-play backfill.
    old = {k: v for k, v in rec.items() if k != "touch_counts"}
    path = halftime.record_path("t.1", "1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old))
    halftime.clear_record_cache()
    assert not halftime.has_touch_counts(old)
    assert [r["event_id"] for r in halftime.records_needing_play_backfill()] == ["1"]
    status = halftime.backfill_box_entries(fetcher=lambda url: {"items": plays, "pageCount": 1}, pause_s=0)
    assert status["todo"] == 1 and status["done"] == 1
    assert json.loads(path.read_text())["touch_counts"] == counts
    assert halftime.records_needing_play_backfill() == []


def test_timeline_endpoint_stamps_the_grade(monkeypatch, tmp_path):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from types import SimpleNamespace
    from urllib.request import urlopen

    from eeesoc import dashboard
    from eeesoc.dashboard import make_handler

    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    halftime.clear_record_cache()
    grade.clear_grade_cache()
    for rec in _history(4):
        path = halftime.record_path("t.1", rec["event_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec))
    seen = {}

    def fake_timeline(league, event_id, **kw):
        seen.update(kw, league=league, event_id=event_id)
        return _live(20, home_shots=[4, 8, 12, 16, 20], home_box=range(2, 21, 2))

    monkeypatch.setattr(dashboard, "build_event_timeline", fake_timeline)
    handler = make_handler(SimpleNamespace(matches=[], by_id={}, history=[], corpus=[], season="2026-27"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/live/timeline?league=t.1&event_id=live&home=Alpha&away=Beta&home_id=10&away_id=20&start=2026-09-01T18:00Z&clock=20'"
        with urlopen(url) as res:
            body = json.loads(res.read())
    finally:
        server.shutdown()
        server.server_close()
        grade.clear_grade_cache()
    g = body["grade"]
    assert seen["home_id"] == "10" and g["minute"] == 20
    assert g["home"]["basis"] == "club" and g["home"]["games"] == 4 and g["home"]["letter"] in {"B", "C"}
    assert [p["key"] for p in g["home"]["parts"]] == ["goals", "chances", "xg", "box", "possession"]
    # Beta has shot twice a game at 30' and 60' in the archive: nothing by 20' is its usual.
    assert g["away"]["basis"] == "club" and {p["key"]: p["actual"] for p in g["away"]["parts"]}["chances"] == 0


def test_chiclet_shows_the_grade_row_above_the_line_graph():
    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "+ chicletPostureHtml(tl, m) + chicletGradeHtml(tl, m)" in js
    assert ".mc-grade {" in css and ".mc-grade-mark.up" in css and ".mc-grade-mark.down" in css
    assert "📈 grade" in html and "its own usual" in html
    start = js.index("  const GRADE_HELP =")
    end = js.index("  function chicletStatsHtml(tl, m)")
    script = (
        'const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");\n'
        + js[start:end]
        + """
const part = (key, actual, usual, score, weight) => ({ key, actual, usual, score, weight, games: 6 });
const tl = {
  home: "Alpha FC", away: "Beta <United>",
  grade: {
    version: 1, minute: 20, final: false,
    home: { score: 19, letter: "F", games: 6, basis: "club", parts: [
      part("goals", 0, 0.4, 34, 0.2), part("chances", 0, 4.6, 5, 0.2), part("xg", 0, 0.46, 17, 0.2),
      part("box", 1, 9.5, 4, 0.25), part("possession", 44, 58, 6, 0.15) ] },
    away: { score: 78, letter: "A", games: 0, basis: "league", parts: [
      part("goals", 1, 0.3, 88, 0.2), part("chances", 6, 2.9, 90, 0.2), part("box", 9, 6.5, 70, 0.25), part("possession", 56, 50, 50, 0.15) ] },
  },
};
const out = chicletGradeHtml(tl, {});
let bad = 0;
const need = (s, why) => { if (!out.includes(s)) { console.error("missing", why, s); bad++; } };
need('<span class="mc-grade" data-grade-minute="20">', "grade block");
need('>vs usual by 20′</span>', "says which minute the usual is read at");
need('<span class="mc-grade-mark down" title="Alpha FC: F · 19 of 100 against its usual by 20′ (50 = usual)"><b>F</b> 19</span>', "home letter + number, toned low");
need('<span class="mc-grade-mark up" title="Beta &lt;United&gt;: A · 78 of 100 against its usual by 20′ (50 = usual)"><b>A</b> 78</span>', "away letter + number, toned high");
need('<i class="mc-key mc-key-home"></i>Alpha FC', "home key");
need('<i class="mc-key mc-key-away"></i>Beta &lt;United&gt;', "away name escaped");
need('<span class="mc-grade-part down" title="Alpha FC chances (shots incl. blocked): 0 so far, usually 4.6 by 20′ → 5 of 100 · 20% of the grade"><small>Chances</small><b>0</b><i>/4.6</i></span>', "chances so far / usual");
need('<small>xG</small><b>0.00</b><i>/0.46</i>', "xG to two places");
need('<small>Poss</small><b>44%</b><i>/58%</i>', "possession as a share");
need('<span class="mc-grade-part" title="Beta &lt;United&gt; possession: 56% so far, usually 50% by 20′ → 50 of 100 · 15% of the grade">', "a read on its usual is untoned");
need('<span class="mc-grade-part none" title="xG: not measured in this club’s archived games yet"><small>xG</small>—</span>', "a read the feed lacks stays blank");
need('>6 games</span>', "club basis");
need('>league avg</span>', "league basis");
if ((out.match(/class="mc-grade-part/g) || []).length !== 10) { console.error("expected five reads per side"); bad++; }
// One side with no grade still gets its row; no grade at all → nothing.
const half = chicletGradeHtml({ home: "A", away: "B", grade: { minute: 90, final: true, home: tl.grade.home, away: null } }, {});
if (!half.includes(">no history</span>") || !half.includes("vs usual over a full game")) { console.error("ungraded side / full-time wording"); bad++; }
if (chicletGradeHtml({ grade: null }, {}) !== "" || chicletGradeHtml({}, {}) !== "" || chicletGradeHtml({ grade: { minute: 9, home: null, away: null } }, {}) !== "") { console.error("empty grade should render nothing"); bad++; }
process.exit(bad ? 1 : 0);
"""
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout
