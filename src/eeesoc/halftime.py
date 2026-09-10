"""
0-0 first-half archive.

Every finished match the dashboard sees (or backfills from ESPN) is stored on
disk with its event strip and xG series.  From that archive we can:

* list the games that were 0-0 at half time, broken out by league;
* profile what those halves looked like (80% bands for shots / SOT / xG,
  what happened after the break);
* score a live 0-0 first half against the archive to find its closest
  lookalikes and what they turned into.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from eeesoc.cache import cache_root, read_json, write_json
from eeesoc.live import (
    LEAGUES,
    SITE_SCOREBOARD,
    LiveMatch,
    _fetch_json,
    build_event_timeline,
    parse_scoreboard,
    parse_start,
)

HALF_MINUTE = 45
CHECKPOINTS = (15, 30, 45)
ARCHIVE_DIR = "halftime"
# A half with almost no plays is missing data, not a quiet game.
MIN_EVENTS_FOR_TRUST = 6
# Similarity distance → match percentage: 100·exp(-d / SCALE).
_MATCH_SCALE = 2.4

# (weight, scale) per per-side feature — one extra shot on target costs more
# than one extra corner, and xG carries the most signal.
_FEATURE_WEIGHTS: dict[str, tuple[float, float]] = {
    "shots": (1.0, 3.0),
    "sot": (1.3, 2.0),
    "blocked": (0.4, 2.0),
    "corners": (0.5, 3.0),
    "xg": (2.2, 0.5),
}
_CHECKPOINT_WEIGHT = (0.8, 0.4)

_records_lock = threading.Lock()
_records_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_backfill_lock = threading.Lock()
_backfill_state: dict[str, Any] = {
    "running": False,
    "days_back": 0,
    "scanned": 0,
    "archived": 0,
    "skipped": 0,
    "errors": [],
    "started_at": None,
    "finished_at": None,
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def archive_dir() -> Path:
    return cache_root() / ARCHIVE_DIR


def record_path(league_slug: str, event_id: str) -> Path:
    safe_league = str(league_slug).replace("/", "_")
    safe_event = str(event_id).replace("/", "_")
    return archive_dir() / safe_league / f"{safe_event}.json"


def has_record(league_slug: str, event_id: str) -> bool:
    return record_path(league_slug, event_id).is_file()


def _in_first_half(ev: dict[str, Any], minute: int) -> bool:
    """Event belongs to the first ``minute`` minutes (period wins over the minute label)."""
    m = ev.get("minute")
    if m is None:
        return False
    period = ev.get("period")
    if minute >= HALF_MINUTE:
        if period is not None:
            return int(period) == 1
        return int(m) <= HALF_MINUTE
    if period is not None and int(period) != 1:
        return False
    return int(m) <= minute


def _second_half_goals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for ev in events:
        if ev.get("kind") not in {"goal", "own_goal"}:
            continue
        if _in_first_half(ev, HALF_MINUTE):
            continue
        out.append(
            {
                "minute": ev.get("minute"),
                "clock": ev.get("clock") or f"{ev.get('minute')}'",
                "team": ev.get("team"),
                "kind": ev.get("kind"),
                "player": ev.get("player"),
            }
        )
    out.sort(key=lambda g: (int(g.get("minute") or 0), str(g.get("clock") or "")))
    return out


def has_xg(events: list[dict[str, Any]]) -> bool:
    """ESPN publishes expectedGoals for most but not all leagues (e.g. Eredivisie / Primeira lack it)."""
    return any((ev.get("xg") or 0) > 0 for ev in events or [])


def side_features(
    events: list[dict[str, Any]],
    *,
    minute: int = HALF_MINUTE,
) -> dict[str, Any]:
    """
    Per-side attacking counts + xG up to ``minute`` (≤45 → inside the first half).

    ``xg_at`` holds cumulative xG at each 15' checkpoint reached so far, which
    lets the similarity score compare the *shape* of a half, not just totals.
    """
    minute = max(1, min(HALF_MINUTE, int(minute)))
    sides: dict[str, dict[str, Any]] = {
        s: {"shots": 0, "sot": 0, "blocked": 0, "corners": 0, "goals": 0, "xg": 0.0, "xg_at": {}}
        for s in ("home", "away")
    }
    xg_pts: dict[str, list[tuple[int, float]]] = {"home": [], "away": []}
    for ev in events or []:
        side = ev.get("team")
        if side not in sides or not _in_first_half(ev, minute):
            continue
        kind = ev.get("kind")
        row = sides[side]
        if kind in {"shot", "shot_on", "blocked", "goal"}:
            row["shots"] += 1
        if kind in {"shot_on", "goal"}:
            row["sot"] += 1
        if kind == "blocked":
            row["blocked"] += 1
        if kind == "corner":
            row["corners"] += 1
        if kind in {"goal", "own_goal"}:
            row["goals"] += 1
        xg = ev.get("xg")
        if xg is not None and kind != "own_goal":
            try:
                xg_pts[side].append((int(ev.get("minute") or 0), float(xg)))
            except (TypeError, ValueError):
                pass
    for side, row in sides.items():
        pts = xg_pts[side]
        row["xg"] = round(sum(x for _, x in pts), 3)
        for cp in CHECKPOINTS:
            if cp > minute:
                break
            row["xg_at"][str(cp)] = round(sum(x for m, x in pts if m <= cp), 3)
    total = {
        key: round(sides["home"][key] + sides["away"][key], 3)
        for key in ("shots", "sot", "blocked", "corners", "goals", "xg")
    }
    return {
        "minute": minute,
        "home": sides["home"],
        "away": sides["away"],
        "total": total,
        "has_xg": has_xg(events),
    }


def record_from_timeline(tl: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Compact archive row for a finished match timeline; None when there is no play data."""
    meta = meta or {}
    events = list(tl.get("events") or [])
    if len(events) < MIN_EVENTS_FOR_TRUST:
        return None
    ht = side_features(events, minute=HALF_MINUTE)
    ft_home = int(tl.get("home_score") if tl.get("home_score") is not None else meta.get("home_score") or 0)
    ft_away = int(tl.get("away_score") if tl.get("away_score") is not None else meta.get("away_score") or 0)
    start = str(meta.get("start") or tl.get("start") or "")
    started = parse_start(start)
    trimmed_events = [
        {
            "minute": ev.get("minute"),
            "kind": ev.get("kind"),
            "team": ev.get("team"),
            "xg": ev.get("xg"),
            "player": ev.get("player"),
            "clock": ev.get("clock"),
            "period": ev.get("period"),
            "text": ev.get("text"),
        }
        for ev in events
    ]
    xg = tl.get("xg") or {}
    return {
        "event_id": str(tl.get("event_id") or meta.get("event_id") or ""),
        "league_slug": str(tl.get("league_slug") or meta.get("league_slug") or ""),
        "league_chiclet": str(meta.get("league_chiclet") or ""),
        "league_name": str(meta.get("league_name") or ""),
        "home": str(tl.get("home") or meta.get("home") or ""),
        "away": str(tl.get("away") or meta.get("away") or ""),
        "home_id": str(meta.get("home_id") or ""),
        "away_id": str(meta.get("away_id") or ""),
        "start": start,
        "date": started.date().isoformat() if started else "",
        "ft_home": ft_home,
        "ft_away": ft_away,
        "ht_home": int(ht["home"]["goals"]),
        "ht_away": int(ht["away"]["goals"]),
        "ht_goals": int(ht["total"]["goals"]),
        "n_events": len(events),
        "has_xg": has_xg(events),
        "first_half": ht,
        "second_half_goals": _second_half_goals(events),
        "events": trimmed_events,
        "xg": {
            "home": list(xg.get("home") or []),
            "away": list(xg.get("away") or []),
            "home_total": xg.get("home_total") or 0.0,
            "away_total": xg.get("away_total") or 0.0,
        },
        "archived_at": time.time(),
    }


