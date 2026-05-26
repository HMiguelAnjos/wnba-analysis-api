from typing import Literal, Optional
from pydantic import BaseModel

from src.schemas.anomaly_schemas import HotStatSchema


class LiveTeamSchema(BaseModel):
    team_id: int
    name: str
    tricode: str
    score: int


# BlowoutRiskSchema é declarado aqui (antes de LiveGameSchema) porque os
# game tiles do scoreboard expõem `blowout_risk` direto — assim o front
# pode mostrar um badge de alerta no card sem precisar buscar o ranking
# do jogo. Cálculo é trivial (só usa period/clock/score) e cacheado junto.
class BlowoutRiskSchema(BaseModel):
    """
    Probabilidade estimada de garbage time (titulares saindo, banco assumindo).
    Calculado a partir do contexto do placar + período + tempo restante.
    `final` é estado especial para jogos encerrados (não há "risco" futuro).
    """
    percentage: int                                                # 0–100
    level: Literal["low", "medium", "high", "final"]
    reason: str                                                    # explicação curta


class LiveGameSchema(BaseModel):
    game_id: str
    game_status: str
    period: int
    clock: str
    # ISO 8601 UTC do início agendado do jogo (ex: "2026-05-04T23:00:00Z").
    # O front converte pro timezone local do usuário no display.
    game_time_utc: str | None = None
    home_team: LiveTeamSchema
    away_team: LiveTeamSchema
    # Risco de blowout calculado a partir do scoreboard (period/clock/score).
    # None pra jogos não iniciados. Permite o front mostrar badge no card
    # mesmo sem ter o ranking carregado.
    blowout_risk: BlowoutRiskSchema | None = None


class TodayGamesSchema(BaseModel):
    date: str
    games: list[LiveGameSchema]
    # Fonte do scoreboard:
    #   "live"      → veio do nba_api.live (dia atual segundo a NBA)
    #   "scheduled" → veio do ScoreboardV2 (consulta por data específica,
    #                 usado quando o live ainda mostra jogos de ontem
    #                 mas o ET já virou).
    # Default "live" pra manter compat com fixtures e tests antigos.
    source: Literal["live", "scheduled"] = "live"
    # True quando todos os jogos retornados estão `final`. Permite o front
    # renderizar um cabeçalho "todos finalizados — próximos jogos amanhã"
    # sem precisar varrer a lista de novo.
    all_final: bool = False


# ------------------------------------------------------------------ #
# Boxscore                                                            #
# ------------------------------------------------------------------ #

class LivePlayerStatsSchema(BaseModel):
    player_id: int
    name: str
    position: str
    jersey_num: str = ""              # número da camisa (ex: "23"), default vazio
    is_starter: bool
    minutes: float
    points: int
    rebounds: int
    assists: int
    steals: int
    blocks: int
    turnovers: int
    field_goals_made: int
    field_goals_attempted: int
    three_pointers_made: int
    three_pointers_attempted: int
    free_throws_made: int
    free_throws_attempted: int
    plus_minus: int
    fouls: int
    on_court: bool


class LiveTeamBoxscoreSchema(BaseModel):
    team_id: int
    name: str
    tricode: str
    score: int
    players: list[LivePlayerStatsSchema]


class LiveBoxscoreSchema(BaseModel):
    game_id: str
    game_status: str
    period: int
    clock: str
    home_team: LiveTeamBoxscoreSchema
    away_team: LiveTeamBoxscoreSchema


# ------------------------------------------------------------------ #
# Live analysis                                                       #
# ------------------------------------------------------------------ #

class LiveCurrentStatsSchema(BaseModel):
    points: int
    rebounds: int
    assists: int
    field_goals_made: int
    field_goals_attempted: int
    three_pointers_made: int
    three_pointers_attempted: int
    free_throws_made: int
    free_throws_attempted: int


class LiveSeasonAverageSchema(BaseModel):
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


