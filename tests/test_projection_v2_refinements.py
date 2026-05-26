"""
Testes pros 9 itens de refinamento de projeção (mai/2026).

Cobrem:
  - usage_proxy + usage_label                    (item 6)
  - player_variance_factor                        (item 2)
  - bet_recommendation                            (item 4)
  - HeatSignal.for_stat (per-stat heat)           (item 1)
  - HeatDetector com sinais playmaking/rebounding (item 1)
  - ProjectionEngine UNEXPECTED_REST cap          (item 3)
  - rotation_context single source closing_prob   (item 9)
"""

from __future__ import annotations

from src.services.hot_streak.heat_detector import HeatDetector, HeatSignal
from src.services.projection.projection_engine import ProjectionEngine
from src.services.rotation.rotation_provider import RotationProfile
from src.services.rotation.rotation_context import build_context
from src.utils.stats import (
    bet_recommendation,
    player_variance_factor,
    usage_label,
    usage_proxy,
)


# ─── Item 6: Usage proxy ───────────────────────────────────────────────────


def test_usage_proxy_role_player():
    # 4 FGA em 20min = 0.20 fga/min → role player baixíssimo
    assert usage_proxy(season_fga=4.0, season_minutes=20.0) == 0.10


def test_usage_proxy_starter_padrao():
    # 12 FGA em 30min = 0.40 fga/min → starter padrão (p50)
    assert abs(usage_proxy(season_fga=12.0, season_minutes=30.0) - 0.50) < 0.01


def test_usage_proxy_secondary_star():
    # 16 FGA em 30min = 0.53 fga/min
    val = usage_proxy(season_fga=16.0, season_minutes=30.0)
    assert 0.65 < val < 0.80


def test_usage_proxy_primary_option():
    # 22 FGA em 34min = 0.65 fga/min → p90
    val = usage_proxy(season_fga=22.0, season_minutes=34.0)
    assert val >= 0.85


def test_usage_proxy_handles_zero_minutes():
    # Edge case: minutos zero
    assert usage_proxy(season_fga=10.0, season_minutes=0.0) == 0.5


def test_usage_label_thresholds():
    assert usage_label(0.95) == "primary_option"
    assert usage_label(0.65) == "secondary_option"
    assert usage_label(0.40) == "starter"
    assert usage_label(0.20) == "role_player"
    assert usage_label(0.05) == "low_usage"


# ─── Item 2: Variance factor ───────────────────────────────────────────────


def test_variance_factor_consistent_player():
    # CV ≈ 0.05 (muito consistente: 19, 20, 21, 22, 18)
    val = player_variance_factor([19.0, 20.0, 21.0, 22.0, 18.0])
    assert val >= 0.95


def test_variance_factor_volatile_player():
    # CV alto (35, 8, 12, 28, 17) → factor cai
    val = player_variance_factor([35.0, 8.0, 12.0, 28.0, 17.0])
    assert val < 0.7


def test_variance_factor_extreme_volatility():
    # CV > 0.8 → factor mínimo
    val = player_variance_factor([0.0, 30.0, 0.0, 30.0, 0.0])
    assert val <= 0.30


def test_variance_factor_handles_small_sample():
    # < 3 jogos = neutro
    assert player_variance_factor([20.0, 22.0]) == 0.5
    assert player_variance_factor([]) == 0.5


def test_variance_factor_handles_zero_mean():
    # Mean ≤ 0 = neutro
    assert player_variance_factor([0.0, 0.0, 0.0]) == 0.5


# ─── Item 4: Bet recommendation ────────────────────────────────────────────


def test_bet_recommendation_pass_low_edge():
    rec, size = bet_recommendation(edge=0.5, betting_confidence=0.9, usage=0.7)
    assert rec == "PASS"
    assert size == 0.0


def test_bet_recommendation_pass_low_confidence():
    # Edge alto mas confidence ruim = PASS
    rec, size = bet_recommendation(edge=4.0, betting_confidence=0.30, usage=0.7)
    assert rec == "PASS"


