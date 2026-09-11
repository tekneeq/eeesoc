"""eeesoc Discord bot — ``!soc`` command surface.

Only messages that start with ``!soc`` (or ``!eee``) are handled:

    !soc help
    !soc live [league]
    !soc upcoming [league]
    !soc finished [league]
    !soc winprob
    !soc fixture HOME AWAY
    !soc similar TEAM [minute]
    !soc today

Env (``.env`` on the eeesoc EC2 host, loaded by docker ``--env-file``):

    DISCORD_BOT_TOKEN         required
    DISCORD_CHANNEL_ID        channel for the ready greeting, live alerts, and 9:30 AM ET slate
    EEESOC_DISCORD_POLL_S     optional poll interval (default 20)
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from eeesoc.live import LEAGUES, fetch_live_board, parse_start

load_dotenv()

try:
    import discord
    from discord import Intents
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "discord package missing — install with `uv sync` / `pip install discord`"
    ) from e


PREFIXES = ("!soc", "!eee")
_DISCORD_SAFE_LEN = 1800
_DEAD_STATUS = ("postponed", "canceled", "cancelled", "suspended", "abandoned", "forfeit")
_DEFAULT_TZ = "America/New_York"

HELP_TEXT = """**eeesoc · `!soc` commands**
```
!soc help                     this message
!soc live [league]            in-play scoreboard
!soc upcoming [league]        today's remaining kickoffs
!soc finished [league]        today's / yesterday's full-time
!soc winprob                  EPL picks + 30-day record
!soc fixture HOME AWAY        WinProb detail for a pair
!soc similar TEAM [minute]    historical lookalikes
!soc today                    today's slate (EPL / Championship / …)
```
Examples:
`!soc live`
`!soc live epl`
`!soc upcoming uala`
`!soc winprob`
`!soc fixture Arsenal Chelsea`
`!soc similar Everton 53`
`!soc today`

