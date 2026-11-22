"""
Live grade: how a side is playing against its own usual, minute for minute.

The archive keeps every finished game at minute level, so "usual" is not a
per-game average scaled by the clock — it is what this club had actually put
up by the same minute of its past games. A side that normally has two chances
by 20' and has none grades low; the same blank at 5' barely moves it.

Five reads go in, each scored 0–100 as a percentile of what the club usually
has by now (50 = exactly its usual), then weighted into one number:

* goals scored, chances (shots incl. blocked), xG, touches in the other box
  — counts, scored against a count distribution around the club's usual;
* possession — share of on-ball plays so far against its usual share.

Clubs with few archived games are pulled toward the league's average, so the
grade reads "against the league" until the club has a history of its own.
"""

from __future__ import annotations

import math
import threading
import time
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from itertools import accumulate
from typing import Any

from eeesoc.halftime import (
    MIN_EVENTS_FOR_TRUST,
    _record_has_xg,
    has_current_box,
    has_touch_counts,
    load_records,
)

GRADE_VERSION = 1
MAX_MINUTE = 90
# Before this the counts are too small to say anything about either side.
MIN_MINUTE = 5
# Most recent games that make up a club's "usual".
HISTORY_GAMES = 30
# League-average games mixed into every club's usual, so one odd game is not a baseline.
PRIOR_GAMES = 2.0
# A league needs this many archived side-games before its own average is the prior.
LEAGUE_MIN_SIDES = 8
# Possession needs a real sample of on-ball plays before a share means anything.
MIN_TOUCHES = 20
# A finished game with fewer on-ball plays than this had a goals-only feed: it
# would read as a club that never shoots, so it is no part of anyone's usual.
THIN_FEED_TOUCHES = 100

WEIGHTS = {"goals": 0.20, "chances": 0.20, "xg": 0.20, "box": 0.25, "possession": 0.15}
LABELS = {"goals": "Goals", "chances": "Chances", "xg": "xG", "box": "Box", "possession": "Poss"}
# Variance over mean of each count around a club's own usual. Goals are close
# to Poisson; chances and box touches come in bursts, so the same shortfall is
# weaker evidence there.
DISPERSION = {"goals": 1.1, "chances": 2.0, "box": 3.5}
# Variance of an xG total per unit of expected xG (a few big chances carry it).
XG_VARIANCE = 0.5
# Game-to-game spread of a possession share, in points, once the sample is full.
POSSESSION_SD = 8.0
POSSESSION_FULL_MINUTE = 30

LETTERS = ((75, "A"), (60, "B"), (40, "C"), (25, "D"), (0, "F"))

_CACHE_TTL_S = 120.0
_lock = threading.Lock()
_cache: tuple[float, "GradeIndex"] | None = None


@dataclass(slots=True)
class SideGame:
    """One club's side of one game, kept as minute lists so any cut is a bisect."""

    event_id: str
    league: str
    start: str
    goals: list[int]
    chances: list[int]
    xg_minutes: list[int] | None
    xg_cumulative: list[float] | None
    box: list[int] | None
    touch_own: array | None
    touch_all: array | None

    def at(self, metric: str, minute: int) -> float | None:
        """This side's value for ``metric`` through ``minute``; None when the game did not measure it."""
        m = max(0, min(MAX_MINUTE, int(minute)))
        if metric == "goals":
            return float(bisect_right(self.goals, m))
        if metric == "chances":
            return float(bisect_right(self.chances, m))
        if metric == "box":
            return None if self.box is None else float(bisect_right(self.box, m))
        if metric == "xg":
            if self.xg_minutes is None or self.xg_cumulative is None:
                return None
            i = bisect_right(self.xg_minutes, m)
            return self.xg_cumulative[i - 1] if i else 0.0
        if metric == "possession":
            if self.touch_own is None or self.touch_all is None or self.touch_all[m] < MIN_TOUCHES:
                return None
            return 100.0 * self.touch_own[m] / self.touch_all[m]
        return None


def _minute(value: Any) -> int:
    try:
        return max(0, min(MAX_MINUTE, int(value)))
    except (TypeError, ValueError):
        return 0