def archive_timeline(tl: dict[str, Any], meta: dict[str, Any] | None = None, *, force: bool = False) -> dict[str, Any] | None:
    """Persist a finished timeline once; returns the stored record (or None when skipped)."""
    if not tl or not tl.get("final"):
        return None
    league = str(tl.get("league_slug") or (meta or {}).get("league_slug") or "")
    event_id = str(tl.get("event_id") or (meta or {}).get("event_id") or "")
    if not league or not event_id:
        return None
    path = record_path(league, event_id)
    if path.is_file() and not force:
        return None
    rec = record_from_timeline(tl, meta)
    if rec is None:
        return None
    write_json(path, rec)
    with _records_lock:
        _records_cache[str(path)] = (path.stat().st_mtime, rec)
    return rec


def load_records(leagues: set[str] | None = None) -> list[dict[str, Any]]:
    """All archived rows (mtime-cached), optionally limited to league slugs."""
    root = archive_dir()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for league_dir in sorted(root.iterdir()):
        if not league_dir.is_dir():
            continue
        if leagues is not None and league_dir.name not in leagues:
            continue
        for path in sorted(league_dir.glob("*.json")):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            key = str(path)
            with _records_lock:
                hit = _records_cache.get(key)
            if hit and hit[0] == mtime:
                out.append(hit[1])
                continue
            try:
                rec = read_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            with _records_lock:
                _records_cache[key] = (mtime, rec)
            out.append(rec)
    return out


