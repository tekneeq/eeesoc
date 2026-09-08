"""Kickoff / goal Discord alerts with Similar scoreline paths.

The bot polls the live board, remembers the last score per event, and posts
when a match goes ``pre → in`` or the score increases. Each post includes
the live path (0-0 → …) and each club's historical “from here” branches —
the same trees as the Similar tab.
"""
from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from eeesoc.cache import cache_root
from eeesoc.live import fetch_live_board, parse_clock_minute
from eeesoc.scorelines import build_live_scoreline_eval

_DEAD = ("postponed", "canceled", "cancelled", "suspended", "abandoned", "forfeit")
_DEFAULT_POLL_S = 20
_STATE_NAME = "discord-alerts.json"


def poll_seconds() -> float:
    raw = (os.getenv("EEESOC_DISCORD_POLL_S") or str(_DEFAULT_POLL_S)).strip()
    try:
        return max(8.0, min(120.0, float(raw)))
    except ValueError:
        return float(_DEFAULT_POLL_S)


def state_path() -> Path:
    override = (os.getenv("EEESOC_DISCORD_STATE") or "").strip()
    if override:
        return Path(override).expanduser()
    return cache_root() / _STATE_NAME


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _is_dead(match: dict[str, Any]) -> bool:
    blob = f"{match.get('detail') or ''} {match.get('clock') or ''} {match.get('state') or ''}".lower()
    return any(word in blob for word in _DEAD)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def flatten_board(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for group in board.get("leagues") or []:
        for match in group.get("matches") or []:
            if _is_dead(match):
                continue
            event_id = str(match.get("event_id") or "")
            if not event_id:
                continue
            rows[event_id] = {
                "event_id": event_id,
                "state": str(match.get("state") or ""),
                "home": match.get("home") or "?",
                "away": match.get("away") or "?",
                "home_score": _int(match.get("home_score")),
                "away_score": _int(match.get("away_score")),
                "clock": match.get("clock") or "",
                "detail": match.get("detail") or "",
                "start": match.get("start") or "",
                "home_id": str(match.get("home_id") or ""),
                "away_id": str(match.get("away_id") or ""),
                "league_slug": group.get("slug") or match.get("league_slug") or "",
                "league_chiclet": group.get("chiclet") or match.get("league_chiclet") or "",
                "league_name": group.get("name") or match.get("league_name") or "",
            }
    return rows


def _score_key(row: dict[str, Any]) -> tuple[int, int]:
    return _int(row.get("home_score")), _int(row.get("away_score"))


def _score_label(home: int, away: int) -> str:
    return f"{home}-{away}"


def _tracked(row: dict[str, Any], path: list[str] | None = None) -> dict[str, Any]:
    hs, aws = _score_key(row)
    steps = list(path or [])
    label = _score_label(hs, aws)
    if not steps or steps[-1] != label:
        if not steps:
            steps = ["0-0"]
        if steps[-1] != label:
            steps.append(label)
    return {
        "state": row.get("state") or "",
        "home": row.get("home") or "",
        "away": row.get("away") or "",
        "home_score": hs,
        "away_score": aws,
        "path": steps,
    }


@dataclass(frozen=True)
class Alert:
    kind: str  # kickoff | goal
    match: dict[str, Any]
    prev_home: int
    prev_away: int
    path: list[str]


def detect_alerts(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    *,
    seeded: bool,
) -> tuple[list[Alert], dict[str, dict[str, Any]]]:
    """Compare two flattened boards. First successful poll only seeds state."""
    next_state: dict[str, dict[str, Any]] = {}
    alerts: list[Alert] = []

    for event_id, row in current.items():
        prior = previous.get(event_id)
        prior_path = list((prior or {}).get("path") or [])
        tracked = _tracked(row, prior_path)
        next_state[event_id] = tracked
        if not seeded:
            continue

        state = row.get("state") or ""
        hs, aws = _score_key(row)
        # Only treat pre → in as kickoff. A newly seen in-play row is mid-game catch-up.
        if state == "in" and prior is not None and (prior.get("state") or "") == "pre":
            alerts.append(
                Alert(kind="kickoff", match=row, prev_home=0, prev_away=0, path=["0-0"])
            )
        if prior is None:
            continue
        ph, pa = _int(prior.get("home_score")), _int(prior.get("away_score"))
        if (hs, aws) == (ph, pa):
            continue
        if hs + aws < ph + pa:
            continue
        if (hs, aws) == (0, 0):
            continue
        alerts.append(Alert(kind="goal", match=row, prev_home=ph, prev_away=pa, path=tracked["path"]))

    return alerts, next_state


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seeded": False, "matches": {}}
    if not isinstance(raw, dict):
        return {"seeded": False, "matches": {}}
    matches = raw.get("matches")
    if not isinstance(matches, dict):
        matches = {}
    return {"seeded": bool(raw.get("seeded")), "matches": matches}


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


def format_branches(tree: dict[str, Any] | None, *, limit: int = 5) -> str:
    if not tree or not tree.get("count"):
        return "no mapped history"
    bits: list[str] = []
    for row in (tree.get("branches") or [])[:limit]:
        mark = " ←" if row.get("is_live_branch") else ""
        bits.append(f"{row.get('score')} {_pct(row.get('pct'))}{mark}")
    extra = f" · ended same {_pct(tree.get('pct_ended_same'))}" if tree.get("pct_ended_same") is not None else ""
    return f"n={tree.get('count')}  " + (" · ".join(bits) if bits else "—") + extra


def format_similar_paths(scorelines: dict[str, Any] | None) -> str:
    if not scorelines:
        return ""
    trees = scorelines.get("trees") or {}
    home = scorelines.get("home_fd") or scorelines.get("home") or "Home"
    away = scorelines.get("away_fd") or scorelines.get("away") or "Away"
    minute = scorelines.get("minute")
    at = f" @ {minute}′" if minute is not None else ""
    lines = [
        f"**From here** `{scorelines.get('scoreline')}`{at}",
        f"{home}: {format_branches(trees.get('home'))}",
        f"{away}: {format_branches(trees.get('away'))}",
    ]
    return "\n".join(lines)


def format_alert(alert: Alert, *, scorelines: dict[str, Any] | None = None) -> str:
    m = alert.match
    chiclet = m.get("league_chiclet") or ""
    home = m.get("home") or "?"
    away = m.get("away") or "?"
    hs, aws = _score_key(m)
    clock = m.get("clock") or ""
    path = " → ".join(alert.path or [_score_label(hs, aws)])
    similar = format_similar_paths(scorelines)

    if alert.kind == "kickoff":
        lines = [
            f"▶️ **Kickoff** · {chiclet}".rstrip(" ·"),
            f"**{home} vs {away}** · `{hs}–{aws}`",
            f"Path: `{path}`",
        ]
    else:
        scorer = ""
        if hs > alert.prev_home:
            scorer = home
        elif aws > alert.prev_away:
            scorer = away
        who = f" · {scorer}" if scorer else ""
        lines = [
            f"⚽ **Goal**{who} · {chiclet} · {clock or 'LIVE'}".rstrip(" ·"),
            f"**{home} {hs}–{aws} {away}**",
            f"Path: `{path}`",
        ]
    if similar:
        lines.append(similar)
    return "\n".join(lines)


def _load_corpus() -> list[Any]:
    from eeesoc.cli import _default_season
    from eeesoc.data import load_season, previous_season_label

    season = os.getenv("EEESOC_SEASON") or _default_season()
    if not season:
        return []
    current = load_season(season)
    history: list[Any] = []
    try:
        history = load_season(previous_season_label(season))
    except ValueError:
        history = []
    return [*history, *current]


def scoreline_for_alert(
    alert: Alert,
    corpus: Iterable[Any],
    *,
    eval_fn: Callable[..., dict[str, Any]] = build_live_scoreline_eval,
) -> dict[str, Any] | None:
    if not corpus:
        return None
    m = alert.match
    minute = parse_clock_minute(m.get("clock") or "", default=1)
    prev_h = alert.prev_home if alert.kind == "goal" else None
    prev_a = alert.prev_away if alert.kind == "goal" else None
    try:
        return eval_fn(
            corpus,
            home_name=str(m.get("home") or ""),
            away_name=str(m.get("away") or ""),
            home_score=_int(m.get("home_score")),
            away_score=_int(m.get("away_score")),
            home_id=str(m.get("home_id") or ""),
            away_id=str(m.get("away_id") or ""),
            prev_home=prev_h,
            prev_away=prev_a,
            minute=minute,
            prev_minute=max(0, minute - 1) if alert.kind == "goal" else None,
            limit_peers=4,
        )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return None


def poll_alerts(
    *,
    board: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    corpus: Iterable[Any] | None = None,
    fetch_board: Callable[..., dict[str, Any]] = fetch_live_board,
    eval_fn: Callable[..., dict[str, Any]] = build_live_scoreline_eval,
) -> tuple[list[str], dict[str, Any]]:
    """One poll: return Discord messages and the updated persisted state."""
    payload = board if board is not None else fetch_board(live_only=False, days_back=1)
    current = flatten_board(payload)
    store = state if state is not None else load_state()
    previous = store.get("matches") if isinstance(store.get("matches"), dict) else {}
    seeded = bool(store.get("seeded"))
    alerts, next_matches = detect_alerts(previous, current, seeded=seeded)

    history = list(corpus) if corpus is not None else (_load_corpus() if alerts else [])
    messages: list[str] = []
    for alert in alerts:
        scorelines = scoreline_for_alert(alert, history, eval_fn=eval_fn)
        messages.append(format_alert(alert, scorelines=scorelines))

    return messages, {"seeded": True, "matches": next_matches}


def run_poll_and_persist(
    *,
    path: Path | None = None,
    board: dict[str, Any] | None = None,
    corpus: Iterable[Any] | None = None,
) -> list[str]:
    store = load_state(path)
    messages, nxt = poll_alerts(board=board, state=store, corpus=corpus)
    save_state(nxt, path)
    return messages
