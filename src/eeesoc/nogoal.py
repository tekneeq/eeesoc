"""
No-more-goals-this-half: how likely is it that nobody scores again before the break
(or the final whistle), given the minute, the league and the score?

Everything is fitted from the finished-match archive (``eeesoc.halftime``), and refits
as the archive grows.  What the archive says — and this drives the design:

* **Time remaining dominates.**  The empirical goal hazard by minute (including the
  goals ESPN files at 45' / 90' for stoppage time) gives the expected number of
  goals still to come in the half, ``lambda``; ``P(no goal) = exp(-lambda)``.
* **League scoring rate matters** — a fifth less in Liga Profesional than the
  Bundesliga — so the hazard is scaled by the league's goals per game, shrunk
  toward the global rate over ``LEAGUE_PRIOR_GAMES``.
* **0-0 games stay quieter**: a fitted multiplier below 1 while the game is
  goalless, above 1 once it is not (fitted per half).
* **Penalty-box entries are the edge.**  Working backwards from every half that
  finished 0-0: they had the same passes, final-third passes, corners, fouls and
  cards as the rest — but fewer completed passes *into the box*.  Two factors are
  fitted from that, both multiplicative on the hazard: the fixture's *box habit*
  (how often these two clubs' halves usually reach the box, leave-one-out) and the
  *box entries so far* this half against the archive rate for the minute.
* **What does *not* help** (checked by log loss on the archive and left out):
  how many shots or how much xG the half has produced so far, activity in the
  last ten minutes, and club goal rates from a handful of games.  Shots are a noisy
  by-product of box entries; once entries are in, they add nothing out of sample.

``backtest`` replays the archive minute by minute to report calibration (does 70%
mean 70%?) and the trigger policy: for each confidence threshold, how many games
would have fired inside the betting window, at what average minute, and how often
the half really stayed goalless.  ``1 / p`` is the decimal odds you need to break even.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Iterable

from eeesoc.halftime import load_records

GOAL_KINDS = {"goal", "own_goal"}
HALF_END = {1: 45, 2: 90}
HALF_START = {1: 0, 2: 45}
# Minutes used to fit the state multipliers and to score the backtest.
FIT_MINUTES = {1: range(8, 41), 2: range(48, 86)}
# Games of global-average scoring blended into a league's own rate.
LEAGUE_PRIOR_GAMES = 20
# Games of league-average box entries blended into a club's own rate.
BOX_PRIOR_GAMES = 4
# Records with box entries needed before the box factors are fitted at all.
MIN_BOX_RECORDS = 40
HABIT_LABELS = ("quiet", "normal", "busy")
INPLAY_LABELS = ("low", "normal", "high")
# Cuts reported in the backtest's box-edge tables (1st half).
EDGE_MINUTES = (15, 25)
# Trigger policy defaults: fire once per half when P(no goal) clears the threshold
# inside the window — after 35' / 78' the price is too short to be worth it.
DEFAULT_THRESHOLD = 0.65
DEFAULT_WINDOWS = {1: (10, 35), 2: (50, 78)}
BACKTEST_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
_CACHE_TTL_S = 600.0

_lock = threading.Lock()
_cache: tuple[float, tuple[int, int], dict[str, Any], dict[str, Any] | None] | None = None


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def event_period(ev: dict[str, Any]) -> int | None:
    period = ev.get("period")
    if period is None:
        minute = ev.get("minute")
        if minute is None:
            return None
        return 1 if int(minute) <= HALF_END[1] else 2
    try:
        return int(period)
    except (TypeError, ValueError):
        return None


def score_at(events: Iterable[dict[str, Any]], period: int, minute: int) -> tuple[int, int]:
    """(home, away) goals scored in earlier halves plus this half up to and including ``minute``."""
    home = away = 0
    for ev in events:
        if ev.get("kind") not in GOAL_KINDS:
            continue
        p = event_period(ev)
        if p is None or p > period:
            continue
        if p == period and int(ev.get("minute") or 0) > minute:
            continue
        if ev.get("team") == "home":
            home += 1
        elif ev.get("team") == "away":
            away += 1
    return home, away


def goals_after(events: Iterable[dict[str, Any]], period: int, minute: int) -> int:
    """Goals in ``period`` filed after ``minute`` (stoppage-time goals count)."""
    return sum(
        1
        for ev in events
        if ev.get("kind") in GOAL_KINDS and event_period(ev) == period and int(ev.get("minute") or 0) > minute
    )


def box_minutes(source: dict[str, Any] | None, period: int) -> dict[str, list[int]]:
    """``{"home": [minutes], "away": [minutes]}`` of box entries in ``period`` from a record or timeline."""
    box = (source or {}).get("box_entries") or {}
    half = box.get(str(period)) or box.get(period) or {}
    return {
        "home": [int(m) for m in (half.get("home") or [])],
        "away": [int(m) for m in (half.get("away") or [])],
    }


def box_so_far(source: dict[str, Any] | None, period: int, minute: int) -> tuple[int, int]:
    """(home, away) box entries in ``period`` up to and including ``minute``."""
    half = box_minutes(source, period)
    return (
        sum(1 for m in half["home"] if m <= minute),
        sum(1 for m in half["away"] if m <= minute),
    )


def _bucket3(value: float | None, cuts: list[float] | tuple[float, float] | None, labels: tuple[str, str, str]) -> str | None:
    if value is None or not cuts:
        return None
    lo, hi = float(cuts[0]), float(cuts[1])
    if value <= lo:
        return labels[0]
    if value > hi:
        return labels[2]
    return labels[1]


def _tercile_cuts(values: list[float]) -> list[float]:
    vals = sorted(values)
    if len(vals) < 3:
        return [0.0, 0.0]
    return [vals[len(vals) // 3], vals[(2 * len(vals)) // 3]]


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def _league_factor(goals: float, games: int, global_gpg: float) -> float:
    if global_gpg <= 0:
        return 1.0
    return ((goals + LEAGUE_PRIOR_GAMES * global_gpg) / (games + LEAGUE_PRIOR_GAMES)) / global_gpg


def build_model(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    records = load_records() if records is None else records
    n = len(records)
    hazard = {1: [0.0] * (HALF_END[1] + 1), 2: [0.0] * (HALF_END[2] + 1)}
    leagues: dict[str, dict[str, float]] = {}
    total_goals = 0
    for rec in records:
        slug = str(rec.get("league_slug") or "")
        row = leagues.setdefault(slug, {"games": 0, "goals": 0})
        row["games"] += 1
        ft = int(rec.get("ft_home") or 0) + int(rec.get("ft_away") or 0)
        row["goals"] += ft
        total_goals += ft
        for ev in rec.get("events") or []:
            if ev.get("kind") not in GOAL_KINDS:
                continue
            p = event_period(ev)
            if p not in hazard:
                continue
            minute = max(HALF_START[p] + 1, min(HALF_END[p], int(ev.get("minute") or 0)))
            hazard[p][minute] += 1.0 / n if n else 0.0
    gpg = total_goals / n if n else 0.0
    for row in leagues.values():
        row["factor"] = round(_league_factor(row["goals"], int(row["games"]), gpg), 3)
        row["goals_per_game"] = round(row["goals"] / row["games"], 2) if row["games"] else 0.0

    model: dict[str, Any] = {
        "archive_total": n,
        "goals_per_game": round(gpg, 3),
        "hazard": hazard,
        "leagues": leagues,
        "state_mult": {1: {"zero": 1.0, "scoring": 1.0}, 2: {"zero": 1.0, "scoring": 1.0}},
        "stoppage_share": {
            p: round(hazard[p][HALF_END[p]] / sum(hazard[p]), 3) if sum(hazard[p]) else 0.0 for p in hazard
        },
        "fitted_at": time.time(),
    }
    # Fit the 0-0 / scoring multipliers: observed goals after t over the base expectation.
    for p, minutes in FIT_MINUTES.items():
        acc = {"zero": [0.0, 0.0], "scoring": [0.0, 0.0]}
        for rec in records:
            events = rec.get("events") or []
            for t in minutes:
                h, a = score_at(events, p, t)
                key = "zero" if h + a == 0 else "scoring"
                acc[key][0] += goals_after(events, p, t)
                acc[key][1] += _base_lambda(model, p, t)
        for key, (obs, exp) in acc.items():
            model["state_mult"][p][key] = round(obs / exp, 3) if exp > 0 else 1.0
    model["box"] = _fit_box(model, records)
    return model


def _empty_box() -> dict[str, Any]:
    return {
        "records": 0,
        "per_half": {1: 0.0, 2: 0.0},
        "teams": {},
        "leagues": {},
        # Tercile boundaries — labels only (quiet/normal/busy, low/normal/high).
        "habit_cuts": {1: [0.0, 0.0], 2: [0.0, 0.0]},
        "inplay_cuts": {1: [0.0, 0.0], 2: [0.0, 0.0]},
        # Exponents: hazard × rel**beta.
        "habit_beta": {1: 0.0, 2: 0.0},
        "inplay_beta": {1: 0.0, 2: 0.0},
        # Multipliers the exponents imply at each tercile's midpoint (for display).
        "habit_mult": {p: {k: 1.0 for k in HABIT_LABELS} for p in (1, 2)},
        "inplay_mult": {p: {k: 1.0 for k in INPLAY_LABELS} for p in (1, 2)},
    }


def _inplay_rel(box: dict[str, Any], period: int, minute: int, box_total: int) -> float | None:
    """Entries so far vs the archive rate for the minutes elapsed, +1 smoothed so an empty box is finite."""
    per_half = float((box.get("per_half") or {}).get(period) or 0.0)
    elapsed = int(minute) - HALF_START[period]
    if per_half <= 0 or elapsed < 1:
        return None
    expected = per_half / (HALF_END[period] - HALF_START[period]) * elapsed
    return (box_total + 1.0) / (expected + 1.0)


def _median(values: list[float]) -> float:
    vals = sorted(values)
    return vals[len(vals) // 2] if vals else 1.0


def _box_totals(rec: dict[str, Any], period: int) -> tuple[int, int]:
    half = box_minutes(rec, period)
    return len(half["home"]), len(half["away"])


def has_box_data(rec: dict[str, Any]) -> bool:
    """Box entries were kept *and* the feed carried pitch coordinates (a whole game with none is missing data)."""
    if not rec.get("box_entries"):
        return False
    return any(sum(_box_totals(rec, p)) for p in (1, 2))


def _fit_beta(samples: list[tuple[int, float, float]]) -> float:
    """
    Poisson fit of ``y ~ exp · r**beta`` over (y, exp, r) samples by grid search on beta.
    A single monotone exponent per factor keeps the multiplier smooth and hard to overfit.
    """
    if not samples:
        return 0.0
    best_b, best_ll = 0.0, -math.inf
    logs = [(y, e, math.log(r)) for y, e, r in samples if r > 0 and e > 0]
    for i in range(-150, 151):
        b = i / 100
        ll = sum(y * b * lr - e * math.exp(b * lr) for y, e, lr in logs)
        if ll > best_ll:
            best_ll, best_b = ll, b
    return best_b


def _power_factor(rel: float | None, beta: float) -> float:
    if rel is None or rel <= 0:
        return 1.0
    return max(0.5, min(1.8, rel**beta))


def _fit_box(model: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Sequentially fit the box-habit and box-so-far multipliers on top of clock × league × state."""
    box = _empty_box()
    recs = [r for r in records if has_box_data(r)]
    box["records"] = len(recs)
    if len(recs) < MIN_BOX_RECORDS:
        return box
    teams: dict[str, dict[str, Any]] = {}
    leagues: dict[str, dict[str, Any]] = {}
    totals = {1: 0.0, 2: 0.0}
    for rec in recs:
        slug = str(rec.get("league_slug") or "")
        lg = leagues.setdefault(slug, {"games": 0, "total": {1: 0.0, 2: 0.0}})
        lg["games"] += 1
        for p in (1, 2):
            h, a = _box_totals(rec, p)
            totals[p] += h + a
            lg["total"][p] += h + a
            for tid, for_, against in ((str(rec.get("home_id") or ""), h, a), (str(rec.get("away_id") or ""), a, h)):
                if not tid:
                    continue
                row = teams.setdefault(tid, {"games": 0, "for": {1: 0.0, 2: 0.0}, "against": {1: 0.0, 2: 0.0}})
                if p == 1:
                    row["games"] += 1
                row["for"][p] += for_
                row["against"][p] += against
    box["per_half"] = {p: totals[p] / len(recs) for p in (1, 2)}
    box["teams"] = teams
    box["leagues"] = leagues
    model["box"] = box

    # Habit: leave-one-out expected box entries for the fixture, relative to the archive half.
    habit_rel: dict[int, dict[str, float]] = {1: {}, 2: {}}
    for rec in recs:
        for p in (1, 2):
            exp = expected_box(model, p, str(rec.get("home_id") or ""), str(rec.get("away_id") or ""), str(rec.get("league_slug") or ""), exclude=rec)
            if exp is not None and box["per_half"][p] > 0:
                habit_rel[p][str(rec.get("event_id"))] = exp / box["per_half"][p]
    for p, minutes in FIT_MINUTES.items():
        rels = list(habit_rel[p].values())
        box["habit_cuts"][p] = _tercile_cuts(rels)
        samples: list[tuple[int, float, float]] = []
        for rec in recs:
            rel = habit_rel[p].get(str(rec.get("event_id")))
            if rel is None or rel <= 0:
                continue
            events = rec.get("events") or []
            lf = league_factor(model, str(rec.get("league_slug") or ""))
            for t in minutes:
                h, a = score_at(events, p, t)
                sf = float(model["state_mult"][p]["zero" if h + a == 0 else "scoring"])
                samples.append((goals_after(events, p, t), _base_lambda(model, p, t) * lf * sf, rel))
        beta = _fit_beta(samples)
        box["habit_beta"][p] = beta
        box["habit_mult"][p] = _tercile_multipliers(rels, box["habit_cuts"][p], beta, HABIT_LABELS)

    # In-play: box entries so far against the archive rate for the minutes elapsed.
    for p, minutes in FIT_MINUTES.items():
        samples = []
        rels = []
        for rec in recs:
            events = rec.get("events") or []
            lf = league_factor(model, str(rec.get("league_slug") or ""))
            hf = _power_factor(habit_rel[p].get(str(rec.get("event_id"))), box["habit_beta"][p])
            for t in minutes:
                h, a = box_so_far(rec, p, t)
                rel = _inplay_rel(box, p, t, h + a)
                if rel is None:
                    continue
                rels.append(rel)
                gh, ga = score_at(events, p, t)
                sf = float(model["state_mult"][p]["zero" if gh + ga == 0 else "scoring"])
                samples.append((goals_after(events, p, t), _base_lambda(model, p, t) * lf * sf * hf, rel))
        beta = _fit_beta(samples)
        box["inplay_cuts"][p] = _tercile_cuts(rels)
        box["inplay_beta"][p] = beta
        box["inplay_mult"][p] = _tercile_multipliers(rels, box["inplay_cuts"][p], beta, INPLAY_LABELS)
    return box


