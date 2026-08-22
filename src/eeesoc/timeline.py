"""Build minute-level shot / goal timelines from FT box scores."""

from __future__ import annotations

import hashlib
from typing import Iterable

from eeesoc.models import GoalEvent


def _seed(*parts: object) -> int:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:8], 16)


def _rng(seed: int) -> Iterable[float]:
    """Tiny deterministic LCG yielding floats in [0, 1)."""
    x = seed % (2**31 - 1) or 1
    while True:
        x = (1103515245 * x + 12345) % (2**31)
        yield x / (2**31)


def place_goals(
    home_ht: int,
    away_ht: int,
    home_ft: int,
    away_ft: int,
    seed: int,
) -> list[GoalEvent]:
    """Place goals into 1H / 2H windows matching HT and FT totals."""
    rng = _rng(seed)
    events: list[GoalEvent] = []

    def slot(count: int, team: str, lo: int, hi: int) -> None:
        used: set[int] = set()
        for _ in range(max(0, count)):
            for _attempt in range(40):
                minute = lo + int(next(rng) * (hi - lo + 1))
                minute = max(lo, min(hi, minute))
                if minute not in used:
                    used.add(minute)
                    events.append(GoalEvent(minute=minute, team=team))
                    break

    slot(home_ht, "home", 1, 45)
    slot(away_ht, "away", 1, 45)
    slot(home_ft - home_ht, "home", 46, 90)
    slot(away_ft - away_ht, "away", 46, 90)
    events.sort(key=lambda g: (g.minute, g.team))
    return events


def cumulative_shots(
    total: int,
    sot_total: int,
    seed: int,
    length: int = 91,
) -> tuple[list[int], list[int]]:
    """
    Spread shots across minutes; SOT is a subset that lands earlier or with shots.
    Returns (shots_by_min, sot_by_min) length `length` (index 0 unused / zero).
    """
    total = max(0, int(total))
    sot_total = max(0, min(int(sot_total), total))
    shots = [0] * length
    sot = [0] * length
    if total == 0:
        return shots, sot

    rng = _rng(seed)
    # Prefer later pressure: beta-ish via squaring
    raw = [next(rng) ** 0.65 for _ in range(total)]
    s = sum(raw) or 1.0
    weights = [r / s for r in raw]

    shot_minutes: list[int] = []
    acc = 0.0
    for w in weights:
        acc += w
        minute = 1 + int(acc * 89)
        minute = max(1, min(90, minute))
        shot_minutes.append(minute)

    # Assign first sot_total shots (by minute order) as SOT for stability
    ordered = sorted(enumerate(shot_minutes), key=lambda t: (t[1], t[0]))
    sot_idx = {i for i, _ in ordered[:sot_total]}

    running_shots = 0
    running_sot = 0
    by_min_shots = {m: 0 for m in range(1, 91)}
    by_min_sot = {m: 0 for m in range(1, 91)}
    for i, m in enumerate(shot_minutes):
        by_min_shots[m] += 1
        if i in sot_idx:
            by_min_sot[m] += 1

    for m in range(1, 91):
        running_shots += by_min_shots[m]
        running_sot += by_min_sot[m]
        shots[m] = running_shots
        sot[m] = running_sot
    shots[0] = 0
    sot[0] = 0
    return shots, sot


def build_timelines(
    match_id: str,
    home_ht: int,
    away_ht: int,
    home_ft: int,
    away_ft: int,
    home_shots: int,
    away_shots: int,
    home_sot: int,
    away_sot: int,
) -> dict:
    seed = _seed(match_id, home_ft, away_ft, home_shots, away_shots)
    goals = place_goals(home_ht, away_ht, home_ft, away_ft, seed)
    hs, hst = cumulative_shots(home_shots, home_sot, seed ^ 0xA5)
    as_, ast = cumulative_shots(away_shots, away_sot, seed ^ 0x5A)
    return {
        "goals": goals,
        "home_shots_by_min": hs,
        "away_shots_by_min": as_,
        "home_sot_by_min": hst,
        "away_sot_by_min": ast,
    }
