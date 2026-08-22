"""Warm and load Premier League match corpora from football-data.co.uk."""

from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.request
from typing import Iterable

from eeesoc.cache import cache_path, read_json, write_json
from eeesoc.models import GoalEvent, Match
from eeesoc.timeline import build_timelines

FD_BASE = "https://www.football-data.co.uk/mmz4281/{code}/E0.csv"
USER_AGENT = "eeesoc/0.1 (+https://github.com/tekneeq/eeesoc)"

# EPL:YYYY → football-data season folder (season starting YYYY)
_SEASON_RE = re.compile(r"^EPL:(\d{4})$", re.I)


def parse_warm_spec(spec: str) -> tuple[str, str]:
    """
    Return (label, football-data code).
    EPL:2025 → ('EPL:2025', '2526') for 2025/26.
    """
    m = _SEASON_RE.match(spec.strip())
    if not m:
        raise ValueError(f"Unsupported warm spec {spec!r}; expected EPL:YYYY")
    year = int(m.group(1))
    code = f"{year % 100:02d}{(year + 1) % 100:02d}"
    return f"EPL:{year}", code


def previous_season_label(label: str) -> str:
    m = _SEASON_RE.match(label)
    if not m:
        raise ValueError(label)
    year = int(m.group(1)) - 1
    return f"EPL:{year}"


def _fetch_csv(code: str) -> str:
    url = FD_BASE.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Failed to download {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to download {url}: {exc.reason}") from exc


def _safe_int(value: str | None, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def matches_from_csv(text: str, season_label: str) -> list[Match]:
    reader = csv.DictReader(io.StringIO(text))
    out: list[Match] = []
    for idx, row in enumerate(reader):
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        if not home or not away:
            continue
        date = (row.get("Date") or "").strip()
        match_id = f"{season_label}:{date}:{home}:{away}:{idx}"
        home_ft = _safe_int(row.get("FTHG"))
        away_ft = _safe_int(row.get("FTAG"))
        home_ht = _safe_int(row.get("HTHG"))
        away_ht = _safe_int(row.get("HTAG"))
        home_shots = _safe_int(row.get("HS"))
        away_shots = _safe_int(row.get("AS"))
        home_sot = _safe_int(row.get("HST"))
        away_sot = _safe_int(row.get("AST"))
        tl = build_timelines(
            match_id,
            home_ht,
            away_ht,
            home_ft,
            away_ft,
            home_shots,
            away_shots,
            home_sot,
            away_sot,
        )
        out.append(
            Match(
                match_id=match_id,
                season=season_label,
                date=date,
                home=home,
                away=away,
                home_goals_ft=home_ft,
                away_goals_ft=away_ft,
                home_shots_ft=home_shots,
                away_shots_ft=away_shots,
                home_sot_ft=home_sot,
                away_sot_ft=away_sot,
                home_goals_ht=home_ht,
                away_goals_ht=away_ht,
                goals=tl["goals"],
                home_shots_by_min=tl["home_shots_by_min"],
                away_shots_by_min=tl["away_shots_by_min"],
                home_sot_by_min=tl["home_sot_by_min"],
                away_sot_by_min=tl["away_sot_by_min"],
            )
        )
    return out


def inject_everton_preset(matches: list[Match], season_label: str) -> list[Match]:
    """
    Ensure an Everton 53' demo fixture exists:
    42'/53' · 12/4 vs 6/1
    """
    preset_id = f"{season_label}:preset:Everton:53"
    matches = [m for m in matches if m.match_id != preset_id]

    # Build exact cumulative series hitting 12/4 vs 6/1 at minute 53
    def ramp(targets: dict[int, tuple[int, int]], length: int = 91) -> tuple[list[int], list[int]]:
        shots = [0] * length
        sot = [0] * length
        s = st = 0
        checkpoints = sorted(targets)
        last = 0
        for minute in checkpoints:
            ts, tst = targets[minute]
            # linear fill between last+1 and minute
            span = max(1, minute - last)
            for m in range(last + 1, minute + 1):
                progress = (m - last) / span
                shots[m] = s + int(round((ts - s) * progress))
                sot[m] = st + int(round((tst - st) * progress))
            shots[minute], sot[minute] = ts, tst
            s, st = ts, tst
            last = minute
        for m in range(last + 1, 91):
            shots[m], sot[m] = s, st
        return shots, sot

    hs, hst = ramp({42: (8, 3), 53: (12, 4), 90: (14, 5)})
    as_, ast = ramp({42: (4, 1), 53: (6, 1), 90: (9, 2)})
    goals = [
        GoalEvent(minute=42, team="home"),
        GoalEvent(minute=53, team="home"),
    ]
    preset = Match(
        match_id=preset_id,
        season=season_label,
        date="01/01/2026",
        home="Everton",
        away="Demo United",
        home_goals_ft=2,
        away_goals_ft=0,
        home_shots_ft=14,
        away_shots_ft=9,
        home_sot_ft=5,
        away_sot_ft=2,
        home_goals_ht=1,
        away_goals_ht=0,
        goals=goals,
        home_shots_by_min=hs,
        away_shots_by_min=as_,
        home_sot_by_min=hst,
        away_sot_by_min=ast,
    )
    return [preset, *matches]


def season_cache_file(season_label: str):
    safe = season_label.replace(":", "_")
    return cache_path("seasons", f"{safe}.json")


def save_season(season_label: str, matches: Iterable[Match]) -> None:
    payload = {
        "season": season_label,
        "matches": [m.to_dict() for m in matches],
    }
    write_json(season_cache_file(season_label), payload)


def load_season(season_label: str) -> list[Match]:
    data = read_json(season_cache_file(season_label))
    if not data:
        return []
    return [Match.from_dict(m) for m in data.get("matches", [])]


def warm(spec: str, *, include_previous: bool = True) -> dict[str, int]:
    """Download and cache season (and previous season for Similar)."""
    label, code = parse_warm_spec(spec)
    counts: dict[str, int] = {}

    raw = _fetch_csv(code)
    matches = matches_from_csv(raw, label)
    matches = inject_everton_preset(matches, label)
    save_season(label, matches)
    counts[label] = len(matches)

    if include_previous:
        prev = previous_season_label(label)
        _, prev_code = parse_warm_spec(prev)
        prev_raw = _fetch_csv(prev_code)
        prev_matches = matches_from_csv(prev_raw, prev)
        save_season(prev, prev_matches)
        counts[prev] = len(prev_matches)

    return counts


def list_cached_seasons() -> list[str]:
    from eeesoc.cache import cache_root

    seasons_dir = cache_root() / "seasons"
    if not seasons_dir.is_dir():
        return []
    out: list[str] = []
    for path in sorted(seasons_dir.glob("*.json")):
        out.append(path.stem.replace("_", ":", 1))
    return out
