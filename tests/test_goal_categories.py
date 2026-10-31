"""Goal categories: how each goal came about, read from the plays before the shot."""

from __future__ import annotations

import json
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
    assert ".mc-bl-tag.cat" in css and ".cat-mistake" in css and ".mc-gm-g" in css and ".mc-goalmix" in css
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

// ⚽ goals row: scored / conceded chips with the top three kinds, and the standout against the league.
const row = goalMixRowHtml(m);
if (!row.includes('<span class="mc-power-label" title="')) process.exit(16);
if (!row.includes("⚽ goals</span>")) process.exit(17);
if (!row.includes('<span class="mc-gm-g scored"><em>scored 11</em><b><span class="cat-team" title="5 of 11 scored: team goal">🎯5</span><span class="cat-set_piece" title="3 of 11 scored: set piece">📐3</span><span class="cat-solo" title="2 of 11 scored: solo goal">⚡2</span><span class="mc-gm-more">+1</span></b></span>')) process.exit(18);
if (!row.includes('<span class="mc-gm-g conceded"><em>conceded 8</em><b><span class="cat-mistake" title="4 of 8 conceded: defensive mistake">🎁4</span><span class="cat-team" title="3 of 8 conceded: team goal">🎯3</span><span class="cat-screamer" title="1 of 8 conceded: screamer">🚀1</span></b></span>')) process.exit(19);
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