def _tercile_multipliers(rels: list[float], cuts: list[float], beta: float, labels: tuple[str, str, str]) -> dict[str, float]:
    """The multiplier at the median of each tercile — what the exponent means in plain numbers."""
    groups: dict[str, list[float]] = {k: [] for k in labels}
    for r in rels:
        b = _bucket3(r, cuts, labels)
        if b:
            groups[b].append(r)
    return {k: round(_power_factor(_median(v), beta), 3) if v else 1.0 for k, v in groups.items()}


def _team_box_rate(model: dict[str, Any], team_id: str, period: int, key: str, league_slug: str, *, exclude: dict[str, Any] | None) -> float | None:
    """A club's box entries per half for/against, shrunk toward its league's per-side mean."""
    box = model.get("box") or {}
    if not box.get("teams"):
        return None
    lg = (box.get("leagues") or {}).get(league_slug)
    lg_games = int(lg["games"]) if lg else 0
    lg_total = float(lg["total"][period]) if lg else 0.0
    row = box["teams"].get(team_id)
    games = int(row["games"]) if row else 0
    total = float(row[key][period]) if row else 0.0
    if exclude is not None:
        h, a = _box_totals(exclude, period)
        if str(exclude.get("league_slug") or "") == league_slug and lg:
            lg_games -= 1
            lg_total -= h + a
        if row and team_id in {str(exclude.get("home_id") or ""), str(exclude.get("away_id") or "")}:
            games -= 1
            is_home = team_id == str(exclude.get("home_id") or "")
            own, other = (h, a) if is_home else (a, h)
            total -= own if key == "for" else other
    # A league with no coordinate data yet falls back to the archive-wide rate.
    side_mean = lg_total / (2 * lg_games) if lg_games > 0 and lg_total > 0 else float(box["per_half"][period]) / 2
    if games <= 0 or total < 0:
        return side_mean
    return (total + BOX_PRIOR_GAMES * side_mean) / (games + BOX_PRIOR_GAMES)