def clear_record_cache() -> None:
    with _records_lock:
        _records_cache.clear()


# ---------------------------------------------------------------------------
# 0-0 profile
# ---------------------------------------------------------------------------


def _record_has_xg(rec: dict[str, Any]) -> bool:
    flag = rec.get("has_xg")
    if flag is None:
        flag = has_xg(rec.get("events") or [])
    return bool(flag)


def zero_zero_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if int(r.get("ht_goals") or 0) == 0 and int(r.get("n_events") or 0) >= MIN_EVENTS_FOR_TRUST
    ]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _band(values: list[float], *, digits: int = 2) -> dict[str, float]:
    if not values:
        return {"p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0, "mean": 0.0}
    return {
        "p10": round(_percentile(values, 10), digits),
        "p25": round(_percentile(values, 25), digits),
        "p50": round(_percentile(values, 50), digits),
        "p75": round(_percentile(values, 75), digits),
        "p90": round(_percentile(values, 90), digits),
        "mean": round(mean(values), digits),
    }


def _pct(n: int, total: int) -> int:
    return int(round(100.0 * n / total)) if total else 0


def _ft_outcome(rec: dict[str, Any]) -> str:
    h, a = int(rec.get("ft_home") or 0), int(rec.get("ft_away") or 0)
    if h > a:
        return "home"
    if a > h:
        return "away"
    return "draw"


def _first_2h_goal_bucket(rec: dict[str, Any]) -> str | None:
    goals = rec.get("second_half_goals") or []
    if not goals:
        return None
    m = int(goals[0].get("minute") or 0)
    if m <= 60:
        return "46-60"
    if m <= 75:
        return "61-75"
    return "76-90+"


def profile_zero_zero(records: list[dict[str, Any]], *, minute: int = HALF_MINUTE) -> dict[str, Any]:
    """
    What a 0-0 first half looks like, as 80% bands (p10–p90) plus outcomes.

    ``minute`` < 45 re-cuts each half at that minute so a live game at 27'
    is compared against the same 27 minutes of every archived 0-0 half.
    """
    rows = zero_zero_records(records)
    n = len(rows)
    if not n:
        return {"n": 0, "minute": minute}
    feats = [
        r["first_half"] if minute >= HALF_MINUTE and r.get("first_half") else side_features(r.get("events") or [], minute=minute)
        for r in rows
    ]
    # xG bands only over halves where ESPN actually published xG — a league
    # without the feed must not read as "0.00 xG".
    xg_feats = [f for r, f in zip(rows, feats) if _record_has_xg(r)]
    totals = {
        "shots": [float(f["total"]["shots"]) for f in feats],
        "sot": [float(f["total"]["sot"]) for f in feats],
        "blocked": [float(f["total"]["blocked"]) for f in feats],
        "corners": [float(f["total"]["corners"]) for f in feats],
        "xg": [float(f["total"]["xg"]) for f in xg_feats],
    }
    bands = {
        key: _band(vals, digits=2 if key == "xg" else 1) for key, vals in totals.items()
    }
    sides = {
        side: {
            **{
                key: round(mean(float(f[side][key]) for f in feats), 2)
                for key in ("shots", "sot", "blocked", "corners")
            },
            "xg": round(mean(float(f[side]["xg"]) for f in xg_feats), 2) if xg_feats else 0.0,
        }
        for side in ("home", "away")
    }
    xg_curve = []
    for cp in CHECKPOINTS:
        if cp > minute:
            break
        vals = [
            float(f["home"]["xg_at"].get(str(cp), 0.0)) + float(f["away"]["xg_at"].get(str(cp), 0.0))
            for f in xg_feats
        ]
        xg_curve.append({"minute": cp, **_band(vals, digits=2)})

    outcomes = {o: sum(1 for r in rows if _ft_outcome(r) == o) for o in ("home", "draw", "away")}
    ended_0_0 = sum(1 for r in rows if int(r.get("ft_home") or 0) == 0 and int(r.get("ft_away") or 0) == 0)
    any_2h = sum(1 for r in rows if r.get("second_half_goals"))
    total_2h = sum(len(r.get("second_half_goals") or []) for r in rows)
    over_1_5 = sum(1 for r in rows if int(r.get("ft_home") or 0) + int(r.get("ft_away") or 0) >= 2)
    buckets = {"46-60": 0, "61-75": 0, "76-90+": 0}
    for r in rows:
        b = _first_2h_goal_bucket(r)
        if b:
            buckets[b] += 1
    # Who broke the deadlock first — did the side with more first-half xG score first?
    xg_leader_scored_first = 0
    with_first_goal = 0
    for r, f in zip(rows, feats):
        goals = r.get("second_half_goals") or []
        if not goals:
            continue
        with_first_goal += 1
        hx, ax = float(f["home"]["xg"]), float(f["away"]["xg"])
        leader = "home" if hx > ax else "away" if ax > hx else None
        first = goals[0]
        scorer = first.get("team")
        if leader and scorer == leader:
            xg_leader_scored_first += 1

    return {
        "n": n,
        "n_xg": len(xg_feats),
        "minute": minute,
        "bands": bands,
        "sides": sides,
        "xg_curve": xg_curve,
        "outcomes": {
            "home_win_pct": _pct(outcomes["home"], n),
            "draw_pct": _pct(outcomes["draw"], n),
            "away_win_pct": _pct(outcomes["away"], n),
            "ended_0_0_pct": _pct(ended_0_0, n),
            "any_2h_goal_pct": _pct(any_2h, n),
            "over_1_5_pct": _pct(over_1_5, n),
            "avg_2h_goals": round(total_2h / n, 2),
            "first_2h_goal_pct": {k: _pct(v, n) for k, v in buckets.items()},
            "xg_leader_scored_first_pct": _pct(xg_leader_scored_first, with_first_goal),
            "with_first_goal": with_first_goal,
        },
    }


def _trim_record_for_client(rec: dict[str, Any]) -> dict[str, Any]:
    first_half_events = [ev for ev in rec.get("events") or [] if _in_first_half(ev, HALF_MINUTE)]
    xg = rec.get("xg") or {}

    def _cut(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [p for p in series if int(p.get("minute") or 0) <= HALF_MINUTE]

    home_series = _cut(list(xg.get("home") or []))
    away_series = _cut(list(xg.get("away") or []))
    return {
        "event_id": rec.get("event_id"),
        "league_slug": rec.get("league_slug"),
        "league_chiclet": rec.get("league_chiclet"),
        "league_name": rec.get("league_name"),
        "home": rec.get("home"),
        "away": rec.get("away"),
        "start": rec.get("start"),
        "date": rec.get("date"),
        "ft_home": rec.get("ft_home"),
        "ft_away": rec.get("ft_away"),
        "ht_home": rec.get("ht_home"),
        "ht_away": rec.get("ht_away"),
        "has_xg": _record_has_xg(rec),
        "first_half": rec.get("first_half"),
        "second_half_goals": rec.get("second_half_goals") or [],
        # Chiclet-shaped timeline payload, cut at 45'.
        "timeline": {
            "event_id": rec.get("event_id"),
            "home": rec.get("home"),
            "away": rec.get("away"),
            "minute": HALF_MINUTE,
            "elapsed_seconds": HALF_MINUTE * 60,
            "frozen": True,
            "final": True,
            "max_minute": 90,
            "view_max_minute": HALF_MINUTE,
            "home_score": rec.get("ht_home"),
            "away_score": rec.get("ht_away"),
            "events": first_half_events,
            "xg": {
                "home": home_series,
                "away": away_series,
                "home_total": home_series[-1]["cumulative"] if home_series else 0.0,
                "away_total": away_series[-1]["cumulative"] if away_series else 0.0,
            },
        },
    }


def zero_zero_board(
    records: list[dict[str, Any]] | None = None,
    *,
    leagues: list[tuple[str, str]] | None = None,
    league_filter: set[str] | None = None,
) -> dict[str, Any]:
    """Archive rows that were 0-0 at HT, grouped by league, with per-league + overall profiles."""
    league_list = leagues or LEAGUES
    records = load_records() if records is None else records
    zeros = zero_zero_records(records)
    order = {slug: i for i, (slug, _) in enumerate(league_list)}
    labels = dict(league_list)

    groups: dict[str, dict[str, Any]] = {}
    for rec in zeros:
        slug = str(rec.get("league_slug") or "")
        bucket = groups.setdefault(
            slug,
            {
                "slug": slug,
                "name": rec.get("league_name") or labels.get(slug, slug),
                "chiclet": rec.get("league_chiclet") or labels.get(slug, slug),
                "records": [],
                "matches": [],
            },
        )
        bucket["records"].append(rec)

    chiclets = [
        {
            "slug": slug,
            "label": label,
            "count": len(groups.get(slug, {}).get("records", [])),
            "archived": sum(1 for r in records if r.get("league_slug") == slug),
        }
        for slug, label in league_list
    ]
    # Leagues we archived that are not in the configured list still get a chiclet.
    for slug, bucket in groups.items():
        if slug not in order:
            chiclets.append(
                {
                    "slug": slug,
                    "label": bucket["chiclet"],
                    "count": len(bucket["records"]),
                    "archived": sum(1 for r in records if r.get("league_slug") == slug),
                }
            )

    selected_slugs = league_filter if league_filter else None
    grouped = []
    for slug, bucket in sorted(groups.items(), key=lambda kv: order.get(kv[0], 999)):
        if selected_slugs is not None and slug not in selected_slugs:
            continue
        rows = sorted(bucket["records"], key=lambda r: str(r.get("start") or ""), reverse=True)
        grouped.append(
            {
                "slug": slug,
                "name": bucket["name"],
                "chiclet": bucket["chiclet"],
                "profile": profile_zero_zero(rows),
                "matches": [_trim_record_for_client(r) for r in rows],
            }
        )

    selected_records = [
        r for r in zeros if selected_slugs is None or str(r.get("league_slug") or "") in selected_slugs
    ]
    with _backfill_lock:
        backfill = dict(_backfill_state)
    return {
        "archive_total": len(records),
        "zero_total": len(zeros),
        "selected_total": len(selected_records),
        "chiclets": chiclets,
        "leagues": grouped,
        "profile": profile_zero_zero(selected_records),
        "backfill": backfill,
        "fetched_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Live 0-0 half vs archive
# ---------------------------------------------------------------------------


def live_cut_minute(tl: dict[str, Any]) -> int:
    """Minute at which to cut a live timeline (never past half time)."""
    clock = str(tl.get("clock") or "").lower()
    if tl.get("final") or "ht" in clock or "half" in clock:
        return HALF_MINUTE
    minute = int(tl.get("minute") or 1)
    return max(1, min(HALF_MINUTE, minute))


def is_zero_zero_first_half(tl: dict[str, Any]) -> bool:
    """Live timeline still 0-0 and not yet into the second half."""
    if int(tl.get("home_score") or 0) or int(tl.get("away_score") or 0):
        return False
    if tl.get("final"):
        return False
    events = tl.get("events") or []
    if any(ev.get("kind") in {"goal", "own_goal"} for ev in events):
        return False
    if any(ev.get("period") not in (None, 1) for ev in events):
        return False
    clock = str(tl.get("clock") or "").lower()
    if "ht" in clock or "half" in clock:
        return True
    return int(tl.get("minute") or 1) <= HALF_MINUTE


def feature_distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    """
    Weighted L1 distance between two cut-feature sets.

    When either half has no xG feed the xG terms are dropped and the rest is
    rescaled to the full weight, so xG-less leagues are still comparable on
    shots / SOT / blocked / corners instead of looking artificially quiet.
    """
    use_xg = bool(a.get("has_xg", True)) and bool(b.get("has_xg", True))
    d = 0.0
    weight_used = 0.0
    weight_full = 0.0
    for side in ("home", "away"):
        fa, fb = a[side], b[side]
        for key, (weight, scale) in _FEATURE_WEIGHTS.items():
            weight_full += weight
            if key == "xg" and not use_xg:
                continue
            weight_used += weight
            d += weight * abs(float(fa[key]) - float(fb[key])) / scale
        w, s = _CHECKPOINT_WEIGHT
        for cp, va in (fa.get("xg_at") or {}).items():
            weight_full += w
            if not use_xg:
                continue
            weight_used += w
            vb = (fb.get("xg_at") or {}).get(cp, 0.0)
            d += w * abs(float(va) - float(vb)) / s
    if weight_used and weight_used < weight_full:
        d *= weight_full / weight_used
    return round(d, 4)


def match_pct(distance: float) -> int:
    return int(round(100.0 * math.exp(-max(0.0, distance) / _MATCH_SCALE)))


def _rank_pct(value: float, population: list[float]) -> int:
    """Share of the population strictly below ``value`` (0–100)."""
    if not population:
        return 0
    below = sum(1 for v in population if v < value)
    return _pct(below, len(population))


def similar_zero_zero(
    live_tl: dict[str, Any],
    records: list[dict[str, Any]] | None = None,
    *,
    limit: int = 8,
    league_filter: set[str] | None = None,
) -> dict[str, Any]:
    """
    Closest archived 0-0 first halves to a live one, cut at the live minute.

    Returns the ranked lookalikes with a match %, where the live half sits in
    the archive's shot / xG distribution, and what the lookalikes turned into.
    """
    records = load_records() if records is None else records
    zeros = zero_zero_records(records)
    if league_filter:
        zeros = [r for r in zeros if str(r.get("league_slug") or "") in league_filter]
    minute = live_cut_minute(live_tl)
    live_feat = side_features(live_tl.get("events") or [], minute=minute)
    # Before the first shot a live half has no xG *yet*; whether the league has an
    # xG feed at all is answered by its archived games.
    league = str(live_tl.get("league_slug") or "")
    league_rows = [r for r in records if str(r.get("league_slug") or "") == league]
    league_xg_feed = any(_record_has_xg(r) for r in league_rows) if league_rows else None
    if league_xg_feed is not None:
        live_feat["has_xg"] = bool(league_xg_feed)
    scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for rec in zeros:
        feat = side_features(rec.get("events") or [], minute=minute)
        scored.append((feature_distance(live_feat, feat), rec, feat))
    # Ties (common early in a half) go to the most recent game.
    scored.sort(key=lambda t: str(t[1].get("start") or ""), reverse=True)
    scored.sort(key=lambda t: t[0])
    top = scored[: max(1, int(limit))]
    lookalikes = []
    for dist, rec, feat in top:
        row = _trim_record_for_client(rec)
        row["cut"] = feat
        row["distance"] = dist
        row["match_pct"] = match_pct(dist)
        lookalikes.append(row)
    top_records = [rec for _, rec, _ in top]
    population = profile_zero_zero(zeros, minute=minute)
    pop_shots = [float(f["total"]["shots"]) for _, _, f in scored]
    pop_xg = [float(f["total"]["xg"]) for _, rec, f in scored if _record_has_xg(rec)]
    pop_sot = [float(f["total"]["sot"]) for _, _, f in scored]
    return {
        "event_id": live_tl.get("event_id"),
        "league_slug": live_tl.get("league_slug"),
        "home": live_tl.get("home"),
        "away": live_tl.get("away"),
        "minute": minute,
        "is_zero_zero_first_half": is_zero_zero_first_half(live_tl),
        "live": live_feat,
        "league_xg_feed": league_xg_feed,
        "archive_n": len(zeros),
        "population": population,
        "rank": {
            "shots_pct": _rank_pct(float(live_feat["total"]["shots"]), pop_shots),
            "sot_pct": _rank_pct(float(live_feat["total"]["sot"]), pop_sot),
            "xg_pct": _rank_pct(float(live_feat["total"]["xg"]), pop_xg) if live_feat["has_xg"] else None,
            "xg_n": len(pop_xg),
        },
        "lookalikes": lookalikes,
        "lookalike_outcomes": profile_zero_zero(top_records).get("outcomes") if top_records else None,
        "avg_match_pct": int(round(mean(r["match_pct"] for r in lookalikes))) if lookalikes else 0,
    }


# ---------------------------------------------------------------------------
# Backfill from ESPN
# ---------------------------------------------------------------------------


def _scoreboard_day_url(league_slug: str, day: date) -> str:
    quoted = urllib.parse.quote(league_slug, safe=".")
    return f"{SITE_SCOREBOARD.format(league=quoted)}?dates={day.strftime('%Y%m%d')}"


def finished_matches_for_day(
    league_slug: str,
    chiclet: str,
    day: date,
    *,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> list[LiveMatch]:
    fetch = fetcher or _fetch_json
    payload = fetch(_scoreboard_day_url(league_slug, day))
    return [m for m in parse_scoreboard(payload, league_slug, chiclet) if m.state == "post"]


def archive_match(
    m: LiveMatch,
    *,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Fetch the play-by-play for a finished match and archive it (skips existing rows)."""
    if has_record(m.league_slug, m.event_id) and not force:
        return None
    tl = build_event_timeline(
        m.league_slug,
        m.event_id,
        home=m.home,
        away=m.away,
        home_id=m.home_id,
        away_id=m.away_id,
        clock="FT",
        home_score=m.home_score,
        away_score=m.away_score,
        fetcher=fetcher,
        use_cache=False,
    )
    meta = {
        **m.to_dict(),
        "league_chiclet": m.league_chiclet,
        "league_name": m.league_name,
    }
    return archive_timeline(tl, meta, force=force)


def backfill(
    *,
    days_back: int = 14,
    leagues: list[tuple[str, str]] | None = None,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    workers: int = 4,
    today: date | None = None,
) -> dict[str, Any]:
    """Archive every finished match in the last ``days_back`` days across the leagues."""
    league_list = leagues or LEAGUES
    today = today or datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(0, max(0, int(days_back)) + 1)]
    with _backfill_lock:
        if _backfill_state["running"]:
            return {**_backfill_state, "busy": True}
        _backfill_state.update(
            {
                "running": True,
                "days_back": int(days_back),
                "scanned": 0,
                "archived": 0,
                "skipped": 0,
                "errors": [],
                "started_at": time.time(),
                "finished_at": None,
            }
        )

    candidates: dict[str, LiveMatch] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(finished_matches_for_day, slug, label, day, fetcher=fetcher): (slug, day)
            for slug, label in league_list
            for day in days
        }
        for fut in as_completed(futures):
            slug, day = futures[fut]
            try:
                for m in fut.result():
                    candidates.setdefault(f"{m.league_slug}:{m.event_id}", m)
            except Exception as exc:  # noqa: BLE001 — one bad day must not stop the sweep
                errors.append(f"{slug} {day.isoformat()}: {exc}")

    todo = [m for m in candidates.values() if not has_record(m.league_slug, m.event_id)]
    skipped = len(candidates) - len(todo)
    with _backfill_lock:
        _backfill_state.update({"scanned": len(candidates), "skipped": skipped, "errors": errors[:20]})

    archived = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(archive_match, m, fetcher=fetcher): m for m in todo}
        for fut in as_completed(futures):
            m = futures[fut]
            try:
                if fut.result() is not None:
                    archived += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{m.league_slug} {m.event_id}: {exc}")
            with _backfill_lock:
                _backfill_state["archived"] = archived
                _backfill_state["errors"] = errors[:20]

    with _backfill_lock:
        _backfill_state.update({"running": False, "finished_at": time.time()})
        return dict(_backfill_state)


def backfill_status() -> dict[str, Any]:
    with _backfill_lock:
        return dict(_backfill_state)


def start_backfill_thread(*, days_back: int = 14, leagues: list[tuple[str, str]] | None = None) -> bool:
    """Kick off a background backfill; False when one is already running."""
    with _backfill_lock:
        if _backfill_state["running"]:
            return False

    def _run() -> None:
        try:
            backfill(days_back=days_back, leagues=leagues)
        except Exception as exc:  # noqa: BLE001 — never take the server down
            with _backfill_lock:
                _backfill_state.update(
                    {"running": False, "finished_at": time.time(), "errors": [f"backfill: {exc}"]}
                )

    threading.Thread(target=_run, name="eeesoc-halftime-backfill", daemon=True).start()
    return True


def start_backfill_loop(*, initial_days_back: int = 14, refresh_days_back: int = 2, every_s: float = 6 * 3600) -> None:
    """Initial sweep at boot, then top up recent days on a timer."""

    def _loop() -> None:
        first = True
        while True:
            days = initial_days_back if first else refresh_days_back
            first = False
            try:
                backfill(days_back=days)
            except Exception as exc:  # noqa: BLE001
                with _backfill_lock:
                    _backfill_state.update(
                        {"running": False, "finished_at": time.time(), "errors": [f"backfill: {exc}"]}
                    )
            time.sleep(every_s)

    threading.Thread(target=_loop, name="eeesoc-halftime-backfill-loop", daemon=True).start()