def side_game(rec: dict[str, Any], side: str, *, has_xg: bool | None = None) -> SideGame:
    """Read one side of an archive record (or a live timeline, which has the same shape)."""
    other = "away" if side == "home" else "home"
    goals: list[int] = []
    chances: list[int] = []
    xg_points: list[tuple[int, float]] = []
    for ev in rec.get("events") or []:
        if ev.get("team") != side:
            continue
        kind = ev.get("kind")
        minute = _minute(ev.get("minute"))
        if kind in {"goal", "own_goal"}:
            goals.append(minute)
        if kind in {"shot", "shot_on", "blocked", "goal"}:
            chances.append(minute)
        xg = ev.get("xg")
        if xg and kind != "own_goal":
            xg_points.append((minute, float(xg)))
    goals.sort()
    chances.sort()
    xg_points.sort()

    with_xg = _record_has_xg(rec) if has_xg is None else has_xg
    xg_minutes = [m for m, _ in xg_points] if with_xg else None
    xg_cumulative = [round(v, 4) for v in accumulate(x for _, x in xg_points)] if with_xg else None

    box: list[int] | None = None
    entries = rec.get("box_entries")
    if isinstance(entries, dict) and has_current_box(rec):
        box = sorted(
            _minute(m)
            for half in ("1", "2")
            for m in ((entries.get(half) or {}).get(side) or [])
        )

    touch_own = touch_all = None
    if has_touch_counts(rec):
        counts = rec["touch_counts"]
        own = [int(n or 0) for n in counts[side]][: MAX_MINUTE + 1]
        opp = [int(n or 0) for n in counts[other]][: MAX_MINUTE + 1]
        own += [0] * (MAX_MINUTE + 1 - len(own))
        opp += [0] * (MAX_MINUTE + 1 - len(opp))
        touch_own = array("H", (min(n, 65535) for n in accumulate(own)))
        touch_all = array("H", (min(n, 65535) for n in accumulate(a + b for a, b in zip(own, opp))))

    return SideGame(
        event_id=str(rec.get("event_id") or ""),
        league=str(rec.get("league_slug") or ""),
        start=str(rec.get("start") or ""),
        goals=goals,
        chances=chances,
        xg_minutes=xg_minutes,
        xg_cumulative=xg_cumulative,
        box=box,
        touch_own=touch_own,
        touch_all=touch_all,
    )


def _name_key(name: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in str(name or "").lower()).split())


class GradeIndex:
    """Every archived side-game, by club and by league, with league averages memoised per minute."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.by_id: dict[str, list[SideGame]] = {}
        self.by_name: dict[tuple[str, str], list[SideGame]] = {}
        self.by_league: dict[str, list[SideGame]] = {}
        self.everything: list[SideGame] = []
        self._means: dict[tuple[str, str, int], tuple[float, int] | None] = {}
        for rec in records:
            if len(rec.get("events") or []) < MIN_EVENTS_FOR_TRUST:
                continue
            league = str(rec.get("league_slug") or "")
            for side in ("home", "away"):
                game = side_game(rec, side)
                if game.touch_all is not None and game.touch_all[MAX_MINUTE] < THIN_FEED_TOUCHES:
                    continue
                team_id = str(rec.get(f"{side}_id") or "")
                if team_id:
                    self.by_id.setdefault(team_id, []).append(game)
                key = _name_key(rec.get(side) or "")
                if key:
                    self.by_name.setdefault((league, key), []).append(game)
                self.by_league.setdefault(league, []).append(game)
                self.everything.append(game)

    def history(self, *, league: str, team_id: str, name: str, event_id: str, start: str) -> list[SideGame]:
        """A club's most recent games before this one, across every competition it is archived in."""
        pool = self.by_id.get(str(team_id or "")) if team_id else None
        if not pool:
            pool = self.by_name.get((league, _name_key(name)), [])
        games = [
            g
            for g in pool
            if g.event_id != str(event_id or "") and not (start and g.start and g.start >= start)
        ]
        games.sort(key=lambda g: g.start, reverse=True)
        return games[:HISTORY_GAMES]

    def league_mean(self, league: str, metric: str, minute: int) -> tuple[float, int] | None:
        """Average of ``metric`` through ``minute`` over the league (or the whole archive when the league is thin)."""
        key = (league, metric, minute)
        if key not in self._means:
            values = _values(self.by_league.get(league, []), metric, minute)
            if len(values) < LEAGUE_MIN_SIDES:
                values = _values(self.everything, metric, minute)
            self._means[key] = (sum(values) / len(values), len(values)) if values else None
        return self._means[key]


