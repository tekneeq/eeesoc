"""Morning Discord slate: 9:30 AM ET due-window, league filter, and formatting."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from eeesoc.digest import (
    DIGEST_LEAGUES,
    collect_digest,
    digest_due,
    digest_leagues,
    format_morning_digest,
    is_todays_match,
    pending_digest,
    todays_matches,
)
from eeesoc.discorder import handle_command

TZ = ZoneInfo("America/New_York")


def _iso(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _match(
    home: str,
    away: str,
    start: datetime,
    *,
    slug: str = "eng.1",
    chiclet: str = "EPL",
    state: str = "pre",
    clock: str = "0'",
    detail: str = "Scheduled",
    home_score: int = 0,
    away_score: int = 0,
) -> dict:
    return {
        "home": home,
        "away": away,
        "start": _iso(start),
        "state": state,
        "clock": clock,
        "detail": detail,
        "home_score": home_score,
        "away_score": away_score,
        "league_slug": slug,
        "league_chiclet": chiclet,
    }


def _board(*matches: dict) -> dict:
    groups: dict[str, dict] = {}
    for match in matches:
        slug = match["league_slug"]
        bucket = groups.setdefault(
            slug,
            {"slug": slug, "chiclet": match.get("league_chiclet") or slug, "name": slug, "matches": []},
        )
        bucket["matches"].append(match)
    return {"leagues": list(groups.values())}


def test_digest_leagues_cover_requested_competitions():
    slugs = {slug for slug, _label in DIGEST_LEAGUES}
    assert slugs == {
        "eng.1",
        "eng.2",
        "esp.1",
        "ita.1",
        "ger.1",
        "fra.1",
        "uefa.champions",
        "uefa.europa",
        "uefa.europa.conf",
        "usa.1",
    }


def test_digest_leagues_env_override(monkeypatch):
    monkeypatch.setenv("EEESOC_DIGEST_LEAGUES", "eng.1, usa.1, eng.1")
    assert digest_leagues() == [("eng.1", "EPL"), ("usa.1", "MLS")]


def test_digest_due_930_et_once_per_day():
    morning = datetime(2026, 9, 12, 9, 29, tzinfo=TZ)
    ready = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    late = datetime(2026, 9, 12, 14, 5, tzinfo=TZ)
    next_morning = datetime(2026, 9, 13, 9, 30, tzinfo=TZ)
    assert not digest_due(morning)
    assert digest_due(ready)
    assert digest_due(late)
    assert not digest_due(ready, sent_on="2026-09-12")
    assert digest_due(next_morning, sent_on="2026-09-12")


def test_todays_matches_skip_tomorrow_postponed_and_other_leagues():
    now = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    epl = _match("Liverpool", "Everton", now + timedelta(hours=5))
    live = _match(
        "Arsenal",
        "Chelsea",
        now - timedelta(hours=1),
        state="in",
        clock="33'",
        detail="1st Half",
        home_score=1,
    )
    champ = _match(
        "Leeds",
        "Norwich",
        now + timedelta(hours=2),
        slug="eng.2",
        chiclet="Championship",
    )
    mls = _match(
        "LAFC",
        "Miami",
        now.replace(hour=19, minute=30),
        slug="usa.1",
        chiclet="MLS",
    )
    liga_mx = _match(
        "America",
        "Chivas",
        now + timedelta(hours=8),
        slug="mex.1",
        chiclet="Liga MX",
    )
    tomorrow = _match("Barcelona", "Valencia", now + timedelta(days=1), slug="esp.1", chiclet="La Liga")
    dead = _match("Postponed United", "Cancelled City", now + timedelta(hours=3), detail="Postponed")
    board = _board(epl, live, champ, mls, liga_mx, tomorrow, dead)
    rows = todays_matches(board, now)
    pairs = {(r["home"], r["away"]) for r in rows}
    assert pairs == {("Liverpool", "Everton"), ("Arsenal", "Chelsea"), ("Leeds", "Norwich"), ("LAFC", "Miami")}
    assert is_todays_match(epl, now)
    assert not is_todays_match(tomorrow, now)
    assert not is_todays_match(dead, now)


def test_format_groups_leagues_and_shows_live_score():
    now = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    board = _board(
        _match("Liverpool", "Everton", now.replace(hour=15, minute=0)),
        _match(
            "Arsenal",
            "Chelsea",
            now.replace(hour=7, minute=30),
            state="in",
            clock="33'",
            home_score=1,
            away_score=0,
        ),
        _match(
            "Leeds",
            "Norwich",
            now.replace(hour=10, minute=0),
            slug="eng.2",
            chiclet="Championship",
        ),
        _match(
            "Tottenham",
            "West Ham",
            now.replace(hour=7, minute=0),
            state="post",
            clock="FT",
            home_score=2,
            away_score=1,
        ),
    )
    text = "\n\n".join(format_morning_digest(board, now))
    assert "Today · Sat 12 Sep" in text
    assert "**EPL**" in text and "**Championship**" in text
    assert "`3:00 PM` Liverpool vs Everton" in text
    assert "`LIVE 33'` Arsenal 1–0 Chelsea" in text
    assert "`FT` Tottenham 2–1 West Ham" in text
    assert "`10:00 AM` Leeds vs Norwich" in text
    assert "Liga MX" not in text


def test_pending_digest_none_before_window_then_posts_once():
    now = datetime(2026, 9, 12, 9, 29, tzinfo=TZ)
    later = datetime(2026, 9, 12, 9, 31, tzinfo=TZ)
    board = _board(_match("Liverpool", "Everton", later + timedelta(hours=5)))
    assert pending_digest(board=board, now=now, state={}) is None
    chunks = pending_digest(board=board, now=later, state={})
    assert chunks and "Liverpool vs Everton" in chunks[0]
    assert pending_digest(board=board, now=later, state={"sent_on": "2026-09-12"}) is None


def test_empty_day_still_posts_a_line():
    now = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    chunks = format_morning_digest({"leagues": []}, now)
    assert len(chunks) == 1
    assert "no matches" in chunks[0]


def test_long_slate_splits_into_discord_chunks():
    now = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    matches = [
        _match(
            f"Home Club {i:02d} United",
            f"Away Town {i:02d} Athletic",
            now.replace(hour=10, minute=min(i, 59)),
            slug="eng.2",
            chiclet="Championship",
        )
        for i in range(80)
    ]
    chunks = format_morning_digest(_board(*matches), now)
    assert len(chunks) >= 2
    assert all(len(c) <= 1800 for c in chunks)
    assert "Championship" in chunks[0]


def test_handle_command_today_uses_injected_board():
    now = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    board = _board(_match("Liverpool", "Everton", now.replace(hour=15, minute=0)))
    reply = handle_command("!soc today", board=board, now=now)
    assert reply and "Liverpool vs Everton" in reply
    assert "3:00 PM" in reply


def test_collect_digest_fetches_digest_leagues_only():
    now = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
    seen: dict = {}

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return _board(_match("Liverpool", "Everton", now.replace(hour=15)))

    chunks = collect_digest(now=now, fetch_board=fake_fetch)
    assert seen["leagues"] == DIGEST_LEAGUES
    assert seen["live_only"] is False
    assert seen["use_cache"] is False
    assert "Liverpool" in chunks[0]
