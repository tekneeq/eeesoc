"""Goal categories: how each goal came about, read from the plays before the shot."""

from __future__ import annotations

import json
import os
from pathlib import Path

from eeesoc import halftime
from eeesoc.live import (
    GOAL_CATEGORIES,
    GOAL_TAG_VERSION,
    build_event_timeline,
    classify_goal,
    clear_timeline_cache,
    goal_tags_from_plays,
)

HOME, AWAY = "10", "20"


def _play(ptype, seconds, team, x=None, y=None, *, athlete=None, text="", period=1, end=None, **extra):
    minute = seconds // 60 + 1
    p = {
        "type": {"type": ptype, "text": ptype},
        "clock": {"displayValue": f"{minute}'", "value": seconds},
        "period": {"number": period},
        "team": {"$ref": f"http://x/teams/{team}?lang=en"},
        "text": text,
        "shortText": text.split(" (")[0] if text else "",
    }
    if x is not None:
        p["fieldPositionX"] = x
    if y is not None:
        p["fieldPositionY"] = y
    if end is not None:
        p["fieldPosition2X"], p["fieldPosition2Y"] = end
    if athlete:
        p["participants"] = [{"athlete": {"$ref": f"http://x/athletes/{athlete}?lang=en"}}]
    p.update(extra)
    return p


def _goal(seconds, team, *, athlete="9", text="Goal! Striker (Home) right footed shot from the centre of the box. Assisted by Mate.", info="Regular Play", yards=10, xg=0.3, x=92.0, y=50.0, **extra):
    return _play(
        "goal", seconds, team, x, y, athlete=athlete, text=text, scoringPlay=True, shotInfo={"text": info}, statYardage=yards, expectedGoals=xg, **extra
    )


def test_every_category_is_reachable():
    # ESPN labels these on the goal play itself.
    assert classify_goal([_goal(100, HOME, info="Own Goal", text="Own Goal by Back, Away.")], 0)["category"] == "own_goal"
    assert classify_goal([_goal(100, HOME, info="Penalty", text="Goal! Striker (Home) converts the penalty.")], 0)["category"] == "penalty"
    fk = classify_goal([_goal(100, HOME, info="Free Kick", yards=28)], 0)
    assert fk["category"] == "free_kick" and fk["reason"] == "direct free kick, 28 yards"
    corner = classify_goal([_goal(100, HOME, info="From Corner", text="Goal! Striker (Home) header from the centre of the box.")], 0)
    assert corner["category"] == "set_piece" and corner["reason"] == "from a corner · header"
    assert classify_goal([_goal(100, HOME, info="Throw-in Set Piece")], 0)["reason"] == "from a throw-in"

    # A screamer: distance, or almost no xG from outside the box.
    far = classify_goal([_goal(100, HOME, yards=30, xg=0.03, x=70.0)], 0)
    assert far["category"] == "screamer" and far["reason"] == "30 yards out, xG 0.03"
    low = classify_goal([_goal(100, HOME, yards=22, xg=0.02, text="Goal! Striker (Home) shot from outside the box.")], 0)
    assert low["category"] == "screamer"

    # Solo: the scorer beat a man, or carried the ball a long way.
    take_on = [_play("take-on", 95, HOME, 60.0, 50.0, athlete="9"), _goal(100, HOME)]
    assert classify_goal(take_on, 1) == {**classify_goal(take_on, 1), "category": "solo", "reason": "beat 1 defender on the way to the shot"}
    carry = [_play("ball-touch", 92, HOME, 55.0, 50.0, athlete="9"), _goal(100, HOME, x=92.0, y=50.0)]
    out = classify_goal(carry, 1)
    assert out["category"] == "solo" and out["reason"] == "carried the ball 39m to the shot"

    # Counter from ESPN's label; team from the passing move; scramble when nothing reads.
    fast = [_play("pass", 95, HOME, 40.0, 50.0, end=(70.0, 50.0)), _goal(100, HOME, info="Fast Break")]
    assert classify_goal(fast, 1)["reason"] == "fast break, 1 pass"
    move = [_play("pass", 80 + i * 4, HOME, 40.0 + i * 10, 50.0, end=(50.0 + i * 10, 50.0)) for i in range(4)] + [_goal(100, HOME)]
    assert classify_goal(move, 4) == {**classify_goal(move, 4), "category": "team", "reason": "4-pass move"}
    assert classify_goal([_goal(100, HOME, text="Goal! Striker (Home) header. Assisted by Mate with a cross.")], 0)["reason"] == "assisted from a cross · header"
    assert classify_goal([_goal(100, HOME, text="Goal! Striker (Home) shot from close range.")], 0) == {
        "category": "scramble",
        "reason": "loose ball in the box",
        "yards": 10,
        "shot_info": "regular play",
    }


