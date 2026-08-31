"""Scoreline-state visits, FT outcomes, and transition trees."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from eeesoc.models import Match
from eeesoc.teams import resolve_team, team_matches


def score_path(match: Match) -> list[tuple[int, int, int]]:
    """
    Chronological scoreline visits: (minute, home_goals, away_goals).

    Always starts at (0, 0, 0). Each goal appends the new absolute scoreline.
    """
    path: list[tuple[int, int, int]] = [(0, 0, 0)]
    hg = ag = 0
    for g in sorted(match.goals, key=lambda x: (x.minute, x.team)):
        if g.team == "home":
            hg += 1
        else:
            ag += 1
        path.append((g.minute, hg, ag))
    return path


def team_score_path(match: Match, side: str) -> list[tuple[int, int, int]]:
    """Scoreline path from a club's perspective: (minute, for, against)."""
    if side not in {"home", "away"}:
        raise ValueError("side must be home or away")
    path: list[tuple[int, int, int]] = [(0, 0, 0)]
    scored = conceded = 0
    for g in sorted(match.goals, key=lambda x: (x.minute, x.team)):
        if g.team == side:
            scored += 1
        else:
            conceded += 1
        path.append((g.minute, scored, conceded))
    return path


def first_visit(
    path: list[tuple[int, int, int]], for_goals: int, against_goals: int
) -> tuple[int, int] | None:
    """Return (index, minute) of first visit to for-against (or home-away) scoreline."""
    for i, (minute, a, b) in enumerate(path):
        if a == for_goals and b == against_goals:
            return i, minute
    return None


def state_index_at(path: list[tuple[int, int, int]], minute: int) -> int:
    """Index of the scoreline state in effect at ``minute`` (last change ≤ minute)."""
    idx = 0
    for i, (m, _, _) in enumerate(path):
        if m <= minute:
            idx = i
        else:
            break
    return idx


