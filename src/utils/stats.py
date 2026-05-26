from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.schemas.nba_schemas import GameLogSchema

logger = logging.getLogger(__name__)

TREND_THRESHOLD = 1.0
POINTS_WEIGHT = 0.85
REBOUNDS_WEIGHT = 0.6
ASSISTS_WEIGHT = 0.7
SHOT_VOLUME_BONUS_WEIGHT = 0.12
FIELD_GOAL_MADE_WEIGHT = 0.45
THREE_POINTER_MADE_WEIGHT = 0.35
FREE_THROW_MADE_WEIGHT = 0.2
FIELD_GOAL_MISS_WEIGHT = 0.3
FREE_THROW_MISS_WEIGHT = 0.15


def parse_minutes(min_str: str) -> float:
    """Convert MIN field to float minutes. Handles '34:30', '34.5', '34'."""
    s = str(min_str).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) + int(parts[1]) / 60
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def rounded(value: float) -> float:
    return round(value, 1)


def safe_average(values: list[float]) -> float:
    if not values:
        return 0.0
    return rounded(sum(values) / len(values))


def calc_stat_averages(logs: list["GameLogSchema"]) -> dict[str, float]:
    if not logs:
        return {
            "points": 0.0,
            "rebounds": 0.0,
            "assists": 0.0,
            "minutes": 0.0,
            "field_goals_made": 0.0,
            "field_goals_attempted": 0.0,
            "three_pointers_made": 0.0,
            "three_pointers_attempted": 0.0,
            "free_throws_made": 0.0,
            "free_throws_attempted": 0.0,
        }
    return {
        "points": safe_average([float(g.points) for g in logs]),
        "rebounds": safe_average([float(g.rebounds) for g in logs]),
        "assists": safe_average([float(g.assists) for g in logs]),
        "minutes": safe_average([parse_minutes(g.minutes) for g in logs]),
        "field_goals_made": safe_average([float(g.field_goals_made) for g in logs]),
        "field_goals_attempted": safe_average([float(g.field_goals_attempted) for g in logs]),
        "three_pointers_made": safe_average([float(g.three_pointers_made) for g in logs]),
        "three_pointers_attempted": safe_average([float(g.three_pointers_attempted) for g in logs]),
        "free_throws_made": safe_average([float(g.free_throws_made) for g in logs]),
        "free_throws_attempted": safe_average([float(g.free_throws_attempted) for g in logs]),
    }


def calc_trend_status(last5_pts: float, season_pts: float) -> str:
    diff = last5_pts - season_pts
    if diff > TREND_THRESHOLD:
        return "above_average"
    if diff < -TREND_THRESHOLD:
        return "below_average"
    return "stable"


def calc_shooting_impact(
    field_goals_made_diff: float,
    field_goals_attempted_diff: float,
    three_pointers_made_diff: float,
    free_throws_made_diff: float,
    field_goal_misses_diff: float,
    free_throw_misses_diff: float,
) -> float:
    return rounded(
        field_goals_made_diff * FIELD_GOAL_MADE_WEIGHT
        + max(field_goals_attempted_diff, 0.0) * SHOT_VOLUME_BONUS_WEIGHT
        + three_pointers_made_diff * THREE_POINTER_MADE_WEIGHT
        + free_throws_made_diff * FREE_THROW_MADE_WEIGHT
        - field_goal_misses_diff * FIELD_GOAL_MISS_WEIGHT
        - free_throw_misses_diff * FREE_THROW_MISS_WEIGHT
    )


def calc_player_score(
    points_diff: float,
    rebounds_diff: float,
    assists_diff: float,
    shooting_impact: float,
) -> float:
    box_score_component = (
        points_diff * POINTS_WEIGHT
        + rebounds_diff * REBOUNDS_WEIGHT
        + assists_diff * ASSISTS_WEIGHT
    )
    return rounded(box_score_component + shooting_impact)


def calc_player_status(score: float) -> str:
    if score >= 5:
        return "hot"
    if score >= 2:
        return "above_average"
    if score > -2:
        return "normal"
    if score > -5:
        return "below_average"
    return "cold"


_PER_STAT_STATUS_THRESHOLDS = {
    "points":   (5.0, 2.0, -2.0, -5.0),
    "rebounds": (3.0, 1.0, -1.0, -3.0),
    "assists":  (3.0, 1.0, -1.0, -3.0),
}


def calc_per_stat_status(diff: float, stat: str) -> str:
    """
    Status (hot/above_average/normal/below_average/cold) calculado por stat
    individual a partir da diferença vs média de temporada (ou prior).

    Diferente de `calc_player_status`, que classifica o jogador inteiro a
    partir de um score composto, isso permite mostrar "frio em pts mas
    quente em ast" — útil pra apostas em props específicas.
    """
    thresholds = _PER_STAT_STATUS_THRESHOLDS.get(stat)
    if thresholds is None:
        return "normal"
    hot, above, below, cold = thresholds
    if diff >= hot:
        return "hot"
    if diff >= above:
        return "above_average"
    if diff > below:
        return "normal"
    if diff > cold:
        return "below_average"
    return "cold"


_CONFIDENCE_LABEL_THRESHOLDS = (0.66, 0.40)  # ≥0.66 high, ≥0.40 medium, < low


def confidence_label(value: float) -> str:
    """Mapeia float [0..1] em low/medium/high pra UI."""
    if value >= _CONFIDENCE_LABEL_THRESHOLDS[0]:
        return "high"
    if value >= _CONFIDENCE_LABEL_THRESHOLDS[1]:
        return "medium"
    return "low"


def sample_confidence_from_minutes(minutes: float) -> float:
    """
    Confiança da AMOSTRA (minutos jogados ATÉ AGORA neste jogo).

    Curva calibrada com a tabela existente do ProjectionEngine:
      <6  min  → very_low (0.10)
      <14 min  → low      (0.35)
      <24 min  → medium   (0.65)
      ≥24 min  → high     (0.90)
    """
    if minutes >= 24:
        return 0.90
    if minutes >= 14:
        return 0.65
    if minutes >= 6:
        return 0.35
    return 0.10


def projection_confidence_from_label(label: str) -> float:
    """Mapeia o `confidence` string da projeção (very_low..high) em [0..1]."""
    return {
        "high": 0.90,
        "medium": 0.65,
        "low": 0.35,
        "very_low": 0.10,
    }.get(label, 0.50)