class LiveExpectedStatsSchema(BaseModel):
    points: float
    rebounds: float
    assists: float
    field_goals_made: float
    field_goals_attempted: float
    three_pointers_made: float
    three_pointers_attempted: float
    free_throws_made: float
    free_throws_attempted: float


class LiveDifferenceSchema(BaseModel):
    points: float
    rebounds: float
    assists: float
    field_goals_made: float
    field_goals_attempted: float
    three_pointers_made: float
    three_pointers_attempted: float
    free_throws_made: float
    free_throws_attempted: float


class LivePlayerAnalysisSchema(BaseModel):
    player_id: int
    name: str
    jersey_num: str = ""              # número da camisa
    team: str
    minutes: float
    fouls: int
    is_starter: bool
    on_court: bool
    current: LiveCurrentStatsSchema
    season_average: LiveSeasonAverageSchema
    expected_until_now: LiveExpectedStatsSchema
    difference: LiveDifferenceSchema
    shooting_impact: float
    status: str
    score: float
    # Nota 0–10 do jogador NESTA partida (mesma fórmula da aba Lineups).
    # Diferente de `score` (sinal interno de ranking), o rating é calibrado
    # pra ser mostrado direto pro usuário com label legível.
    performance_rating: float
    performance_label: str          # Excelente | Bom | Regular | Ruim | N/A
    low_confidence: bool             # True quando jogou < 10 min


class LiveAnalysisErrorSchema(BaseModel):
    player_id: int
    name: str
    reason: str


class LiveGameAnalysisSchema(BaseModel):
    game_id: str
    season: str
    game_status: str
    period: int
    clock: str
    analysis_type: str
    players: list[LivePlayerAnalysisSchema]
    hot_players: list[LivePlayerAnalysisSchema]
    cold_players: list[LivePlayerAnalysisSchema]
    errors: list[LiveAnalysisErrorSchema]


class LivePlayerComparisonSchema(BaseModel):
    player_id: int
    game_id: str
    name: str
    team: str
    minutes: float
    current: LiveCurrentStatsSchema
    season_average: LiveSeasonAverageSchema
    expected_until_now: LiveExpectedStatsSchema
    difference: LiveDifferenceSchema
    shooting_impact: float
    status: str
    analysis_type: str


# ------------------------------------------------------------------ #
# Hot ranking                                                         #
# ------------------------------------------------------------------ #

class PaceProjectionSchema(BaseModel):
    """
    Projeção até o fim do jogo com margem de erro + diagnóstico.

    `confidence` reflete o tamanho de amostra (minutos jogados) — projeções
    com poucos minutos são naturalmente menos confiáveis.

    `reason` é a explicação curta do que dominou a projeção (cold start,
    hot shrinkage, blowout, foul trouble, etc.) — útil pro front mostrar
    contexto pro usuário em vez de só um número.

    `indeterminate` (mai/2026): True quando a amostra é pequena demais
    pra projetar com confiança E o jogador ainda não produziu nada. Nesses
    casos, retornamos `expected = stat_atual` (sem extrapolação) e o front
    mostra "—" em vez de um número enganoso. Tanto a `decision` do
    `FairLineSchema` quanto a `bet_recommendation` viram `NEUTRAL`/`PASS`
    automaticamente.
    """
    low: float
    expected: float
    high: float
    confidence: Literal["very_low", "low", "medium", "high"] = "medium"
    reason: str = ""
    indeterminate: bool = False
    # Breakdown debug (mai/2026): dict opcional com TODOS os intermediários
    # da projeção (prior_rate, current_rate, weight_current, blended_rate
    # antes/depois do period_rate, target_minutes em cada step, multiplicadores
    # aplicados, sanity_cap). Front pode mostrar via ?debug=1.
    breakdown: Optional[dict] = None