def expected_box(
    model: dict[str, Any],
    period: int,
    home_id: str,
    away_id: str,
    league_slug: str,
    *,
    exclude: dict[str, Any] | None = None,
) -> float | None:
    """Expected box entries (both sides) in ``period`` for this fixture; None without box data."""
    box = model.get("box") or {}
    if not box.get("teams") or not (home_id or away_id):
        return None
    hf = _team_box_rate(model, home_id, period, "for", league_slug, exclude=exclude)
    ha = _team_box_rate(model, home_id, period, "against", league_slug, exclude=exclude)
    af = _team_box_rate(model, away_id, period, "for", league_slug, exclude=exclude)
    aa = _team_box_rate(model, away_id, period, "against", league_slug, exclude=exclude)
    if None in (hf, ha, af, aa):
        return None
    return (hf + aa) / 2 + (af + ha) / 2


def habit_factor(
    model: dict[str, Any],
    period: int,
    home_id: str,
    away_id: str,
    league_slug: str,
    *,
    exclude: dict[str, Any] | None = None,
) -> tuple[float, float | None, str | None, float | None]:
    """(multiplier, expected / archive half, bucket, expected box entries) for the fixture."""
    box = model.get("box") or {}
    exp = expected_box(model, period, home_id, away_id, league_slug, exclude=exclude)
    per_half = float((box.get("per_half") or {}).get(period) or 0.0)
    if exp is None or exp <= 0 or per_half <= 0:
        return 1.0, None, None, exp
    rel = exp / per_half
    bucket = _bucket3(rel, box["habit_cuts"][period], HABIT_LABELS)
    return _power_factor(rel, float(box["habit_beta"][period])), rel, bucket, exp


