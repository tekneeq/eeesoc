"""Similar-match lookalike scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

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