def test_bet_recommendation_large_aligned():
    rec, size = bet_recommendation(edge=3.5, betting_confidence=0.85, usage=0.7)
    assert rec == "LARGE"
    assert size == 1.0


def test_bet_recommendation_medium():
    rec, size = bet_recommendation(edge=2.5, betting_confidence=0.65, usage=0.5)
    assert rec == "MEDIUM"
    assert size == 0.66


def test_bet_recommendation_small():
    rec, size = bet_recommendation(edge=1.5, betting_confidence=0.50, usage=0.5)
    assert rec == "SMALL"
    assert size == 0.33


def test_bet_recommendation_role_player_capped_at_small():
    # Role player (usage < 0.30) nunca passa de SMALL
    rec, size = bet_recommendation(edge=4.0, betting_confidence=0.90, usage=0.20)
    assert rec == "SMALL"


def test_bet_recommendation_primary_option_easier_thresholds():
    # Primary option (usage >= 0.65) atinge LARGE com confidence menor
    rec_primary, _ = bet_recommendation(edge=3.5, betting_confidence=0.72, usage=0.80)
    rec_normal, _ = bet_recommendation(edge=3.5, betting_confidence=0.72, usage=0.50)
    assert rec_primary == "LARGE"
    # Mesmo edge/conf, starter padrão fica em MEDIUM
    assert rec_normal == "MEDIUM"


def test_bet_recommendation_negative_edge_uses_magnitude():
    # Edge negativo grande (UNDER bet) também deve recomendar
    rec, size = bet_recommendation(edge=-3.5, betting_confidence=0.85, usage=0.7)
    assert rec == "LARGE"


# ─── Book bypass (mai/2026) ────────────────────────────────────────────────


def test_book_bypass_accepts_small_with_low_confidence():
    """
    Caso Hayes: edge real +2.2, confidence 0.385 (< 0.40), usage baixo.
    Sem book bypass: PASS. Com book bypass: SMALL.

    O book serve como segunda opinião — quando o livro também vê edge
    ≥ 2.0, falso positivo solo do nosso modelo é mitigado.
    """
    # Sem book bypass: confidence baixa → PASS
    rec_no_book, _ = bet_recommendation(
        edge=2.2, betting_confidence=0.385, usage=0.25,
        has_real_book=False,
    )
    assert rec_no_book == "PASS"

    # Com book bypass: aceita SMALL
    rec_with_book, _ = bet_recommendation(
        edge=2.2, betting_confidence=0.385, usage=0.25,
        has_real_book=True,
    )
    assert rec_with_book == "SMALL"


def test_book_bypass_requires_edge_2_or_higher():
    """
    Book bypass só ativa quando edge ≥ 2.0. Edge menor + confidence
    baixa = PASS mesmo com book.
    """
    rec, _ = bet_recommendation(
        edge=1.5, betting_confidence=0.385, usage=0.5,
        has_real_book=True,
    )
    assert rec == "PASS"


def test_book_bypass_does_not_escalate_to_medium():
    """
    Book bypass NÃO escalona pra MEDIUM/LARGE. Confidence ainda
    governa tamanhos maiores. Edge real +4 com confidence 0.30
    → SMALL (não MEDIUM/LARGE).
    """
    rec, size = bet_recommendation(
        edge=4.0, betting_confidence=0.30, usage=0.5,
        has_real_book=True,
    )
    assert rec == "SMALL"
    assert size == 0.33


def test_book_bypass_works_for_role_player():
    """
    Role player (usage < 0.30) com confidence baixa + book agreeing
    deve virar SMALL (não PASS). Caso Hayes exatamente.
    """
    rec, _ = bet_recommendation(
        edge=2.5, betting_confidence=0.38, usage=0.20,
        has_real_book=True,
    )
    assert rec == "SMALL"