def test_defensive_mistake_restarts_the_sequence_at_the_turnover():
    # Away pass around the back, then a miscontrol in their own third; the home side scores two touches later.
    # The passes *before* the turnover must not read as a home passing move.
    plays = [
        _play("pass", 70, AWAY, 30.0, 40.0, end=(32.0, 60.0), text="Pass by Keeper (Away)"),
        _play("pass", 74, AWAY, 32.0, 60.0, end=(28.0, 50.0), text="Pass by Back (Away)"),
        _play("pass", 78, AWAY, 28.0, 50.0, end=(26.0, 45.0), text="Pass by Mid (Away)"),
        _play("ball-touch", 84, AWAY, 24.0, 45.0, text="Jon Ander Olasagasti (Away) Ball touch"),
        _play("pass", 88, HOME, 76.0, 55.0, end=(85.0, 50.0), text="Pass by Mate (Home)"),
        _goal(92, HOME, text="Goal! Striker (Home) right footed shot. Assisted by Mate."),
    ]
    tag = classify_goal(plays, 5)
    assert tag["category"] == "mistake"
    assert tag["reason"] == "Jon Ander Olasagasti miscontrol in his own third, one pass later"

    # Lost the ball inside his own box → "in his own box"; a long move after the turnover is a team goal.
    box_gift = plays[:3] + [_play("dispossessed", 84, AWAY, 10.0, 50.0, text="Back (Away) dispossessed"), _goal(88, HOME)]
    assert classify_goal(box_gift, 4)["reason"] == "Back dispossessed in his own box, straight to the shot"
    patient = plays[:4] + [_play("pass", 86 + i * 2, HOME, 60.0 + i * 8, 50.0, end=(68.0 + i * 8, 50.0)) for i in range(3)] + [_goal(96, HOME)]
    assert classify_goal(patient, 7)["category"] == "team"
    # A cross is a created chance, not a gift, even right after a turnover.
    crossed = plays[:4] + [_goal(88, HOME, text="Goal! Striker (Home) header. Assisted by Wing with a cross.")]
    assert classify_goal(crossed, 4)["category"] == "team"
    # A turnover in the opponent's half is pressing, not a gift.
    plain = "Goal! Striker (Home) right footed shot from close range."
    high = plays[:3] + [_play("ball-touch", 84, AWAY, 60.0, 45.0, text="Mid (Away) Ball touch"), _goal(88, HOME, text=plain)]
    assert classify_goal(high, 4)["category"] == "scramble"
    # Too long after the turnover and the defence has reset.
    stale = plays[:4] + [_goal(84 + 20, HOME, text=plain)]
    assert classify_goal(stale, 4)["category"] == "scramble"

    # A keeper spill straight to the shot, and a clearance that comes straight back.
    spill = [_play("save", 95, AWAY, 3.0, 50.0, text="Keeper (Away) save"), _goal(98, HOME)]
    assert classify_goal(spill, 1)["reason"] == "Keeper keeper spill, straight to the shot"
    clear = [_play("clear", 96, AWAY, 12.0, 50.0, text="Back (Away) clearance"), _goal(99, HOME)]
    assert classify_goal(clear, 1)["reason"] == "Back clearance straight back in his own box, straight to the shot"
    late_clear = [_play("clear", 90, AWAY, 12.0, 50.0, text="Back (Away) clearance"), _goal(99, HOME, text=plain)]
    assert classify_goal(late_clear, 1)["category"] == "scramble"


def test_real_levante_goal_reads_as_a_gift():
    plays = json.loads(Path(__file__).with_name("fixtures").joinpath("plays-levante-atletico.json").read_text())
    tags = goal_tags_from_plays(plays)
    assert [(t["minute"], t["category"]) for t in tags] == [(36, "mistake")]
    assert tags[0]["reason"] == "Jon Ander Olasagasti miscontrol in his own third, straight to the shot"
    assert tags[0]["period"] == 1 and "Grimaldo" in tags[0]["text"]