def _values(games: list[SideGame], metric: str, minute: int) -> list[float]:
    return [v for g in games if (v := g.at(metric, minute)) is not None]


def _mean_at(games: list[SideGame], metric: str, minute: int, *, mid: bool) -> tuple[float, int] | None:
    """
    Average through ``minute``. A live clock at minute N is partway through
    it, while a finished game's minute N is whole — ``mid`` splits the
    difference with the minute before.
    """
    now = _values(games, metric, minute)
    if not now:
        return None
    value = sum(now) / len(now)
    if mid and minute > 0 and metric != "possession":
        before = _values(games, metric, minute - 1)
        if before:
            value = (value + sum(before) / len(before)) / 2.0
    return value, len(now)


def usual_at(
    index: GradeIndex,
    games: list[SideGame],
    *,
    league: str,
    metric: str,
    minute: int,
    mid: bool = False,
) -> tuple[float, int] | None:
    """
    What this club usually has by ``minute`` and how many of its own games say so.

    The club's own average is mixed with ``PRIOR_GAMES`` games of the league
    average, so a single archived game does not become the yardstick. With no
    games of its own the club is measured against the league alone.
    """
    own = _mean_at(games, metric, minute, mid=mid)
    prior = index.league_mean(league, metric, minute)
    if prior is not None and mid and minute > 0 and metric != "possession":
        before = index.league_mean(league, metric, minute - 1)
        if before is not None:
            prior = ((prior[0] + before[0]) / 2.0, prior[1])
    if metric == "possession" and prior is not None:
        # Both sides of every game are in the pool, so the league share is 50 by construction.
        prior = (50.0, prior[1])
    if own is None and prior is None:
        return None
    if own is None:
        return prior[0], 0  # type: ignore[index]
    value, n = own
    if prior is None:
        return value, n
    return (n * value + PRIOR_GAMES * prior[0]) / (n + PRIOR_GAMES), n


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def count_percentile(actual: float, usual: float, dispersion: float = 1.0) -> float:
    """
    Where ``actual`` sits among the counts a side with mean ``usual`` produces.

    Mid-percentile of a Poisson (``dispersion`` ≤ 1) or a negative binomial
    with variance ``dispersion × usual``: half the weight of the observed
    count goes below it, so a side exactly at its usual lands near 0.5.
    """
    k = max(0, int(round(actual)))
    if usual <= 1e-9:
        return 0.5 if k == 0 else 1.0
    if dispersion <= 1.0 + 1e-9:
        p = math.exp(-usual)
        step = lambda i: usual / (i + 1)  # noqa: E731
    else:
        keep = 1.0 / dispersion
        r = usual / (dispersion - 1.0)
        p = keep**r
        step = lambda i: (i + r) / (i + 1) * (1.0 - keep)  # noqa: E731
    below = 0.0
    for i in range(k):
        below += p
        p *= step(i)
    return max(0.0, min(1.0, below + 0.5 * p))


def metric_percentile(metric: str, actual: float, usual: float, minute: int) -> float:
    if metric == "xg":
        sd = math.sqrt(XG_VARIANCE * max(usual, 0.0) + 0.01)
        return _normal_cdf((actual - usual) / sd)
    if metric == "possession":
        sd = POSSESSION_SD * math.sqrt(max(1.0, POSSESSION_FULL_MINUTE / max(1, minute)))
        return _normal_cdf((actual - usual) / sd)
    return count_percentile(actual, usual, DISPERSION.get(metric, 1.0))


def letter_for(score: float) -> str:
    return next(letter for floor, letter in LETTERS if score >= floor)


def _round(metric: str, value: float) -> float:
    if metric == "xg":
        return round(value, 2)
    if metric == "possession":
        return round(value)
    return round(value, 1)


