"""
Clinical power: how well each club finishes the chances it creates.

Built from the finished-match archive (``eeesoc.halftime``): every shot a
club took, the xG ESPN attached to it, and the goals that followed.

* ``power`` is goals scored per 100 xG — 100 means the club scores exactly
  what its chances were worth, 150 means half again as many.  A small prior
  (two "league-average" xG worth of chances) pulls thin samples toward par
  so one lucky game does not read as 400.
* Leagues without an ESPN xG feed fall back to shots-on-target conversion
  against the league's own conversion rate, on the same 100 = par scale.
* ``clinical`` is True when the club has scored more than its chances were
  worth (power > 100); otherwise it is wasteful.
* ``rank`` is the club's place in its league table of power (1 = most
  clinical), over clubs with at least ``MIN_GAMES`` archived games.

Offence power is the superset of clinical: everything that leads to goals.
Each of the club's per-game rates — xG created, shots, shots on target,
corners (sustained pressure) and goals — is divided by the league's rate,
shrunk toward par, and blended with ``OFFENSE_WEIGHTS_*``.  100 = a
league-typical attack; 130 = creates ~30% more than the league.  ``potent``
is True above 100, otherwise the attack is blunt.

Defence power is the mirror image: how little chance quality the club
allows.  ``defense_power`` is league-average xG conceded per game over the
club's own xG conceded per game (shrunk toward par), so 100 = allows exactly
the league's typical chances, 125 = allows a fifth less.  ``solid`` is True
above 100, otherwise the defence is leaky.  Leagues without xG use shots on
target conceded per game instead.

Momentum is recent form: points per game over the club's last ``FORM_GAMES``
results, weighted toward the most recent, shrunk toward the league's points
per game and put on the same 100 = par scale.  ``form`` is the W/D/L string
(oldest → newest); ``rising`` is True above 100, otherwise the club is fading.

Potential is the club's underlying strength once finishing luck is stripped
out: the geometric mean of its chance-creation index (xG created vs the
league) and its defence power, so 100 = a league-typical side on chance
quality both ways.  ``results_power`` is the same construction on actual
goals; ``potential_tag`` is ``upside`` when results lag the underlying
numbers by ``POTENTIAL_GAP`` or more (they should improve), ``overachieving``
when results outrun them, otherwise ``steady``.

Clean sheets are counted from the final score: ``clean_sheets`` over every
archived game, ``recent_clean_sheets`` over the last ``FORM_GAMES``, and
``clean_sheet_rank`` by total (ties to the club that needed fewer games).
``tight`` is True when the club's clean-sheet rate beats the league's.

``half_goals`` is the club's history of goals per half: ``first`` is how
often its first halves produced 0 / 1 / 2 / 3+ goals — ``total`` (both sides
combined), ``scored`` (the club's own) and ``allowed`` — and ``second_by_ht``
is the same three splits for second halves, keyed by the half-time score from
the club's point of view (``"0-1"`` = trailed 0-1 at the break).  The league
carries the same tables (home-away view, each game once, so ``scored`` is the
home side's) as a fallback when a club has fewer than
``HALF_GOALS_MIN_SAMPLE`` matches.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from eeesoc.halftime import _record_has_xg, load_records
from eeesoc.live import LEAGUES

MIN_GAMES = 2
# Prior weight in xG (or SOT for the fallback) that pulls thin samples to par.
PRIOR_XG = 2.0
PRIOR_SOT = 5.0
# Prior weight in games for the per-game offensive / defensive rates.
PRIOR_GAMES = 2.0
# Offence composite: each per-game rate is divided by the league's, then blended.
# Chance quality leads; volume, pressure (corners) and goals actually scored follow.
OFFENSE_WEIGHTS_XG = {"xg": 0.40, "shots": 0.20, "sot": 0.15, "corners": 0.10, "goals": 0.15}
OFFENSE_WEIGHTS_SOT = {"shots": 0.35, "sot": 0.25, "corners": 0.15, "goals": 0.25}
# Momentum looks at this many most-recent results, newest weighted heaviest.
FORM_GAMES = 5
# Potential vs results must differ by at least this much to be tagged upside / overachieving.
POTENTIAL_GAP = 5.0
# Fewer matching games than this and the chiclet falls back to the league-wide half-goal split.
HALF_GOALS_MIN_SAMPLE = 3
_CACHE_TTL_S = 300.0

_lock = threading.Lock()
_cache: tuple[float, int, dict[str, Any]] | None = None


def team_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _team_totals(records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """league_slug → team key → raw totals."""
    leagues: dict[str, dict[str, dict[str, Any]]] = {}
    for rec in records:
        slug = str(rec.get("league_slug") or "")
        if not slug:
            continue
        has_xg = _record_has_xg(rec)
        bucket = leagues.setdefault(slug, {})
        for side in ("home", "away"):
            name = str(rec.get(side) or "")
            tid = str(rec.get(f"{side}_id") or "")
            key = tid or team_key(name)
            row = bucket.setdefault(
                key,
                {
                    "team": name,
                    "team_id": tid,
                    "key": team_key(name),
                    "league_slug": slug,
                    "league_chiclet": rec.get("league_chiclet") or "",
                    "games": 0,
                    "games_xg": 0,
                    "goals": 0,
                    "shots": 0,
                    "sot": 0,
                    "xg": 0.0,
                    "corners": 0,
                    "conceded": 0,
                    "shots_against": 0,
                    "sot_against": 0,
                    "xg_against": 0.0,
                    "scored": 0,
                    "points": 0,
                    "results": [],
                },
            )
            if not row["team"] and name:
                row["team"] = name
            row["games"] += 1
            if has_xg:
                row["games_xg"] += 1
            other = "away" if side == "home" else "home"
            gf = int(rec.get(f"ft_{side}") or 0)
            ga = int(rec.get(f"ft_{other}") or 0)
            pts = 3 if gf > ga else (1 if gf == ga else 0)
            row["scored"] += gf
            row["points"] += pts
            ht_known = rec.get("ht_home") is not None and rec.get("ht_away") is not None
            row["results"].append(
                {
                    "start": str(rec.get("start") or rec.get("date") or ""),
                    "opponent": str(rec.get(other) or ""),
                    "venue": side,
                    "gf": gf,
                    "ga": ga,
                    "ht_gf": int(rec.get(f"ht_{side}") or 0) if ht_known else None,
                    "ht_ga": int(rec.get(f"ht_{other}") or 0) if ht_known else None,
                    "points": pts,
                    "letter": "W" if pts == 3 else ("D" if pts == 1 else "L"),
                }
            )
            for ev in rec.get("events") or []:
                kind = ev.get("kind")
                team = ev.get("team")
                if team == side:
                    if kind in {"shot", "shot_on", "blocked", "goal"}:
                        row["shots"] += 1
                    if kind in {"shot_on", "goal"}:
                        row["sot"] += 1
                    if kind == "goal":
                        row["goals"] += 1
                    if kind == "corner":
                        row["corners"] += 1
                    xg = ev.get("xg")
                    if xg and kind != "own_goal" and has_xg:
                        row["xg"] += float(xg)
                elif team == other:
                    if kind in {"goal", "own_goal"}:
                        row["conceded"] += 1
                    if kind in {"shot", "shot_on", "blocked", "goal"}:
                        row["shots_against"] += 1
                    if kind in {"shot_on", "goal"}:
                        row["sot_against"] += 1
                    xg = ev.get("xg")
                    if xg and kind != "own_goal" and has_xg:
                        row["xg_against"] += float(xg)
    return leagues


GOAL_BUCKETS = ("0", "1", "2", "3+")


def _goal_dist(totals: list[int]) -> dict[str, Any]:
    """How often a half produced 0 / 1 / 2 / 3+ goals: ``counts`` follows ``GOAL_BUCKETS``."""
    counts = [0, 0, 0, 0]
    for n in totals:
        counts[min(n, 3)] += 1
    return {"n": len(totals), "counts": counts}


def half_goals(results: list[dict[str, Any]]) -> dict[str, Any]:
    """First-half total goals, and second-half totals keyed by the half-time score.

    ``results`` are one side's games (``ht_gf``/``ht_ga`` from that side's view), so
    ``second_by_ht["0-1"]`` is what happened after the club trailed 0-1 at the break.
    """
    known = [g for g in results if g.get("ht_gf") is not None and g.get("ht_ga") is not None]

    def _split(pairs: list[tuple[int, int]]) -> dict[str, Any]:
        """(scored, allowed) per game → total / scored / allowed bucket splits."""
        return {
            "total": _goal_dist([s + a for s, a in pairs]),
            "scored": _goal_dist([s for s, _ in pairs]),
            "allowed": _goal_dist([a for _, a in pairs]),
        }

    first = [(g["ht_gf"], g["ht_ga"]) for g in known]
    by_ht: dict[str, list[tuple[int, int]]] = {}
    for g in known:
        second = (max(0, g["gf"] - g["ht_gf"]), max(0, g["ga"] - g["ht_ga"]))
        by_ht.setdefault(f'{g["ht_gf"]}-{g["ht_ga"]}', []).append(second)
    return {"first": _split(first), "second_by_ht": {k: _split(v) for k, v in sorted(by_ht.items())}}


def _league_table(slug: str, teams: dict[str, dict[str, Any]], label: str) -> dict[str, Any]:
    rows = list(teams.values())
    xg_games = sum(r["games_xg"] for r in rows)
    basis = "xg" if xg_games and xg_games >= 0.5 * sum(r["games"] for r in rows) else "sot"
    tot_goals = sum(r["goals"] for r in rows)
    tot_xg = sum(r["xg"] for r in rows)
    tot_sot = sum(r["sot"] for r in rows)
    par_ratio = (tot_goals / tot_xg) if (basis == "xg" and tot_xg > 0) else 1.0
    par_conv = (tot_goals / tot_sot) if tot_sot else 0.3
    # League-typical chances allowed per game (every game is counted once per side).
    tot_games = sum(r["games"] for r in rows)
    par_xga = (sum(r["xg_against"] for r in rows) / xg_games) if xg_games else 0.0
    par_sota = (sum(r["sot_against"] for r in rows) / tot_games) if tot_games else 0.0
    # League-typical attacking output per team-game, for the offence composite.
    par_rates = {
        "xg": (tot_xg / xg_games) if xg_games else 0.0,
        "shots": (sum(r["shots"] for r in rows) / tot_games) if tot_games else 0.0,
        "sot": (tot_sot / tot_games) if tot_games else 0.0,
        "corners": (sum(r["corners"] for r in rows) / tot_games) if tot_games else 0.0,
        "goals": (tot_goals / tot_games) if tot_games else 0.0,
    }
    weights = OFFENSE_WEIGHTS_XG if basis == "xg" else OFFENSE_WEIGHTS_SOT
    # League points per team-game (≈1.37 with a typical draw rate) anchors momentum's par.
    par_ppg = (sum(r["points"] for r in rows) / tot_games) if tot_games else 1.0
    par_scored = (sum(r["scored"] for r in rows) / tot_games) if tot_games else 0.0
    # Share of team-games that ended without conceding, league-wide.
    par_cs_pct = (100.0 * sum(1 for r in rows for g in r["results"] if g["ga"] == 0) / tot_games) if tot_games else 0.0

    def _shrunk_rate(total: float, games: float, par: float) -> float:
        return (total + PRIOR_GAMES * par) / (games + PRIOR_GAMES)

    def _momentum(r: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
        recent = sorted(r["results"], key=lambda g: g["start"])[-FORM_GAMES:]
        if not recent or par_ppg <= 0:
            return 100.0, recent
        # Linear recency weights (oldest 1 … newest n), scaled so they sum to n games'
        # worth of evidence before the prior pulls thin samples toward par.
        raw = [float(i) for i in range(1, len(recent) + 1)]
        scale = len(recent) / sum(raw)
        wpts = sum(w * scale * g["points"] for w, g in zip(raw, recent))
        ppg = _shrunk_rate(wpts, len(recent), par_ppg)
        return 100.0 * ppg / par_ppg, recent

    def _potential(r: dict[str, Any], defense: float) -> tuple[float, float]:
        """(underlying strength from chance quality, the same index on actual goals)."""
        if basis == "xg" and par_rates["xg"] > 0:
            creation = _shrunk_rate(r["xg"], r["games_xg"], par_rates["xg"]) / par_rates["xg"]
        elif par_rates["sot"] > 0:
            creation = _shrunk_rate(r["sot"], r["games"], par_rates["sot"]) / par_rates["sot"]
        else:
            creation = 1.0
        potential = 100.0 * (creation * defense / 100.0) ** 0.5
        if par_scored > 0:
            gf_idx = _shrunk_rate(r["scored"], r["games"], par_scored) / par_scored
            ga_pg = _shrunk_rate(r["conceded"], r["games"], par_scored)
            ga_idx = par_scored / ga_pg if ga_pg > 0 else 1.0
            results = 100.0 * (gf_idx * ga_idx) ** 0.5
        else:
            results = 100.0
        return potential, results

    def _offense(r: dict[str, Any]) -> float:
        score = 0.0
        for key, weight in weights.items():
            par = par_rates[key]
            games = r["games_xg"] if key == "xg" else r["games"]
            if par <= 0:
                score += weight
                continue
            rate = (r[key] + PRIOR_GAMES * par) / (games + PRIOR_GAMES)
            score += weight * (rate / par)
        return 100.0 * score

    out_rows = []
    for r in rows:
        offense = _offense(r)
        if basis == "xg":
            # Goals per 100 xG; the prior adds two league-typical xG worth of chances.
            power = 100.0 * (r["goals"] + PRIOR_XG * par_ratio) / (r["xg"] + PRIOR_XG)
            # Chances allowed per game vs the league, shrunk over two par games.
            xga_pg = (r["xg_against"] + PRIOR_GAMES * par_xga) / (r["games_xg"] + PRIOR_GAMES)
            defense = 100.0 * par_xga / xga_pg if xga_pg > 0 else 100.0
        else:
            conv = (r["goals"] + PRIOR_SOT * par_conv) / (r["sot"] + PRIOR_SOT) if r["sot"] + PRIOR_SOT > 0 else par_conv
            power = 100.0 * conv / par_conv if par_conv else 100.0
            sota_pg = (r["sot_against"] + PRIOR_GAMES * par_sota) / (r["games"] + PRIOR_GAMES)
            defense = 100.0 * par_sota / sota_pg if sota_pg > 0 else 100.0
        momentum, recent = _momentum(r)
        potential, results = _potential(r, defense)
        gap = potential - results
        potential_tag = "upside" if gap >= POTENTIAL_GAP else ("overachieving" if gap <= -POTENTIAL_GAP else "steady")
        clean_sheets = sum(1 for g in r["results"] if g["ga"] == 0)
        cs_pct = 100.0 * clean_sheets / r["games"] if r["games"] else 0.0
        row = {
            **{k: v for k, v in r.items() if k != "results"},
            # Only what the chiclet tooltip needs; the full log would triple the payload.
            "recent": [{"date": g["start"][:10], "opponent": g["opponent"], "venue": g["venue"], "gf": g["gf"], "ga": g["ga"], "letter": g["letter"]} for g in recent],
            "form": "".join(g["letter"] for g in recent),
            "recent_points": sum(g["points"] for g in recent),
            "recent_scored": sum(g["gf"] for g in recent),
            "recent_allowed": sum(g["ga"] for g in recent),
            "points_per_game": round(r["points"] / r["games"], 2) if r["games"] else 0.0,
            "scored_per_game": round(r["scored"] / r["games"], 2) if r["games"] else 0.0,
            "momentum": int(round(momentum)),
            "rising": momentum > 100.0,
            "potential": int(round(potential)),
            "results_power": int(round(results)),
            "potential_tag": potential_tag,
            "clean_sheets": clean_sheets,
            "clean_sheet_pct": int(round(cs_pct)),
            "recent_clean_sheets": sum(1 for g in recent if g["ga"] == 0),
            "tight": cs_pct > par_cs_pct,
            "half_goals": half_goals(r["results"]),
            "xg": round(r["xg"], 2),
            "xg_against": round(r["xg_against"], 2),
            "power": int(round(power)),
            "conversion_pct": int(round(100.0 * r["goals"] / r["sot"])) if r["sot"] else 0,
            "goals_per_game": round(r["goals"] / r["games"], 2) if r["games"] else 0.0,
            "xg_per_game": round(r["xg"] / r["games_xg"], 2) if r["games_xg"] else 0.0,
            "clinical": power > 100.0,
            "offense_power": int(round(offense)),
            "potent": offense > 100.0,
            "shots_per_game": round(r["shots"] / r["games"], 2) if r["games"] else 0.0,
            "sot_per_game": round(r["sot"] / r["games"], 2) if r["games"] else 0.0,
            "corners_per_game": round(r["corners"] / r["games"], 2) if r["games"] else 0.0,
            "defense_power": int(round(defense)),
            "solid": defense > 100.0,
            "conceded_per_game": round(r["conceded"] / r["games"], 2) if r["games"] else 0.0,
            "xga_per_game": round(r["xg_against"] / r["games_xg"], 2) if r["games_xg"] else 0.0,
            "sot_against_per_game": round(r["sot_against"] / r["games"], 2) if r["games"] else 0.0,
            "basis": basis,
            "ranked": r["games"] >= MIN_GAMES,
            "rank": None,
            "offense_rank": None,
            "defense_rank": None,
            "momentum_rank": None,
            "potential_rank": None,
            "clean_sheet_rank": None,
        }
        out_rows.append(row)

    ranked = [r for r in out_rows if r["ranked"]]
    ranked.sort(key=lambda r: (-r["power"], -r["goals"], r["team"]))
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    by_offense = sorted(ranked, key=lambda r: (-r["offense_power"], -r["xg"], -r["shots"], r["team"]))
    for i, r in enumerate(by_offense, start=1):
        r["offense_rank"] = i
    by_defense = sorted(ranked, key=lambda r: (-r["defense_power"], r["conceded"], r["team"]))
    for i, r in enumerate(by_defense, start=1):
        r["defense_rank"] = i
    by_momentum = sorted(ranked, key=lambda r: (-r["momentum"], -r["recent_points"], r["recent_allowed"] - r["recent_scored"], r["team"]))
    for i, r in enumerate(by_momentum, start=1):
        r["momentum_rank"] = i
    by_potential = sorted(ranked, key=lambda r: (-r["potential"], -r["xg"], -r["sot"], r["team"]))
    for i, r in enumerate(by_potential, start=1):
        r["potential_rank"] = i
    # Most clean sheets first; equal totals go to the club that needed fewer games for them.
    by_clean = sorted(ranked, key=lambda r: (-r["clean_sheets"], -r["clean_sheet_pct"], -r["recent_clean_sheets"], r["conceded"], r["team"]))
    for i, r in enumerate(by_clean, start=1):
        r["clean_sheet_rank"] = i
    out_rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["team"]))
    return {
        "slug": slug,
        "label": label,
        "basis": basis,
        "teams_ranked": len(ranked),
        "form_games": FORM_GAMES,
        # League-wide fallback for clubs with a thin sample: each game once, home-away view.
        "half_goals": half_goals([g for r in rows for g in r["results"] if g["venue"] == "home"]),
        "half_goals_min_sample": HALF_GOALS_MIN_SAMPLE,
        "goal_buckets": list(GOAL_BUCKETS),
        "par_clean_sheet_pct": int(round(par_cs_pct)),
        "par_points_per_game": round(par_ppg, 2),
        "par_goals_per_game": round(par_scored, 2),
        "par_conversion_pct": int(round(100 * par_conv)),
        "par_rates": {k: round(v, 2) for k, v in par_rates.items()},
        "offense_weights": weights,
        "par_xga_per_game": round(par_xga, 2),
        "par_sot_against_per_game": round(par_sota, 2),
        "teams": out_rows,
    }


def build_clinical_board(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    records = load_records() if records is None else records
    labels = dict(LEAGUES)
    totals = _team_totals(records)
    leagues = {
        slug: _league_table(slug, teams, labels.get(slug, next(iter(teams.values()))["league_chiclet"] or slug))
        for slug, teams in totals.items()
    }
    return {
        "archive_total": len(records),
        "min_games": MIN_GAMES,
        "leagues": leagues,
        "fetched_at": time.time(),
    }


def clinical_board(*, use_cache: bool = True) -> dict[str, Any]:
    """Cached board; recomputed when the archive grows or the TTL lapses."""
    global _cache
    records = load_records()
    with _lock:
        if use_cache and _cache is not None:
            ts, n, payload = _cache
            if n == len(records) and time.time() - ts < _CACHE_TTL_S:
                return payload
    payload = build_clinical_board(records)
    with _lock:
        _cache = (time.time(), len(records), payload)
    return payload


def clear_clinical_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def lookup_team(board: dict[str, Any], league_slug: str, *, team_id: str = "", name: str = "") -> dict[str, Any] | None:
    league = (board.get("leagues") or {}).get(league_slug)
    if not league:
        return None
    key = team_key(name)
    for row in league.get("teams") or []:
        if team_id and row.get("team_id") == str(team_id):
            return row
    for row in league.get("teams") or []:
        if key and row.get("key") == key:
            return row
    return None
