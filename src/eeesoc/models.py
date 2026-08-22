"""Core match / snapshot models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class GoalEvent:
    minute: int
    team: str  # "home" | "away"


@dataclass(frozen=True)
class MatchSnapshot:
    """Frozen in-play state used for Similar lookalikes."""

    minute: int
    home_goals: int
    away_goals: int
    home_shots: int
    away_shots: int
    home_sot: int
    away_sot: int
    goal_minutes: tuple[int, ...] = ()

    def label(self) -> str:
        goals = "/".join(f"{m}'" for m in self.goal_minutes) or "—"
        return (
            f"{goals} · {self.home_shots}/{self.home_sot} vs "
            f"{self.away_shots}/{self.away_sot}"
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchSnapshot:
        return cls(
            minute=int(data["minute"]),
            home_goals=int(data["home_goals"]),
            away_goals=int(data["away_goals"]),
            home_shots=int(data["home_shots"]),
            away_shots=int(data["away_shots"]),
            home_sot=int(data["home_sot"]),
            away_sot=int(data["away_sot"]),
            goal_minutes=tuple(int(m) for m in data.get("goal_minutes", ())),
        )


@dataclass
class Match:
    match_id: str
    season: str
    date: str
    home: str
    away: str
    home_goals_ft: int
    away_goals_ft: int
    home_shots_ft: int
    away_shots_ft: int
    home_sot_ft: int
    away_sot_ft: int
    home_goals_ht: int = 0
    away_goals_ht: int = 0
    goals: list[GoalEvent] = field(default_factory=list)
    # Cumulative shots/SOT at each minute 1..90 (index 0 unused)
    home_shots_by_min: list[int] = field(default_factory=list)
    away_shots_by_min: list[int] = field(default_factory=list)
    home_sot_by_min: list[int] = field(default_factory=list)
    away_sot_by_min: list[int] = field(default_factory=list)

    def snapshot_at(self, minute: int) -> MatchSnapshot:
        minute = max(1, min(90, int(minute)))
        hs = _at(self.home_shots_by_min, minute)
        as_ = _at(self.away_shots_by_min, minute)
        hst = _at(self.home_sot_by_min, minute)
        ast = _at(self.away_sot_by_min, minute)
        scored = [g for g in self.goals if g.minute <= minute]
        return MatchSnapshot(
            minute=minute,
            home_goals=sum(1 for g in scored if g.team == "home"),
            away_goals=sum(1 for g in scored if g.team == "away"),
            home_shots=hs,
            away_shots=as_,
            home_sot=hst,
            away_sot=ast,
            goal_minutes=tuple(g.minute for g in scored),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "season": self.season,
            "date": self.date,
            "home": self.home,
            "away": self.away,
            "home_goals_ft": self.home_goals_ft,
            "away_goals_ft": self.away_goals_ft,
            "home_shots_ft": self.home_shots_ft,
            "away_shots_ft": self.away_shots_ft,
            "home_sot_ft": self.home_sot_ft,
            "away_sot_ft": self.away_sot_ft,
            "home_goals_ht": self.home_goals_ht,
            "away_goals_ht": self.away_goals_ht,
            "goals": [{"minute": g.minute, "team": g.team} for g in self.goals],
            "home_shots_by_min": self.home_shots_by_min,
            "away_shots_by_min": self.away_shots_by_min,
            "home_sot_by_min": self.home_sot_by_min,
            "away_sot_by_min": self.away_sot_by_min,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Match:
        goals = [
            GoalEvent(minute=int(g["minute"]), team=str(g["team"]))
            for g in data.get("goals", [])
        ]
        return cls(
            match_id=str(data["match_id"]),
            season=str(data["season"]),
            date=str(data["date"]),
            home=str(data["home"]),
            away=str(data["away"]),
            home_goals_ft=int(data["home_goals_ft"]),
            away_goals_ft=int(data["away_goals_ft"]),
            home_shots_ft=int(data["home_shots_ft"]),
            away_shots_ft=int(data["away_shots_ft"]),
            home_sot_ft=int(data["home_sot_ft"]),
            away_sot_ft=int(data["away_sot_ft"]),
            home_goals_ht=int(data.get("home_goals_ht", 0)),
            away_goals_ht=int(data.get("away_goals_ht", 0)),
            goals=goals,
            home_shots_by_min=list(data.get("home_shots_by_min") or []),
            away_shots_by_min=list(data.get("away_shots_by_min") or []),
            home_sot_by_min=list(data.get("home_sot_by_min") or []),
            away_sot_by_min=list(data.get("away_sot_by_min") or []),
        )


def _at(series: list[int], minute: int) -> int:
    if not series:
        return 0
    if minute >= len(series):
        return series[-1]
    return series[minute]