class FairLineSchema(BaseModel):
    """
    Linha estimada (synthetic bookmaker) pra um mercado específico.

    `line` é a linha que estimamos que um bookmaker abriria.
    `edge` é (nossa_projeção_fim_de_jogo − line) — positivo = OVER tem
    valor; negativo = UNDER tem valor.
    `decision` é o resumo de strategy a partir do edge:
      STRONG_OVER (>=+3) | LEAN_OVER (>=+1) |
      NEUTRAL (-1<edge<+1) |
      LEAN_UNDER (<=-1) | STRONG_UNDER (<=-3).
    `reason` explica o que dominou o cálculo da linha (volume FGA acima,
      blowout, foul trouble, piso obrigatório, etc.). Útil pro front
      mostrar tooltip; default vazio pra compat.
    `betting_confidence` 0..1 (mai/2026) — convicção na recomendação,
      considerando heat alignment, magnitude do edge e qualidade da amostra.
      Quando heat e edge apontam DIREÇÕES OPOSTAS (jogador frio mas projeção
      OVER), confiança despenca. Não é o mesmo que projection_confidence:
      pode haver alta projeção com baixa convicção pra apostar.
    `betting_confidence_label` traduz pro front (low/medium/high).
    """
    line: float
    edge: float
    decision: Literal[
        "STRONG_OVER", "LEAN_OVER", "NEUTRAL", "LEAN_UNDER", "STRONG_UNDER"
    ]
    reason: str = ""
    betting_confidence: float = 0.0       # 0..1
    betting_confidence_label: Literal["low", "medium", "high"] = "low"
    # ── Linha REAL do mercado (mai/2026, The Odds API) ────────────────────
    # Quando ENABLE_REAL_ODDS=1 e o evento+jogador estão disponíveis, esses
    # campos são populados. Caso contrário ficam None e o front renderiza
    # apenas o synthetic. NÃO substitui `line/edge/decision` — fica lado a
    # lado pra usuário comparar nossa modelagem contra o mercado real.
    real_line: Optional[float] = None         # média entre books US
    real_edge: Optional[float] = None         # projeção − real_line
    real_decision: Optional[Literal[
        "STRONG_OVER", "LEAN_OVER", "NEUTRAL", "LEAN_UNDER", "STRONG_UNDER"
    ]] = None
    real_book_count: int = 0                  # quantos books contribuíram
    # Idade da linha real em segundos (mai/2026). Permite front mostrar
    # "linha atualizada há Xs" pro usuário avaliar frescor — útil em jogos
    # ao vivo onde linhas podem mover e nosso cache pode estar até ~60s
    # defasado em Q3-Q4. None quando real_line=None.
    real_line_age_seconds: Optional[int] = None
    # Recomendação ponderada (item 4, mai/2026): combina edge + betting_confidence
    # + usage do jogador. Diferente de `decision` (que olha só edge), considera
    # qualidade do sinal — edge alto com confidence baixa NÃO vira LARGE bet.
    # Quando real_line existe, baseado em real_edge; senão em edge synthetic.
    bet_recommendation: Literal["PASS", "SMALL", "MEDIUM", "LARGE"] = "PASS"
    bet_recommendation_size: float = 0.0      # 0.0 (pass) | 0.33 | 0.66 | 1.0


class ConfidenceBreakdownSchema(BaseModel):
    """
    Breakdown explicável das 4 confidences que compõem o sinal final do
    jogador. Cada uma 0..1, com label (low/medium/high) pro front pintar.

    - sample_confidence: tamanho de amostra (minutos jogados ATÉ AGORA).
      Mede quão estável é o ritmo observado neste jogo.
    - rotation_confidence: rotação histórica (nbarotations.info). 0 quando
      provider falha ou jogador sem perfil; alta quando ≥ 5 jogos coerentes.
    - projection_confidence: confiança da projeção fim-de-jogo. Alta quando
      minutos jogados altos + sem foul trouble/blowout/cold start.
    - overall_confidence: média ponderada das 3 acima (sample 40, projeção 40,
      rotação 20). É o número único que o front pode usar pra ordenação ou
      "filtro de qualidade do sinal".
    """
    sample: float
    sample_label: Literal["low", "medium", "high"]
    rotation: float
    rotation_label: Literal["low", "medium", "high"]
    projection: float
    projection_label: Literal["low", "medium", "high"]
    overall: float
    overall_label: Literal["low", "medium", "high"]


