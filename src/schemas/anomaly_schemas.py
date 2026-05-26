from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

StatType = Literal["PTS", "REB", "AST", "3PM", "EFF", "STL", "BLK", "FOUL"]
Severity = Literal["LOW", "MEDIUM", "HIGH", "EXTREME"]


class AnomalyPlayerStatsSchema(BaseModel):
    player_id: int
    player_name: str
    team_abbr: str
    minutes: float
    points: int
    rebounds: int
    assists: int
    steals: int
    blocks: int
    three_pointers_made: int
    fouls_personal: Optional[int] = None
    minute_of_game: int


class HotStatSchema(BaseModel):
    player_id: int
    player_name: str
    team_abbr: str
    stat_type: StatType
    value: float
    pace: float
    projected_total: float
    anomaly_score: float
    severity: Severity
    description: str
    minute_of_game: int


class AnomalyResponseSchema(BaseModel):
    game_id: str
    minute_of_game: int
    alerts: list[HotStatSchema]