def grade_side(
    index: GradeIndex,
    live: SideGame,
    games: list[SideGame],
    *,
    league: str,
    minute: int,
    mid: bool = False,
) -> dict[str, Any] | None:
    """One side's grade at ``minute``: the weighted score, its letter and every read behind it."""
    parts: list[dict[str, Any]] = []
    for metric, weight in WEIGHTS.items():
        actual = live.at(metric, MAX_MINUTE)
        base = usual_at(index, games, league=league, metric=metric, minute=minute, mid=mid)
        if actual is None or base is None:
            continue
        usual, n = base
        pct = metric_percentile(metric, actual, usual, minute)
        parts.append(
            {
                "key": metric,
                "label": LABELS[metric],
                "actual": _round(metric, actual),
                "usual": _round(metric, usual),
                "score": round(100 * pct),
                "weight": weight,
                "games": n,
            }
        )
    total = sum(p["weight"] for p in parts)
    if not parts or total <= 0:
        return None
    score = round(sum(p["score"] * p["weight"] for p in parts) / total)
    return {
        "score": score,
        "letter": letter_for(score),
        "games": len(games),
        "basis": "club" if games else "league",
        "parts": parts,
    }


def _live_has_xg(tl: dict[str, Any]) -> bool:
    """
    A live game with no shots yet has no xG either, and that zero is real — so
    xG is trusted unless shots have landed and none of them carries a value.
    """
    shots = [ev for ev in tl.get("events") or [] if ev.get("kind") in {"shot", "shot_on", "blocked", "goal"}]
    return len(shots) < 3 or any(ev.get("xg") for ev in shots)


def grade_timeline(
    tl: dict[str, Any],
    *,
    home_id: str = "",
    away_id: str = "",
    start: str = "",
    index: GradeIndex | None = None,
) -> dict[str, Any] | None:
    """Both sides' grades for a live (or just finished) timeline; None before there is anything to grade."""
    if not tl:
        return None
    final = bool(tl.get("final"))
    minute = MAX_MINUTE if final else _minute(tl.get("minute"))
    if minute < MIN_MINUTE:
        return None
    if has_touch_counts(tl):
        seen = sum(int(n or 0) for side in ("home", "away") for n in tl["touch_counts"][side])
        if seen < (THIN_FEED_TOUCHES if final else MIN_TOUCHES):
            # No play-by-play behind this game (goals-only feed): nothing to grade.
            return None
    elif not tl.get("events"):
        return None
    index = index or grade_index()
    league = str(tl.get("league_slug") or "")
    event_id = str(tl.get("event_id") or "")
    live_xg = _live_has_xg(tl)
    # Between the whistles the clock stands still, so the minute is whole.
    mid = not final and not tl.get("frozen")
    out: dict[str, Any] = {"version": GRADE_VERSION, "minute": minute, "final": final}
    for side, team_id in (("home", home_id), ("away", away_id)):
        games = index.history(
            league=league, team_id=str(team_id or ""), name=str(tl.get(side) or ""), event_id=event_id, start=start
        )
        out[side] = grade_side(
            index, side_game(tl, side, has_xg=live_xg), games, league=league, minute=minute, mid=mid
        )
    return out if out.get("home") or out.get("away") else None


def stamp_grade(tl: dict[str, Any], *, home_id: str = "", away_id: str = "", start: str = "") -> dict[str, Any]:
    """Attach the grade block onto a timeline payload (never fails the timeline)."""
    try:
        tl["grade"] = grade_timeline(tl, home_id=home_id, away_id=away_id, start=start)
    except Exception:  # noqa: BLE001 — a grade must never take the chiclet down
        tl["grade"] = None
    return tl


def grade_index(*, use_cache: bool = True) -> GradeIndex:
    """Cached index over the archive; rebuilt every couple of minutes as games finish."""
    global _cache
    with _lock:
        if use_cache and _cache is not None and time.time() - _cache[0] < _CACHE_TTL_S:
            return _cache[1]
    index = GradeIndex(load_records())
    with _lock:
        _cache = (time.time(), index)
    return index


def clear_grade_cache() -> None:
    global _cache
    with _lock:
        _cache = None