# BlowoutRiskSchema agora é definido no topo do arquivo (acima de
# LiveGameSchema) pra que game tiles do scoreboard possam expor o risco
# direto. Definição duplicada removida — uma única classe ativa.


class PlayerBlowoutImpactSchema(BaseModel):
    """
    IMPACTO do blowout sobre um JOGADOR específico.
    Diferente do risco do jogo: aqui dizemos se ESTE jogador tende a perder
    minutos. Reservas de fim de banco normalmente NÃO recebem impacto
    (eles ganham minutos no garbage time). Titulares e jogadores de alta
    minutagem recebem.
    """
    applies: bool                                                  # True → mostrar flag
    level: Literal["low", "medium", "high"]
    reason: str


class QuarterStatsSchema(BaseModel):
    """
    Stats de um jogador num período específico (quarto). Agregado a partir
    do play-by-play live. Períodos sem jogadas pro player simplesmente
    não aparecem na lista; o front renderiza "—" pra esses.
    """
    period: int                   # 1-4 = quartos regulares; 5+ = OT (1ª, 2ª, ...)
    points: int = 0
    assists: int = 0
    rebounds: int = 0
    three_pt_made: int = 0
    two_pt_made: int = 0
    # Tempo em quadra no período (mai/2026). Derivado das substituições
    # do PBP. Default 0.0 quando dados de subs não estão disponíveis.
    minutes_played: float = 0.0
    # Intervalos em quadra: cada item é [clock_in, clock_out] em minutos
    # restantes do período. Clock conta decrescente (12 → 0), então
    # clock_in > clock_out. Front usa pra renderizar a timeline visual.
    # Ex: [[12.0, 7.5], [4.0, 0.0]] = jogou 12:00→7:30 e 4:00→0:00.
    intervals: list[list[float]] = []