def bet_recommendation(
    *,
    edge: float,
    betting_confidence: float,
    usage: float = 0.5,
    has_real_book: bool = False,
    variance_factor: float = 1.0,
) -> tuple[str, float]:
    """
    Converte (edge, confidence) numa recomendação discreta de tamanho.

    Tamanho ∈ {PASS, SMALL, MEDIUM, LARGE}. Idéia: NÃO basta ter edge
    alto — confidence também precisa segurar. Edge=+5 com confidence
    de 0.20 (lixo de sample) deve ser PASS, não LARGE.

    Matriz de decisão (calibrada conservadora):

      LARGE:  edge ≥ 3.0 AND confidence ≥ 0.75
      MEDIUM: edge ≥ 2.0 AND confidence ≥ 0.60
      SMALL:  edge ≥ 1.0 AND confidence ≥ 0.45
      PASS:   tudo abaixo

    `usage` modula: role player < 0.30 nunca chega em MEDIUM/LARGE.
    Star primary > 0.65 ganha boost de meio degrau (LARGE em 0.70).

    BOOK BYPASS (mai/2026):
      Quando `has_real_book=True` E o edge passado (que já é o real_edge
      do book) é ≥ 2.0 E `betting_confidence ≥ 0.30`, aceitamos SMALL
      como mínimo mesmo com confidence baixa. O book serve como "segunda
      opinião" — falso positivo solo do nosso modelo é mitigado quando
      o book também vê edge.

      Floor de 0.30 evita pathological: cara com sinais super
      contraditórios (heat oposto ao edge, sample 4 min, etc) chega a
      confidence < 0.30. Mesmo book agreeing nesses casos é arriscado
      demais — provavelmente os dois estão calibrando com poucos dados.

      Bypass NÃO escalona pra MEDIUM/LARGE — confidence ainda governa
      tamanhos maiores (segurança em camadas).

    VARIANCE PENALTY (mai/2026, P-final):
      `variance_factor` (0..1, computado por player_variance_factor) penaliza
      tamanho da aposta pra caras voláteis:
        < 0.30 (muito volátil): cap em SMALL, downgrade STRONG → LEAN
        0.30-0.50 (volátil): cap em MEDIUM (nunca LARGE)
        >= 0.50 (consistente): matriz normal

      Filosofia: rookies e role players com upside variável têm muito
      ruído game-to-game pra justificar apostas grandes. Mesma edge
      teórica em cara consistente é mais confiável.

    Args:
        edge: projection − line (synthetic OU real, conforme caller)
        betting_confidence: 0..1
        usage: 0..1 (vem de usage_proxy)
        has_real_book: True quando o `edge` passado veio do book real
        variance_factor: 0..1 (1=consistente, 0=imprevisível)

    Returns:
        (label, confidence_multiplier) — fração da aposta full-stack.
    """
    abs_edge = abs(edge)

    # Book bypass: book real concorda com edge significativo (≥ 2.0)
    # E temos pelo menos confidence mínima (≥ 0.30) — evita pathological
    # com sinais super contraditórios.
    BOOK_BYPASS_CONF_FLOOR = 0.30
    book_bypass = (
        has_real_book
        and abs_edge >= 2.0
        and betting_confidence >= BOOK_BYPASS_CONF_FLOOR
    )

    # Edges pequenos < 1 sempre PASS (book bypass não ajuda)
    if abs_edge < 1.0:
        return ("PASS", 0.0)

    # Confidence muito baixa → PASS, EXCETO se book bypass ativo.
    # Com book bypass: aceita SMALL (book validou direção + magnitude
    # + temos confidence mínima de 0.30).
    if betting_confidence < 0.40:
        if book_bypass:
            return ("SMALL", 0.33)
        return ("PASS", 0.0)

    # Role player (usage baixo = volátil demais)
    if usage < 0.30:
        # Cap em SMALL — nunca chegam em MEDIUM/LARGE.
        if abs_edge >= 1.5 and betting_confidence >= 0.55:
            return ("SMALL", 0.5)
        # Book bypass também aceita SMALL em role player com confidence
        # menor (já passou ≥ 0.40 acima + book concorda).
        if book_bypass:
            return ("SMALL", 0.33)
        return ("PASS", 0.0)

    # Boost pra primary option (mais previsível)
    primary_boost = usage >= 0.65

    # Matriz principal — determina TIER teórico
    if abs_edge >= 3.0 and betting_confidence >= (0.70 if primary_boost else 0.75):
        rec = ("LARGE", 1.0)
    elif abs_edge >= 2.0 and betting_confidence >= (0.55 if primary_boost else 0.60):
        rec = ("MEDIUM", 0.66)
    elif abs_edge >= 1.0 and betting_confidence >= 0.45:
        rec = ("SMALL", 0.33)
    else:
        return ("PASS", 0.0)

    # VARIANCE PENALTY — cap por volatilidade individual do jogador
    if variance_factor < 0.30:
        # Muito volátil → cap em SMALL (nunca MEDIUM/LARGE)
        if rec[0] in ("MEDIUM", "LARGE"):
            return ("SMALL", 0.33)
    elif variance_factor < 0.50:
        # Volátil → cap em MEDIUM
        if rec[0] == "LARGE":
            return ("MEDIUM", 0.66)

    return rec


def betting_confidence_from_signals(
    *,
    edge: float,
    heat_score: float,
    sample_confidence: float,
    projection_confidence: float,
) -> float:
    """
    Convicção pra apostar (item 7).

    Ideia central: NÃO basta ter edge alto — o sinal precisa estar
    alinhado com o heat. Se o edge diz OVER (+) mas heat diz "frio"
    (-), as duas leituras se contradizem e a confiança despenca.

    Componentes (todos [0..1]):
      magnitude  — |edge| escalado, satura em 4 pts.
      alignment  — produto sign(edge) × sign(heat), positivo = mesma direção.
                   Default 0.5 (neutro) quando heat ≈ 0 ou edge ≈ 0.
      quality    — média de sample × projection_confidence.

    Pesos: 0.35 magnitude + 0.30 alignment + 0.35 quality.

    SAMPLE CEILING (mai/2026 — fix Robinson/Duren 6.9min APOSTAR FORTE):
      magnitude + alignment somam 65% do score e inflam JUSTAMENTE quando
      o sample é pequeno (5pts em 6.9min → projeção 16 → edge +5.5 →
      magnitude satura). A amostra minúscula é a causa do edge alto E o
      motivo pra não confiar — auto-reforço vicioso.

      Solução: sample_confidence vira TETO multiplicativo, não só 35% de
      uma média. Convicção não pode exceder o que o tamanho da amostra
      sustenta, por maior que o edge pareça:

        ceiling = 0.40 + 0.60 × sample_confidence
          <6min  (0.10) → 0.46  (abaixo de MEDIUM 0.60 → PASS)
          6-14min(0.35) → 0.61  (MEDIUM máx — nunca LARGE 0.75)
          14-24m (0.65) → 0.79  (pode LARGE)
          24+min (0.90) → 0.94  (range completo)
    """
    magnitude = min(abs(edge) / 4.0, 1.0)

    if abs(edge) < 0.5 or abs(heat_score) < 0.15:
        alignment = 0.50
    else:
        same_dir = (edge >= 0) == (heat_score >= 0)
        if same_dir:
            alignment = 0.5 + 0.5 * min(abs(heat_score), 1.0)
        else:
            alignment = 0.5 - 0.5 * min(abs(heat_score), 1.0)

    quality = (sample_confidence + projection_confidence) / 2.0

    score = 0.35 * magnitude + 0.30 * alignment + 0.35 * quality

    # Teto por tamanho de amostra — gate, não componente.
    sample_ceiling = 0.40 + 0.60 * sample_confidence
    score = min(score, sample_ceiling)

    return round(max(0.0, min(score, 1.0)), 3)


# ---------------------------------------------------------------------------
# Usage proxy / offensive priority (item 6, mai/2026)
# ---------------------------------------------------------------------------
# Quanto o jogador é "primário" no ataque do time. NÃO é o usage rate
# verdadeiro (precisaria de team_possessions ao vivo) — é uma aproximação
# robusta baseada em FGA/min comparada com benchmarks da liga.
#
# Benchmarks NBA modernos (jogadores ativos, 2023-2026):
#   - Ponta de role player: ~0.20-0.30 FGA/min
#   - Starter padrão:        ~0.35-0.45 FGA/min
#   - Estrela secundária:    ~0.50-0.60 FGA/min
#   - Star primário:         ~0.65+ FGA/min  (LeBron/Luka/SGA range)
#
# Calibrado pra que p10≈0.20, p50≈0.40, p90≈0.65 — convertido em score
# 0..1 pra usar como "weight" em confidence/recommendation.
LEAGUE_FGA_PER_MIN_P10 = 0.20
LEAGUE_FGA_PER_MIN_P50 = 0.40
LEAGUE_FGA_PER_MIN_P90 = 0.65


