"""
Derivações de padrões de rotação a partir do histograma cru.

O histograma de 48 floats (probabilidade de quadra cada minuto) é
suficiente pra derivar padrões mais semânticos que a UI/projeção
podem consumir diretamente:

  - rest/play windows por quarter (intervalos contíguos)
  - clutch usage (quanto joga em jogos apertados vs blowouts)
  - blowout return probability
  - usual start/end de cada shift

Tudo computado em memória — sem API calls extras. Heurísticas
calibradas conservadoramente (sem overfit a 1 jogador específico).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional

from src.services.rotation.nbarotations_parser import GameRotationEntry

# Threshold de probabilidade pra considerar "em quadra"
PLAY_THRESHOLD = 0.55
# Threshold pra "descansando"
REST_THRESHOLD = 0.45
# Tamanho mínimo de window (em minutos) — descartar ruído
MIN_WINDOW_LEN = 2

# Quartos no jogo regular (Q1-Q4 = minutos 0..47).
QUARTERS = [
    (1, 0, 12),
    (2, 12, 24),
    (3, 24, 36),
    (4, 36, 48),
]


@dataclass
class TimeWindow:
    """Janela contínua de minutos com mesmo estado (jogando ou descansando)."""
    from_minute: int    # inclusive (índice no histograma 0-47)
    to_minute: int      # exclusivo
    probability: float  # média de prob nessa janela


@dataclass
class QuarterPattern:
    """Padrão observado de UM quarter."""
    quarter: int                 # 1..4
    expected_minutes: float      # soma das probs do quarter
    usual_start_minute: Optional[int]   # 1º minuto onde prob >= PLAY_THRESHOLD
    usual_end_minute: Optional[int]      # último minuto onde prob >= PLAY_THRESHOLD
    rest_windows: list[TimeWindow]
    play_windows: list[TimeWindow]


@dataclass
class ClutchUsage:
    """Comportamento em jogos apertados — derivado por self-clustering."""
    usually_closes_games: bool
    fourth_quarter_usage_rate: float          # 0..1 (avg prob no Q4 nos jogos "close")
    close_game_minutes_probability: float     # 0..1 (prob de jogar no crunch time)


@dataclass
class BlowoutRisk:
    """Comportamento em blowouts — derivado por self-clustering."""
    fourth_quarter_return_probability_when_blowout: float   # 0..1
    typical_minutes_lost_in_blowout: float                  # min a menos vs jogo normal


# ---------------------------------------------------------------------------
# Derivação de windows por quarter
# ---------------------------------------------------------------------------


def derive_quarter_patterns(
    histogram: list[float],
) -> list[QuarterPattern]:
    """
    A partir do histograma agregado (48 floats), extrai padrões por quarter.
    Identifica janelas contíguas de jogo/descanso usando thresholds.
    """
    if not histogram or len(histogram) < 48:
        return []

    patterns: list[QuarterPattern] = []
    for q, start, end in QUARTERS:
        slice_probs = histogram[start:end]
        expected = round(sum(slice_probs), 2)

        # Usual start: 1º minuto do quarter onde prob >= PLAY_THRESHOLD
        usual_start = None
        usual_end = None
        for i, p in enumerate(slice_probs):
            if p >= PLAY_THRESHOLD:
                if usual_start is None:
                    usual_start = start + i
                usual_end = start + i

        rest_windows = _extract_windows(slice_probs, start, kind="rest")
        play_windows = _extract_windows(slice_probs, start, kind="play")

        patterns.append(QuarterPattern(
            quarter=q,
            expected_minutes=expected,
            usual_start_minute=usual_start,
            usual_end_minute=usual_end,
            rest_windows=rest_windows,
            play_windows=play_windows,
        ))

    return patterns


def _extract_windows(
    probs: list[float],
    offset: int,
    *,
    kind: str,   # "play" | "rest"
) -> list[TimeWindow]:
    """
    Extrai janelas contíguas onde a prob bate o threshold do kind.
    Janelas curtas (< MIN_WINDOW_LEN) são descartadas como ruído.
    """
    threshold = PLAY_THRESHOLD if kind == "play" else REST_THRESHOLD

    def in_window(p: float) -> bool:
        return p >= threshold if kind == "play" else p <= threshold

    windows: list[TimeWindow] = []
    start_i: Optional[int] = None
    for i, p in enumerate(probs):
        if in_window(p):
            if start_i is None:
                start_i = i
        else:
            if start_i is not None:
                length = i - start_i
                if length >= MIN_WINDOW_LEN:
                    avg_prob = statistics.mean(probs[start_i:i])
                    windows.append(TimeWindow(
                        from_minute=offset + start_i,
                        to_minute=offset + i,
                        probability=round(avg_prob, 3),
                    ))
                start_i = None

    # Fecha janela aberta no fim do slice
    if start_i is not None:
        length = len(probs) - start_i
        if length >= MIN_WINDOW_LEN:
            avg_prob = statistics.mean(probs[start_i:])
            windows.append(TimeWindow(
                from_minute=offset + start_i,
                to_minute=offset + len(probs),
                probability=round(avg_prob, 3),
            ))
    return windows


# ---------------------------------------------------------------------------
# Clutch & Blowout — derivam do conjunto bruto de jogos via self-clustering
# ---------------------------------------------------------------------------


CLOSE_GAME_MARGIN = 6      # ≤ 6 pts = jogo decidido no crunch
BLOWOUT_MARGIN = 18        # ≥ 18 pts = blowout (treinador encosta)
# Mínimo de jogos com margem real conhecida pra preferir o método novo
# sobre o self-clustering. Abaixo disso, é mais confiável usar self-clustering.
MIN_GAMES_WITH_MARGIN = 5


def derive_clutch_and_blowout(
    games: list[GameRotationEntry],
    margins: Optional[dict[str, int]] = None,
) -> tuple[ClutchUsage, BlowoutRisk]:
    """
    Classifica cada jogo como close/blowout/normal pra derivar perfis de
    clutch e blowout. Dois modos:

    1. **Real margin** (preferido): quando `margins` é fornecido e cobre
       ≥ MIN_GAMES_WITH_MARGIN dos jogos, usa a margem final verdadeira do
       placar. Mais robusto: jogo competitivo onde o jogador saiu por foul
       não é classificado errado como blowout.

    2. **Self-clustering** (fallback): usa o próprio Q4 do jogador (≥ 0.60 =
       close, ≤ 0.25 com Q1-Q3 ≥ 0.40 = blowout). Heurística antiga, ainda
       útil quando margins não estão disponíveis.

    Args:
        games: lista de jogos do nbarotations
        margins: opcional, mapping gamecode → |home_score - away_score|.
                 Game não presente = sem dado real, classifica via fallback.
    """
    if not games:
        return _neutral_clutch(), _neutral_blowout()

    margins = margins or {}
    games_with_real_margin = sum(1 for g in games if g.gamecode in margins)
    use_real_margin = games_with_real_margin >= MIN_GAMES_WITH_MARGIN

    close_games: list[GameRotationEntry] = []
    blowout_games: list[GameRotationEntry] = []
    normal_games: list[GameRotationEntry] = []

    for g in games:
        if len(g.histogram) < 48:
            continue

        if use_real_margin and g.gamecode in margins:
            m = margins[g.gamecode]
            if m <= CLOSE_GAME_MARGIN:
                close_games.append(g)
            elif m >= BLOWOUT_MARGIN:
                blowout_games.append(g)
            else:
                normal_games.append(g)
            continue

        q4 = g.histogram[36:48]
        q1q3 = g.histogram[0:36]
        q4_avg = statistics.mean(q4) if q4 else 0
        q1q3_avg = statistics.mean(q1q3) if q1q3 else 0

        if q4_avg >= 0.60:
            close_games.append(g)
        elif q4_avg <= 0.25 and q1q3_avg >= 0.40:
            blowout_games.append(g)
        else:
            normal_games.append(g)

    # ── Clutch ──────────────────────────────────────────────────────────
    if close_games:
        # Avg Q4 nos jogos "close"
        q4_avgs = [
            statistics.mean(g.histogram[36:48]) for g in close_games
        ]
        fourth_q_rate = round(statistics.mean(q4_avgs), 3)
        # Prob de jogar no crunch (últimos 5 min do Q4)
        crunch_avgs = [
            statistics.mean(g.histogram[43:48]) for g in close_games
        ]
        crunch_prob = round(statistics.mean(crunch_avgs), 3)
        clutch = ClutchUsage(
            # "Usually closes" se acima de 0.5 dos jogos foram close E ele
            # foi clutch nesses (>= 0.7 prob no Q4 final)
            usually_closes_games=(
                len(close_games) >= max(2, len(games) // 3)
                and crunch_prob >= 0.70
            ),
            fourth_quarter_usage_rate=fourth_q_rate,
            close_game_minutes_probability=crunch_prob,
        )
    else:
        clutch = _neutral_clutch()

    # ── Blowout ────────────────────────────────────────────────────────
    if blowout_games and normal_games:
        normal_q4_avgs = [
            statistics.mean(g.histogram[36:48]) for g in normal_games
        ]
        blowout_q4_avgs = [
            statistics.mean(g.histogram[36:48]) for g in blowout_games
        ]
        normal_total = [sum(g.histogram) for g in normal_games]
        blowout_total = [sum(g.histogram) for g in blowout_games]
        normal_total_avg = statistics.mean(normal_total)
        blowout_total_avg = statistics.mean(blowout_total)

        blowout = BlowoutRisk(
            fourth_quarter_return_probability_when_blowout=round(
                statistics.mean(blowout_q4_avgs), 3
            ),
            typical_minutes_lost_in_blowout=round(
                max(0.0, normal_total_avg - blowout_total_avg), 2
            ),
        )
    else:
        blowout = _neutral_blowout()

    return clutch, blowout


def _neutral_clutch() -> ClutchUsage:
    return ClutchUsage(
        usually_closes_games=False,
        fourth_quarter_usage_rate=0.0,
        close_game_minutes_probability=0.0,
    )


def _neutral_blowout() -> BlowoutRisk:
    return BlowoutRisk(
        fourth_quarter_return_probability_when_blowout=0.0,
        typical_minutes_lost_in_blowout=0.0,
    )