def inplay_factor(model: dict[str, Any], period: int, minute: int, box_total: int | None) -> tuple[float, float | None, str | None]:
    """(multiplier, (entries so far + 1) / (archive expectation for the minutes elapsed + 1), bucket)."""
    box = model.get("box") or {}
    if box_total is None or not box.get("teams"):
        return 1.0, None, None
    rel = _inplay_rel(box, period, minute, int(box_total))
    if rel is None:
        return 1.0, None, None
    bucket = _bucket3(rel, box["inplay_cuts"][period], INPLAY_LABELS)
    return _power_factor(rel, float(box["inplay_beta"][period])), rel, bucket


def _base_lambda(model: dict[str, Any], period: int, minute: int) -> float:
    """Expected goals in ``period`` after ``minute`` for an average game."""
    hz = model["hazard"][period]
    return sum(hz[m] for m in range(max(HALF_START[period] + 1, minute + 1), HALF_END[period] + 1))


def league_factor(model: dict[str, Any], league_slug: str, *, exclude: tuple[int, int] | None = None) -> float:
    """League scoring scale; ``exclude=(goals, games)`` leaves a game out for the backtest."""
    row = (model.get("leagues") or {}).get(league_slug)
    if not row:
        return 1.0
    if exclude is None:
        return float(row["factor"])
    goals, games = exclude
    return _league_factor(row["goals"] - goals, int(row["games"]) - games, model["goals_per_game"])