def usage_proxy(
    season_fga: float, season_minutes: float
) -> float:
    """
    Usage proxy ∈ [0, 1] baseado em FGA/min vs distribuição da liga.

    0.0 = role player puro (não atira), 1.0 = star primário.
    Fora dos extremos: interpolação linear entre p10/p50/p90 (sem assumir
    distribuição normal — força bruta empírica é mais robusta).

    Returns:
        float [0, 1]. Não levanta — entradas inválidas viram 0.5 (neutro).
    """
    if season_minutes <= 0 or season_fga < 0:
        return 0.5
    fga_per_min = season_fga / season_minutes

    if fga_per_min <= LEAGUE_FGA_PER_MIN_P10:
        return 0.10
    if fga_per_min <= LEAGUE_FGA_PER_MIN_P50:
        # Interpola entre p10 (0.10) e p50 (0.50)
        ratio = (fga_per_min - LEAGUE_FGA_PER_MIN_P10) / (
            LEAGUE_FGA_PER_MIN_P50 - LEAGUE_FGA_PER_MIN_P10
        )
        return 0.10 + 0.40 * ratio
    if fga_per_min <= LEAGUE_FGA_PER_MIN_P90:
        # Interpola entre p50 (0.50) e p90 (0.90)
        ratio = (fga_per_min - LEAGUE_FGA_PER_MIN_P50) / (
            LEAGUE_FGA_PER_MIN_P90 - LEAGUE_FGA_PER_MIN_P50
        )
        return 0.50 + 0.40 * ratio
    # Acima de p90 — cap em 1.0
    return min(1.0, 0.90 + (fga_per_min - LEAGUE_FGA_PER_MIN_P90) * 0.5)


# ---------------------------------------------------------------------------
# Variância histórica (item 2, mai/2026)
# ---------------------------------------------------------------------------
# Cara com season=20 mas [35, 8, 12, 28, 17] last 5 é MUITO diferente de
# cara com [19, 21, 20, 22, 18]. Hoje só `prior_strength_min` global lida
# com isso — mas a confidence final NÃO reflete a variância individual.
#
# Esse helper computa o coeficiente de variação (CV = std / mean) e devolve
# um fator [0..1] que penaliza confidence pra jogadores voláteis. CV alto
# = sinal mais ruidoso = projeção menos confiável → menor confidence.


def player_variance_factor(values: list[float]) -> float:
    """
    Fator de confidence ∈ [0, 1] baseado na variância histórica do stat.

    Returns:
        1.0  → consistência alta (CV ≤ 0.20, ex.: starter regular)
        0.7  → variância média (CV ≈ 0.40)
        0.4  → variância alta (CV ≈ 0.60, role player com upside)
        0.2  → muito volátil (CV ≥ 0.80)

    Edge cases:
        - Lista vazia ou < 3 jogos → 0.5 (neutro, sem amostra)
        - Mean ≤ 0 → 0.5 (não dá pra normalizar)
    """
    if not values or len(values) < 3:
        return 0.5
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.5

    # Desvio padrão amostral (n-1)
    n = len(values)
    sq_diffs = sum((v - mean) ** 2 for v in values)
    std = (sq_diffs / (n - 1)) ** 0.5
    cv = std / mean

    # Mapeamento empírico — cap nos extremos
    if cv <= 0.20:
        return 1.0
    if cv <= 0.40:
        # Interpola 1.0 → 0.7
        return 1.0 - (cv - 0.20) / 0.20 * 0.30
    if cv <= 0.60:
        # Interpola 0.7 → 0.4
        return 0.7 - (cv - 0.40) / 0.20 * 0.30
    if cv <= 0.80:
        # Interpola 0.4 → 0.2
        return 0.4 - (cv - 0.60) / 0.20 * 0.20
    return 0.2


def usage_label(usage: float) -> str:
    """Mapeia usage [0,1] em rótulo humano pra UI."""
    if usage >= 0.80:
        return "primary_option"      # Star primário
    if usage >= 0.55:
        return "secondary_option"    # Estrela secundária
    if usage >= 0.30:
        return "starter"             # Starter padrão
    if usage >= 0.15:
        return "role_player"         # Role player com algum tiro
    return "low_usage"               # Praticamente não atira


def rest_factor(rest_days: int) -> float:
    """
    Multiplicador de produção baseado em dias de descanso.

    Calibrado por padrões NBA observados:
      0 dias (back-to-back): -8% (fadiga)
      1 dia (normal):         0% (baseline)
      2 dias:                +2% (descanso bom)
      3+ dias:               +3% (cap — descanso excessivo perde ritmo)
    """
    if rest_days <= 0:
        return 0.92
    if rest_days == 1:
        return 1.00
    if rest_days == 2:
        return 1.02
    return 1.03


# ---------------------------------------------------------------------------
# Synthetic bookmaker fair line
# ---------------------------------------------------------------------------
# Estima a linha de prop bet que um bookmaker (Bet365, DraftKings, etc.)
# abriria pra um jogador. Quando não houver odds API real plugada, isso
# é o que alimenta o `edge` (projeção − linha) e a recomendação de aposta.
#
# A função vai além de "blend de médias": incorpora minutos jogados,
# volume/eficiência de arremesso, blowout, foul trouble e role. Cada
# influência tem peso nomeado pra calibração transparente.

# ── Pesos do prior histórico (somam 1.0) ───────────────────────────────────
# Calibrados contra Bet365 (PHI x NYK, mai/2026). Antes era 30/40/30 que
# punia muito slumps recentes. Agora ancoramos forte na temporada — é o
# que oddsmakers fazem: forma recente é ajuste fino, não driver primário.
LINE_W_SEASON  = 0.55
LINE_W_LAST_10 = 0.30
LINE_W_LAST_5  = 0.15

# ── Bayesian-like sample factor ────────────────────────────────────────────
# Quanto maior, mais peso pro prior em jogadores com poucos minutos.
# 14 ≈ "duas observações típicas de half-game" como prior — dá tempo
# pro live signal estabilizar antes de tomar conta.
LINE_PRIOR_STRENGTH_MIN = 14.0

# ── Composição do live signal (somam 1.0) ──────────────────────────────────
# Live signal mistura ritmo atual extrapolado + projeção do nosso modelo.
# 50/50 dá robustez: live_pace pega tendência crua; model corrige por
# foul/blowout/cold-start.
LINE_LIVE_PACE_WEIGHT  = 0.50
LINE_LIVE_MODEL_WEIGHT = 0.50

# ── Ajustes por volume/eficiência de arremesso (mainly PTS) ───────────────
# Cara com 12 FGA em 18 min e avg de 8 FGA/22min está com volume +83% —
# linha sobe. Cara com 2 FGA em 18 min está com volume −68% — linha desce.
# Cap evita que extremos exagerem (sample pequeno).
LINE_SHOT_VOLUME_WEIGHT       = 0.25
LINE_SHOT_VOLUME_CAP_PCT      = 0.15      # cap em ±15% do prior
LINE_SHOT_VOLUME_MIN_MINUTES  = 6.0       # precisa de amostra mínima

# Aproveitamento: FG% acima/abaixo da média projeta pontos extras/perdidos.
# Eq.: extra_pts = (current_efg − season_efg) × current_fga
LINE_EFFICIENCY_WEIGHT     = 1.00
LINE_EFFICIENCY_CAP_PCT    = 0.10     # cap em ±10% do prior
LINE_EFFICIENCY_MIN_FGA    = 4         # precisa de pelo menos 4 arremessos

# ── Ajustes contextuais ─────────────────────────────────────────────────────
LINE_BENCH_OFF_COURT_DISC    = 0.95    # reserva fora de quadra: -5%

# Blowout — fatores diferenciados por papel (Fase 3, mai/2026):
#   Titulares perdem MAIS minutos em blowout (treinador descansa cedo).
#   Reservas perdem menos / podem até ganhar minutos no garbage time,
#   mas a produção média por minuto deles é menor.
LINE_BLOWOUT_STARTER_FACTOR  = 0.30
LINE_BLOWOUT_BENCH_FACTOR    = 0.10
# Compat: alias mantido pra não quebrar imports antigos. Apenas o starter
# factor é usado quando a chamada não diferencia (legacy paths).
LINE_BLOWOUT_RISK_ADJ        = LINE_BLOWOUT_STARTER_FACTOR

