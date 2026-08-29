"""Similar-match lookalike scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from eeesoc.models import Match, MatchSnapshot


@dataclass(frozen=True)
class SimilarHit:
    match: Match
    snapshot: MatchSnapshot
    distance: float
    score: float

    def to_dict(self) -> dict:
        return {
            "match_id": self.match.match_id,
            "season": self.match.season,
            "date": self.match.date,
            "home": self.match.home,
            "away": self.match.away,
            "distance": round(self.distance, 4),
            "score": round(self.score, 4),
            "snapshot": self.snapshot.to_dict(),
            "label": self.snapshot.label(),
            "ft": f"{self.match.home_goals_ft}-{self.match.away_goals_ft}",
        }


def snapshot_distance(a: MatchSnapshot, b: MatchSnapshot) -> float:
    """Weighted L1 distance across scoreline, shots/SOT, and goal timing."""
    score_term = abs(a.home_goals - b.home_goals) * 3.0 + abs(a.away_goals - b.away_goals) * 3.0
    shot_term = (
        abs(a.home_shots - b.home_shots) * 0.35
        + abs(a.away_shots - b.away_shots) * 0.35
        + abs(a.home_sot - b.home_sot) * 0.9
        + abs(a.away_sot - b.away_sot) * 0.9
    )
    minute_term = abs(a.minute - b.minute) * 0.05

    # Goal-minute bipartite-ish: compare sorted lists with padding
    ga = list(a.goal_minutes)
    gb = list(b.goal_minutes)
    n = max(len(ga), len(gb))
    goal_term = abs(len(ga) - len(gb)) * 1.5
    for i in range(min(len(ga), len(gb))):
        goal_term += abs(ga[i] - gb[i]) * 0.08
    if n and not ga and not gb:
        goal_term = 0.0

    return score_term + shot_term + minute_term + goal_term


def find_similar(
    query: MatchSnapshot,
    corpus: Iterable[Match],
    *,
    limit: int = 12,
    exclude_ids: set[str] | None = None,
) -> list[SimilarHit]:
    exclude_ids = exclude_ids or set()
    hits: list[SimilarHit] = []
    for match in corpus:
        if match.match_id in exclude_ids:
            continue
        snap = match.snapshot_at(query.minute)
        dist = snapshot_distance(query, snap)
        score = 1.0 / (1.0 + dist)
        hits.append(SimilarHit(match=match, snapshot=snap, distance=dist, score=score))
    hits.sort(key=lambda h: (h.distance, h.match.date))
    return hits[:limit]


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def opponent_scored_context(
    corpus: Iterable[Match],
    *,
    goal_minute: int,
    scored_by: str,
    window: int = 5,
    limit: int = 12,
) -> dict[str, Any]:
    """
    Historical peers where the same side scored around ``goal_minute``.

    For each peer, report both teams' shots/SOT at that goal — and averages for
    the conceding side ("my team when the opponent scored").
    """
    if scored_by not in {"home", "away"}:
        raise ValueError("scored_by must be home or away")
    conceded_by = "away" if scored_by == "home" else "home"
    lo = max(1, goal_minute - window)
    hi = min(90, goal_minute + window)

    peers: list[dict[str, Any]] = []
    for match in corpus:
        for goal in match.goals:
            if goal.team != scored_by:
                continue
            if goal.minute < lo or goal.minute > hi:
                continue
            snap = match.snapshot_at(goal.minute)
            if conceded_by == "home":
                my_shots, my_sot = snap.home_shots, snap.home_sot
                opp_shots, opp_sot = snap.away_shots, snap.away_sot
                my_name, opp_name = match.home, match.away
            else:
                my_shots, my_sot = snap.away_shots, snap.away_sot
                opp_shots, opp_sot = snap.home_shots, snap.home_sot
                my_name, opp_name = match.away, match.home
            peers.append(
                {
                    "match_id": match.match_id,
                    "season": match.season,
                    "date": match.date,
                    "home": match.home,
                    "away": match.away,
                    "goal_minute": goal.minute,
                    "scored_by": scored_by,
                    "scored_by_name": opp_name,
                    "conceded_by": conceded_by,
                    "conceded_by_name": my_name,
                    "my_shots": my_shots,
                    "my_sot": my_sot,
                    "opp_shots": opp_shots,
                    "opp_sot": opp_sot,
                    "home_shots": snap.home_shots,
                    "away_shots": snap.away_shots,
                    "home_sot": snap.home_sot,
                    "away_sot": snap.away_sot,
                    "home_goals": snap.home_goals,
                    "away_goals": snap.away_goals,
                    "label": snap.label(),
                    "ft": f"{match.home_goals_ft}-{match.away_goals_ft}",
                    "minute_delta": abs(goal.minute - goal_minute),
                }
            )
            break  # one peer row per match (first matching goal)

    peers.sort(key=lambda p: (p["minute_delta"], p["date"]))
    peers = peers[:limit]

    return {
        "goal_minute": goal_minute,
        "scored_by": scored_by,
        "conceded_by": conceded_by,
        "window": window,
        "count": len(peers),
        "avg_my_shots": _avg([float(p["my_shots"]) for p in peers]),
        "avg_my_sot": _avg([float(p["my_sot"]) for p in peers]),
        "avg_opp_shots": _avg([float(p["opp_shots"]) for p in peers]),
        "avg_opp_sot": _avg([float(p["opp_sot"]) for p in peers]),
        "avg_home_shots": _avg([float(p["home_shots"]) for p in peers]),
        "avg_home_sot": _avg([float(p["home_sot"]) for p in peers]),
        "avg_away_shots": _avg([float(p["away_shots"]) for p in peers]),
        "avg_away_sot": _avg([float(p["away_sot"]) for p in peers]),
        "peers": peers,
    }