League can be a chiclet (`EPL`) or slug (`eng.1`). `!eee` is an alias for `!soc`.
The bot also posts that slate on its own every day at 9:30 AM ET.
"""


def _clip(text: str) -> str:
    if len(text) <= _DISCORD_SAFE_LEN:
        return text
    return text[: _DISCORD_SAFE_LEN - 20] + "\n… _(truncated)_"


def _tz() -> ZoneInfo:
    name = (os.getenv("EEESOC_TZ") or _DEFAULT_TZ).strip() or _DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo(_DEFAULT_TZ)


def _now() -> datetime:
    return datetime.now(_tz())


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _short(name: str, limit: int = 16) -> str:
    text = str(name or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _kickoff(match: dict[str, Any]) -> datetime | None:
    started = parse_start(str(match.get("start") or ""))
    if started is None:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=ZoneInfo("UTC"))
    return started.astimezone(_tz())


def _same_local_day(when: datetime, now: datetime | None = None) -> bool:
    now = now or _now()
    return (when.year, when.month, when.day) == (now.year, now.month, now.day)


def _is_dead(match: dict[str, Any]) -> bool:
    blob = f"{match.get('detail') or ''} {match.get('clock') or ''} {match.get('state') or ''}".lower()
    return any(word in blob for word in _DEAD_STATUS)


def is_upcoming_match(match: dict[str, Any], now: datetime | None = None) -> bool:
    if str(match.get("state") or "") != "pre" or _is_dead(match):
        return False
    kick = _kickoff(match)
    return bool(kick and _same_local_day(kick, now))


def _kickoff_label(match: dict[str, Any]) -> str:
    kick = _kickoff(match)
    if kick is None:
        return "KO"
    time = kick.strftime("%I:%M %p").lstrip("0")
    if _same_local_day(kick):
        return f"Today {time}"
    return f"{kick.strftime('%a')} {time}"


def _record_line(block: dict[str, Any] | None) -> str:
    rec = block or {}
    if not rec.get("total"):
        return "—"
    return f"{rec.get('correct', 0)}–{rec.get('wrong', 0)} ({_pct(rec.get('pct'))})"


def _league_query(token: str) -> str:
    return token.strip().lower().replace(" ", "")


def _league_aliases(slug: str, chiclet: str, name: str = "") -> set[str]:
    bits = {
        _league_query(slug),
        _league_query(chiclet),
        _league_query(name),
        _league_query(slug.split(".")[-1] if "." in slug else slug),
    }
    extra = {
        "epl": {"epl", "eng1", "premier", "premierleague"},
        "la liga": {"laliga", "liga"},
        "serie a": {"seriea", "seria"},
        "bundesliga": {"bundes", "bundesliga"},
        "ligue 1": {"ligue1", "ligue"},
        "ucl": {"ucl", "uclchampions", "champions"},
        "uel": {"uel", "europa"},
        "uecl": {"uecl", "conference", "conferenceleague"},
    }
    for key, aliases in extra.items():
        if _league_query(chiclet) == _league_query(key) or _league_query(name) == _league_query(key):
            bits.update(aliases)
    return {b for b in bits if b}


def _match_league(match: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    wanted = _league_query(query)
    aliases = _league_aliases(
        str(match.get("league_slug") or ""),
        str(match.get("league_chiclet") or ""),
        str(match.get("league_name") or ""),
    )
    return wanted in aliases or any(wanted in alias for alias in aliases if len(wanted) >= 3)


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


def _in_scope(match: dict[str, Any], scope: str, now: datetime | None = None) -> bool:
    state = str(match.get("state") or "")
    if scope == "live":
        return state == "in"
    if scope == "finished":
        return state == "post"
    if scope == "upcoming":
        return is_upcoming_match(match, now)
    return True


def format_board(board: dict[str, Any], scope: str, league: str = "", now: datetime | None = None) -> str:
    rows = [m for m in _flat_matches(board) if _in_scope(m, scope, now) and _match_league(m, league)]
    if scope == "finished":
        rows.sort(key=lambda m: str(m.get("start") or ""), reverse=True)
    elif scope == "upcoming":
        rows.sort(key=lambda m: (str(m.get("start") or ""), str(m.get("home") or "")))

    titles = {"live": "Live", "upcoming": "Upcoming today", "finished": "Finished"}
    heading = titles.get(scope, scope.title())
    if league:
        heading += f" · {league}"
    if not rows:
        empty = {
            "live": "No live matches right now.",
            "upcoming": "No upcoming kickoffs left today.",
            "finished": "No finished matches in this window.",
        }.get(scope, "No matches.")
        return f"**{heading}**\n{empty}"

    lines = [f"**{heading}** · {len(rows)}"]
    for match in rows[:18]:
        home = _short(match.get("home") or "?")
        away = _short(match.get("away") or "?")
        chiclet = match.get("league_chiclet") or ""
        if scope == "upcoming":
            lines.append(f"`{_kickoff_label(match)}` {home} vs {away} · {chiclet}")
        else:
            clock = "FT" if scope == "finished" else (match.get("clock") or "LIVE")
            hs = match.get("home_score", 0)
            aws = match.get("away_score", 0)
            lines.append(f"`{clock}` {home} {hs}–{aws} {away} · {chiclet}")
    if len(rows) > 18:
        lines.append(f"_…{len(rows) - 18} more_")
    return "\n".join(lines)


def format_winprob(board: dict[str, Any]) -> str:
    rec = (board or {}).get("record") or {}
    last30 = rec.get("last30") or {}
    fixtures = list((board or {}).get("fixtures") or [])
    lines = [
        f"**WinProb** · last {rec.get('window_days') or 30} days `{_record_line(last30)}`",
        f"season `{_record_line(rec.get('season'))}`",
    ]
    if not fixtures:
        lines.append("No upcoming EPL fixtures in the model window.")
        return "\n".join(lines)

    lines.append("**Picks**")
    for fx in fixtures[:12]:
        home = _short(fx.get("home_fd") or fx.get("home") or "?")
        away = _short(fx.get("away_fd") or fx.get("away") or "?")
        when = _kickoff_label(fx)
        pick = fx.get("pick")
        if not pick:
            lines.append(f"`{when}` {home} vs {away} — no pick")
            continue
        team = fx.get("pick_team") or ("Draw" if pick == "draw" else pick)
        bucket = fx.get("bucket") or ""
        extra = f" · {bucket}" if bucket else ""
        lines.append(f"`{when}` {home} vs {away} — **{_short(team, 14)}** {_pct(fx.get('pick_prob'))}{extra}")
    if len(fixtures) > 12:
        lines.append(f"_…{len(fixtures) - 12} more_")
    return "\n".join(lines)


def format_fixture_detail(detail: dict[str, Any]) -> str:
    if detail.get("error"):
        return f"Could not map that pair to EPL history ({detail.get('error')})."
    home = detail.get("home_fd") or "?"
    away = detail.get("away_fd") or "?"
    pred = detail.get("prediction") or {}
    probs = pred.get("probs") or {}
    pick = pred.get("pick_team") or pred.get("pick") or "—"
    lines = [
        f"**{home} vs {away}**",
        f"Pick **{pick}** {_pct(pred.get('pick_prob'))}  "
        f"H {_pct(probs.get('home'))} · D {_pct(probs.get('draw'))} · A {_pct(probs.get('away'))}",
    ]
    home_form = (detail.get("home_form") or {}).get("season") or {}
    away_form = (detail.get("away_form") or {}).get("season") or {}
    if home_form or away_form:
        lines.append(
            f"Season {home} {home_form.get('wins', 0)}-{home_form.get('draws', 0)}-{home_form.get('losses', 0)}  ·  "
            f"{away} {away_form.get('wins', 0)}-{away_form.get('draws', 0)}-{away_form.get('losses', 0)}"
        )
    h2h = detail.get("h2h") or []
    if h2h:
        bits = [f"{r.get('home')} {r.get('ft')} {r.get('away')}" for r in h2h[:4]]
        lines.append("H2H: " + " · ".join(bits))
    return "\n".join(lines)


def format_similar(match_label: str, hits: list[Any]) -> str:
    if not hits:
        return f"No similar matches for {match_label}."
    lines = [f"**Similar** · {match_label}"]
    for hit in hits[:8]:
        payload = hit.to_dict() if hasattr(hit, "to_dict") else hit
        lines.append(
            f"`{payload.get('score', 0):0.3f}` {payload.get('date')}  "
            f"{payload.get('home')} vs {payload.get('away')}  "
            f"[{payload.get('label')}]  FT {payload.get('ft')}"
        )
    return "\n".join(lines)


def _load_seasons() -> tuple[str | None, list[Any], list[Any]]:
    from eeesoc.cli import _default_season
    from eeesoc.data import load_season, previous_season_label

    season = os.getenv("EEESOC_SEASON") or _default_season()
    if not season:
        return None, [], []
    current = load_season(season)
    history: list[Any] = []
    try:
        history = load_season(previous_season_label(season))
    except ValueError:
        history = []
    return season, current, history


def _cmd_board(
    scope: str,
    league: str = "",
    *,
    board: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    payload = board if board is not None else fetch_live_board(live_only=False, days_back=1)
    return format_board(payload, scope, league, now=now)


def _cmd_winprob(*, board: dict[str, Any] | None = None) -> str:
    if board is not None:
        return format_winprob(board)
    season, current, history = _load_seasons()
    if not season or not current:
        return "No cached season — warm with `eeesoc --warm EPL:2025` first."
    from eeesoc.winprob import build_winprob_board

    return format_winprob(build_winprob_board(current, history))


def _cmd_fixture(home: str, away: str, *, detail: dict[str, Any] | None = None) -> str:
    if detail is not None:
        return format_fixture_detail(detail)
    season, current, history = _load_seasons()
    if not season or not current:
        return "No cached season — warm with `eeesoc --warm EPL:2025` first."
    from eeesoc.winprob import build_fixture_detail

    return format_fixture_detail(build_fixture_detail(home, away, current, history))


def _cmd_similar(query: str, minute: int, *, hits: list[Any] | None = None, label: str | None = None) -> str:
    if hits is not None:
        return format_similar(label or f"{query} @ {minute}'", hits)
    season, current, history = _load_seasons()
    if not season or not current:
        return "No cached season — warm with `eeesoc --warm EPL:2025` first."
    from eeesoc.similar import find_similar

    corpus = [*history, *current]
    matches = {m.match_id: m for m in current}
    match = matches.get(query)
    if match is None:
        lowered = query.lower()
        for candidate in current:
            if lowered in candidate.match_id.lower() or lowered == candidate.home.lower() or lowered in candidate.home.lower():
                match = candidate
                break
    if match is None:
        return f"Match not found: `{query}`. Try a home club name (e.g. `Everton`)."
    snap = match.snapshot_at(minute)
    found = find_similar(snap, corpus, exclude_ids={match.match_id})
    return format_similar(f"{match.home} vs {match.away} @ {minute}' — {snap.label()}", found)


def _split_pair(args: list[str]) -> tuple[str, str]:
    tokens = [t for t in args if t]
    if not tokens:
        return "", ""
    joined = " ".join(tokens)
    lower = joined.lower()
    if " vs " in lower:
        idx = lower.index(" vs ")
        return joined[:idx].strip(), joined[idx + 4 :].strip()
    if len(tokens) == 2:
        return tokens[0], tokens[1]
    mid = max(1, len(tokens) // 2)
    return " ".join(tokens[:mid]), " ".join(tokens[mid:])


def parse_command(text: str) -> tuple[str, list[str]] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    matched = None
    for prefix in PREFIXES:
        if lowered == prefix or lowered.startswith(prefix + " "):
            matched = prefix
            break
    if matched is None:
        return None
    rest = raw[len(matched) :].strip()
    if not rest:
        return "help", []
    parts = rest.split()
    return parts[0].lower(), parts[1:]


def _cmd_today(*, board: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    from eeesoc.digest import collect_digest

    return "\n\n".join(collect_digest(board=board, now=now))


def handle_command(
    text: str,
    *,
    board: dict[str, Any] | None = None,
    winprob: dict[str, Any] | None = None,
    fixture_detail: dict[str, Any] | None = None,
    similar_hits: list[Any] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return a reply for a `!soc` / `!eee` message, or None if it is not for us."""
    parsed = parse_command(text)
    if parsed is None:
        return None
    sub, args = parsed

    if sub in ("help", "?", "commands"):
        return HELP_TEXT
    if sub in ("live", "inplay", "in-play"):
        return _cmd_board("live", " ".join(args), board=board, now=now)
    if sub in ("upcoming", "soon", "ko", "kickoff"):
        return _cmd_board("upcoming", " ".join(args), board=board, now=now)
    if sub in ("finished", "ft", "final"):
        return _cmd_board("finished", " ".join(args), board=board, now=now)
    if sub in ("winprob", "wp", "picks"):
        return _cmd_winprob(board=winprob)
    if sub in ("fixture", "match", "h2h"):
        home, away = _split_pair(args)
        if not home or not away:
            return "Usage: `!soc fixture HOME AWAY`  (or `HOME vs AWAY`)"
        return _cmd_fixture(home, away, detail=fixture_detail)
    if sub in ("similar", "lookalike", "lookalikes"):
        if not args:
            return "Usage: `!soc similar TEAM [minute]`"
        minute = 53
        name_parts = list(args)
        if name_parts[-1].isdigit():
            minute = int(name_parts.pop())
        if not name_parts:
            return "Usage: `!soc similar TEAM [minute]`"
        return _cmd_similar(" ".join(name_parts), minute, hits=similar_hits)
    if sub in ("today", "slate", "schedule", "fixtures"):
        return _cmd_today(board=board, now=now)
    if sub in ("leagues", "league"):
        labels = ", ".join(label for _slug, label in LEAGUES)
        return f"**Leagues** · {labels}"
    return f"Unknown `!soc` command `{sub}`. Try `!soc help`."


