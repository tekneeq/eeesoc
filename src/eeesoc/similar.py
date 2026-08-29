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


def _rate(count: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(count / total, 3)


def _after_goal_timeline(
    match: Match,
    *,
    trigger_minute: int,
    scored_by: str,
    conceded_by: str,
) -> dict[str, Any]:
    """Goals after the trigger — rest of 1H, 2H, and full remainder."""
    after = [g for g in match.goals if g.minute > trigger_minute]
    rest_1h = [g for g in after if g.minute <= 45]
    second_half = [g for g in after if g.minute >= 46]

    def pack(goals: list) -> list[dict[str, Any]]:
        rows = []
        for g in goals:
            side = "my" if g.team == conceded_by else "opp"
            rows.append(
                {
                    "minute": g.minute,
                    "team": g.team,
                    "side": side,
                    "team_name": match.home if g.team == "home" else match.away,
                    "half": "1H" if g.minute <= 45 else "2H",
                }
            )
        return rows

    after_rows = pack(after)
    sh_rows = pack(second_half)
    my_after = sum(1 for g in after if g.team == conceded_by)
    opp_after = sum(1 for g in after if g.team == scored_by)
    my_2h = sum(1 for g in second_half if g.team == conceded_by)
    opp_2h = sum(1 for g in second_half if g.team == scored_by)
    home_after = sum(1 for g in after if g.team == "home")
    away_after = sum(1 for g in after if g.team == "away")
    home_2h = sum(1 for g in second_half if g.team == "home")
    away_2h = sum(1 for g in second_half if g.team == "away")

    # Equalizer / next goal from the conceding side
    next_my = next((r for r in after_rows if r["side"] == "my"), None)
    next_opp = next((r for r in after_rows if r["side"] == "opp"), None)
    next_any = after_rows[0] if after_rows else None
    next_2h = sh_rows[0] if sh_rows else None

    return {
        "after_goals": after_rows,
        "rest_1h_goals": pack(rest_1h),
        "second_half_goals": sh_rows,
        "more_goals": len(after),
        "more_goals_1h": len(rest_1h),
        "more_goals_2h": len(second_half),
        "my_after": my_after,
        "opp_after": opp_after,
        "my_2h": my_2h,
        "opp_2h": opp_2h,
        "home_after": home_after,
        "away_after": away_after,
        "home_2h": home_2h,
        "away_2h": away_2h,
        "next_goal_minute": next_any["minute"] if next_any else None,
        "next_goal_side": next_any["side"] if next_any else None,
        "next_my_minute": next_my["minute"] if next_my else None,
        "next_opp_minute": next_opp["minute"] if next_opp else None,
        "next_2h_minute": next_2h["minute"] if next_2h else None,
        "next_2h_side": next_2h["side"] if next_2h else None,
        "equalized": my_after > 0,
        "after_label": (
            " · ".join(f"{r['minute']}'{r['side'][0]}" for r in after_rows) or "no more goals"
        ),
        "second_half_label": (
            " · ".join(f"{r['minute']}'{r['side'][0]}" for r in sh_rows) or "no 2H goals"
        ),
    }


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

    Focus: what happened next — more goals, 2nd-half goals, and when.
    Also includes shots/SOT at the trigger for context.
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

            timeline = _after_goal_timeline(
                match,
                trigger_minute=goal.minute,
                scored_by=scored_by,
                conceded_by=conceded_by,
            )
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
                    **timeline,
                }
            )
            break  # one peer row per match (first matching goal)

    peers.sort(key=lambda p: (p["minute_delta"], p["date"]))
    peers = peers[:limit]
    n = len(peers)

    # Aggregate "when" buckets for 2H goals across peers
    when_2h: dict[str, int] = {}
    for p in peers:
        for g in p["second_half_goals"]:
            # bucket: 46-60, 61-75, 76-90
            m = int(g["minute"])
            if m <= 60:
                bucket = "46-60"
            elif m <= 75:
                bucket = "61-75"
            else:
                bucket = "76-90"
            key = f"{bucket}:{g['side']}"
            when_2h[key] = when_2h.get(key, 0) + 1

    when_2h_rows = [
        {
            "bucket": bucket,
            "side": side,
            "count": count,
        }
        for (bucket_side, count) in sorted(when_2h.items(), key=lambda kv: (-kv[1], kv[0]))
        for bucket, side in [bucket_side.split(":", 1)]
    ]

    return {
        "goal_minute": goal_minute,
        "scored_by": scored_by,
        "conceded_by": conceded_by,
        "window": window,
        "count": n,
        # shots (secondary)
        "avg_my_shots": _avg([float(p["my_shots"]) for p in peers]),
        "avg_my_sot": _avg([float(p["my_sot"]) for p in peers]),
        "avg_opp_shots": _avg([float(p["opp_shots"]) for p in peers]),
        "avg_opp_sot": _avg([float(p["opp_sot"]) for p in peers]),
        "avg_home_shots": _avg([float(p["home_shots"]) for p in peers]),
        "avg_home_sot": _avg([float(p["home_sot"]) for p in peers]),
        "avg_away_shots": _avg([float(p["away_shots"]) for p in peers]),
        "avg_away_sot": _avg([float(p["away_sot"]) for p in peers]),
        # goals after trigger (primary)
        "avg_more_goals": _avg([float(p["more_goals"]) for p in peers]),
        "avg_more_goals_1h": _avg([float(p["more_goals_1h"]) for p in peers]),
        "avg_more_goals_2h": _avg([float(p["more_goals_2h"]) for p in peers]),
        "avg_my_after": _avg([float(p["my_after"]) for p in peers]),
        "avg_opp_after": _avg([float(p["opp_after"]) for p in peers]),
        "avg_my_2h": _avg([float(p["my_2h"]) for p in peers]),
        "avg_opp_2h": _avg([float(p["opp_2h"]) for p in peers]),
        "avg_home_2h": _avg([float(p["home_2h"]) for p in peers]),
        "avg_away_2h": _avg([float(p["away_2h"]) for p in peers]),
        "pct_any_more_goals": _rate(sum(1 for p in peers if p["more_goals"] > 0), n),
        "pct_any_2h_goals": _rate(sum(1 for p in peers if p["more_goals_2h"] > 0), n),
        "pct_equalized": _rate(sum(1 for p in peers if p["equalized"]), n),
        "avg_next_goal_minute": _avg(
            [float(p["next_goal_minute"]) for p in peers if p["next_goal_minute"] is not None]
        ),
        "avg_next_my_minute": _avg(
            [float(p["next_my_minute"]) for p in peers if p["next_my_minute"] is not None]
        ),
        "avg_next_2h_minute": _avg(
            [float(p["next_2h_minute"]) for p in peers if p["next_2h_minute"] is not None]
        ),
        "when_2h": when_2h_rows,
        "peers": peers,
    }
