"""
Live "no more goals this half" monitor.

Every poll it walks the in-play board, scores each game with ``eeesoc.nogoal`` and
fires **once per game per half** when the probability clears the threshold inside
the betting window (defaults: 10'–35' and 50'–78').  Each signal is then tracked to
resolution — ``held`` if the half ended goalless, ``busted`` at the minute a goal
landed — and both the trigger and the outcome are posted to Discord (webhook) so a
running record builds up in the channel and in ``<cache>/nogoal-signals.json``.

Environment
-----------
EEESOC_DISCORD_WEBHOOK      Discord webhook URL to post to (unset = evaluate only)
EEESOC_NOGOAL_THRESHOLD     P(no goal) needed to fire (default 0.65)
EEESOC_NOGOAL_WINDOW_1H     minutes the 1st-half trigger may fire, e.g. 10-35
EEESOC_NOGOAL_WINDOW_2H     same for the 2nd half, e.g. 50-78
EEESOC_NOGOAL_LEAGUES       comma-separated league slugs to fire for, e.g. arg.1,eng.2 (unset = all)
EEESOC_NOGOAL_POLL_S        seconds between polls (default 20)
EEESOC_NOGOAL_MONITOR       0 disables the background thread in the dashboard
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from eeesoc.cache import cache_root
from eeesoc.live import build_event_timeline, fetch_live_board
from eeesoc.nogoal import GOAL_KINDS, HALF_END, evaluate_live, event_period, nogoal_model

_STATE_NAME = "nogoal-signals.json"
_DEAD = ("postponed", "canceled", "cancelled", "suspended", "abandoned", "forfeit")
_DEFAULT_POLL_S = 20
# An open signal whose game has vanished from the board for this long is voided.
_VOID_AFTER_S = 3 * 3600
_MAX_LOG = 400

_lock = threading.Lock()
_latest: dict[str, Any] = {"at": 0.0, "live": {}}
_thread: threading.Thread | None = None


def webhook_url() -> str:
    return (os.getenv("EEESOC_DISCORD_WEBHOOK") or "").strip()


def poll_seconds() -> float:
    raw = (os.getenv("EEESOC_NOGOAL_POLL_S") or str(_DEFAULT_POLL_S)).strip()
    try:
        return max(8.0, min(120.0, float(raw)))
    except ValueError:
        return float(_DEFAULT_POLL_S)


def state_path() -> Path:
    override = (os.getenv("EEESOC_NOGOAL_STATE") or "").strip()
    return Path(override).expanduser() if override else cache_root() / _STATE_NAME


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    signals = raw.get("signals") if isinstance(raw, dict) else None
    return {"signals": signals if isinstance(signals, dict) else {}}


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    signals = state.get("signals") or {}
    if len(signals) > _MAX_LOG:
        keep = sorted(signals.items(), key=lambda kv: float(kv[1].get("fired_at") or 0), reverse=True)[:_MAX_LOG]
        signals = dict(keep)
    payload = {"signals": signals, "updated_at": time.time()}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


# ---------------------------------------------------------------------------
# Board helpers
# ---------------------------------------------------------------------------


def _is_dead(match: dict[str, Any]) -> bool:
    blob = f"{match.get('detail') or ''} {match.get('clock') or ''} {match.get('state') or ''}".lower()
    return any(word in blob for word in _DEAD)


def flatten_live(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """event_id → match row for every in-play game on the board."""
    rows: dict[str, dict[str, Any]] = {}
    for group in board.get("leagues") or []:
        for match in group.get("matches") or []:
            if _is_dead(match) or str(match.get("state") or "") != "in":
                continue
            event_id = str(match.get("event_id") or "")
            if not event_id:
                continue
            rows[event_id] = {
                **match,
                "event_id": event_id,
                "league_slug": group.get("slug") or match.get("league_slug") or "",
                "league_chiclet": group.get("chiclet") or match.get("league_chiclet") or "",
                "league_name": group.get("name") or match.get("league_name") or "",
            }
    return rows


def default_timeline(match: dict[str, Any]) -> dict[str, Any]:
    cs = match.get("clock_seconds")
    return build_event_timeline(
        str(match.get("league_slug") or ""),
        str(match.get("event_id") or ""),
        home=str(match.get("home") or ""),
        away=str(match.get("away") or ""),
        home_id=str(match.get("home_id") or ""),
        away_id=str(match.get("away_id") or ""),
        clock=str(match.get("clock") or ""),
        clock_seconds=int(cs) if isinstance(cs, (int, float)) else None,
        home_score=int(match.get("home_score") or 0),
        away_score=int(match.get("away_score") or 0),
    )


def _half_goals(timeline: dict[str, Any], period: int) -> int:
    return sum(1 for e in timeline.get("events") or [] if e.get("kind") in GOAL_KINDS and event_period(e) == period)


def _half_over(timeline: dict[str, Any], period: int) -> bool:
    if timeline.get("final"):
        return True
    if period == 1:
        clock = str(timeline.get("clock") or "").lower()
        if any(e for e in timeline.get("events") or [] if event_period(e) == 2):
            return True
        return "ht" in clock or "halftime" in clock or "half time" in clock or "half-time" in clock
    return False


def _shots_so_far(timeline: dict[str, Any], period: int) -> dict[str, Any]:
    events = [e for e in timeline.get("events") or [] if event_period(e) == period]
    shots = sum(1 for e in events if e.get("kind") in {"shot", "shot_on", "blocked", "goal"})
    sot = sum(1 for e in events if e.get("kind") in {"shot_on", "goal"})
    xg = sum(float(e.get("xg") or 0) for e in events if e.get("kind") != "own_goal")
    return {"shots": shots, "sot": sot, "xg": round(xg, 2)}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _record(signals: dict[str, Any]) -> tuple[int, int]:
    done = [s for s in signals.values() if s.get("status") in {"held", "busted"}]
    return sum(1 for s in done if s["status"] == "held"), len(done)


def format_trigger(signal: dict[str, Any]) -> str:
    half = "HT" if signal["period"] == 1 else "FT"
    ev = signal["eval"]
    tempo = signal.get("tempo") or {}
    clock = signal.get("clock") or f"{signal['fired_minute']}′"
    shots = int(tempo.get("shots", 0) or 0)
    shots_txt = f"{shots} shot" if shots == 1 else f"{shots} shots"
    lines = [
        f"🚫 **No more goals this half?** · {signal.get('league_chiclet') or ''} · {clock}".rstrip(" ·"),
        f"**{signal['home']} {signal['score'][0]}–{signal['score'][1]} {signal['away']}**",
        f"P(no goal to {half}) **{ev['p_no_goal_pct']}%** · break-even odds **{ev['break_even_odds']}** · {ev['lambda']:.2f} goals still expected",
        f"Why: {ev['minutes_left']}′ + stoppage left (base {ev['base_lambda']:.2f}) × league {ev['league_factor']:.2f} × {'still 0–0' if ev['zero_zero'] else 'game has goals'} {ev['state_factor']:.2f}",
    ]
    box_line = _box_line(signal, ev)
    if box_line:
        lines.append(box_line)
    lines += [
        f"Also this half: {shots_txt} · {tempo.get('sot', 0)} on target · xG {tempo.get('xg', 0)} (context only — shots add nothing once box entries are in)",
        f"Only take it if the book pays more than {ev['break_even_odds']}.",
    ]
    return "\n".join(lines)


def _box_line(signal: dict[str, Any], ev: dict[str, Any]) -> str:
    parts = []
    if ev.get("box_total") is not None:
        n = int(ev["box_total"])
        parts.append(
            f"Box entries so far **{n}** ({signal['home']} {ev.get('box_home', 0)} · {signal['away']} {ev.get('box_away', 0)})"
            + (f" — {ev['inplay_bucket']} for the minute ×{ev['inplay_factor']:.2f}" if ev.get("inplay_bucket") else "")
        )
    if ev.get("habit_bucket"):
        parts.append(
            f"these clubs usually reach the box {ev.get('habit_expected', 0)}× a half — {ev['habit_bucket']} fixture ×{ev['habit_factor']:.2f}"
        )
    return "Edge: " + " · ".join(parts) if parts else ""


def format_resolution(signal: dict[str, Any], signals: dict[str, Any]) -> str:
    held, total = _record(signals)
    rec = f"Record: {held}/{total} held ({int(round(100 * held / total)) if total else 0}%)"
    ev = signal["eval"]
    who = f"{signal['home']} {signal['score'][0]}–{signal['score'][1]} {signal['away']}"
    if signal["status"] == "held":
        end = "HT" if signal["period"] == 1 else "FT"
        return f"✅ **Held** · {who} · {end} — no goal after the {signal['fired_minute']}′ signal (P was {ev['p_no_goal_pct']}%). {rec}"
    if signal["status"] == "busted":
        note = signal.get("resolved_note") or ""
        return f"❌ **Busted** · {who} · goal{note} — signal was at {signal['fired_minute']}′ (P {ev['p_no_goal_pct']}%). {rec}"
    return f"⚪ **Void** · {who} · game dropped off the board before the half resolved."


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------


def poll(
    *,
    board: dict[str, Any] | None = None,
    model: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    fetch_timeline: Callable[[dict[str, Any]], dict[str, Any]] = default_timeline,
    now: float | None = None,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """One pass: (Discord messages, updated state, live evaluations by event_id)."""
    now = time.time() if now is None else now
    payload = board if board is not None else fetch_live_board(live_only=True)
    mdl = model if model is not None else nogoal_model()[0]
    store = state if state is not None else load_state()
    signals: dict[str, Any] = store.setdefault("signals", {})
    live = flatten_live(payload)
    messages: list[str] = []
    evals: dict[str, Any] = {}

    for event_id, match in live.items():
        try:
            tl = fetch_timeline(match)
        except Exception:  # noqa: BLE001 — one bad feed must not stall the loop
            traceback.print_exc()
            continue
        ev = evaluate_live(
            mdl,
            tl,
            league_slug=str(match.get("league_slug") or ""),
            home_id=str(match.get("home_id") or ""),
            away_id=str(match.get("away_id") or ""),
        )
        key = f"{event_id}:{ev['period']}"
        existing = signals.get(key)
        evals[event_id] = {
            **ev,
            "event_id": event_id,
            "signal": {k: existing[k] for k in ("status", "fired_minute", "resolved_minute") if k in existing} if existing else None,
        }
        if ev["ready"] and existing is None and ev["phase"] in {"1h", "2h"}:
            signal = {
                "key": key,
                "event_id": event_id,
                "league_slug": match.get("league_slug") or "",
                "league_chiclet": match.get("league_chiclet") or "",
                "league_name": match.get("league_name") or "",
                "home": match.get("home") or "?",
                "away": match.get("away") or "?",
                "home_id": match.get("home_id") or "",
                "away_id": match.get("away_id") or "",
                "start": match.get("start") or "",
                "period": ev["period"],
                "fired_at": now,
                "fired_minute": ev["minute"],
                "clock": tl.get("clock") or match.get("clock") or "",
                "score": [ev["home_score"], ev["away_score"]],
                "half_goals_at_fire": _half_goals(tl, ev["period"]),
                "tempo": _shots_so_far(tl, ev["period"]),
                "eval": ev,
                "status": "open",
                "last_seen": now,
            }
            signals[key] = signal
            evals[event_id]["signal"] = {"status": "open", "fired_minute": ev["minute"]}
            messages.append(format_trigger(signal))

    # Resolve open signals.
    for key, signal in list(signals.items()):
        if signal.get("status") != "open":
            continue
        match = live.get(str(signal.get("event_id")))
        if match is None:
            seen = signal.get("last_seen")
            if seen is None:
                seen = signal.get("fired_at", now)
            if now - float(seen) > _VOID_AFTER_S:
                signal["status"] = "void"
                signal["resolved_at"] = now
                messages.append(format_resolution(signal, signals))
            continue
        signal["last_seen"] = now
        try:
            tl = fetch_timeline(match)
        except Exception:  # noqa: BLE001
            continue
        period = int(signal["period"])
        goals_now = _half_goals(tl, period)
        if goals_now > int(signal.get("half_goals_at_fire") or 0):
            later = [
                e for e in tl.get("events") or [] if e.get("kind") in GOAL_KINDS and event_period(e) == period
            ]
            last = later[-1] if later else None
            signal["status"] = "busted"
            signal["resolved_at"] = now
            signal["resolved_minute"] = int(last.get("minute") or 0) if last else None
            scorer = signal["home"] if last and last.get("team") == "home" else signal["away"] if last else ""
            signal["resolved_note"] = f" at {signal['resolved_minute']}′ ({scorer})" if last else ""
            signal["final_score"] = [int(tl.get("home_score") or 0), int(tl.get("away_score") or 0)]
            messages.append(format_resolution(signal, signals))
        elif _half_over(tl, period):
            signal["status"] = "held"
            signal["resolved_at"] = now
            signal["resolved_minute"] = HALF_END[period]
            signal["final_score"] = [int(tl.get("home_score") or 0), int(tl.get("away_score") or 0)]
            messages.append(format_resolution(signal, signals))
        if evals.get(str(signal["event_id"])) is not None and evals[str(signal["event_id"])]["period"] == period:
            evals[str(signal["event_id"])]["signal"] = {
                k: signal[k] for k in ("status", "fired_minute", "resolved_minute") if k in signal
            }

    return messages, store, evals


def post_discord(messages: list[str], url: str | None = None, *, opener: Callable[..., Any] = urllib.request.urlopen) -> int:
    """POST each message to the webhook; returns how many were delivered."""
    target = url if url is not None else webhook_url()
    if not target or not messages:
        return 0
    sent = 0
    for text in messages:
        body = json.dumps({"content": text[:1990], "username": "eeesoc no-goal"}).encode("utf-8")
        req = urllib.request.Request(target, data=body, headers={"Content-Type": "application/json", "User-Agent": "eeesoc"})
        try:
            with opener(req, timeout=10) as resp:  # noqa: S310 — operator-configured webhook
                resp.read()
            sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[nogoal] discord post failed: {exc}")
    return sent


def run_once(*, path: Path | None = None, board: dict[str, Any] | None = None, post: bool = True) -> list[str]:
    store = load_state(path)
    messages, nxt, evals = poll(board=board, state=store)
    save_state(nxt, path)
    with _lock:
        _latest["at"] = time.time()
        _latest["live"] = evals
    if post and messages:
        post_discord(messages)
    return messages


def latest(*, max_age_s: float = 60.0) -> dict[str, Any]:
    """Most recent live evaluations, refreshed synchronously when stale."""
    with _lock:
        fresh = time.time() - float(_latest["at"]) < max_age_s
        snapshot = dict(_latest["live"])
    if not fresh:
        try:
            run_once()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        with _lock:
            snapshot = dict(_latest["live"])
    return {"live": snapshot, "at": _latest["at"], "webhook": bool(webhook_url())}


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts or 0), tz=timezone.utc).strftime("%Y-%m-%d")


def daily_record(signals: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """UTC-day buckets: how many signals held / busted / open / void."""
    rows = signals.values() if isinstance(signals, dict) else signals
    buckets: dict[str, dict[str, Any]] = {}
    for signal in rows:
        day = _utc_day(float(signal.get("fired_at") or 0))
        bucket = buckets.setdefault(
            day,
            {"date": day, "held": 0, "busted": 0, "open": 0, "void": 0, "fired": 0, "keys": []},
        )
        status = signal.get("status") or "open"
        if status in {"held", "busted", "open", "void"}:
            bucket[status] += 1
        bucket["fired"] += 1
        if signal.get("key"):
            bucket["keys"].append(signal["key"])
    out = sorted(buckets.values(), key=lambda r: r["date"])
    for bucket in out:
        done = bucket["held"] + bucket["busted"]
        bucket["hit_pct"] = int(round(100 * bucket["held"] / done)) if done else None
    return out


def tally_bets(rows: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    """Won = held (half stayed goalless), lost = busted, live = still open."""
    items = list(rows.values()) if isinstance(rows, dict) else list(rows)
    won = sum(1 for s in items if s.get("status") == "held")
    lost = sum(1 for s in items if s.get("status") == "busted")
    live = sum(1 for s in items if s.get("status") == "open")
    voided = sum(1 for s in items if s.get("status") == "void")
    settled = won + lost
    return {
        "fired": len(items),
        "won": won,
        "lost": lost,
        "live": live,
        "void": voided,
        "settled": settled,
        "hit_pct": int(round(100 * won / settled)) if settled else None,
    }


def bets_by_league(rows: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    """Group alarms by league, newest first inside each, leagues with the most fires first."""
    items = list(rows.values()) if isinstance(rows, dict) else list(rows)
    groups: dict[str, dict[str, Any]] = {}
    for signal in items:
        slug = str(signal.get("league_slug") or "")
        key = slug or str(signal.get("league_chiclet") or "other")
        group = groups.setdefault(
            key,
            {
                "league_slug": slug,
                "league_chiclet": str(signal.get("league_chiclet") or slug or "Other"),
                "league_name": str(signal.get("league_name") or ""),
                "signals": [],
            },
        )
        if signal.get("league_chiclet"):
            group["league_chiclet"] = str(signal["league_chiclet"])
        if signal.get("league_name") and not group["league_name"]:
            group["league_name"] = str(signal["league_name"])
        group["signals"].append(signal)
    out = []
    for group in groups.values():
        group["signals"].sort(key=lambda s: float(s.get("fired_at") or 0), reverse=True)
        out.append({**group, **tally_bets(group["signals"])})
    out.sort(key=lambda g: (-int(g["fired"]), str(g["league_chiclet"])))
    return out


def signal_log(path: Path | None = None) -> dict[str, Any]:
    signals = load_state(path)["signals"]
    rows = sorted(signals.values(), key=lambda s: float(s.get("fired_at") or 0), reverse=True)
    held, total = _record(signals)
    return {
        "signals": rows,
        "held": held,
        "resolved": total,
        "open": sum(1 for s in rows if s.get("status") == "open"),
        "hit_pct": int(round(100 * held / total)) if total else None,
        "daily": daily_record(signals),
        "bets": {**tally_bets(rows), "leagues": bets_by_league(rows)},
    }


def monitor_enabled() -> bool:
    return (os.getenv("EEESOC_NOGOAL_MONITOR") or "1").strip() not in {"0", "false", "no", "off"}


def start_monitor_thread(*, every_s: float | None = None) -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    interval = every_s if every_s is not None else poll_seconds()

    def _loop() -> None:
        while True:
            try:
                run_once()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(interval)

    _thread = threading.Thread(target=_loop, name="eeesoc-nogoal-monitor", daemon=True)
    _thread.start()
    return True