async def _handle_message(msg: discord.Message) -> None:
    if not any(msg.content.lower().startswith(prefix) for prefix in PREFIXES):
        return
    try:
        reply = handle_command(msg.content)
        if reply:
            await msg.reply(_clip(reply))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        await msg.reply(f"Error: `{type(e).__name__}: {e}`")


async def _resolve_channel(client: discord.Client) -> discord.abc.Messageable | None:
    channel_id = (os.getenv("DISCORD_CHANNEL_ID") or "").strip()
    if not channel_id:
        print("[discord] DISCORD_CHANNEL_ID unset — no greeting, live alerts, or morning slate.")
        return None
    try:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        return channel
    except Exception as e:  # noqa: BLE001
        print(f"[discord] could not resolve channel {channel_id}: {e!r}")
        return None


async def _alert_loop(client: discord.Client, channel: discord.abc.Messageable) -> None:
    from eeesoc.digest import mark_digest_sent, pending_digest
    from eeesoc.discord_alerts import poll_seconds, run_poll_and_persist

    interval = poll_seconds()
    print(f"[discord] live alerts every {interval:.0f}s → channel {getattr(channel, 'id', '?')}")
    print("[discord] morning slate at 9:30 AM ET")
    while not client.is_closed():
        try:
            messages = await asyncio.to_thread(run_poll_and_persist)
            for text in messages:
                await channel.send(_clip(text))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            print(f"[discord] alert poll failed: {e!r}")
        try:
            chunks = await asyncio.to_thread(pending_digest)
            if chunks is not None:
                for text in chunks:
                    await channel.send(text)
                mark_digest_sent()
                print("[discord] posted morning slate")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            print(f"[discord] morning slate failed: {e!r}")
        await asyncio.sleep(interval)


