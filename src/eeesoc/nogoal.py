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
* **What does *not* help** (checked by log loss on the archive and left out):
  how many shots or how much xG the half has produced so far, activity in the
  last ten minutes, and club attack/defence rates from a handful of games.  A
  quiet first quarter-hour tells you almost nothing about the next thirty minutes.

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
# Trigger policy defaults: fire once per half when P(no goal) clears the threshold
# inside the window — after 35' / 78' the price is too short to be worth it.
DEFAULT_THRESHOLD = 0.65
DEFAULT_WINDOWS = {1: (10, 35), 2: (50, 78)}
BACKTEST_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
_CACHE_TTL_S = 600.0

_lock = threading.Lock()
_cache: tuple[float, int, dict[str, Any], dict[str, Any] | None] | None = None


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
    return model


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
) -> dict[str, float]:
    if period not in HALF_END:
        raise ValueError(f"period must be 1 or 2, got {period!r}")
    minute = max(HALF_START[period], min(HALF_END[period], int(minute)))
    base = _base_lambda(model, period, minute)
    lf = league_scale if league_scale is not None else league_factor(model, league_slug)
    sf = float(model["state_mult"][period]["zero" if zero_zero else "scoring"])
    lam = base * lf * sf
    return {"lambda": lam, "base": base, "league_factor": lf, "state_factor": sf, "minutes_left": HALF_END[period] - minute}


def no_goal_probability(model: dict[str, Any], **kwargs: Any) -> float:
    return math.exp(-remaining_lambda(model, **kwargs)["lambda"])


# ---------------------------------------------------------------------------
# Backtest: calibration and trigger policy
# ---------------------------------------------------------------------------


def _bucket(p: float) -> str:
    lo = min(90, int(p * 10) * 10)
    return f"{lo}-{lo + 10}"


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
    ll_model = ll_base = 0.0
    n_samples = 0
    policy: dict[float, dict[int, list[float]]] = {th: {1: [0, 0, 0.0], 2: [0, 0, 0.0]} for th in thresholds}
    quiet_check = {"quiet": [0, 0], "all": [0, 0]}

    for rec in records:
        events = rec.get("events") or []
        slug = str(rec.get("league_slug") or "")
        ft = int(rec.get("ft_home") or 0) + int(rec.get("ft_away") or 0)
        scale = league_factor(model, slug, exclude=(ft, 1))
        for p, minutes in FIT_MINUTES.items():
            probs: dict[int, float] = {}
            for t in minutes:
                h, a = score_at(events, p, t)
                lam = remaining_lambda(model, period=p, minute=t, league_slug=slug, zero_zero=(h + a == 0), league_scale=scale)
                prob = math.exp(-lam["lambda"])
                probs[t] = prob
                y = 1 if goals_after(events, p, t) == 0 else 0
                q = min(0.995, max(0.005, prob))
                ll_model -= y * math.log(q) + (1 - y) * math.log(1 - q)
                qb = min(0.995, max(0.005, math.exp(-_base_lambda(model, p, t))))
                ll_base -= y * math.log(qb) + (1 - y) * math.log(1 - qb)
                n_samples += 1
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


def evaluate_live(model: dict[str, Any], timeline: dict[str, Any], *, league_slug: str) -> dict[str, Any]:
    period, minute, phase = live_period(timeline)
    home = int(timeline.get("home_score") or 0)
    away = int(timeline.get("away_score") or 0)
    parts = remaining_lambda(model, period=period, minute=minute, league_slug=league_slug, zero_zero=(home + away == 0))
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
    cached_model: dict[str, Any] | None = None
    with _lock:
        if use_cache and _cache is not None:
            ts, n, model, bt = _cache
            if n == len(records) and time.time() - ts < _CACHE_TTL_S:
                if bt is not None or not with_backtest:
                    return model, bt
                cached_model = model
    model = cached_model if cached_model is not None else build_model(records)
    bt = backtest(model, records) if with_backtest else None
    with _lock:
        if _cache is not None and _cache[1] == len(records) and bt is None:
            bt = _cache[3]
        _cache = (time.time(), len(records), model, bt)
    return model, bt


def clear_nogoal_cache() -> None:
    global _cache
    with _lock:
        _cache = None