def test_variance_caps_bet_size_for_volatile_player():
    """
    Variance penalty (mai/2026): cara muito volátil (variance < 0.30)
    nunca chega em MEDIUM/LARGE — cap em SMALL.
    """
    # Edge alto + confidence alta normalmente daria LARGE
    rec_normal, _ = bet_recommendation(
        edge=3.5, betting_confidence=0.80, usage=0.7,
        variance_factor=1.0,  # consistente
    )
    assert rec_normal == "LARGE"

    # Mesmo edge + confidence + cara muito volátil → cap em SMALL
    rec_volatile, _ = bet_recommendation(
        edge=3.5, betting_confidence=0.80, usage=0.7,
        variance_factor=0.25,  # muito volátil
    )
    assert rec_volatile == "SMALL"


def test_variance_caps_bet_at_medium_for_moderately_volatile():
    """Variance 0.30-0.50: cap em MEDIUM, não LARGE."""
    rec, _ = bet_recommendation(
        edge=3.5, betting_confidence=0.80, usage=0.7,
        variance_factor=0.40,  # moderadamente volátil
    )
    assert rec == "MEDIUM"


def test_variance_does_not_affect_consistent_player():
    """Variance ≥ 0.50: matriz normal, sem cap adicional."""
    rec, _ = bet_recommendation(
        edge=3.5, betting_confidence=0.80, usage=0.7,
        variance_factor=0.75,  # consistente
    )
    assert rec == "LARGE"


def test_book_bypass_with_normal_confidence_keeps_matrix():
    """
    Quando confidence é boa, book bypass não muda nada — matriz
    normal continua governando. Edge 3 + confidence 0.80 = LARGE
    independente de book.
    """
    rec, _ = bet_recommendation(
        edge=3.5, betting_confidence=0.80, usage=0.7,
        has_real_book=True,
    )
    assert rec == "LARGE"


def test_book_bypass_blocked_by_confidence_floor():
    """
    Floor de 0.30 no book bypass — confidence muito baixa (sinais
    pathological contraditórios) → PASS mesmo com book agreeing.

    Caso típico: cara com 4 min jogados (sample tiny) + heat oposto
    ao edge. Os dois "concordando" pode ser só calibração lenta com
    poucos dados.
    """
    # Confidence 0.25 < floor 0.30 → PASS mesmo com book + edge alto
    rec, _ = bet_recommendation(
        edge=2.5, betting_confidence=0.25, usage=0.5,
        has_real_book=True,
    )
    assert rec == "PASS"

    # Confidence exatamente 0.30 → passa (>= floor)
    rec_at_floor, _ = bet_recommendation(
        edge=2.5, betting_confidence=0.30, usage=0.5,
        has_real_book=True,
    )
    assert rec_at_floor == "SMALL"


# ─── Item 1: HeatSignal.for_stat ───────────────────────────────────────────


def test_heat_signal_for_stat_routes_correctly():
    sig = HeatSignal(
        score=0.5, label="hot", components={}, reason="",
        scoring=0.8, playmaking=0.2, rebounding=-0.3,
    )
    assert sig.for_stat("points") == 0.8
    assert sig.for_stat("assists") == 0.2
    assert sig.for_stat("rebounds") == -0.3
    # 3PM herda do scoring
    assert sig.for_stat("three_pm") == 0.8
    # Stat desconhecido cai no composto
    assert sig.for_stat("steals") == 0.5


def test_heat_detector_returns_per_stat_signals():
    """HeatDetector deve preencher scoring/playmaking/rebounding."""
    h = HeatDetector()
    sig = h.score(
        minutes_played=20.0,
        current_points=20, current_fga=12, current_fgm=8, current_3pm=2,
        current_fta=4, current_ftm=4,
        current_assists=8, current_rebounds=6, current_turnovers=1,
        season_minutes=30.0,
        season_fga_per_min=0.4, season_efg=0.55, season_fta_per_min=0.10,
        season_ast_per_min=0.20, season_reb_per_min=0.15,
    )
    # eFG alto + volume alto + ataque ao aro → scoring positivo
    assert sig.scoring > 0.3
    # AST/min atual = 8/20 = 0.4 vs season 0.20 → ratio 2× → playmaking positivo
    assert sig.playmaking > 0.3
    # REB/min atual = 6/20 = 0.30 vs season 0.15 → ratio 2× → rebounding positivo
    assert sig.rebounding > 0.3


