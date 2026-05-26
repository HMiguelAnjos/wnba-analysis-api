"""
RotationContext — saída explicável da camada de rotação.

Toma um `RotationProfile` + estado atual do jogo + estado do jogador
e produz um objeto que a UI consome diretamente:

  - expectedRemainingMinutes
  - currentRotationStatus (EXPECTED_REST, UNEXPECTED_REST, etc.)
  - blowoutRisk (LOW/MEDIUM/HIGH)
  - closingGameProbability
  - rotationConfidence (sample size + variância)
  - notes humanas

Tudo derivado das estruturas já presentes em RotationProfile —
zero chamadas externas aqui.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Limiares pra classificar status
PROB_PLAY = 0.55      # ≥ → "deveria estar em quadra"
PROB_REST = 0.45      # ≤ → "deveria estar descansando"

# Limiares de ANOMALIA (mai/2026 — fix falso-positivo dos alertas
# "jogando mais/menos que o normal"). Variação normal de substituição
# (um minuto que o jogador joga 50-70% das vezes) NÃO é anomalia. Só
# alertamos quando é claramente fora do padrão:
#   - UNEXPECTED_REST: no banco num minuto que ele QUASE SEMPRE joga.
#   - UNEXPECTED_ON_COURT: em quadra num minuto que ele QUASE NUNCA joga.
STRONG_PLAY = 0.78    # ≥ → quase sempre em quadra neste minuto
STRONG_REST = 0.22    # ≤ → quase sempre descansando neste minuto

# Sinal PRIMÁRIO (mai/2026, a pedido): minutos ACUMULADOS jogados vs
# esperados até este ponto do jogo. Mais robusto que o instantâneo —
# "esperava 12 e jogou 18" = jogando mais; "esperava 12 e jogou 5" =
# jogando menos. Esperado = soma das probabilidades por minuto até agora.
MINUTES_DELTA = 4.0          # min de diferença pra virar alerta
MIN_ELAPSED_FOR_PACE = 5     # só após ~5 min de jogo (antes é ruído)


@dataclass
class RotationContextResult:
    """Snapshot de rotação pro frontend. Sempre populado, mesmo em fallback."""
    available: bool                              # False = fallback puro, sem perfil real
    expected_remaining_minutes: float
    current_rotation_status: str                 # EXPECTED_REST | UNEXPECTED_REST | ...
    blowout_risk: str                            # LOW | MEDIUM | HIGH
    closing_game_probability: float              # 0..1
    rotation_confidence: float                   # 0..1
    notes: list[str]
    sample_games: int                            # transparência sobre amostra


def build_context(
    *,
    profile,                              # RotationProfile
    expected_remaining_minutes: float,
    period: int,
    clock_minutes_remaining: float,
    is_player_on_court: bool,
    score_difference: int,
    is_close_game: bool,
    minutes_played: float = 0.0,
) -> RotationContextResult:
    """
    Constrói o objeto pra UI a partir do estado atual + perfil do jogador.

    Args:
      profile: RotationProfile (com ou sem dados reais)
      expected_remaining_minutes: já calculado pelo provider
      period, clock_minutes_remaining: estado do jogo
      is_player_on_court: status atual real do jogador
      score_difference: diferença de placar (positivo = time do player ganha)
      is_close_game: jogo apertado (definido pelo caller; ex: |diff| <= 8)
    """
    notes: list[str] = []

    # Sem perfil real → contexto neutro, marcado como unavailable
    if profile.is_fallback or not profile.minute_probabilities:
        return RotationContextResult(
            available=False,
            expected_remaining_minutes=round(expected_remaining_minutes, 1),
            current_rotation_status="UNKNOWN",
            blowout_risk="LOW",
            closing_game_probability=0.0,
            rotation_confidence=0.0,
            notes=["Padrão de rotação não disponível — usando fallback baseado em média da temporada"],
            sample_games=0,
        )

    # ── 1. Status atual ────────────────────────────────────────────────
    # Probabilidade no minuto atual segundo o histograma do jogador
    elapsed = max(0.0, 12.0 - clock_minutes_remaining)
    minute_idx = min(47, int(round((period - 1) * 12 + elapsed)))
    if period > 4:
        # OT: fora da granularidade do histograma — neutro
        prob_now = 0.5
    else:
        prob_now = profile.minute_probabilities[minute_idx]

    # Minutos ESPERADOS acumulados até agora = soma das probabilidades
    # por minuto até o minuto atual. Comparado com o que ele REALMENTE
    # jogou, é o sinal primário de "jogando mais/menos que o esperado".
    if period <= 4:
        expected_so_far = float(sum(profile.minute_probabilities[:minute_idx]))
    else:
        expected_so_far = minutes_played  # OT fora da granularidade
    minutes_vs_expected = minutes_played - expected_so_far

    # Gate de FRONTEIRA DE PERÍODO (fim de quarto/intervalo ou início do
    # quarto): subs do novo período ainda não aconteceram → não é
    # "jogando menos", é só transição.
    is_period_boundary = (
        clock_minutes_remaining <= 0.5
        or clock_minutes_remaining >= 11.0
    )
    enough_elapsed = period <= 4 and minute_idx >= MIN_ELAPSED_FOR_PACE

    # Base instantânea (em quadra/banco) — usada pra clutch e leitura.
    base_status = "EXPECTED_ON_COURT" if is_player_on_court else "EXPECTED_REST"

    if enough_elapsed and minutes_vs_expected >= MINUTES_DELTA:
        # Jogando MAIS que o esperado (acumulado), em quadra ou não.
        status = "UNEXPECTED_ON_COURT"
        notes.append(
            f"Jogando mais que o esperado: {minutes_played:.0f} min jogados "
            f"vs ~{expected_so_far:.0f} previstos até agora"
        )
    elif (
        enough_elapsed
        and not is_player_on_court
        and not is_period_boundary
        and minutes_vs_expected <= -MINUTES_DELTA
    ):
        # Jogando MENOS que o esperado (acumulado) e está no banco.
        status = "UNEXPECTED_REST"
        notes.append(
            f"Jogando menos que o esperado: {minutes_played:.0f} min jogados "
            f"vs ~{expected_so_far:.0f} previstos até agora — projeção reduzida"
        )
    else:
        status = base_status
        # Notas de contexto (não-anomalia)
        if not is_player_on_court:
            if is_period_boundary and prob_now >= STRONG_PLAY:
                notes.append(
                    "Transição de período — aguardando escalação do novo quarto"
                )
            elif prob_now <= PROB_REST:
                return_minute = _next_play_minute(
                    profile.minute_probabilities, minute_idx
                )
                if return_minute is not None:
                    clock_str = _format_minute(return_minute)
                    notes.append(
                        f"Descanso típico — costuma voltar por volta de {clock_str}"
                    )

    # ── 2. Blowout risk pro JOGADOR ────────────────────────────────────
    blowout_data = profile.blowout_risk or {}
    return_prob = blowout_data.get("fourth_quarter_return_probability_when_blowout", 0.0)
    minutes_lost = blowout_data.get("typical_minutes_lost_in_blowout", 0.0)
    abs_diff = abs(score_difference)

    if abs_diff >= 20 and period >= 4:
        # Blowout claro no Q4
        if return_prob < 0.3:
            blowout_risk = "HIGH"
            notes.append(
                f"Blowout no Q4 + jogador costuma sentar nessa situação (perde ~{minutes_lost:.0f}min)"
            )
        elif return_prob < 0.5:
            blowout_risk = "MEDIUM"
        else:
            blowout_risk = "LOW"
    elif abs_diff >= 15 and period >= 3:
        blowout_risk = "MEDIUM"
    else:
        blowout_risk = "LOW"

    # ── 3. Closing game probability ────────────────────────────────────
    # Single source (mai/2026, item 9): toda a lógica de "joga no fim ou
    # não" mora aqui. Gates aplicados antes de expor o número:
    #   - precisa estar no Q4+ (sem sentido pré-Q4)
    #   - jogo precisa estar competitivo (close ou margem ≤ 11)
    #   - quando dentro do crunch window (≤5min do Q4) E o jogador é
    #     historicamente clutch, exibe nota humana
    #
    # O número final é o `close_game_minutes_probability` do perfil
    # (= prob. do jogador estar em quadra nos últimos 5 min de jogos
    # apertados, agregado dos últimos 10 jogos). Caller usa direto, sem
    # gate adicional.
    clutch_data = profile.clutch_usage or {}
    raw_prob = float(clutch_data.get("close_game_minutes_probability", 0.0))
    is_in_crunch = clutch_minute_in_window(period, clock_minutes_remaining)
    is_competitive_q4 = period >= 4 and abs_diff < 12

    if is_competitive_q4 and raw_prob > 0:
        closing_prob = raw_prob
        if (
            is_close_game
            and is_in_crunch
            and clutch_data.get("usually_closes_games")
        ):
            notes.append(
                f"Costuma fechar jogos apertados ({closing_prob*100:.0f}% de prob. no crunch time)"
            )
    else:
        closing_prob = 0.0

    # ── 4. Confidence baseado em sample size ───────────────────────────
    sample = profile.sample_games
    if sample >= 10:
        confidence = 0.85
    elif sample >= 6:
        confidence = 0.65
    elif sample >= 3:
        confidence = 0.40
    else:
        confidence = 0.20

    # ── 5. Note adicional sobre tempo restante esperado ────────────────
    if expected_remaining_minutes < 1.0 and period <= 4 and clock_minutes_remaining > 2.0:
        notes.append("Provavelmente já encerrou a participação — projeção fechada")

    return RotationContextResult(
        available=True,
        expected_remaining_minutes=round(expected_remaining_minutes, 1),
        current_rotation_status=status,
        blowout_risk=blowout_risk,
        closing_game_probability=round(closing_prob, 3),
        rotation_confidence=confidence,
        notes=notes,
        sample_games=sample,
    )


def _next_play_minute(
    probs: list[float],
    from_idx: int,
    threshold: float = PROB_PLAY,
) -> Optional[int]:
    """Retorna o próximo índice (minuto do jogo) com prob >= threshold, ou None."""
    for i in range(from_idx + 1, len(probs)):
        if probs[i] >= threshold:
            return i
    return None


def _format_minute(minute_idx: int) -> str:
    """Converte índice 0..47 em 'Q3 8:00' style."""
    if minute_idx >= 48:
        return f"OT {minute_idx - 47}"
    quarter = minute_idx // 12 + 1
    minute_in_q = minute_idx % 12
    minutes_remaining = 12 - minute_in_q
    return f"Q{quarter} {minutes_remaining}min restantes"


def clutch_minute_in_window(period: int, clock_minutes_remaining: float) -> bool:
    """True se estamos no crunch time (Q4 com <=5min)."""
    return period >= 4 and clock_minutes_remaining <= 5.5