def _pct(count: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(count / total, 3)


def _ft_key(hf: int, af: int) -> str:
    return f"{hf}-{af}"


def scoreline_outcomes(
    corpus: Iterable[Match],
    *,
    for_goals: int,
    against_goals: int,
    team: str | None = None,
    limit_peers: int = 12,
    at_minute: int | None = None,
) -> dict[str, Any]:
    """
    Summarise what followed the scoreline ``for_goals–against_goals``.

    When ``at_minute`` is set, only matches whose score *at that minute* was
    the given scoreline count, and "next" is the first change after that
    minute — so percentages respect the time left in the match. Otherwise,
    matches that ever hit the scoreline count from their first visit.

    If ``team`` is set (FD canonical), scorelines are from that club's perspective
    (for/against) across home and away games. Otherwise absolute home–away.
    """
    visits: list[dict[str, Any]] = []
    ft_counts: Counter[str] = Counter()
    next_counts: Counter[str] = Counter()
    ended_same = 0
    more_goals = 0
    next_for = next_against = 0  # / home / away when league
    perspective = "team" if team else "league"

    for match in corpus:
        if team:
            side = team_matches(match.home, match.away, team)
            if not side:
                continue
            path = team_score_path(match, side)
            ft_for = match.home_goals_ft if side == "home" else match.away_goals_ft
            ft_ag = match.away_goals_ft if side == "home" else match.home_goals_ft
        else:
            side = None
            path = score_path(match)
            ft_for, ft_ag = match.home_goals_ft, match.away_goals_ft

        if at_minute is None:
            hit = first_visit(path, for_goals, against_goals)
            if hit is None:
                continue
            idx, visit_minute = hit
        else:
            idx = state_index_at(path, at_minute)
            visit_minute, cur_for, cur_against = path[idx]
            if (cur_for, cur_against) != (for_goals, against_goals):
                continue
        ft = _ft_key(ft_for, ft_ag)
        ft_counts[ft] += 1
        same = ft_for == for_goals and ft_ag == against_goals
        if same:
            ended_same += 1
        else:
            more_goals += 1

        nxt = path[idx + 1] if idx + 1 < len(path) else None
        next_label = None
        next_side = None
        if nxt:
            _, nf, na = nxt
            next_label = _ft_key(nf, na)
            next_counts[next_label] += 1
            if nf > for_goals:
                next_for += 1
                next_side = "for" if team else "home"
            elif na > against_goals:
                next_against += 1
                next_side = "against" if team else "away"
        else:
            next_counts["FT"] += 1

        visits.append(
            {
                "match_id": match.match_id,
                "season": match.season,
                "date": match.date,
                "home": match.home,
                "away": match.away,
                "side": side,
                "visit_minute": visit_minute,
                "scoreline": _ft_key(for_goals, against_goals),
                "ft": ft,
                "ft_for": ft_for,
                "ft_against": ft_ag,
                "ended_same": same,
                "more_goals": not same,
                "next": next_label,
                "next_side": next_side,
                "path_after": [
                    {"minute": m, "score": _ft_key(a, b)} for m, a, b in path[idx + 1 :]
                ],
            }
        )

    n = len(visits)
    # Prefer recent-ish peers in the UI list
    visits.sort(key=lambda v: v["date"], reverse=True)

    ft_dist = [
        {"score": score, "count": count, "pct": _pct(count, n)}
        for score, count in ft_counts.most_common()
    ]
    next_dist = [
        {"score": score, "count": count, "pct": _pct(count, n)}
        for score, count in next_counts.most_common()
    ]

    return {
        "perspective": perspective,
        "team": team,
        "scoreline": _ft_key(for_goals, against_goals),
        "for_goals": for_goals,
        "against_goals": against_goals,
        "at_minute": at_minute,
        "count": n,
        "pct_ended_same": _pct(ended_same, n),
        "pct_more_goals": _pct(more_goals, n),
        "pct_next_for": _pct(next_for, n),
        "pct_next_against": _pct(next_against, n),
        "ft_distribution": ft_dist,
        "next_distribution": next_dist,
        "peers": visits[:limit_peers],
    }


def transition_tree(
    corpus: Iterable[Match],
    *,
    from_for: int,
    from_against: int,
    team: str | None = None,
    live_to: tuple[int, int] | None = None,
    at_minute: int | None = None,
) -> dict[str, Any]:
    """
    From scoreline A–B, % of next states (the branch tree).

    ``live_to`` marks the branch the live match actually followed.
    ``at_minute`` conditions on games at that scoreline at that minute.
    """
    base = scoreline_outcomes(
        corpus,
        for_goals=from_for,
        against_goals=from_against,
        team=team,
        limit_peers=0,
        at_minute=at_minute,
    )
    live_key = _ft_key(*live_to) if live_to else None
    branches = []
    for row in base["next_distribution"]:
        branches.append(
            {
                **row,
                "is_live_branch": live_key is not None and row["score"] == live_key,
            }
        )
    return {
        "perspective": base["perspective"],
        "team": team,
        "from": base["scoreline"],
        "at_minute": at_minute,
        "count": base["count"],
        "branches": branches,
        "live_to": live_key,
        "pct_ended_same": base["pct_ended_same"],
        "pct_more_goals": base["pct_more_goals"],
        "ft_distribution": base["ft_distribution"],
    }


def build_live_scoreline_eval(
    corpus: Iterable[Match],
    *,
    home_name: str,
    away_name: str,
    home_score: int,
    away_score: int,
    home_id: str = "",
    away_id: str = "",
    prev_home: int | None = None,
    prev_away: int | None = None,
    minute: int | None = None,
    prev_minute: int | None = None,
    limit_peers: int = 8,
) -> dict[str, Any]:
    """
    Full live evaluation: home-team history, away-team history, then league.

    Scorelines for each club use for/against. League uses absolute home–away.
    When ``minute`` is set, everything conditions on games at this scoreline
    at that minute — so a 60' 0-1 only compares against games that were 0-1
    at the 60th, and branches respect the time remaining.
    Always attaches forward transition trees from the current scoreline.
    When ``prev_*`` is provided (score before the latest goal), also attaches
    retrospective trees with the live branch highlighted, conditioned at
    ``prev_minute`` (just before the goal) when available.
    """
    home_fd = resolve_team(home_name, espn_id=home_id or None)
    away_fd = resolve_team(away_name, espn_id=away_id or None)
    at_minute = max(0, min(90, minute)) if minute is not None else None
    prev_at = max(0, min(90, prev_minute)) if prev_minute is not None else None

    home_eval = (
        scoreline_outcomes(
            corpus,
            for_goals=home_score,
            against_goals=away_score,
            team=home_fd,
            limit_peers=limit_peers,
            at_minute=at_minute,
        )
        if home_fd
        else None
    )
    away_eval = (
        scoreline_outcomes(
            corpus,
            for_goals=away_score,
            against_goals=home_score,
            team=away_fd,
            limit_peers=limit_peers,
            at_minute=at_minute,
        )
        if away_fd
        else None
    )
    league_eval = scoreline_outcomes(
        corpus,
        for_goals=home_score,
        against_goals=away_score,
        team=None,
        limit_peers=limit_peers,
        at_minute=at_minute,
    )

    # Forward trees from the *current* scoreline — always shown on Similar.
    trees: dict[str, Any] = {
        "home": (
            transition_tree(
                corpus,
                from_for=home_score,
                from_against=away_score,
                team=home_fd,
                at_minute=at_minute,
            )
            if home_fd
            else None
        ),
        "away": (
            transition_tree(
                corpus,
                from_for=away_score,
                from_against=home_score,
                team=away_fd,
                at_minute=at_minute,
            )
            if away_fd
            else None
        ),
        "league": transition_tree(
            corpus,
            from_for=home_score,
            from_against=away_score,
            team=None,
            at_minute=at_minute,
        ),
    }

    # Retrospective trees: branches available at the previous score, with the
    # live path highlighted (only once the score has actually moved).
    from_prev: dict[str, Any] = {}
    if (
        prev_home is not None
        and prev_away is not None
        and (prev_home, prev_away) != (home_score, away_score)
    ):
        from_prev["home"] = (
            transition_tree(
                corpus,
                from_for=prev_home,
                from_against=prev_away,
                team=home_fd,
                live_to=(home_score, away_score),
                at_minute=prev_at,
            )
            if home_fd
            else None
        )
        from_prev["away"] = (
            transition_tree(
                corpus,
                from_for=prev_away,
                from_against=prev_home,
                team=away_fd,
                live_to=(away_score, home_score),
                at_minute=prev_at,
            )
            if away_fd
            else None
        )
        from_prev["league"] = transition_tree(
            corpus,
            from_for=prev_home,
            from_against=prev_away,
            team=None,
            live_to=(home_score, away_score),
            at_minute=prev_at,
        )

    return {
        "scoreline": _ft_key(home_score, away_score),
        "home": home_name,
        "away": away_name,
        "home_fd": home_fd,
        "away_fd": away_fd,
        "home_score": home_score,
        "away_score": away_score,
        "minute": at_minute,
        "prev_minute": prev_at,
        "prev_scoreline": (
            _ft_key(prev_home, prev_away)
            if prev_home is not None
            and prev_away is not None
            and (prev_home, prev_away) != (home_score, away_score)
            else None
        ),
        "home_history": home_eval,
        "away_history": away_eval,
        "league_history": league_eval,
        "trees": trees,
        "trees_from_prev": from_prev,
    }