# Foul trouble — antes era 5% uniforme por falta acima de 3 (LINE_FOUL_TROUBLE_PENALTY).
# Agora usamos `foul_trouble_multiplier(fouls, game_minutes_remaining)` que
# considera quanto tempo de jogo resta — coach protege mais quando há
# muito jogo pela frente, deixa correr quando é crunch time.
# Constante mantida só pra compat caso algo externo importe; não é usada.
LINE_FOUL_TROUBLE_PENALTY    = 0.05

# ── Anchoring suave à projeção (sempre ativo quando há current_stat) ───────
# Calibrado contra Bet365 ao longo de 2026:
#
# 1ª passada (PHI/SAS/NYK, mai/2026): peak=0.95 — corretos quando jogador
#    está próximo do plateau e Bet365 desconta levemente abaixo da projeção.
#
# 2ª passada (PHI x NYK Q3, mai/2026): peak=1.05 — Bet365 abriu linhas
#    ACIMA da nossa projeção pros titulares em ritmo on-pace:
#      Oubre   pace=0.92 → bet365=24.5 vs nossa proj=23.8 → mult=1.03
#      Bridges pace=0.85 → bet365=20.5 vs nossa proj=20.1 → mult=1.02
#      Brunson pace=0.84 → bet365=30.5 vs nossa proj=28.7 → mult=1.06
#
#    Conclusão: Bet365 NÃO desconta — ele paga continuação total ou leve
#    prêmio em on-pace. O cap antigo (0.95) capava nossa linha entre 1.5
#    e 3 pts abaixo do mercado, gerando edges falsos pra OVER. Subindo
#    pra 1.05 elimina esse viés sistemático sem alterar a curva pra
#    casos cold (pace < 0.5 continua puxado pra baixo via BASE=0.78).
#
# Implementamos como blend: line = (1-w) × base_line + w × (proj × mult).
# Peso `w` cresce 1.5× mais rápido que o pace_fraction (cap 1.0) — assim
# a projeção domina já em pace médio (~0.50-0.67), refletindo o que
# observamos na Bet365 (eles confiam na projeção bem antes de "on track").
LINE_ANCHOR_PROJ_BASE       = 0.78    # multiplicador da projeção quando pace ≈ 0
LINE_ANCHOR_PROJ_PEAK       = 1.05    # multiplicador quando pace ≈ 1.0 (era 0.95; ver nota acima)
LINE_ANCHOR_WEIGHT_GAIN     = 1.5     # acelerador do anchor (anchor = min(1, pace × gain))

# ── Floors / Ceiling do normalizador ────────────────────────────────────────
LINE_FLOOR_CURRENT_BUFFER  = 0.5      # piso = current + 0.5 (no-push obrigatório)
# Piso hard de pace: garante alinhamento com projeção em jogadores on-track.
# Roda DEPOIS do anchoring suave; serve de safeguard pra casos extremos.
LINE_FLOOR_PACE_THRESHOLD  = 0.60     # ativar piso de pace
LINE_FLOOR_PACE_FRACTION   = 0.95     # piso = projection × 0.95
LINE_CEIL_OVER_PROJECTION  = 3.0      # teto: line <= projection + 3 (anti-overshoot)


@dataclass
class LineContext:
    """
    Bundle de sinais usado pra estimar a linha de um prop específico
    (player_points, player_rebounds, player_assists). Campos opcionais
    têm default que vira "neutro" (não muda a linha).
    """
    # Histórico (obrigatório — base do prior)
    season_avg: float
    last_10_avg: Optional[float] = None
    last_5_avg: Optional[float] = None
    season_minutes: Optional[float] = None       # avg minutos/jogo na temporada

    # Live state
    current_stat: float = 0.0                   # produção atual neste jogo
    minutes_played: float = 0.0                 # minutos já jogados
    projected_end: Optional[float] = None        # projeção do nosso modelo

    # Volume/efficiency (relevante pra PTS)
    current_fga: int = 0
    current_fgm: int = 0
    current_3pm: int = 0
    season_fga_per_min: Optional[float] = None  # avg FGA / avg minutos
    season_efg: Optional[float] = None           # eFG% médio na temporada

    # Role / contexto do jogo
    is_starter: bool = True
    on_court: bool = True
    fouls: int = 0
    blowout_severity: float = 0.0                # 0..1
    game_minutes_remaining: float = 0.0

    # Matchup (Fase 5, mai/2026) — opcionais; quando ausentes, fator neutro
    # (1.0). Servem pra subir/baixar a linha conforme defesa adversária e
    # pace combinado. Cap aplicado no provider (±15% DRtg, ±10% pace).
    opponent_drtg_factor: float = 1.0    # > 1.0 = defesa fraca
    pace_factor_matchup: float = 1.0     # > 1.0 = pace combinado acima da média


@dataclass
class LineResult:
    """Resultado completo do cálculo de linha — número + diagnóstico."""
    line: float                              # número final pós-normalização
    raw_line: float                          # bruto, antes de floors/teto/.5
    components: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    floor_applied: Optional[str] = None      # "current" | "pace" | None


# ---------------------------------------------------------------------------
# Helpers de contexto compartilhados entre Line e Projection
# ---------------------------------------------------------------------------

def foul_trouble_multiplier(fouls: int, game_minutes_remaining: float) -> float:
    """
    Multiplicador aplicado ao valor RESTANTE da produção por foul trouble.

    Lógica empírica (Fase 3, mai/2026): quanto mais tempo de jogo resta,
    mais o treinador protege o jogador de pegar a próxima falta. No crunch
    time final, deixa jogar e arriscar.

      ≤ 3 faltas: sem ajuste (multiplicador 1.0)
      4 faltas + jogo cedo (>18min restantes): 0.75 — protege agressivo
      4 faltas + meio (8-18min):              0.85 — protege moderado
      4 faltas + crunch (≤8min):              0.92 — joga pelo jogo
      5 faltas + ainda há jogo (>8min):       0.50 — só pra emergência
      5 faltas + crunch (≤8min):              0.70 — vale arriscar
      6+ faltas (fouled out):                 0.20 — só técnicos contam

    Caso real KAT (PHI vs NYK, mai/2026): 4F com ~24min de jogo restante.
    Multiplicador 0.75 reduz a produção esperada do tempo restante em
    25%, alinhando com o desconto que Bet365 aplicou (linha 11.5 enquanto
    nossa projeção sem foul context era 14.6).
    """
    if fouls <= 3:
        return 1.00
    if fouls == 4:
        if game_minutes_remaining > 18:
            return 0.75
        if game_minutes_remaining > 8:
            return 0.85
        return 0.92
    if fouls == 5:
        if game_minutes_remaining > 8:
            return 0.50
        return 0.70
    # 6+ = fouled out
    return 0.20