def test_timeline_and_archive_carry_the_category(monkeypatch, tmp_path):
    monkeypatch.setenv("EEESOC_CACHE", str(tmp_path))
    halftime.clear_record_cache()
    clear_timeline_cache()
    plays = [_play("shot-off-target", 60 * i, HOME, 80.0, 50.0) for i in range(6)] + [
        _play("ball-touch", 84, AWAY, 24.0, 45.0, text="Back (Away) Ball touch"),
        _goal(90, HOME, text="Goal! Striker (Home) right footed shot."),
        _goal(60 * 70, AWAY, period=2, athlete="11", text="Goal! Winger (Away) shot from outside the box.", yards=27, xg=0.03, x=72.0),
    ]
    tl = build_event_timeline("t.1", "e1", home="Home", away="Away", home_id=HOME, away_id=AWAY, clock="FT", fetcher=lambda url: {"items": plays, "pageCount": 1}, use_cache=False)
    goals = [e for e in tl["events"] if e["kind"] == "goal"]
    assert [(e["team"], e["category"]) for e in goals] == [("home", "mistake"), ("away", "screamer")]
    assert goals[0]["reason"] == "Back miscontrol in his own third, straight to the shot"
    assert [(b["category"], b["reason"]) for b in tl["bulletin"]] == [("mistake", goals[0]["reason"]), ("screamer", "27 yards out, xG 0.03")]
    assert tl["goal_tags"] == GOAL_TAG_VERSION
    assert "category" not in tl["events"][0]  # shots carry no category

    rec = halftime.record_from_timeline(tl, {"league_slug": "t.1", "event_id": "e1", "home_id": HOME, "away_id": AWAY})
    assert rec["goal_tags"] == GOAL_TAG_VERSION
    stored = [e for e in rec["events"] if e["kind"] == "goal"]
    assert [(e["category"], e["reason"]) for e in stored] == [(g["category"], g["reason"]) for g in goals]
    assert halftime.records_missing_box_entries([rec]) == []

    # A record archived before categories existed is stamped by the backfill, goal by goal.
    old = {k: v for k, v in rec.items() if k != "goal_tags"}
    old["events"] = [{k: v for k, v in e.items() if k not in {"category", "reason"}} for e in rec["events"]]
    assert halftime.records_missing_box_entries([old]) == [old]
    stamped = halftime.stamp_goal_tags(old["events"], goal_tags_from_plays(plays))
    assert [(e.get("category"), e.get("reason")) for e in stamped if e["kind"] == "goal"] == [(g["category"], g["reason"]) for g in goals]
    assert all("category" not in e for e in stamped if e["kind"] != "goal")
    # Minutes that do not line up pair off in order when the counts agree; otherwise they are left alone.
    shifted = [{**e, "minute": e["minute"] + 1} for e in old["events"]]
    assert [e.get("category") for e in halftime.stamp_goal_tags(shifted, goal_tags_from_plays(plays)) if e["kind"] == "goal"] == ["mistake", "screamer"]
    assert [e.get("category") for e in halftime.stamp_goal_tags(shifted, goal_tags_from_plays(plays)[:1]) if e["kind"] == "goal"] == [None, None]

    path = halftime.record_path("t.1", "e1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old))
    enriched = halftime.enrich_box_entries(old, fetcher=lambda url: {"items": plays, "pageCount": 1})
    assert enriched["goal_tags"] == GOAL_TAG_VERSION
    assert [e["category"] for e in enriched["events"] if e["kind"] == "goal"] == ["mistake", "screamer"]
    assert json.loads(path.read_text())["goal_tags"] == GOAL_TAG_VERSION
    assert set(GOAL_CATEGORIES) >= {"mistake", "screamer", "team"}


def test_chiclet_tags_goals_with_category_rarity_and_club_mix():
    """Bulletin chip + rarity, the 'how' line under the stats, and the ⚽ goals power row."""
    import subprocess
    import tempfile

    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "function goalTagHtml" in js and "function chicletGoalMixHtml" in js and "function goalMixRowHtml" in js
    assert 'rows.findIndex((html) => html.includes("🧤 clean sheets"))' in js
    assert "chicletBulletinHtml(cached, m)" in js and "chicletBulletinHtml(tl, m)" in js
    assert "paintGoalRarity();" in js
    assert "+ chicletGoalMixHtml(tl, m) + chicletPostureHtml(tl, m)" in js
    assert ".mc-bl-tag.cat" in css and ".cat-mistake" in css and ".mc-bl-name" in css and ".mc-gm-g" in css and ".mc-goalmix" in css
    assert "<b>🎁 gift</b>" in html and "<b>⚽ goals</b>" in html and "1 in 4 games" in html

    script = (
        js[js.index("  function escapeHtml(s)") : js.index("  // Club-type tags and articles")]
        + js[js.index("  // —— Goal categories ——") : js.index("  // Clinical power — goals per 100 xG")]
        + r"""
const league = {
  label: "La Liga",
  goal_mix: { games: 40, goals: 110, categories: { team: { goals: 33, share_pct: 30, per_game: 0.83, game_pct: 60 }, mistake: { goals: 20, share_pct: 18, per_game: 0.5, game_pct: 38 }, screamer: { goals: 9, share_pct: 8, per_game: 0.23, game_pct: 23 } } },
};
const state = { clinical: { leagues: { "esp.1": league, thin: { label: "Thin", goal_mix: { games: 3, goals: 5, categories: { screamer: { goals: 1, share_pct: 20, per_game: 0.33, game_pct: 33 } } } } } } };
const rows = {
  H: { goal_mix: { games: 6, scored: { team: 5, set_piece: 3, solo: 2, penalty: 1 }, conceded: { mistake: 4, team: 3, screamer: 1 }, scored_total: 11, conceded_total: 8 }, _league: league },
  A: { goal_mix: { games: 0, scored: {}, conceded: {}, scored_total: 0, conceded_total: 0 }, _league: league },
};
function clinicalFor(slug, id) { return rows[id] || null; }
const m = { event_id: "e1", league_slug: "esp.1", home: "Levante", away: "Atlético", home_id: "H", away_id: "A" };

// Rarity: uncommon kinds read as "1 in N games"; common ones as a share; thin archives say nothing.
let r = goalRarity("screamer", "esp.1");
if (!r || r.text !== "1 in 4 games" || !r.rare || r.share !== 8) process.exit(2);
r = goalRarity("team", "esp.1");
if (r.text !== "60% of games" || r.rare) process.exit(3);
r = goalRarity("free_kick", "esp.1");
if (!r.rare || r.text !== "first in 40 La Liga games") process.exit(4);
if (goalRarity("screamer", "thin") !== null || goalRarity("screamer", "nope") !== null) process.exit(5);

// Bulletin: the category chip replaces the old P / OG marks and carries the reason; rare kinds show the odds.
const tl = {
  bulletin: [
    { minute: 36, clock: "36'", kind: "goal", team: "away", player: "Grimaldo", category: "mistake", reason: "Olasagasti miscontrol in his own third, straight to the shot" },
    { minute: 70, clock: "70'", kind: "goal", team: "home", player: "Romero", penalty: true, category: "penalty", reason: "penalty" },
    { minute: 80, clock: "80'", kind: "goal", team: "home", player: "Brugui", category: "screamer", reason: "31 yards out, xG 0.02" },
    { minute: 88, clock: "88'", kind: "own_goal", team: "away", player: "Toljan" },
  ],
};
const board = chicletBulletinHtml(tl, m);
if (!board.includes('<span class="mc-bl-tag cat cat-mistake" title="defensive mistake: Olasagasti miscontrol in his own third, straight to the shot · the defending side lost the ball in its own third and the shot came within a pass · 18% of La Liga goals, seen in 38% of games (1 in 3 games, 40 archived)">🎁 gift</span> <span class="mc-bl-rare">1 in 3 games</span>')) process.exit(6);
if (!board.includes('cat-screamer"') || !board.includes("🚀 screamer</span> <span class=\"mc-bl-rare\">1 in 4 games</span>")) process.exit(7);
if (!board.includes("⚪ pen</span>") || board.includes('mc-bl-tag pen"')) process.exit(8);
// The name sits in its own span so it ellipsizes before the tag does.
if (!board.includes('<span class="mc-bl-who mc-bl-goal"><span class="mc-bl-name">Grimaldo</span> <span class="mc-bl-tag cat cat-mistake"')) process.exit(25);
// Untagged own goal (archived before categories) keeps the old OG mark.
if (!board.includes('<span class="mc-bl-tag og">OG</span>')) process.exit(9);
// Penalty without a league archive: no rarity suffix.
const noLeague = chicletBulletinHtml(tl, { ...m, league_slug: "nope" });
if (noLeague.includes("mc-bl-rare")) process.exit(10);

// The "how" line: one chip per kind, counts, rarity on the rare ones, the players in the tooltip.
const how = chicletGoalMixHtml(tl, m);
if (!how.startsWith('<span class="mc-goalmix"><span class="mc-gm-label"')) process.exit(11);
if (!how.includes('<span class="mc-gm-item cat-mistake" title="defensive mistake ×1: 36\' Grimaldo — Olasagasti miscontrol in his own third, straight to the shot · 18% of La Liga goals, seen in 38% of games (1 in 3 games, 40 archived)">🎁 gift<i>1 in 3 games</i></span>')) process.exit(12);
if (!how.includes("🚀 screamer<i>1 in 4 games</i>") || !how.includes("⚪ pen<i>first in 40 La Liga games</i></span>")) process.exit(13);
if (how.includes("own_goal")) process.exit(14);  // untagged goals are left out
if (chicletGoalMixHtml({ bulletin: [{ kind: "goal", minute: 3 }] }, m) !== "" || chicletGoalMixHtml(null, m) !== "") process.exit(15);

// ⚽ goals row: every kind with its count, biggest first, over a bar split in the same proportions.
const row = goalMixRowHtml(m);
if (!row.includes('<span class="mc-power-label" title="')) process.exit(16);
if (!row.includes("⚽ goals</span>")) process.exit(17);
if (!row.includes('<span class="mc-gm-g scored"><em>scored 11</em><span class="mc-gm-bar"><i class="cat-team" style="flex:5" title="5 of 11 scored: team goals (45%)"></i><i class="cat-set_piece" style="flex:3" title="3 of 11 scored: set pieces (27%)"></i><i class="cat-solo" style="flex:2" title="2 of 11 scored: solo goals (18%)"></i><i class="cat-penalty" style="flex:1" title="1 of 11 scored: penalty (9%)"></i></span><b><span class="cat-team" title="5 of 11 scored: team goals (45%)">🎯5</span><span class="cat-set_piece" title="3 of 11 scored: set pieces (27%)">📐3</span><span class="cat-solo" title="2 of 11 scored: solo goals (18%)">⚡2</span><span class="cat-penalty" title="1 of 11 scored: penalty (9%)">⚪1</span></b></span>')) process.exit(18);
if (!row.includes('<span class="mc-gm-g conceded"><em>conceded 8</em><span class="mc-gm-bar"><i class="cat-mistake" style="flex:4"') || !row.includes('<b><span class="cat-mistake" title="4 of 8 conceded: defensive mistakes (50%)">🎁4</span><span class="cat-team" title="3 of 8 conceded: team goals (38%)">🎯3</span><span class="cat-screamer" title="1 of 8 conceded: screamer (13%)">🚀1</span></b></span>')) process.exit(19);
if (row.includes("mc-gm-more")) process.exit(26);  // nothing is folded into a +N any more
// Gifts are 50% of what they concede against the league's 18%: that is the tag.
if (!row.includes('<span class="mc-power-tag cat-mistake" title="4 of 8 conceded were defensive mistakes (50%; league 18%)">🎁 gift 50% GA</span>')) process.exit(20);
if (!row.includes('class="mc-power-side mc-gm-side home conceded-mistake"')) process.exit(21);
if (!row.includes("Stands out: 4 of 8 conceded were defensive mistakes, 50% against the league's 18%.")) process.exit(22);
// The away club's goals are not tagged yet → dash with the reason.
if (!row.includes('<span class="mc-power-side mc-gm-side away none" title="Atlético: the archived goals are not categorised yet — the backfill is reading the play-by-play"><b class="mc-power-num">—</b></span>')) process.exit(23);
if (!goalMixRowHtml({ ...m, home_id: "zz" }).includes("Levante: no finished games archived yet")) process.exit(24);
"""
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        name = fh.name
    try:
        proc = subprocess.run(["node", name], capture_output=True, text=True)
    finally:
        Path(name).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout or proc.returncode


def test_allowed_by_kind_table_shows_last_conceded_and_how_often():
    """A row per kind each club has let in: the last one (date, opponent, minute, games ago) and 1 in N games."""
    import subprocess
    import tempfile

    js = Path("src/eeesoc/static/app.js").read_text(encoding="utf-8")
    css = Path("src/eeesoc/static/app.css").read_text(encoding="utf-8")
    html = Path("src/eeesoc/static/index.html").read_text(encoding="utf-8")
    assert "function allowedTableHtml" in js and "function paintAllowedTables" in js and "paintAllowedTables();" in js
    assert 'class="mc-allowed" data-allowed-for=' in js
    assert ".mc-al-grid" in css and ".mc-al-last" in css and ".match-chiclet.collapsed .mc-allowed" in css
    assert "<b>goals allowed by kind</b>" in html

    script = (
        js[js.index("  function escapeHtml(s)") : js.index("  // Club-type tags and articles")]
        + js[js.index("  // Club-type tags and articles") : js.index("  // scope: \"live\" (default)")]
        + js[js.index("  // —— Goal categories ——") : js.index("  // Clinical power — goals per 100 xG")]
        + r"""
const kind = (n, every, pct, since, last, per_game) => ({ n, every, game_pct: pct, games_since: since, last, per_game: per_game ?? (n ? n / 6 : 0) });
const none = (games) => ({ n: 0, every: null, game_pct: 0, games_since: games, last: null, per_game: 0 });
const league = {
  label: "La Liga",
  par_allowed_by_kind: { games: 48, total: 66, kinds: { mistake: { n: 5, per_game: 0.1, every: 9.6, game_pct: 10 }, screamer: { n: 9, per_game: 0.19, every: 5.3, game_pct: 19 }, team: { n: 20, per_game: 0.42, every: 2.4, game_pct: 38 } } },
};
const lev = {
  games: 6, total: 5,
  kinds: {
    team: kind(1, 6.0, 17, 4, { start: "2026-09-05T18:00:00Z", opponent: "Real Betis", venue: "away", gf: 1, ga: 2, minute: 71, player: "Isco", reason: "5-pass move" }),
    mistake: kind(3, 2.0, 50, 0, { start: "2026-09-21T18:00:00Z", opponent: "Getafe", venue: "home", gf: 0, ga: 1, minute: 36, player: "Mayoral", reason: "Toljan miscontrol in his own third, straight to the shot" }),
    screamer: kind(1, 6.0, 17, 2, { start: "2026-09-13T18:00:00Z", opponent: "Sevilla", venue: "home", gf: 2, ga: 2, minute: 88, player: "Suso", reason: "31 yards out, xG 0.02" }, 0.17),
    solo: none(6), set_piece: none(6), free_kick: none(6), counter: none(6), penalty: none(6), own_goal: none(6), scramble: none(6),
  },
};
const atm = { games: 6, total: 2, kinds: { team: kind(2, 3.0, 33, 1, { start: "2026-09-17T18:00:00Z", opponent: "Barcelona", venue: "home", gf: 1, ga: 1, minute: 12, player: "Lewandowski", reason: "assisted from a cross · header" }), mistake: none(6), screamer: none(6), solo: none(6), set_piece: none(6), free_kick: none(6), counter: none(6), penalty: none(6), own_goal: none(6), scramble: none(6) } };
const rows = { H: { team: "Levante", allowed_by_kind: lev, _league: league }, A: { team: "Atlético Madrid", allowed_by_kind: atm, _league: league }, N: { team: "Newbie", allowed_by_kind: { games: 0, total: 0, kinds: {} }, _league: league } };
const state = { clinical: { leagues: { "esp.1": league } } };
function clinicalFor(slug, id) { return rows[id] || null; }
const m = { event_id: "e1", league_slug: "esp.1", home: "Levante", away: "Atlético Madrid", home_id: "H", away_id: "A" };

const out = allowedTableHtml(m);
if (!out.includes('class="mc-box-rate-label" title="Goals allowed by kind')) process.exit(2);
// Both clubs head the table with their totals.
if (!out.includes('<span class="mc-box-group mc-al-team home" title="Levante: 5 goals conceded over 6 tagged games">Levante<small>5 in 6 g</small></span>')) process.exit(3);
if (!out.includes("Atlético<small>2 in 6 g</small>")) process.exit(4);
// Rows only for kinds somebody has conceded, in category order; the rest in the footer.
const order = ["mc-al-kind cat-team", "mc-al-kind cat-mistake", "mc-al-kind cat-screamer"].map((s) => out.indexOf(s));
if (order.some((i) => i < 0) || order[0] > order[1] || order[1] > order[2]) process.exit(5);
if (out.includes("mc-al-kind cat-solo") || out.includes("mc-al-kind cat-penalty")) process.exit(6);
if (!out.includes("never allowed by either: ") || !out.includes("⚡ solo</span>") || !out.includes("⚪ pen</span>") || out.includes("never allowed by either: <span class=\"cat-team\"")) process.exit(7);
// Levante's last gift: date, venue + opponent + minute, and that it was the last game; the reason on hover.
if (!out.includes('<span class="mc-box-cell mc-al-last" title="Levante last conceded a defensive mistake on Sep 21 v Getafe (0–1): 36\' Mayoral — Toljan miscontrol in his own third, straight to the shot. Last game.">')) process.exit(8);
if (!out.includes("<b>Sep 21</b><small>v Getafe 36'</small><i class=\"mc-box-rank\">last g</i>")) process.exit(9);
// Away game shows @ and the score as it stood on the pitch (home–away), and games ago.
if (!out.includes("<b>Sep 5</b><small>@ Betis 71'</small><i class=\"mc-box-rank mc-al-ago\">4 ago</i>")) process.exit(10);
if (!out.includes("on Sep 5 @ Real Betis (2–1): 71' Isco — 5-pass move. 4 games ago.")) process.exit(11);
// How often: 1 in N with the count, the share, and the league par in the tooltip; gifts 5× the league → amber.
if (!out.includes('<span class="mc-box-cell mc-al-rate down" title="Levante concedes a defensive mistake every 2 games: 3 in 6 games, 0.50 per game, in 50% of games · league 0.10 per game (1 in 9.6)"><b>1 in 2</b><small>3/6 g · 50%</small></span>')) process.exit(12);
// Atlético have never conceded a gift: "never", and 0 reads green against the league.
if (!out.includes('<span class="mc-box-cell none mc-al-never" title="Atlético Madrid has not conceded a defensive mistake in 6 tagged games"><b>never</b><small>in 6 g</small></span>')) process.exit(13);
if (!out.includes('<span class="mc-box-cell mc-al-rate up" title="Atlético Madrid: 0 defensive mistakes conceded in 6 games · league 0.10 per game (1 in 9.6)"><b>0</b><small>0/6 g</small></span>')) process.exit(14);
// Within ±25% of the league: no tone (Levante's one screamer in six ≈ league 0.19).
if (!out.includes('class="mc-box-cell mc-al-rate" title="Levante concedes a screamer every 6 games')) process.exit(15);
// Header row.
if (!out.includes('<span class="mc-box-h">Last</span><span class="mc-box-h">How often</span>')) process.exit(16);

// A club with no tagged games gets dashes; two such clubs → no table at all.
const half = allowedTableHtml({ ...m, away_id: "N", away: "Newbie" });
if (!half.includes('<span class="mc-box-cell none" title="Newbie: no tagged games yet"><b>–</b><small>no games</small></span>')) process.exit(17);
if (allowedTableHtml({ ...m, home_id: "N", away_id: "zz" }) !== "") process.exit(18);
if (allowedDateText("nonsense-date") !== "nonsense-d" || allowedDateText("") !== "—") process.exit(19);
if (allowedAgoText(0) !== "last game" || allowedAgoText(1) !== "1 game ago" || allowedAgoText(3) !== "3 games ago") process.exit(20);
"""
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        name = fh.name
    try:
        proc = subprocess.run(["node", name], capture_output=True, text=True, env={**os.environ, "TZ": "UTC"})
    finally:
        Path(name).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout or proc.returncode