class HotRankingPlayerSchema(BaseModel):
    player_id: int
    name: str
    jersey_num: str = ""              # número da camisa
    team: str
    minutes: float
    current_points: int
    current_assists: int
    current_rebounds: int
    expected_points: float
    expected_assists: float
    expected_rebounds: float
    points_diff: float
    assists_diff: float
    rebounds_diff: float
    # Projeção BASE blended (mantida para compatibilidade — ritmo atual + temporada)
    projected_points: float
    projected_assists: float
    projected_rebounds: float
    # Projeção até o fim do jogo com margem de erro (peso alto no ritmo atual)
    pace_projection_points: PaceProjectionSchema
    pace_projection_assists: PaceProjectionSchema
    pace_projection_rebounds: PaceProjectionSchema
    # Projeção de 3PM (item 8, mai/2026): mercado existe nos books mas
    # não cobríamos. Usa mesmo motor (ProjectionEngine) com season +
    # last_5/10 do 3PM. None na pré-jogo.
    pace_projection_three_pm: Optional["PaceProjectionSchema"] = None
    fair_line_three_pm: Optional["FairLineSchema"] = None
    # Médias recentes — base para o synthetic fair line.
    last_5_points: float
    last_5_rebounds: float
    last_5_assists: float
    last_10_points: float
    last_10_rebounds: float
    last_10_assists: float
    # Linha estimada (synthetic bookmaker) + edge da nossa projeção.
    # Substitui o sinal puro de "atual vs esperado" por algo ancorado
    # na linha provável do mercado.
    fair_line_points: FairLineSchema
    fair_line_rebounds: FairLineSchema
    fair_line_assists: FairLineSchema
    # Contexto que altera a projeção (ajustes já aplicados em pace_projection_*)
    fouls: int
    foul_trouble: bool          # 4+ faltas com risco real de banco
    blowout_risk: bool          # DEPRECATED: use blowout_impact.applies; mantido pra compat
    blowout_impact: PlayerBlowoutImpactSchema | None  # None = não mostrar flag
    on_court: bool              # se está em quadra AGORA (vs descansando no banco)
    is_starter: bool            # titular (campo `starter` da NBA Live API)
    shooting_impact: float
    status: str
    # Status individual por stat (mai/2026): permite mostrar "frio em pts mas
    # quente em ast" no front, em vez de só o status composto.
    points_status: str = "normal"
    assists_status: str = "normal"
    rebounds_status: str = "normal"
    score: float
    # Nota 0–10 do jogador NESTA partida — substitui o `score` no display
    # do front (score continua exposto pra ordenação/decisão).
    performance_rating: float
    performance_label: str          # Excelente | Bom | Regular | Ruim | N/A
    # Heat signal (Fase 4) — composto eFG/FTA/volume/scoring run pra detecção
    # de "jogador quente". score ∈ [-1, +1]; label categoriza pra UI.
    heat_score: float = 0.0          # -1..+1
    heat_label: str = "neutral"      # very_cold|cold|neutral|hot|very_hot
    # Usage proxy (item 6, mai/2026): quão primário o jogador é no ataque
    # do time. 0.0 = role player puro, 1.0 = star primário. Derivado de
    # FGA/min vs distribuição da liga.
    usage: float = 0.5               # 0..1
    usage_label: str = "starter"     # primary_option|secondary_option|starter|role_player|low_usage
    # Rotation context (Fase 2 V3 / nbarotations.info, mai/2026) — None
    # quando provider falha ou feature flag desligada.
    rotation_context: Optional["RotationContextSchema"] = None
    # 4 confidences explícitas (mai/2026): sample/rotation/projection +
    # overall. Substitui o `low_confidence` flag binário por um breakdown
    # explicável que o front pode mostrar como ícones / cores. None aceito
    # pra compat retroativa quando o serviço não consegue computar (rara).
    confidence_breakdown: Optional["ConfidenceBreakdownSchema"] = None
    # Anomaly alerts (item 5, mai/2026): regras determinísticas pra
    # destacar performances anormais (microwave scorer, double-double,
    # foul trouble, etc.). Vazio = nada relevante; ordenado por severity
    # desc. Cada alert tem stat_type + severity + descrição humana.
    anomaly_alerts: list[HotStatSchema] = []
    # Cashout alert (mai/2026): jogador relevante no banco + linha colada
    # + rotação indica retorno provável → atenção pra cashout (NÃO é
    # entrada forte). None quando não se aplica.
    cashout_alert: Optional["CashoutAlertSchema"] = None
    # Similar games analysis (mai/2026): "quando ele teve um início
    # assim, o que aconteceu nos últimos casos similares?". Só populado
    # quando o jogador está em underperformance no Q1 — sinal pra validar
    # entradas tipo "tá 2pts em 8min, OVER vai virar?".
    # Hoje só analisa PONTOS; expansion futura pra REB/AST se valer a pena.
    similar_games_points: Optional["SimilarGamesResultSchema"] = None
    # Split por período (Q1/Q2/Q3/Q4/OT). Vazio se PBP indisponível
    # ou se o jogador não tem ações registradas em nenhum quarto.
    periods: list[QuarterStatsSchema] = []


class CashoutAlertSchema(BaseModel):
    """
    Alerta CASHOUT (mai/2026) — NÃO é "apostar forte".

    Sinaliza jogador relevante/titular que está MOMENTANEAMENTE no banco,
    com a linha colada na produção atual, e que a NBA Rotation indica que
    provavelmente volta ao jogo. Quando ele voltar, a linha tende a subir
    — então é hora de ATENÇÃO pra cashout / ajuste de posição, não de
    entrada agressiva.

    Derivado 100% de dados já existentes (rotation_context + fair_line +
    on_court + is_starter/usage). Não altera decision/bet_recommendation
    — é um aviso paralelo.
    """
    market: Literal["PTS", "REB", "AST"]
    label: str = "Cashout"
    icon: str = "money"
    reason: str
    confidence: Literal["MEDIUM", "HIGH"]
    # Metadata (debug + display)
    current_stat: float
    line: float
    difference_to_line: float          # line - current_stat (sempre 0.5..3.5)
    is_on_court: bool                  # sempre False quando alerta dispara
    expected_remaining_minutes: float
    closing_game_probability: float
    rotation_status: str               # "LIKELY_TO_RETURN"