def blowout_role_factor(is_starter: bool) -> float:
    """
    Fator multiplicado por `blowout_severity` pra obter a redução
    proporcional aplicada à produção restante.

    Titulares perdem mais minutos (treinador descansa); reservas perdem
    menos mas a produção/minuto delas é tipicamente baixa, então o
    impacto líquido também é negativo.
    """
    return LINE_BLOWOUT_STARTER_FACTOR if is_starter else LINE_BLOWOUT_BENCH_FACTOR


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def calculate_estimated_sportsbook_line(ctx: LineContext) -> LineResult:
    """
    Estima a linha que um bookmaker abriria pro mercado descrito em `ctx`.

    Pipeline:
      1. Prior histórico = blend (season, last_10, last_5)
      2. Live signal     = blend (live_pace, model_projection) — só se houver minutos
      3. Base line       = mistura prior↔live conforme tamanho de amostra
      4. Ajustes aditivos: volume, eficiência, blowout, fouls, bench
      5. Normalização sportsbook: pisos (current+0.5, pace tracking),
         teto (≤ projection+3), arredondamento .5

    Returns LineResult com `line`, breakdown por componente, e `reason`
    explicando o que dominou o cálculo.
    """
    components: dict[str, float] = {}
    reasons: list[str] = []

    # ── 1. Prior histórico (blend ponderado) ──────────────────────────────
    last_10 = ctx.last_10_avg if ctx.last_10_avg is not None else ctx.season_avg
    last_5  = ctx.last_5_avg  if ctx.last_5_avg  is not None else ctx.season_avg
    prior = (
        LINE_W_SEASON * ctx.season_avg
        + LINE_W_LAST_10 * last_10
        + LINE_W_LAST_5  * last_5
    )
    components["prior"] = round(prior, 2)
    components["w_season"] = LINE_W_SEASON * ctx.season_avg
    components["w_last_10"] = LINE_W_LAST_10 * last_10
    components["w_last_5"] = LINE_W_LAST_5  * last_5

    # ── 2. Live signal (se houver minutos jogados) ────────────────────────
    if ctx.minutes_played > 0:
        # Ritmo atual extrapolado pra minutos típicos do jogador
        season_min = ctx.season_minutes if ctx.season_minutes and ctx.season_minutes > 0 else 32.0
        live_pace_full_game = (ctx.current_stat / ctx.minutes_played) * season_min

        model_proj = ctx.projected_end if ctx.projected_end is not None else prior

        live_signal = (
            LINE_LIVE_PACE_WEIGHT * live_pace_full_game
            + LINE_LIVE_MODEL_WEIGHT * model_proj
        )
        components["live_pace_full_game"] = round(live_pace_full_game, 2)
        components["model_projection"] = round(model_proj, 2)
        components["live_signal"] = round(live_signal, 2)

        # Sample factor: cresce com minutos. Cap em 0.77 (= 14/(14+14×3))
        # garante que o prior nunca seja zerado.
        sample_factor = ctx.minutes_played / (ctx.minutes_played + LINE_PRIOR_STRENGTH_MIN)
        components["sample_factor"] = round(sample_factor, 3)

        base_line = (1 - sample_factor) * prior + sample_factor * live_signal
    else:
        base_line = prior

    components["base_line"] = round(base_line, 2)
    line = base_line

    # ── 3. Ajuste de volume de arremessos ─────────────────────────────────
    if (
        ctx.current_fga >= 1
        and ctx.season_fga_per_min
        and ctx.season_fga_per_min > 0
        and ctx.minutes_played >= LINE_SHOT_VOLUME_MIN_MINUTES
    ):
        actual_per_min = ctx.current_fga / ctx.minutes_played
        volume_ratio = actual_per_min / ctx.season_fga_per_min
        # +20% volume = +5% na linha (LINE_SHOT_VOLUME_WEIGHT = 0.25)
        raw_volume_adj = LINE_SHOT_VOLUME_WEIGHT * (volume_ratio - 1.0) * prior
        cap = LINE_SHOT_VOLUME_CAP_PCT * prior
        volume_adj = max(-cap, min(cap, raw_volume_adj))
        line += volume_adj
        components["volume_adj"] = round(volume_adj, 2)
        components["volume_ratio"] = round(volume_ratio, 2)
        if volume_adj >= 0.5:
            reasons.append(f"volume FGA acima do habitual (+{volume_adj:.1f})")
        elif volume_adj <= -0.5:
            reasons.append(f"volume FGA abaixo do habitual ({volume_adj:.1f})")

    # ── 4. Ajuste de eficiência (eFG delta) ───────────────────────────────
    if ctx.current_fga >= LINE_EFFICIENCY_MIN_FGA and ctx.season_efg is not None:
        current_efg = (ctx.current_fgm + 0.5 * ctx.current_3pm) / ctx.current_fga
        efg_delta = current_efg - ctx.season_efg
        # Pontos extras/perdidos = delta × FGA × 2 (cada FGA vira ~2 pts)
        # Mas como current_efg já é baseado em pts/FGA, o delta direto já dá pts.
        raw_eff_adj = LINE_EFFICIENCY_WEIGHT * efg_delta * ctx.current_fga
        cap = LINE_EFFICIENCY_CAP_PCT * prior
        eff_adj = max(-cap, min(cap, raw_eff_adj))
        line += eff_adj
        components["efficiency_adj"] = round(eff_adj, 2)
        components["efg_delta"] = round(efg_delta, 3)
        if eff_adj >= 1.0:
            reasons.append(f"aproveitamento alto no jogo (+{eff_adj:.1f})")
        elif eff_adj <= -1.0:
            reasons.append(f"aproveitamento baixo no jogo ({eff_adj:.1f})")

    # ── 5. Blowout: minutos restantes reduzidos (diferenciado por papel) ───
    # Aplica só sobre o que FALTA produzir (line - current). Já realizado
    # não some — é informação consolidada.
    if ctx.blowout_severity > 0:
        role_factor = blowout_role_factor(ctx.is_starter)
        remaining_value = max(line - ctx.current_stat, 0.0)
        bo_adj = -role_factor * ctx.blowout_severity * remaining_value
        line += bo_adj
        components["blowout_adj"] = round(bo_adj, 2)
        components["blowout_role_factor"] = role_factor
        if bo_adj <= -0.5:
            role_label = "titular" if ctx.is_starter else "reserva"
            reasons.append(f"blowout reduz minutos ({role_label}, {bo_adj:.1f})")

    # ── 6. Foul trouble: tabela contextual por minutos restantes ──────────
    # Substitui o desconto uniforme de 5%/falta por multiplicador adaptativo
    # que considera quanto jogo resta (coach protege mais quando há tempo).
    foul_mult = foul_trouble_multiplier(ctx.fouls, ctx.game_minutes_remaining)
    if foul_mult < 1.0:
        remaining_value = max(line - ctx.current_stat, 0.0)
        foul_adj = (foul_mult - 1.0) * remaining_value
        line += foul_adj
        components["foul_multiplier"] = foul_mult
        components["foul_trouble_adj"] = round(foul_adj, 2)
        reasons.append(
            f"foul trouble {ctx.fouls}F com {ctx.game_minutes_remaining:.0f}min restantes ({foul_adj:.1f})"
        )

    # ── 7. Bench player off court (sem contexto favorável) ────────────────
    if not ctx.is_starter and not ctx.on_court and ctx.minutes_played > 0:
        line *= LINE_BENCH_OFF_COURT_DISC
        components["bench_off_court_mult"] = LINE_BENCH_OFF_COURT_DISC
        reasons.append("reserva no banco — desconto")

    # ── 7.5 Matchup: defesa adversária + pace combinado (Fase 5) ──────────
    # Aplicado ao valor RESTANTE da linha. Já realizado é fato.
    matchup_combined = ctx.opponent_drtg_factor * ctx.pace_factor_matchup
    if matchup_combined != 1.0:
        remaining_value = max(line - ctx.current_stat, 0.0)
        matchup_adj = (matchup_combined - 1.0) * remaining_value
        line += matchup_adj
        components["matchup_combined_factor"] = round(matchup_combined, 3)
        components["matchup_adj"] = round(matchup_adj, 2)
        if abs(matchup_adj) >= 0.5:
            direction = "favorável" if matchup_adj > 0 else "defensivo"
            reasons.append(f"matchup {direction} ({matchup_adj:+.1f})")

    raw_line = line

    # ── 8. Normalização sportsbook ────────────────────────────────────────
    # Passa os modificadores contextuais (foul, blowout, matchup) pra que
    # o anchor também aplique sobre o proj_target — sem isso, o anchor
    # dilui os ajustes lineares quando puxa a linha pra perto da projeção.
    line, floor_applied, normalize_reasons = _normalize_sportsbook_line(
        line=line,
        current_stat=ctx.current_stat,
        projected_end=ctx.projected_end,
        foul_multiplier_value=foul_mult,
        blowout_remaining_factor=(
            1.0 - blowout_role_factor(ctx.is_starter) * ctx.blowout_severity
            if ctx.blowout_severity > 0 else 1.0
        ),
        matchup_factor=matchup_combined,
    )
    reasons.extend(normalize_reasons)

    # ── 9. Reason consolidada ─────────────────────────────────────────────
    if not reasons:
        reasons.append("base histórica + ritmo do jogo")
    reason = "; ".join(reasons)

    result = LineResult(
        line=line,
        raw_line=round(raw_line, 2),
        components=components,
        reason=reason,
        floor_applied=floor_applied,
    )

    # Log debug pra calibração (só ativo com LOG_LEVEL=DEBUG)
    logger.debug(
        "line_calc: stat=%s mins=%s prior=%.2f raw=%.2f line=%.1f reason=%s",
        ctx.current_stat, ctx.minutes_played,
        prior, raw_line, line, reason,
    )

    return result