def remaining_lambda(
    model: dict[str, Any],
    *,
    period: int,
    minute: int,
    league_slug: str,
    zero_zero: bool,
    league_scale: float | None = None,
    habit_scale: float = 1.0,
    inplay_scale: float = 1.0,
) -> dict[str, float]:
    if period not in HALF_END:
        raise ValueError(f"period must be 1 or 2, got {period!r}")
    minute = max(HALF_START[period], min(HALF_END[period], int(minute)))
    base = _base_lambda(model, period, minute)
    lf = league_scale if league_scale is not None else league_factor(model, league_slug)
    sf = float(model["state_mult"][period]["zero" if zero_zero else "scoring"])
    lam = base * lf * sf * float(habit_scale) * float(inplay_scale)
    return {
        "lambda": lam,
        "base": base,
        "league_factor": lf,
        "state_factor": sf,
        "habit_factor": float(habit_scale),
        "inplay_factor": float(inplay_scale),
        "minutes_left": HALF_END[period] - minute,
    }


def no_goal_probability(model: dict[str, Any], **kwargs: Any) -> float:
    return math.exp(-remaining_lambda(model, **kwargs)["lambda"])


# ---------------------------------------------------------------------------
# Backtest: calibration and trigger policy
# ---------------------------------------------------------------------------


def _bucket(p: float) -> str:
    lo = min(90, int(p * 10) * 10)
    return f"{lo}-{lo + 10}"


def _pct(held: float, n: float) -> int | None:
    return int(round(100 * held / n)) if n else None