def test_heat_detector_low_minutes_returns_zero_per_stat():
    """Amostra pequena → todos os sub-scores em zero."""
    h = HeatDetector()
    sig = h.score(
        minutes_played=4.0,    # < MIN_MINUTES_FOR_HEAT
        current_points=10, current_fga=5, current_fgm=4, current_3pm=2,
        current_fta=2, current_ftm=2,
        season_minutes=30.0,
    )
    assert sig.scoring == 0.0
    assert sig.playmaking == 0.0
    assert sig.rebounding == 0.0


def test_heat_detector_playmaking_drops_with_turnovers():
    """Cara com 5 ast e 5 TOV não é "quente" em assist."""
    h = HeatDetector()
    sig_no_tov = h.score(
        minutes_played=20.0,
        current_points=10, current_fga=8, current_fgm=4, current_3pm=1,
        current_fta=2, current_ftm=2,
        current_assists=8, current_rebounds=4, current_turnovers=0,
        season_minutes=30.0,
        season_ast_per_min=0.20,
    )
    sig_high_tov = h.score(
        minutes_played=20.0,
        current_points=10, current_fga=8, current_fgm=4, current_3pm=1,
        current_fta=2, current_ftm=2,
        current_assists=8, current_rebounds=4, current_turnovers=5,
        season_minutes=30.0,
        season_ast_per_min=0.20,
    )
    assert sig_no_tov.playmaking > sig_high_tov.playmaking


# ─── UNEXPECTED_REST cap (Mudança 1, mai/2026) ─────────────────────────────
# Comportamento novo: o cap só dispara quando heat_score ≤ -0.3.
# Sem confirmação de frio, descanso é tratado como rotação normal.


def test_projection_unexpected_rest_no_cap_when_heat_neutral():
    """
    Sem heat negativo, UNEXPECTED_REST NÃO trava a projeção.
    Cara em descanso normal entre rotações cai aqui — projeção segue
    extrapolando.
    """
    eng = ProjectionEngine()
    normal = eng.project(
        stat=8, minutes=15.0,
        avg_stat=20.0, avg_minutes=32.0,
        is_final=False,
        heat_score=0.0,
    )
    with_rest_neutral = eng.project(
        stat=8, minutes=15.0,
        avg_stat=20.0, avg_minutes=32.0,
        is_final=False,
        is_unexpected_rest=True,
        heat_score=0.0,
    )
    # Sem heat negativo, projeção deve ser idêntica (ou bem próxima) ao
    # caso normal — flag UNEXPECTED_REST sozinha não bloqueia.
    assert abs(with_rest_neutral["expected"] - normal["expected"]) < 0.5


def test_projection_unexpected_rest_caps_only_when_cold_AND_underperforming():
    """
    Gate v2 (mai/2026): UNEXPECTED_REST cap exige TODAS 3 condições:
    heat ≤ -0.30 + rate_ratio < 0.60 + flag externa.

    Cara mal de produção (3 pts em 15 min, prior 20/32 = 0.625/min):
      rate_ratio = (3/15)/0.625 = 0.32 ≤ 0.60 ✓
      heat -0.50 ≤ -0.30 ✓
      is_unexpected_rest ✓
    Todos os 3 batem → cap dispara.
    """
    eng = ProjectionEngine()
    capped = eng.project(
        stat=3, minutes=15.0,
        avg_stat=20.0, avg_minutes=32.0,
        is_final=False,
        is_unexpected_rest=True,
        heat_score=-0.5,
    )
    # Cap aplicado (descanso fora do padrão na razão)
    assert "Descanso fora do padrão" in capped["reason"]
    # Projeção truncada — current = 3 pts, cap é o MAIOR entre
    # 3*1.15=3.45 e expected_before*0.5
    assert capped["expected"] < 10.0