def _normalize_sportsbook_line(
    line: float,
    current_stat: float,
    projected_end: Optional[float],
    foul_multiplier_value: float = 1.0,
    blowout_remaining_factor: float = 1.0,
    matchup_factor: float = 1.0,
) -> tuple[float, Optional[str], list[str]]:
    """
    Aplica regras de mercado pra transformar a linha bruta em número
    apostável:
      1. Anchoring suave à projeção (peso ∝ pace_fraction); foul/blowout
         também aplicam ao proj_target pra não serem diluídos pelo anchor
      2. Piso obrigatório (linha ≥ atual + 0.5) — REGRA INVIOLÁVEL
      3. Piso hard de pace (≥ proj × 0.95 quando on-track) — só aplica
         quando NÃO há foul/blowout severos (senão floor reverteria a redução)
      4. Teto anti-overshoot (≤ proj + 3)
      5. Arredondamento .5

    Args:
      foul_multiplier_value: 1.0 = sem foul trouble; <1.0 reduz proj_target
      blowout_remaining_factor: 1.0 = sem blowout; <1.0 reduz proj_target

    Returns: (linha_final, floor_aplicado, lista_de_reasons).
    """
    reasons: list[str] = []
    floor_applied: Optional[str] = None

    # Modificadores contextuais: aplicam-se ao alvo do anchor pra que o
    # blend não dilua os ajustes de foul/blowout/matchup.
    contextual_modifier = (
        foul_multiplier_value * blowout_remaining_factor * matchup_factor
    )

    # ── Anchoring suave: linha tende a (projeção × multiplicador) ─────────
    # Calibração contra Bet365: o multiplicador cresce com pace_fraction
    # (jogador validando o ritmo → bookmaker confia mais na projeção).
    # Aplicado antes dos floors pra que eles ainda possam corrigir extremos.
    if (
        projected_end is not None
        and projected_end > 0
        and current_stat > 0
    ):
        pace_fraction = min(current_stat / projected_end, 1.0)
        proj_multiplier = (
            LINE_ANCHOR_PROJ_BASE
            + (LINE_ANCHOR_PROJ_PEAK - LINE_ANCHOR_PROJ_BASE) * pace_fraction
        )
        # Anchor target = projeção × multiplicador (Bet365 confia ~100-105%
        # da projeção em on-pace). DEPOIS, aplicamos `contextual_modifier`
        # só na parte RESTANTE (anchor_target - current) — sem isso o anchor
        # diluiria foul trouble / blowout em jogadores com pace alto.
        anchor_target = projected_end * proj_multiplier
        if contextual_modifier < 1.0:
            remaining = max(anchor_target - current_stat, 0.0)
            proj_target = current_stat + remaining * contextual_modifier
        else:
            proj_target = anchor_target
        anchor_weight = min(1.0, pace_fraction * LINE_ANCHOR_WEIGHT_GAIN)
        line_pre = line
        line = (1 - anchor_weight) * line + anchor_weight * proj_target
        if abs(line - line_pre) >= 0.5:
            reasons.append(
                f"anchored à projeção × {proj_multiplier:.2f} (pace {pace_fraction*100:.0f}%)"
            )

    # ── Piso 1 (REGRA INVIOLÁVEL): linha >= current + 0.5 ─────────────────
    if current_stat > 0 and line < current_stat + LINE_FLOOR_CURRENT_BUFFER:
        line = current_stat + LINE_FLOOR_CURRENT_BUFFER
        floor_applied = "current"
        reasons.append("piso obrigatório: linha ≥ atual + 0.5")

    # ── Piso 2: pace hard (safeguard pra on-track players) ────────────────
    # IMPORTANTE: só aplica quando NÃO há foul/blowout severos. Senão o
    # piso reverteria a redução intencional desses contextos.
    severe_context = contextual_modifier < 0.85
    if (
        not severe_context
        and projected_end is not None
        and projected_end > 0
        and current_stat > 0
        and current_stat / projected_end >= LINE_FLOOR_PACE_THRESHOLD
    ):
        pace_floor = projected_end * LINE_FLOOR_PACE_FRACTION
        if line < pace_floor:
            line = pace_floor
            floor_applied = "pace"
            reasons.append(f"on track p/ projeção — piso {pace_floor:.1f}")

    # ── Teto: nunca muito acima da projeção sem motivo ────────────────────
    if projected_end is not None and projected_end > 0:
        ceiling = projected_end + LINE_CEIL_OVER_PROJECTION
        if line > ceiling:
            line = ceiling
            reasons.append("teto: ≤ projeção + 3")

    # ── Arredondamento .5 padrão de mercado ───────────────────────────────
    line = round(line * 2) / 2

    # ── Mínimo absoluto: 0.5 (linhas <0.5 não existem) ────────────────────
    line = max(0.5, line)

    return line, floor_applied, reasons


def calculate_fair_line(
    season_avg: float,
    last_10_avg: float,
    last_5_avg: float,
    current_stat: float = 0.0,
    projected_end: Optional[float] = None,
) -> float:
    """
    Wrapper compatível: retorna só o número da linha.

    Para o cálculo completo com diagnóstico/components/reason, use
    `calculate_estimated_sportsbook_line(ctx)` direto. Esta wrapper existe
    pra preservar testes e callers que só precisam do float.
    """
    ctx = LineContext(
        season_avg=season_avg,
        last_10_avg=last_10_avg,
        last_5_avg=last_5_avg,
        current_stat=current_stat,
        projected_end=projected_end,
    )
    return calculate_estimated_sportsbook_line(ctx).line


def calculate_edge_decision(edge: float) -> str:
    """
    Mapeia o edge (projeção − linha estimada) para uma decisão.

    Thresholds ASSIMÉTRICOS (mai/2026) — refletem variance diferente
    entre OVER e UNDER em props da NBA:
      OVER:  variance positiva ajuda (ceiling aberto). Edge baixo
             ainda viável estatisticamente.
      UNDER: variance negativa atrapalha (cara pode acelerar). Edge
             precisa ser maior pra compensar o risco.

    Calibração:
      STRONG_OVER:  edge ≥ +3.0   (mantém)
      LEAN_OVER:    edge ≥ +1.5   (filtra OVERs marginais)
      NEUTRAL:      -2.0 < edge < +1.5
      LEAN_UNDER:   edge ≤ -2.0   (filtra UNDERs marginais, caso LeBron-like)
      STRONG_UNDER: edge ≤ -4.0   (mais difícil de bater, exige edge maior)

    Pontos absolutos, não percentuais — esses são os tamanhos típicos
    de aposta com valor positivo na NBA.
    """
    if edge >= 3.0:
        return "STRONG_OVER"
    if edge >= 1.5:
        return "LEAN_OVER"
    if edge <= -4.0:
        return "STRONG_UNDER"
    if edge <= -2.0:
        return "LEAN_UNDER"
    return "NEUTRAL"


