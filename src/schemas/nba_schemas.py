from pydantic import BaseModel
from typing import Optional


class PlayerSchema(BaseModel):
    id: int
    full_name: str
    first_name: str
    last_name: str
    is_active: bool


class GameLogSchema(BaseModel):
    game_id: str
    game_date: str
    matchup: str
    minutes: str
    points: int
    rebounds: int
    assists: int
    field_goals_made: int
    field_goals_attempted: int
    three_pointers_made: int
    three_pointers_attempted: int
    free_throws_made: int
    free_throws_attempted: int


class PlayByPlayEventSchema(BaseModel):
    period: int
    clock: str
    event_type: str
    player_name: Optional[str]
    description_home: Optional[str]
    description_visitor: Optional[str]
    score: Optional[str]


class PointsByPeriodSchema(BaseModel):
    player_id: int
    game_id: str
    points_by_period: dict[str, int]
    total_points: int


# ─── Hot Board (forma recente da liga inteira) ──────────────────────────────
# Derivado de LeagueDashPlayerStats (3 chamadas cacheadas: temporada, L5,
# L10). NÃO é projeção nem recomendação — só leitura de forma recente.
class HotBoardPlayerSchema(BaseModel):
    player_id: int
    name: str
    team: str
    market: str                 # PTS | REB | AST
    season: float
    last5: float
    last10: float
    pct: float                  # variação vs média (−1..+1+)
    score: float                # normalizado −1..+1
    label: str                  # on_fire|heating|stable|cold|ice_cold


class HotBoardMarketSchema(BaseModel):
    hot: list[HotBoardPlayerSchema]
    cold: list[HotBoardPlayerSchema]


class HotBoardSchema(BaseModel):
    season: str
    updated_at: str
    evaluated: int
    available: bool             # False = NBA bloqueou/sem dados → front faz fallback
    PTS: HotBoardMarketSchema
    REB: HotBoardMarketSchema
    AST: HotBoardMarketSchema


# ─── Escalação provável (pré-jogo, informativo — SEM aposta) ───────────────
# Quem normalmente joga (rotação por minutos da temporada) + performance
# nos últimos 3/5/10 jogos. Derivado das janelas cacheadas da liga
# (mesmo custo do hot board). NÃO é a escalação oficial — a NBA só
# publica ~30-60min antes; isto é "provável".
class ProbableStatLine(BaseModel):
    games: int                  # jogos na janela (≤ N)
    points: float
    rebounds: float
    assists: float


class ProbablePlayer(BaseModel):
    player_id: int
    name: str
    team: str
    probable_starter: bool      # heurística: top-5 por minutos do time
    season_minutes: float
    season_games: int
    season: ProbableStatLine
    last3: ProbableStatLine
    last5: ProbableStatLine
    last10: ProbableStatLine


class ProbableTeam(BaseModel):
    tricode: str
    players: list[ProbablePlayer]


class ProbableLineupSchema(BaseModel):
    game_id: str
    season: str
    updated_at: str
    available: bool             # False = sem dados (NBA bloqueou) → fallback
    note: str                   # rótulo "provável / informativo"
    home: ProbableTeam
    away: ProbableTeam


# ─── Team board (ataque/defesa por time — liga inteira, 1 cache) ───────────
# Pontos marcados/sofridos por jogo. Custo ~zero (2 chamadas cacheadas
# 30min, liga inteira). Alimenta o hero do Dashboard pré-jogo.
class TeamBoardTeam(BaseModel):
    team_id: int
    tricode: str
    name: str
    games: int
    points: float               # pontos marcados / jogo
    opp_points: float           # pontos sofridos / jogo
    plus_minus: float


class TeamBoardSchema(BaseModel):
    season: str
    updated_at: str
    available: bool
    teams: list[TeamBoardTeam]
