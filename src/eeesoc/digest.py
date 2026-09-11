"""Morning Discord slate: today's fixtures in the tracked leagues.

The bot posts once at 9:30 AM America/New_York (overridable). Kickoffs are
labelled in that same timezone. Conference League is included here even if
it is not on the live chiclet strip.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from eeesoc.cache import cache_root
from eeesoc.live import LEAGUES, fetch_live_board, parse_start

_DEFAULT_TZ = "America/New_York"
_DEFAULT_HOUR = 9
_DEFAULT_MINUTE = 30
_DEAD = ("postponed", "canceled", "cancelled", "suspended", "abandoned", "forfeit")
_STATE_NAME = "discord-digest.json"
_DISCORD_SAFE_LEN = 1800

# Europe first (already midday at 9:30 ET), MLS last. Conference League is
# fetched for the slate even when it is not a dashboard chiclet.
DIGEST_LEAGUES: list[tuple[str, str]] = [
    ("eng.1", "EPL"),
    ("eng.2", "Championship"),
    ("esp.1", "La Liga"),
    ("ita.1", "Serie A"),
    ("ger.1", "Bundesliga"),
    ("fra.1", "Ligue 1"),
    ("uefa.champions", "UCL"),
    ("uefa.europa", "UEL"),
    ("uefa.europa.conf", "UECL"),
    ("usa.1", "MLS"),
]


def _tz() -> ZoneInfo:
    name = (os.getenv("EEESOC_TZ") or _DEFAULT_TZ).strip() or _DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo(_DEFAULT_TZ)


def _now() -> datetime:
    return datetime.now(_tz())


def digest_hour() -> int:
    raw = (os.getenv("EEESOC_DIGEST_HOUR") or str(_DEFAULT_HOUR)).strip()
    try:
        return max(0, min(23, int(raw)))
    except ValueError:
        return _DEFAULT_HOUR


def digest_minute() -> int:
    raw = (os.getenv("EEESOC_DIGEST_MINUTE") or str(_DEFAULT_MINUTE)).strip()
    try:
        return max(0, min(59, int(raw)))
    except ValueError:
        return _DEFAULT_MINUTE


def digest_leagues() -> list[tuple[str, str]]:
    raw = (os.getenv("EEESOC_DIGEST_LEAGUES") or "").strip()
    labels = {slug: label for slug, label in (*DIGEST_LEAGUES, *LEAGUES)}
    if not raw:
        return list(DIGEST_LEAGUES)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in raw.split(","):
        slug = part.strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append((slug, labels.get(slug, slug)))
    return out or list(DIGEST_LEAGUES)


def state_path() -> Path:
    override = (os.getenv("EEESOC_DIGEST_STATE") or "").strip()
    if override:
        return Path(override).expanduser()
    return cache_root() / _STATE_NAME


def load_digest_state(path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_digest_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


def mark_digest_sent(now: datetime | None = None, path: Path | None = None) -> None:
    when = now or _now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=_tz())
    local = when.astimezone(_tz())
    store = load_digest_state(path)
    store["sent_on"] = local.date().isoformat()
    save_digest_state(store, path)


def digest_due(now: datetime, sent_on: str | None = None, *, hour: int | None = None, minute: int | None = None) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz())
    local = now.astimezone(_tz())
    today = local.date().isoformat()
    if sent_on == today:
        return False
    hh = digest_hour() if hour is None else hour
    mm = digest_minute() if minute is None else minute
    return (local.hour, local.minute) >= (hh, mm)


def _kickoff(match: dict[str, Any]) -> datetime | None:
    started = parse_start(str(match.get("start") or ""))
    if started is None:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=ZoneInfo("UTC"))
    return started.astimezone(_tz())


def _is_dead(match: dict[str, Any]) -> bool:
    blob = f"{match.get('detail') or ''} {match.get('clock') or ''} {match.get('state') or ''}".lower()
    return any(word in blob for word in _DEAD)


def is_todays_match(match: dict[str, Any], now: datetime | None = None) -> bool:
    if _is_dead(match):
        return False
    kick = _kickoff(match)
    if kick is None:
        return False
    now = now or _now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz())
    local = now.astimezone(_tz())
    return (kick.year, kick.month, kick.day) == (local.year, local.month, local.day)


def _flat_matches(board: dict[str, Any]) -> list[dict[str, Any]]:
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
    return rows


def _time_label(match: dict[str, Any]) -> str:
    state = str(match.get("state") or "")
    if state == "in":
        clock = str(match.get("clock") or "LIVE").strip() or "LIVE"
        return f"LIVE {clock}" if clock.upper() != "LIVE" else "LIVE"
    if state == "post":
        return "FT"
    kick = _kickoff(match)
    if kick is None:
        return "KO"
    return kick.strftime("%I:%M %p").lstrip("0")


def _row_line(match: dict[str, Any]) -> str:
    home = str(match.get("home") or "?")
    away = str(match.get("away") or "?")
    state = str(match.get("state") or "")
    if state in ("in", "post"):
        hs = match.get("home_score", 0)
        aws = match.get("away_score", 0)
        return f"`{_time_label(match)}` {home} {hs}–{aws} {away}"
    return f"`{_time_label(match)}` {home} vs {away}"


def _wanted_slugs() -> dict[str, str]:
    return {slug: label for slug, label in digest_leagues()}


def todays_matches(board: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    wanted = _wanted_slugs()
    rows = [
        m
        for m in _flat_matches(board)
        if str(m.get("league_slug") or "") in wanted and is_todays_match(m, now)
    ]
    order = {slug: i for i, slug in enumerate(wanted)}
    rows.sort(
        key=lambda m: (
            order.get(str(m.get("league_slug") or ""), 999),
            str(m.get("start") or ""),
            str(m.get("home") or ""),
        )
    )
    return rows


def _header(now: datetime, n: int) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz())
    local = now.astimezone(_tz())
    day = f"{local.strftime('%a')} {local.day} {local.strftime('%b')}"
    if n:
        return f"**Today · {day}** · {n} match{'es' if n != 1 else ''} · ET"
    labels = " / ".join(label for _slug, label in digest_leagues())
    return f"**Today · {day}** · no matches in {labels}"


def _chunk_messages(header: str, sections: list[str]) -> list[str]:
    if not sections:
        return [header]
    messages: list[str] = []
    current = header
    for section in sections:
        piece = f"\n\n{section}"
        if len(current) + len(piece) <= _DISCORD_SAFE_LEN:
            current += piece
            continue
        if current != header:
            messages.append(current)
        cont = f"{header} · cont."
        if len(cont) + len(piece) <= _DISCORD_SAFE_LEN:
            current = cont + piece
            continue
        # One league block is larger than a Discord message — split by line.
        lines = section.splitlines()
        block = cont
        for line in lines:
            add = ("\n" if block else "") + line
            if len(block) + len(add) > _DISCORD_SAFE_LEN and block != cont:
                messages.append(block)
                block = f"{cont}\n{line}"
            else:
                block += add
        current = block
    if current:
        messages.append(current)
    return messages


def format_morning_digest(board: dict[str, Any], now: datetime | None = None) -> list[str]:
    now = now or _now()
    rows = todays_matches(board, now)
    header = _header(now, len(rows))
    if not rows:
        return [header]
    wanted = _wanted_slugs()
    sections: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for match in rows:
        grouped.setdefault(str(match.get("league_slug") or ""), []).append(match)
    for slug, label in digest_leagues():
        bucket = grouped.get(slug)
        if not bucket:
            continue
        lines = [f"**{wanted.get(slug, label)}**"]
        lines.extend(_row_line(m) for m in bucket)
        sections.append("\n".join(lines))
    return _chunk_messages(header, sections)


def collect_digest(
    *,
    board: dict[str, Any] | None = None,
    now: datetime | None = None,
    fetch_board: Callable[..., dict[str, Any]] = fetch_live_board,
) -> list[str]:
    when = now or _now()
    payload = (
        board
        if board is not None
        else fetch_board(live_only=False, days_back=1, leagues=digest_leagues(), use_cache=False)
    )
    return format_morning_digest(payload, when)


def pending_digest(
    *,
    board: dict[str, Any] | None = None,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
    path: Path | None = None,
    fetch_board: Callable[..., dict[str, Any]] = fetch_live_board,
) -> list[str] | None:
    """Return slate messages if 9:30 ET has passed and we have not posted today.

    ``None`` means not due. A list (possibly the empty-day line) means send it.
    """
    when = now or _now()
    store = state if state is not None else load_digest_state(path)
    sent_on = store.get("sent_on") if isinstance(store, dict) else None
    if not digest_due(when, str(sent_on) if sent_on else None):
        return None
    return collect_digest(board=board, now=when, fetch_board=fetch_board)
