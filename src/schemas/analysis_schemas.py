from pydantic import BaseModel


class StatAveragesSchema(BaseModel):
    points: float
    rebounds: float
    assists: float
    minutes: float
    field_goals_made: float
    field_goals_attempted: float
    three_pointers_made: float
    three_pointers_attempted: float
    free_throws_made: float
    free_throws_attempted: float


class TrendSchema(BaseModel):
    points_vs_season_average: float
    rebounds_vs_season_average: float
    assists_vs_season_average: float


class SeasonAnalysisSchema(BaseModel):
    player_id: int
    season: str
    games_played: int
    averages: StatAveragesSchema
    last_5_games: StatAveragesSchema
    last_10_games: StatAveragesSchema
    trend: TrendSchema


class GameStatSchema(BaseModel):
    game_id: str
    game_date: str
    matchup: str
    minutes: int
    points: int
    rebounds: int
    assists: int


class PbpErrorSchema(BaseModel):
    game_id: str
    reason: str


class PointsByPeriodAverageSchema(BaseModel):
    player_id: int
    season: str
    games_analyzed: int
    points_by_period_average: dict[str, float]
    total_average: float
    errors: list[PbpErrorSchema]


class DashboardSummarySchema(BaseModel):
    games_played: int
    season_points_average: float
    last_5_points_average: float
    last_10_points_average: float


class DashboardPeriodsSchema(BaseModel):
    points_by_period_average: dict[str, float]


class DashboardTrendSchema(BaseModel):
    status: str
    points_difference_last_5_vs_season: float
    points_difference_last_10_vs_season: float


class DashboardGameSchema(BaseModel):
    game_id: str
    game_date: str
    matchup: str
    points: int
    rebounds: int
    assists: int
    minutes: int


class DashboardSchema(BaseModel):
    player_id: int
    season: str
    summary: DashboardSummarySchema
    periods: DashboardPeriodsSchema
    recent_games: list[DashboardGameSchema]
    trend: DashboardTrendSchema