def dampen_decision_for_low_sample(decision: str, minutes_played: float) -> str:
    """
    Rebaixa a DECISÃO quando a amostra (minutos jogados ATÉ AGORA neste
    jogo) é pequena demais pra sustentar uma chamada forte.

    Mai/2026 — fix Robinson/Duren (~7min jogados projetavam +5 edge e
    viravam "APOSTAR FORTE" no card). A projeção em si não está
    "errada" como estimativa central, mas fazer uma chamada STRONG com
    7 minutos de dado é arriscado: o edge alto vem JUSTAMENTE da
    extrapolação de pouco sample.

    Espelha o sample_ceiling do betting_confidence (que rebaixa o
    TAMANHO da aposta), mas no eixo da DECISÃO — que governa o label
    visível do card (STRONG_OVER → "APOSTAR FORTE").

      < 6 min  → qualquer LEAN/STRONG vira NEUTRAL ("observar")
      6–10 min → STRONG vira LEAN ("apostar forte" → "apostar")
      ≥ 10 min → inalterado (sample suficiente pra chamada cheia)
    """
    if minutes_played >= 10.0:
        return decision
    if minutes_played < 6.0:
        if decision in (
            "STRONG_OVER", "LEAN_OVER", "STRONG_UNDER", "LEAN_UNDER",
        ):
            return "NEUTRAL"
        return decision
    # 6 ≤ min < 10: rebaixa STRONG → LEAN (mantém direção, tira o "forte")
    if decision == "STRONG_OVER":
        return "LEAN_OVER"
    if decision == "STRONG_UNDER":
        return "LEAN_UNDER"
    return decision


# ---------------------------------------------------------------------------
# Player-level blowout impact
# ---------------------------------------------------------------------------
# Distingue o RISCO DO JOGO (blowout %) do IMPACTO SOBRE O JOGADOR.
# A flag visual no card só faz sentido pra quem realmente PERDE minutos:
# titulares e jogadores de alta minutagem. Reservas de fim de banco
# tendem a *ganhar* tempo em garbage time — não deveriam receber alerta.

# Limiar abaixo do qual o blowout do jogo é irrelevante pra QUALQUER jogador.
# 55% = ponto onde o jogo está claramente caminhando pra blowout (não só
# um quarto difícil). Abaixo disso ainda tem virada possível e técnicos
# não decidiram poupar ninguém. Calibrado por feedback real (Q4 com 17 pts
# em 7 min = 48% — ainda jogo, sem alarme).
_BLOWOUT_GAME_FLOOR_PCT = 55

# Acima desse limiar de minutos, mesmo reserva já é "rotação principal".
_HIGH_MINUTE_THRESHOLD = 18.0

# Acima desse limiar de minutos pra titular = alta minutagem (impacto severo).
_STARTER_HIGH_MIN = 22.0