class _EdgeAccumulator:
    """Held-to-HT rates by box-habit / box-so-far bucket for goalless games at the edge minutes."""

    def __init__(self) -> None:
        self.habit: dict[int, dict[str, list[int]]] = {t: {k: [0, 0] for k in HABIT_LABELS} for t in EDGE_MINUTES}
        self.inplay: dict[int, dict[str, list[int]]] = {t: {k: [0, 0] for k in INPLAY_LABELS} for t in EDGE_MINUTES}
        self.count: dict[int, dict[str, list[int]]] = {t: {} for t in EDGE_MINUTES}
        self.grid: dict[int, dict[tuple[str, str], list[int]]] = {t: {} for t in EDGE_MINUTES}
        self.base: dict[int, list[int]] = {t: [0, 0] for t in EDGE_MINUTES}

    def add(self, t: int, habit: str | None, inplay: str | None, entries: int, held: int) -> None:
        self.base[t][0] += 1
        self.base[t][1] += held
        if habit:
            self.habit[t][habit][0] += 1
            self.habit[t][habit][1] += held
        if inplay:
            self.inplay[t][inplay][0] += 1
            self.inplay[t][inplay][1] += held
        label = "0-1" if entries <= 1 else "2-3" if entries <= 3 else "4-5" if entries <= 5 else "6+"
        cell = self.count[t].setdefault(label, [0, 0])
        cell[0] += 1
        cell[1] += held
        if habit and inplay:
            g = self.grid[t].setdefault((habit, inplay), [0, 0])
            g[0] += 1
            g[1] += held

    @staticmethod
    def _rows(cells: dict[Any, list[int]], order: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        keys = list(order) if order else sorted(cells)
        return [
            {"bucket": k, "n": cells[k][0], "held_pct": _pct(cells[k][1], cells[k][0])}
            for k in keys
            if k in cells and cells[k][0]
        ]

    def summary(self, model: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
        box = model.get("box") or {}
        out: dict[str, Any] = {
            "records": int(box.get("records") or 0),
            "fitted": bool(box.get("teams")),
            "per_half": {str(p): round(float(v), 2) for p, v in (box.get("per_half") or {}).items()},
            "habit_beta": {str(p): v for p, v in (box.get("habit_beta") or {}).items()},
            "inplay_beta": {str(p): v for p, v in (box.get("inplay_beta") or {}).items()},
            "habit_mult": {str(p): v for p, v in (box.get("habit_mult") or {}).items()},
            "inplay_mult": {str(p): v for p, v in (box.get("inplay_mult") or {}).items()},
            "minutes": {},
            "profile": _half_profile(records),
        }
        for t in EDGE_MINUTES:
            n, held = self.base[t]
            out["minutes"][str(t)] = {
                "n": n,
                "held_pct": _pct(held, n),
                "habit": self._rows(self.habit[t], HABIT_LABELS),
                "inplay": self._rows(self.inplay[t], INPLAY_LABELS),
                "entries": self._rows(self.count[t], ("0-1", "2-3", "4-5", "6+")),
                "grid": [
                    {"habit": hk, "inplay": ik, "n": v[0], "held_pct": _pct(v[1], v[0])}
                    for (hk, ik), v in sorted(self.grid[t].items(), key=lambda kv: (HABIT_LABELS.index(kv[0][0]), INPLAY_LABELS.index(kv[0][1])))
                ],
            }
        return out


_SHOT_KINDS = {"shot", "shot_on", "blocked", "goal"}


def _half_profile(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Average first-half numbers for halves that finished 0-0 versus the rest."""
    groups: dict[str, dict[str, list[float]]] = {"zero": {}, "goal": {}, "n": {}}
    n = {"zero": 0, "goal": 0}
    for rec in records:
        if not has_box_data(rec):
            continue
        key = "zero" if int(rec.get("ht_home") or 0) + int(rec.get("ht_away") or 0) == 0 else "goal"
        n[key] += 1
        ev1 = [e for e in rec.get("events") or [] if event_period(e) == 1]
        h, a = _box_totals(rec, 1)
        stats = {
            "box_entries": h + a,
            "shots": sum(1 for e in ev1 if e.get("kind") in _SHOT_KINDS),
            "sot": sum(1 for e in ev1 if e.get("kind") in {"shot_on", "goal"}),
            "corners": sum(1 for e in ev1 if e.get("kind") == "corner"),
        }
        if rec.get("has_xg"):
            stats["xg"] = sum(float(e.get("xg") or 0) for e in ev1 if e.get("kind") != "own_goal")
        for k, v in stats.items():
            groups[key].setdefault(k, []).append(float(v))
    rows = []
    for k in ("box_entries", "shots", "sot", "corners", "xg"):
        z, g = groups["zero"].get(k) or [], groups["goal"].get(k) or []
        if z and g:
            rows.append({"stat": k, "zero": round(sum(z) / len(z), 2), "goal": round(sum(g) / len(g), 2)})
    return {"zero_n": n["zero"], "goal_n": n["goal"], "rows": rows}


def backtest(
    model: dict[str, Any],
    records: list[dict[str, Any]] | None = None,
    *,
    thresholds: Iterable[float] = BACKTEST_THRESHOLDS,
    windows: dict[int, tuple[int, int]] | None = None,
) -> dict[str, Any]:
    records = load_records() if records is None else records
    windows = windows or DEFAULT_WINDOWS
    calib: dict[str, dict[str, float]] = {}
    ll_model = ll_base = ll_no_box = 0.0
    n_samples = 0
    policy: dict[float, dict[int, list[float]]] = {th: {1: [0, 0, 0.0], 2: [0, 0, 0.0]} for th in thresholds}
    quiet_check = {"quiet": [0, 0], "all": [0, 0]}
    edge = _EdgeAccumulator()

    for rec in records:
        events = rec.get("events") or []
        slug = str(rec.get("league_slug") or "")
        ft = int(rec.get("ft_home") or 0) + int(rec.get("ft_away") or 0)
        scale = league_factor(model, slug, exclude=(ft, 1))
        has_box = has_box_data(rec)
        for p, minutes in FIT_MINUTES.items():
            probs: dict[int, float] = {}
            hf, _hrel, hbucket, _hexp = (
                habit_factor(model, p, str(rec.get("home_id") or ""), str(rec.get("away_id") or ""), slug, exclude=rec)
                if has_box
                else (1.0, None, None, None)
            )
            for t in minutes:
                h, a = score_at(events, p, t)
                bh, ba = box_so_far(rec, p, t) if has_box else (0, 0)
                inf, _irel, ibucket = inplay_factor(model, p, t, bh + ba) if has_box else (1.0, None, None)
                lam = remaining_lambda(
                    model, period=p, minute=t, league_slug=slug, zero_zero=(h + a == 0),
                    league_scale=scale, habit_scale=hf, inplay_scale=inf,
                )
                prob = math.exp(-lam["lambda"])
                probs[t] = prob
                y = 1 if goals_after(events, p, t) == 0 else 0
                q = min(0.995, max(0.005, prob))
                ll_model -= y * math.log(q) + (1 - y) * math.log(1 - q)
                qb = min(0.995, max(0.005, math.exp(-_base_lambda(model, p, t))))
                ll_base -= y * math.log(qb) + (1 - y) * math.log(1 - qb)
                qn = min(0.995, max(0.005, math.exp(-lam["lambda"] / (hf * inf))))
                ll_no_box -= y * math.log(qn) + (1 - y) * math.log(1 - qn)
                n_samples += 1
                if has_box and p == 1 and t in EDGE_MINUTES and h + a == 0:
                    edge.add(t, hbucket, ibucket, bh + ba, y)
                cell = calib.setdefault(_bucket(prob), {"n": 0, "held": 0, "prob_sum": 0.0})
                cell["n"] += 1
                cell["held"] += y
                cell["prob_sum"] += prob
            lo, hi = windows.get(p, (HALF_START[p] + 1, HALF_END[p]))
            for th, per in policy.items():
                for t in range(lo, hi + 1):
                    if t in probs and probs[t] >= th:
                        per[p][0] += 1
                        per[p][1] += 1 if goals_after(events, p, t) == 0 else 0
                        per[p][2] += t
                        break
        # The "quiet first quarter-hour" heuristic, measured directly.
        h, a = score_at(events, 1, 15)
        if h + a == 0:
            shots = sum(1 for e in events if event_period(e) == 1 and int(e.get("minute") or 0) <= 15 and e.get("kind") in {"shot", "shot_on", "blocked", "goal"})
            held = goals_after(events, 1, 15) == 0
            quiet_check["all"][0] += 1
            quiet_check["all"][1] += 1 if held else 0
            if shots < 4:
                quiet_check["quiet"][0] += 1
                quiet_check["quiet"][1] += 1 if held else 0

    calibration = []
    for bucket in sorted(calib, key=lambda b: int(b.split("-")[0])):
        cell = calib[bucket]
        n = int(cell["n"])
        calibration.append(
            {
                "bucket": bucket,
                "n": n,
                "predicted_pct": int(round(100 * cell["prob_sum"] / n)) if n else 0,
                "actual_pct": int(round(100 * cell["held"] / n)) if n else 0,
            }
        )
    policy_rows = []
    for th, per in policy.items():
        row: dict[str, Any] = {"threshold": th, "break_even_odds": round(1 / th, 2) if th > 0 else None}
        for p in (1, 2):
            fired, held, tsum = per[p]
            row[f"h{p}"] = {
                "fired": int(fired),
                "fired_pct": int(round(100 * fired / len(records))) if records else 0,
                "held": int(held),
                "hit_pct": int(round(100 * held / fired)) if fired else None,
                "avg_minute": round(tsum / fired, 1) if fired else None,
            }
        policy_rows.append(row)
    return {
        "archive_total": len(records),
        "samples": n_samples,
        "log_loss": round(ll_model / n_samples, 4) if n_samples else None,
        "log_loss_time_only": round(ll_base / n_samples, 4) if n_samples else None,
        "log_loss_no_box": round(ll_no_box / n_samples, 4) if n_samples else None,
        "box_edge": edge.summary(model, records),
        "calibration": calibration,
        "windows": {str(p): list(w) for p, w in windows.items()},
        "policy": policy_rows,
        "quiet_start": {
            "quiet_n": quiet_check["quiet"][0],
            "quiet_held_pct": int(round(100 * quiet_check["quiet"][1] / quiet_check["quiet"][0])) if quiet_check["quiet"][0] else None,
            "all_n": quiet_check["all"][0],
            "all_held_pct": int(round(100 * quiet_check["all"][1] / quiet_check["all"][0])) if quiet_check["all"][0] else None,
        },
    }


# ---------------------------------------------------------------------------
# Config + live evaluation
# ---------------------------------------------------------------------------


def threshold() -> float:
    raw = (os.getenv("EEESOC_NOGOAL_THRESHOLD") or "").strip()
    try:
        return max(0.5, min(0.95, float(raw))) if raw else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD


def windows() -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    for p in (1, 2):
        raw = (os.getenv(f"EEESOC_NOGOAL_WINDOW_{p}H") or "").strip()
        lo, hi = DEFAULT_WINDOWS[p]
        if raw and "-" in raw:
            try:
                a, b = raw.split("-", 1)
                lo, hi = int(a), int(b)
            except ValueError:
                pass
        lo = max(HALF_START[p] + 1, min(HALF_END[p], lo))
        hi = max(lo, min(HALF_END[p], hi))
        out[p] = (lo, hi)
    return out


def live_period(timeline: dict[str, Any]) -> tuple[int, int, str]:
    """(period, minute within the game, phase) for a live timeline; phase ∈ 1h | ht | 2h | ft."""
    events = timeline.get("events") or []
    clock = str(timeline.get("clock") or "").lower()
    minute = int(timeline.get("minute") or 1)
    if timeline.get("final"):
        return 2, HALF_END[2], "ft"
    if any(event_period(e) == 2 for e in events):
        return 2, max(HALF_END[1] + 1, min(HALF_END[2], minute)), "2h"
    if "ht" in clock or "halftime" in clock or "half time" in clock or "half-time" in clock:
        return 2, HALF_END[1], "ht"
    if minute > HALF_END[1]:
        return 2, min(HALF_END[2], minute), "2h"
    return 1, min(HALF_END[1], minute), "1h"


def evaluate_live(
    model: dict[str, Any],
    timeline: dict[str, Any],
    *,
    league_slug: str,
    home_id: str = "",
    away_id: str = "",
) -> dict[str, Any]:
    period, minute, phase = live_period(timeline)
    home = int(timeline.get("home_score") or 0)
    away = int(timeline.get("away_score") or 0)
    hf, hrel, hbucket, hexp = habit_factor(model, period, str(home_id or ""), str(away_id or ""), league_slug)
    box_known = bool(timeline.get("box_entries"))
    bh, ba = box_so_far(timeline, period, minute) if box_known else (0, 0)
    inf, irel, ibucket = inplay_factor(model, period, minute, (bh + ba) if box_known and phase != "ht" else None)
    parts = remaining_lambda(
        model, period=period, minute=minute, league_slug=league_slug, zero_zero=(home + away == 0),
        habit_scale=hf, inplay_scale=inf,
    )
    p = math.exp(-parts["lambda"])
    lo, hi = windows()[period]
    if phase in {"ft"}:
        window = "closed"
    elif phase == "ht":
        window = "before"
    elif minute < lo:
        window = "before"
    elif minute > hi:
        window = "after"
    else:
        window = "open"
    th = threshold()
    return {
        "period": period,
        "phase": phase,
        "minute": minute,
        "minutes_left": parts["minutes_left"],
        "home_score": home,
        "away_score": away,
        "zero_zero": home + away == 0,
        "p_no_goal": round(p, 3),
        "p_no_goal_pct": int(round(100 * p)),
        "lambda": round(parts["lambda"], 3),
        "base_lambda": round(parts["base"], 3),
        "league_factor": round(parts["league_factor"], 3),
        "state_factor": round(parts["state_factor"], 3),
        "habit_factor": round(hf, 3),
        "habit_bucket": hbucket,
        "habit_rel": round(hrel, 2) if hrel is not None else None,
        "habit_expected": round(hexp, 1) if hexp is not None else None,
        "inplay_factor": round(inf, 3),
        "inplay_bucket": ibucket,
        "inplay_rel": round(irel, 2) if irel is not None else None,
        "box_home": bh if box_known else None,
        "box_away": ba if box_known else None,
        "box_total": (bh + ba) if box_known else None,
        "break_even_odds": round(1 / p, 2) if p > 0 else None,
        "threshold": th,
        "window": window,
        "window_minutes": [lo, hi],
        "ready": window == "open" and p >= th,
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def nogoal_model(*, use_cache: bool = True, with_backtest: bool = False) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Cached (model, backtest); refit when the archive grows or the TTL lapses."""
    global _cache
    records = load_records()
    # Refit when the archive grows or when the box-entry backfill enriches existing records.
    key = (len(records), sum(1 for r in records if r.get("box_entries")))
    cached_model: dict[str, Any] | None = None
    with _lock:
        if use_cache and _cache is not None:
            ts, n, model, bt = _cache
            if n == key and time.time() - ts < _CACHE_TTL_S:
                if bt is not None or not with_backtest:
                    return model, bt
                cached_model = model
    model = cached_model if cached_model is not None else build_model(records)
    bt = backtest(model, records) if with_backtest else None
    with _lock:
        if _cache is not None and _cache[1] == key and bt is None:
            bt = _cache[3]
        _cache = (time.time(), key, model, bt)
    return model, bt


def clear_nogoal_cache() -> None:
    global _cache
    with _lock:
        _cache = None
