"""Map ESPN live team names/ids onto football-data.co.uk club names."""

from __future__ import annotations

import re
from typing import Iterable

# Canonical football-data.co.uk EPL names we care about.
FD_TEAMS: frozenset[str] = frozenset(
    {
        "Arsenal",
        "Aston Villa",
        "Bournemouth",
        "Brentford",
        "Brighton",
        "Burnley",
        "Chelsea",
        "Crystal Palace",
        "Everton",
        "Fulham",
        "Ipswich",
        "Leeds",
        "Leicester",
        "Liverpool",
        "Man City",
        "Man United",
        "Newcastle",
        "Nott'm Forest",
        "Southampton",
        "Sunderland",
        "Tottenham",
        "West Ham",
        "Wolves",
    }
)

# ESPN display / short / nicknames → FD canonical.
_ALIASES: dict[str, str] = {
    # exact FD
    **{t.lower(): t for t in FD_TEAMS},
    # ESPN full / short
    "nottingham forest": "Nott'm Forest",
    "nottm forest": "Nott'm Forest",
    "nott'm forest": "Nott'm Forest",
    "forest": "Nott'm Forest",
    "manchester united": "Man United",
    "man utd": "Man United",
    "manchester city": "Man City",
    "tottenham hotspur": "Tottenham",
    "spurs": "Tottenham",
    "newcastle united": "Newcastle",
    "afc bournemouth": "Bournemouth",
    "brighton & hove albion": "Brighton",
    "brighton and hove albion": "Brighton",
    "wolverhampton wanderers": "Wolves",
    "wolverhampton": "Wolves",
    "west ham united": "West Ham",
    "leeds united": "Leeds",
    "leicester city": "Leicester",
    "ipswich town": "Ipswich",
    "southampton fc": "Southampton",
    "crystal palace": "Crystal Palace",
    "aston villa": "Aston Villa",
}

# ESPN soccer team ids (eng.1) → FD — stable when display names drift.
ESPN_ID_TO_FD: dict[str, str] = {
    "359": "Arsenal",
    "362": "Aston Villa",
    "349": "Bournemouth",
    "337": "Brentford",
    "331": "Brighton",
    "379": "Burnley",
    "363": "Chelsea",
    "384": "Crystal Palace",
    "368": "Everton",
    "370": "Fulham",
    "373": "Ipswich",
    "357": "Leeds",
    "375": "Leicester",
    "364": "Liverpool",
    "382": "Man City",
    "360": "Man United",
    "361": "Newcastle",
    "393": "Nott'm Forest",
    "376": "Southampton",
    "366": "Sunderland",
    "367": "Tottenham",
    "371": "West Ham",
    "380": "Wolves",
}

_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _norm(name: str) -> str:
    s = name.strip().lower()
    s = s.replace("&", " and ")
    s = _STRIP_RE.sub(" ", s)
    return " ".join(s.split())


def resolve_team(name: str | None = None, *, espn_id: str | None = None) -> str | None:
    """Resolve an ESPN name/id to a football-data canonical club name."""
    if espn_id:
        mapped = ESPN_ID_TO_FD.get(str(espn_id).strip())
        if mapped:
            return mapped
    if not name:
        return None
    raw = name.strip()
    if raw in FD_TEAMS:
        return raw
    key = raw.lower()
    if key in _ALIASES:
        return _ALIASES[key]
    n = _norm(raw)
    if n in _ALIASES:
        return _ALIASES[n]
    return None


def team_matches(match_home: str, match_away: str, team: str) -> str | None:
    """Return 'home' / 'away' if ``team`` (FD name) plays in this match, else None."""
    if match_home == team:
        return "home"
    if match_away == team:
        return "away"
    return None


def known_teams(corpus_names: Iterable[str]) -> list[str]:
    return sorted({n for n in corpus_names if n in FD_TEAMS})