def calculate_player_blowout_impact(
    *,
    player_minutes: float,
    is_starter: bool,
    game_blowout_pct: int,
    game_blowout_level: str = "low",
) -> Optional[dict]:
    """
    Decide se a flag "Risco de descanso" deve aparecer pro jogador.

    Args:
        player_minutes: minutos jogados até agora.
        is_starter: titular oficial (campo `starter` da NBA Live API).
        game_blowout_pct: 0–100 do jogo.
        game_blowout_level: 'low' | 'medium' | 'high' | 'final'.

    Returns:
        dict {applies, level, reason} se a flag DEVE aparecer.
        None se não — usar sempre que o jogador for fim de banco ou o
        jogo não estiver perto de blowout.

    Regras (calibradas pelo padrão de rotação NBA):
    - Risco do jogo < 55% → ninguém recebe (jogo ainda em disputa real).
    - Reserva com <18 min → não recebe (esses jogadores GANHAM minutos
      no garbage time, então o blowout favorece eles).
    - Titular OU 18+ min → entra no cálculo:
        * game >= 75 + titular com 22+ min → "high"
        * game >= 75 outros               → "medium"
        * game >= 65                      → "medium"
        * 55-64                           → "low"
    - Jogos finalizados (level=='final'): mesma regra, mas usa frase em
      past-tense ("Foi poupado") em vez de prospectiva ("pode ser poupado").
    """
    if game_blowout_pct < _BLOWOUT_GAME_FLOOR_PCT:
        return None

    is_core_rotation = is_starter or player_minutes >= _HIGH_MINUTE_THRESHOLD
    if not is_core_rotation:
        return None  # fim de banco fica de fora — eles GANHAM minutos

    is_post_game = game_blowout_level == "final"

    # Calcula nível. Tiers calibrados pra threshold mais alto (55):
    # 75+ = blowout iminente / consumado; 65-74 = bem provável; 55-64 = sinal sutil.
    if game_blowout_pct >= 75:
        if is_starter and player_minutes >= _STARTER_HIGH_MIN:
            level = "high"
            reason = (
                "Titular descansado em jogo de blowout"
                if is_post_game else
                "Titular com alta minutagem — alto risco de ser poupado"
            )
        else:
            level = "medium"
            reason = (
                "Rotação principal — pode ter perdido minutos no garbage time"
                if is_post_game else
                "Rotação principal — pode perder minutos no garbage time"
            )
    elif game_blowout_pct >= 65:
        level = "medium"
        reason = (
            "Minutos provavelmente reduzidos pelo placar"
            if is_post_game else
            "Risco moderado de minutos reduzidos"
        )
    else:  # 55-64
        level = "low"
        reason = (
            "Possível descanso pelo placar"
            if is_post_game else
            "Pequeno risco de descanso antecipado"
        )

    return {
        "applies": True,
        "level": level,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Performance rating (0–10) para a aba Lineups
# ---------------------------------------------------------------------------
# Métrica explicável pra dar uma nota rápida do jogador no jogo atual.
# Não é PER nem Game Score — é uma combinação ponderada simples calibrada
# pra escala 0–10 onde:
#   ~5.0 → jogador médio jogando os minutos típicos
#   ~7.0 → desempenho sólido, acima da média
#   ~8.5 → desempenho de destaque, candidato a Player of the Game
#   <5.0 → abaixo do esperado / jogou pouco / muitos erros
#
# Pesos derivados de valores médios da NBA (ex: 1 roubo "vale mais" que
# 1 ponto porque é mais raro e tem alto impacto). Ajustar com base em
# feedback prático.

# Pesos por stat — multiplicados pelo valor bruto.
_RATING_WEIGHTS = {
    "points":    0.50,
    "rebounds":  0.70,
    "assists":   0.90,
    "steals":    1.80,
    "blocks":    1.80,
    "turnovers": -1.20,
    "fouls":     -0.30,
    "plus_minus": 0.10,
}

# Bônus de eficiência: cada 10 pontos percentuais de eFG acima de 50%
# vira +0.5 ponto. Cada 10pp abaixo, -0.5.
_EFG_NEUTRAL = 0.50
_EFG_WEIGHT = 5.0

# Limiar de minutos abaixo do qual a confiança é baixa.
LOW_CONFIDENCE_MINUTES = 10.0
# Quando confiança é baixa, a nota fica capped — não dá pra dar 9.0
# pra cara que jogou 4 minutos.
LOW_CONFIDENCE_CAP = 7.0


# ---------------------------------------------------------------------------
# Blowout risk (probabilidade de garbage time)
# ---------------------------------------------------------------------------
# Combina diferença de placar, período, tempo restante e tipo de jogo
# (playoff = quase nunca tem blowout) numa porcentagem 0–100 com nível
# qualitativo e explicação curta.

def _parse_clock_minutes_remaining(clock: str) -> float:
    """'06:24' → 6.4 minutos. Sem clock → 12 (período inteiro)."""
    s = (clock or "").strip()
    if ":" not in s:
        return 12.0
    try:
        mm, ss = s.split(":")
        return int(mm) + int(ss) / 60.0
    except (ValueError, IndexError):
        return 12.0


def calculate_blowout_risk(
    period: int,
    clock: str,
    home_score: int,
    away_score: int,
    game_status: str = "in_progress",
    is_playoff: bool = False,
) -> tuple[int, str, str]:
    """
    Estima o risco de garbage time (titulares saindo).

    Returns:
        (percentage 0-100, level, reason)
        level ∈ {"low", "medium", "high", "final"}.

    Regras (calibradas pra padrão NBA):
    - Jogo finalizado → 0%, level "final".
    - Não iniciado → 0%, level "low".
    - Q1: praticamente nunca tem blowout (cap 10%).
    - Q2: começa a importar com diff > 18.
    - Q3: risco médio/alto se diff > 20.
    - Q4: risco alto se diff > 15 com pouco tempo restante.
    - Playoffs: corte de 60% no risco (técnicos mantêm titulares).
    """
    if game_status == "final":
        # Para jogo encerrado, calcula o "blowout retroativo" pela margem
        # final. Permite que o front continue mostrando contexto (titular foi
        # poupado, banco ganhou minutos) mesmo após o apito final.
        margin = abs(home_score - away_score)
        if margin >= 30:
            return (90, "final", f"Blowout — vitória por {margin} pontos")
        if margin >= 20:
            return (75, "final", f"Vitória sólida — margem de {margin} pontos")
        if margin >= 12:
            return (50, "final", f"Vitória clara — margem de {margin} pontos")
        if margin >= 6:
            return (20, "final", f"Vitória apertada — {margin} pontos")
        return (5, "final", f"Jogo equilibrado — encerrou em {margin} pts")
    if game_status == "not_started":
        return (0, "low", "Jogo ainda não começou")

    score_diff = abs(home_score - away_score)
    period_clamped = max(period, 1)
    clock_remaining = _parse_clock_minutes_remaining(clock)
    period_label = f"Q{period_clamped}" if period_clamped <= 4 else f"OT{period_clamped - 4}"

    # ── Componente 1: diferença de placar (escala não-linear) ───────────────
    # Diff de 10 ainda é virável; 20 é difícil; 30 é praticamente sentenciado.
    if score_diff <= 5:
        diff_score = 0.0
    elif score_diff <= 10:
        diff_score = (score_diff - 5) * 4.0          # 0→20
    elif score_diff <= 20:
        diff_score = 20.0 + (score_diff - 10) * 4.0  # 20→60
    elif score_diff <= 30:
        diff_score = 60.0 + (score_diff - 20) * 3.0  # 60→90
    else:
        diff_score = 90.0 + min(score_diff - 30, 10) * 1.0  # cap 100

    # ── Componente 2: período (mais tarde = mais peso pro diff) ─────────────
    # Q1 segura muito — qualquer diff é cedo demais pra concluir blowout.
    period_multiplier = {1: 0.20, 2: 0.45, 3: 0.75, 4: 1.0}.get(period_clamped, 0.50)

    # ── Componente 3: tempo restante no quarto atual ────────────────────────
    # Em Q4 com pouco tempo, mesmo diff de 12 é praticamente blowout.
    # Pesos calibrados pra "Q4 + 18 pts + 3min restantes" ≈ 70%.
    if period_clamped >= 4:
        # Q4 com <6 min restantes ganha boost proporcional ao tempo gasto.
        time_pressure = max((6.0 - clock_remaining) / 6.0, 0.0)
    else:
        time_pressure = 0.0

    base = diff_score * period_multiplier + time_pressure * 25.0

    # ── Playoffs: técnicos não relaxam ──────────────────────────────────────
    if is_playoff:
        base *= 0.4

    percentage = int(max(0, min(100, round(base))))

    # ── Nível qualitativo ───────────────────────────────────────────────────
    # 60+ = "high": acima disso é difícil reverter na NBA
    # 30+ = "medium": titulares já podem começar a sair
    if percentage >= 60:
        level = "high"
    elif percentage >= 30:
        level = "medium"
    else:
        level = "low"

    # ── Explicação humana ───────────────────────────────────────────────────
    if percentage <= 5:
        reason = f"Jogo equilibrado ({score_diff} pts no {period_label})"
    elif period_clamped == 1:
        reason = f"Diferença de {score_diff} pts no Q1 — cedo pra concluir"
    elif period_clamped >= 4 and clock_remaining < 4 and score_diff >= 12:
        reason = f"Diferença de {score_diff} pts no {period_label} com {clock_remaining:.0f} min restantes"
    elif percentage >= 65:
        reason = f"Diferença de {score_diff} pts no {period_label} — garbage time iminente"
    elif percentage >= 35:
        reason = f"Diferença de {score_diff} pts no {period_label} — banco pode entrar antes"
    else:
        reason = f"Diferença de {score_diff} pts no {period_label}"

    return (percentage, level, reason)


def _effective_field_goal_pct(
    fgm: int, fga: int, three_pm: int
) -> float | None:
    """
    eFG% = (FGM + 0.5 × 3PM) / FGA.

    Retorna None se não tentou nenhum arremesso (não dá pra avaliar tiro).
    """
    if fga <= 0:
        return None
    return (fgm + 0.5 * three_pm) / fga


def calculate_player_performance_rating(
    points: int = 0,
    rebounds: int = 0,
    assists: int = 0,
    steals: int = 0,
    blocks: int = 0,
    turnovers: int = 0,
    fouls: int = 0,
    plus_minus: int = 0,
    minutes: float = 0.0,
    field_goals_made: int = 0,
    field_goals_attempted: int = 0,
    three_pointers_made: int = 0,
    free_throws_made: int = 0,
    free_throws_attempted: int = 0,
) -> tuple[float, str, bool]:
    """
    Calcula nota 0-10 do jogador no jogo atual.

    Returns:
        (rating, label, low_confidence)

    Labels:
        rating >= 8.5 → "Excelente"
        rating >= 7.0 → "Bom"
        rating >= 5.0 → "Regular"
        rating >  0   → "Ruim"
        rating == 0   → "N/A" (jogador não entrou em quadra)
    """
    # Quem não jogou nada não recebe nota.
    if minutes <= 0 and points == 0 and rebounds == 0 and assists == 0:
        return (0.0, "N/A", True)

    # ── Componente 1: produção bruta ponderada ─────────────────────────────
    raw = (
        points * _RATING_WEIGHTS["points"]
        + rebounds * _RATING_WEIGHTS["rebounds"]
        + assists * _RATING_WEIGHTS["assists"]
        + steals * _RATING_WEIGHTS["steals"]
        + blocks * _RATING_WEIGHTS["blocks"]
        + turnovers * _RATING_WEIGHTS["turnovers"]
        + fouls * _RATING_WEIGHTS["fouls"]
        + plus_minus * _RATING_WEIGHTS["plus_minus"]
    )

    # ── Componente 2: bônus/penalidade de eficiência de arremesso ──────────
    efg = _effective_field_goal_pct(
        field_goals_made, field_goals_attempted, three_pointers_made
    )
    if efg is not None:
        raw += (efg - _EFG_NEUTRAL) * _EFG_WEIGHT

    # ── Componente 3: bônus de free-throw ──────────────────────────────────
    # Quem vai pra linha e converte ganha um pequeno bônus (causa falta +
    # converte). Quem perde lance livre tem leve penalidade.
    if free_throws_attempted > 0:
        ft_pct = free_throws_made / free_throws_attempted
        raw += (ft_pct - 0.75) * 1.0  # 75% é a média NBA aproximada

    # ── Normalização: mapeia raw pra 0–10 ──────────────────────────────────
    # Calibração empírica:
    #   raw = 0  → ~5.0 (jogador médio com pouca produção)
    #   raw = 10 → ~7.0 (jogador sólido)
    #   raw = 20 → ~8.5 (destaque do jogo)
    #   raw = 30 → ~9.5 (monstro)
    rating = 5.0 + raw * 0.18

    # ── Penalidade de minutos curtos ───────────────────────────────────────
    low_confidence = minutes < LOW_CONFIDENCE_MINUTES
    if low_confidence and minutes > 0:
        # Não escapa de receber nota alta jogando 3 minutos
        rating = min(rating, LOW_CONFIDENCE_CAP)

    # ── Clamp final em [0, 10] ─────────────────────────────────────────────
    rating = max(0.0, min(10.0, rating))
    rating = round(rating, 1)

    # ── Label ──────────────────────────────────────────────────────────────
    if rating >= 8.5:
        label = "Excelente"
    elif rating >= 7.0:
        label = "Bom"
    elif rating >= 5.0:
        label = "Regular"
    elif rating > 0:
        label = "Ruim"
    else:
        label = "N/A"

    return (rating, label, low_confidence)
