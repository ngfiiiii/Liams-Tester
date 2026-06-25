from dataclasses import dataclass, field
from typing import Optional

@dataclass
class PlayerResult:
    name: str
    pr: float = 0.0
    placement: Optional[int] = None
    kills: Optional[int] = None
    damage: Optional[int] = None
    team: Optional[str] = None
    source: str = "session"

@dataclass
class TeamResult:
    placement: Optional[int] = None
    players: list[str] = field(default_factory=list)
    points: Optional[int] = None
    eliminations: Optional[int] = None
    time_played: Optional[str] = None
    damage_text: Optional[str] = None
    combined_pr: float = 0.0

    # Live state inferred from Fortnite Tracker timeline/placement data.
    is_eliminated: bool = False
    eliminated_at: Optional[str] = None
    eliminated_order: Optional[int] = None

    @property
    def display_name(self) -> str:
        return " / ".join(self.players) if self.players else "Unknown team"