class RotationContextSchema(BaseModel):
    """
    Saída explicável da camada de rotação (nbarotations.info).
    Visível no payload do hot ranking — front consome diretamente.
    """
    available: bool                                 # False = perfil ausente, fallback puro
    expected_remaining_minutes: float
    current_rotation_status: str                    # EXPECTED_REST | UNEXPECTED_REST | EXPECTED_ON_COURT | UNEXPECTED_ON_COURT | UNKNOWN
    blowout_risk: str                               # LOW | MEDIUM | HIGH
    closing_game_probability: float                 # 0..1
    rotation_confidence: float                      # 0..1
    notes: list[str] = []
    sample_games: int = 0


# Resolve forward ref do RotationContextSchema em HotRankingPlayerSchema.
# Pydantic v2 faz esse rebuild automaticamente, mas explícito é mais seguro.


class HotRankingSchema(BaseModel):
    game_id: str
    limit: int
    ranking: list[HotRankingPlayerSchema]
    # Estado do jogo no momento do request — front usa pra renderizar
    # placar/relógio sem precisar refazer chamada ao scoreboard.
    game_status: str                                               # not_started | in_progress | final
    period: int
    clock: str
    home_score: int
    away_score: int
    blowout_risk: BlowoutRiskSchema
    updated_at: str                                                # ISO 8601 UTC do snapshot


# ------------------------------------------------------------------ #
# Live games cached response                                          #
# ------------------------------------------------------------------ #

class LiveGamesCachedResponseSchema(BaseModel):
    date: str
    games: list[LiveGameSchema]
    updated_at: str          # ISO 8601 UTC
    age_ms: int              # milliseconds since last worker update
    source: Literal["cache"] = "cache"
    # Origem do scoreboard subjacente (do TodayGamesSchema):
    #   - "live"      → veio do nba_api.live (rotação normal)
    #   - "scheduled" → veio do ScoreboardV2 (fallback quando live ainda
    #                   mostrava ontem). Front usa pra badge contextual.
    # Nome diferente de `source` pra não conflitar com o "cache" acima.
    scoreboard_source: Literal["live", "scheduled"] = "live"
    # True quando todos os jogos retornados são `final`. Front usa pra
    # mostrar "🏁 Todos finalizados" quando o fallback não rolou.
    all_final: bool = False


# ------------------------------------------------------------------ #
# Lineups (titulares/reservas + foto + nota de desempenho)            #
# ------------------------------------------------------------------ #
# Diferente do LivePlayerStatsSchema (que filtra quem não jogou e foca
# em produzir análise), este schema mostra o ELENCO COMPLETO do time —
# inclusive jogadores inativos, ainda no banco com 0 minutos, etc.
# Todos os flags vêm direto da NBA Live API (oficial), sem inferência.

class LineupPlayerSchema(BaseModel):
    player_id: int
    name: str
    jersey_num: str
    position: str                    # "PG", "SG", "SF", "PF", "C" ou ""
    is_starter: bool                 # NBA: player.starter == "1"
    is_on_court: bool                # NBA: player.oncourt == "1"
    played: bool                     # NBA: player.played == "1"
    status: str                      # "ACTIVE" | "INACTIVE"
    not_playing_reason: str | None
    photo_url: str                   # CDN da NBA, sempre 200 (fallback silhueta)
    minutes: float
    points: int
    rebounds: int
    assists: int
    steals: int
    blocks: int
    turnovers: int
    fouls: int
    field_goals_made: int
    field_goals_attempted: int
    three_pointers_made: int
    three_pointers_attempted: int
    free_throws_made: int
    free_throws_attempted: int
    plus_minus: int
    performance_rating: float        # 0–10
    performance_label: str           # Excelente | Bom | Regular | Ruim | N/A
    low_confidence: bool             # True se <10 min jogados
    blowout_impact: PlayerBlowoutImpactSchema | None  # None = sem flag
    # Split por quarter do PBP — mesmo formato usado no Hot Picks live.
    # Lista vazia quando o jogo não começou ou PBP indisponível.
    periods: list[QuarterStatsSchema] = []


