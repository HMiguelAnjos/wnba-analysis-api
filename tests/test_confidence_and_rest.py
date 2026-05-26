"""
Testes pras adições de mai/2026:
- per-stat status (item 2)
- rest_factor / B2B (item 8)
- betting_confidence + 4 confidences (itens 6-7)
"""

from src.utils.stats import (
    betting_confidence_from_signals,
    calc_per_stat_status,
    confidence_label,
    projection_confidence_from_label,
    rest_factor,
    sample_confidence_from_minutes,
)


# ─── per-stat status (item 2) ──────────────────────────────────────────────


def test_per_stat_status_points_thresholds():
    assert calc_per_stat_status(6.0, "points") == "hot"
    assert calc_per_stat_status(5.0, "points") == "hot"
    assert calc_per_stat_status(3.0, "points") == "above_average"
    assert calc_per_stat_status(0.0, "points") == "normal"
    assert calc_per_stat_status(-3.0, "points") == "below_average"
    assert calc_per_stat_status(-6.0, "points") == "cold"


def test_per_stat_status_assists_lower_thresholds():
    # AST/REB usam thresholds menores que PTS (3/1/-1/-3 vs 5/2/-2/-5)
    assert calc_per_stat_status(3.0, "assists") == "hot"
    assert calc_per_stat_status(1.5, "assists") == "above_average"
    assert calc_per_stat_status(-1.5, "assists") == "below_average"
    assert calc_per_stat_status(-3.5, "assists") == "cold"
    # Stat desconhecido → normal (defensivo)
    assert calc_per_stat_status(10.0, "blocks") == "normal"


# ─── rest_factor (item 8) ──────────────────────────────────────────────────


def test_rest_factor_b2b_penalty():
    assert rest_factor(0) == 0.92


def test_rest_factor_normal():
    assert rest_factor(1) == 1.00


def test_rest_factor_two_days_boost():
    assert rest_factor(2) == 1.02


def test_rest_factor_caps_at_three_days():
    assert rest_factor(3) == 1.03
    assert rest_factor(7) == 1.03  # cap mantém


# ─── confidence helpers (item 6) ───────────────────────────────────────────


def test_confidence_label_thresholds():
    assert confidence_label(0.10) == "low"
    assert confidence_label(0.40) == "medium"
    assert confidence_label(0.65) == "medium"
    assert confidence_label(0.66) == "high"
    assert confidence_label(0.99) == "high"


def test_sample_confidence_from_minutes_curve():
    # ProjectionEngine confidence steps: 24+/14+/6+/<6
    assert sample_confidence_from_minutes(30) == 0.90
    assert sample_confidence_from_minutes(20) == 0.65
    assert sample_confidence_from_minutes(8) == 0.35
    assert sample_confidence_from_minutes(2) == 0.10


def test_projection_confidence_label_mapping():
    assert projection_confidence_from_label("high") == 0.90
    assert projection_confidence_from_label("medium") == 0.65
    assert projection_confidence_from_label("low") == 0.35
    assert projection_confidence_from_label("very_low") == 0.10
    assert projection_confidence_from_label("garbage") == 0.50  # default


# ─── betting_confidence (item 7) ───────────────────────────────────────────


def test_betting_confidence_aligned_signal_high():
    """Edge OVER + heat hot + boa amostra → confidence alta."""
    score = betting_confidence_from_signals(
        edge=4.0, heat_score=0.8,
        sample_confidence=0.9, projection_confidence=0.9,
    )
    assert score >= 0.85


def test_betting_confidence_misaligned_drops():
    """Edge OVER + heat COLD → contradição → confidence cai."""
    aligned = betting_confidence_from_signals(
        edge=4.0, heat_score=0.8,
        sample_confidence=0.9, projection_confidence=0.9,
    )
    misaligned = betting_confidence_from_signals(
        edge=4.0, heat_score=-0.8,
        sample_confidence=0.9, projection_confidence=0.9,
    )
    assert misaligned < aligned
    # Penalty real (não só ruído de arredondamento)
    assert (aligned - misaligned) >= 0.20


def test_betting_confidence_low_edge_floor():
    """Edge muito pequeno → score baixo independentemente do resto."""
    score = betting_confidence_from_signals(
        edge=0.5, heat_score=0.0,
        sample_confidence=0.5, projection_confidence=0.5,
    )
    assert score <= 0.40


def test_betting_confidence_neutral_heat_uses_default_alignment():
    """Heat ≈ 0 → alignment fica em 0.5 (não pune nem boost)."""
    score = betting_confidence_from_signals(
        edge=2.0, heat_score=0.0,
        sample_confidence=0.7, projection_confidence=0.7,
    )
    # ~0.35×0.5 + 0.30×0.5 + 0.35×0.7 ≈ 0.57
    assert 0.50 <= score <= 0.65


def test_betting_confidence_clamps_to_unit_interval():
    """Inputs extremos não passam de [0, 1]."""
    very_high = betting_confidence_from_signals(
        edge=20.0, heat_score=1.0,
        sample_confidence=1.0, projection_confidence=1.0,
    )
    assert very_high <= 1.0
    very_low = betting_confidence_from_signals(
        edge=-20.0, heat_score=1.0,    # mismatch + magnitude max
        sample_confidence=0.0, projection_confidence=0.0,
    )
    assert very_low >= 0.0


def test_betting_confidence_sample_ceiling_caps_low_minutes():
    """
    Regressão Robinson/Duren (mai/2026): edge enorme + heat alto NÃO
    pode gerar betting_confidence de LARGE quando a amostra é minúscula.

    Caso real: 5 pts em 6.9 min → projeção 16 → edge +5.5. Antes do
    sample ceiling, betting_conf chegava a 0.77 (→ APOSTAR FORTE).
    Pós-fix: teto 0.40 + 0.60×sample_conf trava a convicção.
    """
    # 6.9 min → sample_confidence ≈ 0.35 → teto = 0.40 + 0.60×0.35 = 0.61
    low_sample = betting_confidence_from_signals(
        edge=5.5, heat_score=1.0,           # edge gigante + heat máximo
        sample_confidence=0.35, projection_confidence=0.35,
    )
    assert low_sample <= 0.61 + 1e-9, (
        f"Sample pequeno deveria capar conf em ~0.61, got {low_sample}"
    )

    # <6 min (sample 0.10) → teto = 0.46 (abaixo de MEDIUM 0.60)
    tiny_sample = betting_confidence_from_signals(
        edge=8.0, heat_score=1.0,
        sample_confidence=0.10, projection_confidence=0.10,
    )
    assert tiny_sample <= 0.46 + 1e-9, (
        f"Sample <6min deveria capar em ~0.46, got {tiny_sample}"
    )

    # Controle: 24 min (sample 0.90) → teto 0.94, NÃO capa edge legítimo.
    full_sample = betting_confidence_from_signals(
        edge=5.5, heat_score=0.8,
        sample_confidence=0.90, projection_confidence=0.90,
    )
    assert full_sample >= 0.85, (
        f"Sample completo não deveria ser capado, got {full_sample}"
    )
