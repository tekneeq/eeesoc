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

Defence power is the mirror image: how little chance quality the club
allows.  ``defense_power`` is league-average xG conceded per game over the
club's own xG conceded per game (shrunk toward par), so 100 = allows exactly
the league's typical chances, 125 = allows a fifth less.  ``solid`` is True
above 100, otherwise the defence is leaky.  Leagues without xG use shots on
target conceded per game instead.
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
# Prior weight in games for the per-game defensive rates.
PRIOR_GAMES = 2.0
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
                    "conceded": 0,
                    "shots_against": 0,
                    "sot_against": 0,
                    "xg_against": 0.0,
                },
            )
            if not row["team"] and name:
                row["team"] = name
            row["games"] += 1
            if has_xg:
                row["games_xg"] += 1
            other = "away" if side == "home" else "home"
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

    out_rows = []
    for r in rows:
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
        row = {
            **r,
            "xg": round(r["xg"], 2),
            "xg_against": round(r["xg_against"], 2),
            "power": int(round(power)),
            "conversion_pct": int(round(100.0 * r["goals"] / r["sot"])) if r["sot"] else 0,
            "goals_per_game": round(r["goals"] / r["games"], 2) if r["games"] else 0.0,
            "xg_per_game": round(r["xg"] / r["games_xg"], 2) if r["games_xg"] else 0.0,
            "clinical": power > 100.0,
            "defense_power": int(round(defense)),
            "solid": defense > 100.0,
            "conceded_per_game": round(r["conceded"] / r["games"], 2) if r["games"] else 0.0,
            "xga_per_game": round(r["xg_against"] / r["games_xg"], 2) if r["games_xg"] else 0.0,
            "sot_against_per_game": round(r["sot_against"] / r["games"], 2) if r["games"] else 0.0,
            "basis": basis,
            "ranked": r["games"] >= MIN_GAMES,
            "rank": None,
            "defense_rank": None,
        }
        out_rows.append(row)

    ranked = [r for r in out_rows if r["ranked"]]
    ranked.sort(key=lambda r: (-r["power"], -r["goals"], r["team"]))
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    by_defense = sorted(ranked, key=lambda r: (-r["defense_power"], r["conceded"], r["team"]))
    for i, r in enumerate(by_defense, start=1):
        r["defense_rank"] = i
    out_rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["team"]))
    return {
        "slug": slug,
        "label": label,
        "basis": basis,
        "teams_ranked": len(ranked),
        "par_conversion_pct": int(round(100 * par_conv)),
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