class LineupTeamSchema(BaseModel):
    team_id: int
    name: str
    tricode: str
    score: int
    starters: list[LineupPlayerSchema]      # 5 jogadores titulares
    bench: list[LineupPlayerSchema]         # reservas (jogaram OU no banco)
    inactive: list[LineupPlayerSchema]      # status == INACTIVE


class LineupGameSchema(BaseModel):
    game_id: str
    game_status: str
    period: int
    clock: str
    home_team: LineupTeamSchema
    away_team: LineupTeamSchema
    blowout_risk: BlowoutRiskSchema
    updated_at: str                                                # ISO 8601 UTC


# ─── Rotação (mai/2026) ──────────────────────────────────────────────────
# Aba dedicada: padrão histórico de rotação de cada jogador (nbarotations).
# Heatmap dos 48 minutos do jogo (prob. de estar em quadra) + janelas de
# descanso típicas + flag "quase voltando" quando o jogador está no banco
# mas o histórico diz que costuma voltar nos próximos minutos.


class PlayerRotationSchema(BaseModel):
    player_id: int
    name: str
    jersey_num: str
    position: str
    photo_url: str
    is_starter: bool
    is_on_court: bool
    minutes_played: float                  # minutos NESTE jogo até agora
    # ── Perfil histórico (nbarotations) ──
    has_profile: bool                      # False = fallback (sem dado real)
    sample_games: int                      # jogos amostrados pro histograma
    avg_minutes: float                     # média de minutos totais
    # Heatmap: 48 floats (0..1) — prob. de estar em quadra cada minuto do
    # jogo. minute_idx 0..47 = Q1 0:00 → Q4 12:00. Vazio quando fallback.
    minute_probabilities: list[float] = []
    # Janelas de descanso típicas: lista de [start_min, end_min] (0-47)
    # onde a prob. de jogar é baixa. Pra resumo legível ("descansa Q2 cedo").
    rest_windows: list[list[int]] = []
    # ── Estado ao vivo (só quando in_progress) ──
    current_rotation_status: str = "UNKNOWN"
    expected_remaining_minutes: float = 0.0
    # "Quase voltando": no banco AGORA mas o histórico diz que costuma
    # voltar nos próximos minutos. about_to_return=True + minuto típico.
    about_to_return: bool = False
    return_in_minutes: Optional[float] = None
    notes: list[str] = []


class GameRotationsSchema(BaseModel):
    game_id: str
    game_status: str
    period: int
    clock: str
    home_team_tricode: str
    away_team_tricode: str
    home_players: list[PlayerRotationSchema]
    away_players: list[PlayerRotationSchema]
    updated_at: str                                                # ISO 8601 UTC


# ─── Pre-game preview (mai/2026) ─────────────────────────────────────────
# Tela pré-jogo: mostra médias da temporada, recents e linha dos books
# pra prováveis titulares + bench top. NÃO chama de "projeção" — é
# briefing contextual antes do tipoff. Linhas dos books só vêm populadas
# nos últimos 5 min antes do tipoff (prefetch window).