def build_client() -> discord.Client:
    intents = Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(
            f"[discord] logged in as {client.user} "
            f"(id={client.user and client.user.id})"
        )
        channel = await _resolve_channel(client)
        if channel is None:
            return
        try:
            await channel.send(
                "eeesoc online. Commands start with `!soc` — try `!soc help`. "
                "Kickoff and goals post here with Similar paths. "
                "Today's slate posts at 9:30 AM ET."
            )
        except Exception as e:  # noqa: BLE001
            print(f"[discord] ready greeting failed: {e!r}")
        if not getattr(client, "_eeesoc_alert_task", None):
            client._eeesoc_alert_task = asyncio.create_task(_alert_loop(client, channel))

    @client.event
    async def on_message(msg: discord.Message) -> None:
        if msg.author.bot:
            return
        await _handle_message(msg)

    return client


def run_bot(token: Optional[str] = None) -> None:
    """Block forever on the Discord gateway."""
    token = token or os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Create a Discord application at "
            "https://discord.com/developers/applications and put the bot "
            "token in the eeesoc host `.env`."
        )
    client = build_client()
    try:
        client.run(token)
    except discord.errors.PrivilegedIntentsRequired as e:
        raise SystemExit(
            "Discord rejected privileged intents.\n"
            "In https://discord.com/developers/applications → your app → "
            "Bot → Privileged Gateway Intents, enable **Message Content "
            "Intent**, save, then re-run ./restart-discord-bot.sh.\n"
            f"({e})"
        ) from e


if __name__ == "__main__":
    run_bot()
