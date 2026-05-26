"""
CASHOUT detector (mai/2026).

Sinaliza jogador relevante/titular que está MOMENTANEAMENTE no banco,
com a linha do mercado colada na produção atual, e que a NBA Rotation
indica retorno provável. Quando ele voltar, a linha tende a subir — é
hora de ATENÇÃO pra cashout / ajuste de posição, NÃO de entrada forte.

Função pura, sem I/O. Recebe dados já calculados em outras camadas
(rotation_context + fair_line + on_court + is_starter/usage). Não altera
decision/bet_recommendation — é um aviso paralelo.

Filosofia dos gates: cada exclusor do spec cai num gate determinístico —
reserva profundo → relevância; blowout → blowout_risk; faltas →
foul_trouble; lesão/expulsão → status; pouco tempo → expected_remaining;
linha longe → janela de proximidade; baixa chance de volta →
closing_prob/status.
"""
from __future__ import annotations

from typing import Optional

# ─── Thresholds (calibração inicial — ajustar com dados de produção) ─────────

# Janela de proximidade: linha entre +0.5 e +3.5 acima do stat atual.
# "tem 15 pts, linha 16.5/17.5/18.5" → diff 1.5/2.5/3.5.
LINE_GAP_MIN = 0.5
LINE_GAP_MAX = 3.5

MIN_REMAINING_MINUTES = 4.0          # ainda tem jogo pra ele voltar e produzir
RELEVANCE_USAGE = 0.45               # usage >= isso = relevante no ataque
RELEVANCE_REMAINING = 6.0            # OU expected_remaining alto = relevante
RETURN_CLOSING_PROB = 0.45           # >= isso = boa chance de fechar/voltar
MIN_ROTATION_CONFIDENCE = 0.40       # amostra mínima confiável

# Confidence HIGH exige convicção forte nos 3 eixos.
HIGH_CLOSING_PROB = 0.60
HIGH_ROTATION_CONFIDENCE = 0.65


def detect_cashout(
    *,
    markets: list[tuple[str, float, float]],   # [(market, current_stat, line), ...]
    on_court: bool,
    status: str,                               # "ACTIVE" | "INACTIVE"
    is_starter: bool,
    usage: float,
    foul_trouble: bool,
    rotation_available: bool,
    expected_remaining_minutes: float,
    closing_game_probability: float,
    current_rotation_status: str,              # EXPECTED_REST | UNEXPECTED_REST | ...
    rotation_confidence: float,
    blowout_risk: str,                         # LOW | MEDIUM | HIGH
) -> Optional[dict]:
    """
    Retorna dict (mapeável pra CashoutAlertSchema) ou None.

    `markets`: lista de (label, stat_atual, linha) — linha já deve ser a
    "linha atual" preferida pelo caller (real_line do book quando existe,
    senão a sintética). Avalia os 3 e emite no de linha mais COLADA
    (menor gap) — o mais "na beira" de cruzar quando ele voltar.
    """
    # ── Gates player-level (independem de mercado) ──────────────────────
    if on_court:
        return None
    if status != "ACTIVE":                       # lesão / expulsão / DNP
        return None
    if not rotation_available:                   # sem perfil real → não confia
        return None
    if foul_trouble:                             # 4+ faltas → risco de banco real
        return None
    if blowout_risk == "HIGH":                   # garbage time → não volta
        return None
    if rotation_confidence < MIN_ROTATION_CONFIDENCE:
        return None
    if expected_remaining_minutes < MIN_REMAINING_MINUTES:
        return None

    # Relevância na rotação: titular OU primário no ataque OU ainda tem
    # bastante jogo esperado (proxy de "minutos relevantes").
    is_relevant = (
        is_starter
        or usage >= RELEVANCE_USAGE
        or expected_remaining_minutes >= RELEVANCE_REMAINING
    )
    if not is_relevant:
        return None

    # Retorno provável: descanso DENTRO do padrão (EXPECTED_REST = volta
    # pelo histórico) OU alta prob. de fechar jogo. UNEXPECTED_REST fica
    # de fora — descanso anormal pode ser lesão escondida (risco).
    likely_return = (
        closing_game_probability >= RETURN_CLOSING_PROB
        or current_rotation_status == "EXPECTED_REST"
    )
    if not likely_return:
        return None

    # ── Seleção de mercado: o de linha mais COLADA dentro da janela ─────
    best: Optional[tuple[float, str, float, float]] = None  # (gap, mkt, stat, line)
    for market, current_stat, line in markets:
        if line is None or line <= 0:
            continue
        gap = round(line - current_stat, 2)
        if LINE_GAP_MIN <= gap <= LINE_GAP_MAX:
            if best is None or gap < best[0]:
                best = (gap, market, current_stat, line)

    if best is None:
        return None

    gap, market, current_stat, line = best

    # ── Confidence ──────────────────────────────────────────────────────
    confidence = (
        "HIGH"
        if (
            closing_game_probability >= HIGH_CLOSING_PROB
            and rotation_confidence >= HIGH_ROTATION_CONFIDENCE
            and is_starter
        )
        else "MEDIUM"
    )

    market_pt = {"PTS": "pontos", "REB": "rebotes", "AST": "assistências"}.get(
        market, market.lower()
    )
    role = "titular" if is_starter else "rotação relevante"
    reason = (
        f"Jogador {role} no banco agora — tem {current_stat:.0f} "
        f"{market_pt}, linha {line:g} (faltam {gap:g}). NBA Rotation indica "
        f"retorno provável (~{expected_remaining_minutes:.0f} min restantes"
        + (
            f", {closing_game_probability * 100:.0f}% de fechar jogo"
            if closing_game_probability > 0 else ""
        )
        + "). Atenção pra cashout: a linha tende a subir quando ele voltar."
    )

    return {
        "market": market,
        "label": "Cashout",
        "icon": "money",
        "reason": reason,
        "confidence": confidence,
        "current_stat": round(current_stat, 1),
        "line": round(line, 1),
        "difference_to_line": gap,
        "is_on_court": False,
        "expected_remaining_minutes": round(expected_remaining_minutes, 1),
        "closing_game_probability": round(closing_game_probability, 3),
        "rotation_status": "LIKELY_TO_RETURN",
    }