class GamePreviewPlayerSchema(BaseModel):
    """Stats relevantes pré-jogo pra UM jogador."""
    player_id: int
    name: str
    jersey_num: str = ""
    position: str = ""             # PG/SG/SF/PF/C
    team: str                       # tricode
    is_starter: bool                # do lineup oficial / probable starters
    # Season averages
    season_points: float
    season_rebounds: float
    season_assists: float
    season_minutes: float
    season_three_pm: float
    # Recent form (compat com `null` quando gamelog insuficiente)
    last_5_points: float
    last_5_rebounds: float
    last_5_assists: float
    last_10_points: float
    last_10_rebounds: float
    last_10_assists: float
    # Real lines (Bet365 / DK / FD via The Odds API)
    # None quando fora da janela de prefetch (>5 min antes do tipoff) ou
    # quando o jogador não tem cobertura no The Odds API.
    line_points: Optional[float] = None
    line_rebounds: Optional[float] = None
    line_assists: Optional[float] = None
    book_count: int = 0             # quantos books contribuíram (PTS como proxy)


class GamePreviewMatchupSchema(BaseModel):
    """Contexto de matchup entre os 2 times."""
    home_drtg: float
    away_drtg: float
    home_pace: float
    away_pace: float
    combined_pace: float
    # Total estimado de pontos do jogo (heuristica: combined_pace * 2.2)
    expected_total: int


class SimilarGameSchema(BaseModel):
    """
    Um jogo histórico onde o jogador teve um Q1 similar ao atual.
    Surfaceia "quando ele teve um início assim, o que aconteceu".
    """
    game_id: str
    game_date: str
    matchup: str                       # ex: "SAS @ MIN" ou "SAS vs MEM"
    first_quarter_stat: int            # PTS/REB/AST no Q1
    final_stat: int                    # stat final do jogo
    final_minutes: float                # minutos totais jogados


class SimilarGamesResultSchema(BaseModel):
    """
    Análise de jogos similares (mai/2026): "ele costuma se recuperar
    de inícios assim?". Útil pra validar entradas tipo "KJ tá 2 pts
    em 8 min — apostar OVER ou ele continua frio?".

    Computado lazy: só quando jogador está em situação de underperformance
    no Q1 (atual < 0.6 × prior rate).
    """
    stat: str                          # "points" | "rebounds" | "assists"
    current_first_quarter: int         # stat atual referência do Q1
    sample_size: int                   # quantos jogos similares foram analisados
    games: list[SimilarGameSchema]      # até 10 mais recentes (ordenados desc)
    avg_final_stat: float              # média do stat final nos similares
    median_final_stat: float           # mediana (robusta a outliers)
    season_avg: float                  # média da temporada (referência)
    # Recovery factor: avg_final_stat / season_avg
    #   > 1.0 = costuma virar (recuperação típica)
    #   = 1.0 = neutro
    #   < 1.0 = jogos similares costumam acabar abaixo do normal
    recovery_factor: float


class GamePreviewSchema(BaseModel):
    """
    Briefing pré-jogo. Mostra players prováveis com stats + linhas dos books.

    Quando `game_status == "in_progress"` ou `"final"`, esse endpoint
    devolve a mesma estrutura mas sem chamar mais. Front deve usar
    `/live-hot-ranking` pra dados live.
    """
    game_id: str
    game_status: str
    game_time_utc: Optional[str] = None
    minutes_to_tipoff: Optional[float] = None      # negativo se já passou
    real_lines_available: bool                      # True se prefetch ativou
    home_team: LiveTeamSchema
    away_team: LiveTeamSchema
    starters_home: list[GamePreviewPlayerSchema]
    starters_away: list[GamePreviewPlayerSchema]
    bench_top_home: list[GamePreviewPlayerSchema]  # top 3 bench por min/jogo
    bench_top_away: list[GamePreviewPlayerSchema]
    matchup: Optional[GamePreviewMatchupSchema] = None
    updated_at: str                                  # ISO 8601 UTC


# ── Resolve forward ref de RotationContextSchema ──────────────────────────
# HotRankingPlayerSchema usa "RotationContextSchema" como string porque o
# schema é declarado depois. Em Pydantic v2 chamamos model_rebuild() explícito.
HotRankingPlayerSchema.model_rebuild()
