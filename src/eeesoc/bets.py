"""Totals bets from the finished-match archive: under/over a line, 1H / 2H / FT."""

from __future__ import annotations

import re
from typing import Any

from eeesoc.clinical import team_key

_PERIOD_ALIASES = {
    "1h": "1h",
    "1st": "1h",
    "first": "1h",
    "ht": "1h",
    "2h": "2h",
    "2nd": "2h",
    "second": "2h",
    "ft": "ft",
    "full": "ft",
    "fulltime": "ft",
}
_SIDE_ALIASES = {"under": "under", "u": "under", "over": "over", "o": "over"}
_PERIOD_LABEL = {"1h": "1st half", "2h": "2nd half", "ft": "full time"}


def parse_bets_args(args: list[str]) -> dict[str, Any] | None:
    """``TEAM [under|over] LINE [1h|2h|ft]`` — default under, default full time."""
    tokens = [str(t) for t in args if str(t).strip()]
    if not tokens:
        return None
    period = "ft"
    tail = re.sub(r"[^a-z0-9]+", "", tokens[-1].lower())
    if tail in _PERIOD_ALIASES:
        period = _PERIOD_ALIASES[tail]
        tokens.pop()
    if not tokens:
        return None
    try:
        line = float(tokens[-1])
        tokens.pop()
    except ValueError:
        return None
    if line <= 0 or line > 12:
        return None
    side = "under"
    if tokens and tokens[-1].lower() in _SIDE_ALIASES:
        side = _SIDE_ALIASES[tokens.pop().lower()]
    team = " ".join(tokens).strip()
    if not team:
        return None
    return {"team": team, "line": line, "side": side, "period": period}


def _norm(name: str) -> str:
    return team_key(name)


def name_matches(name: str, query: str) -> bool:
    a, b = _norm(name), _norm(query)
    if not a or not b:
        return False
    if a == b or b in a or (len(a) >= 4 and a in b):
        return True
    a_bits, b_bits = a.split(), b.split()
    if not b_bits:
        return False
    used = [False] * len(a_bits)
    for bit in b_bits:
        hit = False
        for i, tok in enumerate(a_bits):
            if used[i]:
                continue
            if bit == tok or (len(bit) >= 3 and tok.startswith(bit)):
                used[i] = True
                hit = True
                break
        if not hit:
            return False
    return True