def test_projection_unexpected_rest_NOT_capped_when_only_borderline_cold():
    """
    Caso McDaniels real: cara em produção OK (rate_ratio ~0.80),
    apenas borderline cold (heat -0.31). NÃO deve disparar cap.
    """
    eng = ProjectionEngine()
    out = eng.project(
        stat=7, minutes=18.0,                # rate 0.39
        avg_stat=14.0, avg_minutes=29.0,     # prior_rate 0.48
        # rate_ratio = 0.39 / 0.48 = 0.81 — acima de 0.60
        is_final=False,
        is_unexpected_rest=True,
        heat_score=-0.31,                    # borderline
    )
    bd = out["breakdown"]
    # Cap NÃO aplicado
    assert "unexpected_rest_cap_applied" not in bd
    # Skipped pelo gate (rate_ratio alto)
    assert "unexpected_rest_cap_skipped" in bd


def test_projection_unexpected_rest_drops_confidence_only_when_capped():
    """Confidence só cai quando o cap efetivamente dispara."""
    eng = ProjectionEngine()
    # Heat neutro — gate não passa, confidence mantém
    no_cap = eng.project(
        stat=10, minutes=24.0,
        avg_stat=20.0, avg_minutes=32.0,
        is_final=False,
        is_unexpected_rest=True,
        heat_score=0.0,
    )
    assert no_cap["confidence"] == "high"
    # Cara muito cold + heat frio — cap dispara
    capped = eng.project(
        stat=4, minutes=24.0,                # rate 0.17, prior 0.625, ratio 0.27
        avg_stat=20.0, avg_minutes=32.0,
        is_final=False,
        is_unexpected_rest=True,
        heat_score=-0.5,
    )
    assert capped["confidence"] == "low"


# ─── Item 9: Closing game probability single source ────────────────────────


def _make_profile_with_clutch(usually_closes: bool, prob: float) -> RotationProfile:
    return RotationProfile(
        player_id=1,
        total_minutes=32.0,
        minute_probabilities=[0.8] * 48,
        sample_games=10,
        is_fallback=False,
        clutch_usage={
            "usually_closes_games": usually_closes,
            "fourth_quarter_usage_rate": 0.85,
            "close_game_minutes_probability": prob,
        },
        blowout_risk={
            "fourth_quarter_return_probability_when_blowout": 0.5,
            "typical_minutes_lost_in_blowout": 3.0,
        },
    )


def test_closing_prob_zero_when_not_q4():
    profile = _make_profile_with_clutch(usually_closes=True, prob=0.85)
    ctx = build_context(
        profile=profile, expected_remaining_minutes=15.0,
        period=2, clock_minutes_remaining=8.0,
        is_player_on_court=True, score_difference=2, is_close_game=True,
    )
    assert ctx.closing_game_probability == 0.0


def test_closing_prob_populated_in_competitive_q4():
    profile = _make_profile_with_clutch(usually_closes=True, prob=0.85)
    ctx = build_context(
        profile=profile, expected_remaining_minutes=5.0,
        period=4, clock_minutes_remaining=4.0,
        is_player_on_court=True, score_difference=4, is_close_game=True,
    )
    assert ctx.closing_game_probability == 0.85


def test_closing_prob_zero_when_blowout():
    """Q4 com diff >= 12 → não é close → closing_prob volta a zero."""
    profile = _make_profile_with_clutch(usually_closes=True, prob=0.85)
    ctx = build_context(
        profile=profile, expected_remaining_minutes=5.0,
        period=4, clock_minutes_remaining=4.0,
        is_player_on_court=True, score_difference=18, is_close_game=False,
    )
    assert ctx.closing_game_probability == 0.0


def test_closing_prob_uses_perfil_value_directly():
    """Não inflar nem deflacionar — repassa o número do perfil."""
    profile = _make_profile_with_clutch(usually_closes=False, prob=0.45)
    ctx = build_context(
        profile=profile, expected_remaining_minutes=5.0,
        period=4, clock_minutes_remaining=3.0,
        is_player_on_court=True, score_difference=5, is_close_game=True,
    )
    # Perfil não-clutch (usually_closes=False), mas prob > 0 → ainda expõe
    assert ctx.closing_game_probability == 0.45