def find_fixture(board: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Live first, then today's upcoming, then finished — first name match."""
    rows: list[dict[str, Any]] = []
    for group in board.get("leagues") or []:
        for match in group.get("matches") or []:
            rows.append(
                {
                    **match,
                    "league_name": group.get("name") or match.get("league_name") or "",
                    "league_chiclet": group.get("chiclet") or match.get("league_chiclet") or "",
                    "league_slug": group.get("slug") or match.get("league_slug") or "",
                }
            )
    hits = [
        m
        for m in rows
        if name_matches(str(m.get("home") or ""), query) or name_matches(str(m.get("away") or ""), query)
    ]
    if not hits:
        return None

    def rank(m: dict[str, Any]) -> tuple[int, str]:
        state = str(m.get("state") or "")
        if state == "in":
            return (0, "")
        if state == "pre":
            return (1, str(m.get("start") or ""))
        return (2, str(m.get("start") or ""))

    hits.sort(key=rank)
    return hits[0]


def period_goals(rec: dict[str, Any], period: str) -> int | None:
    try:
        ft_h = int(rec.get("ft_home") or 0)
        ft_a = int(rec.get("ft_away") or 0)
    except (TypeError, ValueError):
        return None
    if period == "ft":
        return ft_h + ft_a
    if rec.get("ht_home") is None or rec.get("ht_away") is None:
        return None
    try:
        ht_h = int(rec["ht_home"])
        ht_a = int(rec["ht_away"])
    except (TypeError, ValueError):
        return None
    first = ht_h + ht_a
    if period == "1h":
        return first
    if period == "2h":
        return max(0, ft_h - ht_h) + max(0, ft_a - ht_a)
    return None


def _settle(goals: int, line: float, side: str) -> str:
    if goals < line:
        return "win" if side == "under" else "lose"
    if goals > line:
        return "win" if side == "over" else "lose"
    return "push"


def sample_rate(
    records: list[dict[str, Any]],
    *,
    line: float,
    side: str,
    period: str,
    team: str = "",
    league_slug: str = "",
) -> dict[str, Any]:
    wins = pushes = n = 0
    for rec in records:
        if league_slug and str(rec.get("league_slug") or "") != league_slug:
            continue
        if team and not (
            name_matches(str(rec.get("home") or ""), team) or name_matches(str(rec.get("away") or ""), team)
        ):
            continue
        goals = period_goals(rec, period)
        if goals is None:
            continue
        n += 1
        result = _settle(goals, line, side)
        if result == "win":
            wins += 1
        elif result == "push":
            pushes += 1
    decided = n - pushes
    hit = wins / decided if decided else None
    return {
        "n": n,
        "wins": wins,
        "pushes": pushes,
        "hit_pct": int(round(100 * hit)) if hit is not None else None,
        "fair_odds": round(1 / hit, 2) if hit else None,
    }


def live_goals(match: dict[str, Any], period: str) -> int | None:
    """Goals so far that count toward this period, if we can see them on the board."""
    try:
        current = int(match.get("home_score") or 0) + int(match.get("away_score") or 0)
    except (TypeError, ValueError):
        current = 0
    state = str(match.get("state") or "")
    clock = f"{match.get('clock') or ''} {match.get('detail') or ''}".lower()
    if state != "in":
        return current if state == "post" and period == "ft" else None
    in_first = bool(re.search(r"1st|first", clock)) or (
        not re.search(r"2nd|second|\bht\b|half", clock)
        and (int(m.group(1)) if (m := re.search(r"(\d+)", clock)) else 0) <= 45
    )
    if period == "ft":
        return current
    if period == "1h":
        return current if in_first else None
    if period == "2h":
        # Board score is FT total. Without a half-time score we cannot know
        # how many of those goals belong to the second half.
        if in_first:
            return 0
        try:
            ht = int(match.get("ht_home") or 0) + int(match.get("ht_away") or 0)
        except (TypeError, ValueError):
            return None
        if match.get("ht_home") is None or match.get("ht_away") is None:
            return None
        return max(0, current - ht)
    return None


def evaluate_bet(
    spec: dict[str, Any],
    *,
    match: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    line = float(spec["line"])
    side = spec["side"]
    period = spec["period"]
    home = str((match or {}).get("home") or "")
    away = str((match or {}).get("away") or "")
    slug = str((match or {}).get("league_slug") or "")
    queried = spec["team"]
    home_is_query = bool(home) and name_matches(home, queried)
    named = home if home_is_query else (away if away and name_matches(away, queried) else queried)
    other = away if home_is_query else (home if away else "")
    so_far = live_goals(match, period) if match else None
    settled = None
    if so_far is not None:
        settled = _settle(so_far, line, side)
        # Mid-game: only "already decided" when an under is already over, or an over has already landed.
        if match and str(match.get("state") or "") == "in":
            if not ((side == "under" and settled == "lose") or (side == "over" and settled == "win")):
                settled = None
    return {
        "spec": spec,
        "match": match,
        "named": named,
        "other": other,
        "so_far": so_far,
        "settled": settled,
        "named_rate": sample_rate(records, line=line, side=side, period=period, team=named, league_slug=slug)
        if slug
        else sample_rate(records, line=line, side=side, period=period, team=named),
        "other_rate": sample_rate(records, line=line, side=side, period=period, team=other, league_slug=slug)
        if other and slug
        else {"n": 0, "wins": 0, "pushes": 0, "hit_pct": None, "fair_odds": None},
        "league_rate": sample_rate(records, line=line, side=side, period=period, league_slug=slug)
        if slug
        else {"n": 0, "wins": 0, "pushes": 0, "hit_pct": None, "fair_odds": None},
    }


def _rate_line(label: str, rate: dict[str, Any]) -> str:
    if not rate.get("n"):
        return f"{label}: no archive games yet"
    push = f" · {rate['pushes']} push" if rate.get("pushes") else ""
    fair = f" · fair **{rate['fair_odds']}**" if rate.get("fair_odds") else ""
    pct = f"**{rate['hit_pct']}%**" if rate.get("hit_pct") is not None else "—"
    return f"{label}: {pct} ({rate['wins']}/{rate['n']}{push}){fair}"


def format_bet(payload: dict[str, Any]) -> str:
    spec = payload["spec"]
    side = spec["side"]
    line = spec["line"]
    period = spec["period"]
    market = f"{side.title()} {line:g} {_PERIOD_LABEL[period]}"
    match = payload.get("match")
    if not match:
        lines = [
            f"**Bet** · {market} · no fixture on today's board for **{spec['team']}**",
            _rate_line(payload["named"], payload["named_rate"]),
            "That's the club's own archive across leagues. Pin it to a game with a name from `!soc live` / `!soc upcoming`.",
        ]
        return "\n".join(lines)
    home = match.get("home") or "?"
    away = match.get("away") or "?"
    chiclet = match.get("league_chiclet") or match.get("league_slug") or ""
    state = str(match.get("state") or "")
    if state == "in":
        when = match.get("clock") or "LIVE"
    elif state == "post":
        when = "FT"
    else:
        when = str(match.get("clock") or "KO")
    lines = [
        f"**Bet** · {market} · {home} vs {away} · {chiclet} · `{when}`",
        _rate_line(payload["named"], payload["named_rate"]),
    ]
    if payload.get("other"):
        lines.append(_rate_line(payload["other"], payload["other_rate"]))
    lines.append(_rate_line("League", payload["league_rate"]))
    so_far = payload.get("so_far")
    if so_far is not None and state == "in":
        lines.append(f"This game so far: **{so_far}** goal{'s' if so_far != 1 else ''} toward this line.")
        if payload.get("settled") == "lose":
            lines.append("Already over the line — the under is dead.")
        elif payload.get("settled") == "win":
            lines.append("Already over the line — the over has landed.")
    named = payload["named_rate"]
    if named.get("fair_odds"):
        lines.append(
            f"Take **{side} {line:g}** only if the book pays more than {named['fair_odds']} "
            f"(that's {payload['named']}'s own archive). Not a tip — a hit rate."
        )
    return "\n".join(lines)


USAGE = "Usage: `!soc bets TEAM [under|over] LINE [1h|2h|ft]`   e.g. `!soc bets Liverpool 1.5 1h`"
