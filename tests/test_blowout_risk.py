"""Testes para calculate_blowout_risk."""
from src.utils.stats import calculate_blowout_risk


def test_final_game_blowout_uses_margin():
    """Jogo finalizado em blowout retorna pct alto baseado na margem."""
    pct, level, reason = calculate_blowout_risk(
        period=4, clock="00:00", home_score=137, away_score=98,
        game_status="final",
    )
    assert level == "final"
    assert pct >= 75, f"Margem 39 pts deveria gerar pct>=75, veio {pct}"
    assert "39" in reason


def test_final_game_close_returns_low_pct():
    """Jogo finalizado equilibrado tem pct baixo."""
    pct, level, _ = calculate_blowout_risk(
        period=4, clock="00:00", home_score=110, away_score=107,
        game_status="final",
    )
    assert level == "final"
    assert pct < 20


def test_not_started_returns_low():
    pct, level, _ = calculate_blowout_risk(
        period=0, clock="12:00", home_score=0, away_score=0,
        game_status="not_started",
    )
    assert pct == 0
    assert level == "low"


def test_q1_high_diff_still_low():
    """Q1 com 18 pts de diferença ainda é cedo demais — risco baixo."""
    pct, level, _ = calculate_blowout_risk(
        period=1, clock="03:00", home_score=35, away_score=17,
    )
    assert level == "low", f"Esperado low, veio {level} com pct={pct}"
    assert pct < 35


def test_q2_moderate_diff_starts_to_consider():
    """Q2 com 22 pts já começa a ser risco médio."""
    pct, _, _ = calculate_blowout_risk(
        period=2, clock="04:00", home_score=70, away_score=48,
    )
    assert pct >= 25


def test_q3_big_diff_medium_to_high():
    """Q3 com 25 pts → risco alto."""
    pct, level, _ = calculate_blowout_risk(
        period=3, clock="02:00", home_score=88, away_score=63,
    )
    assert level in ("medium", "high")
    assert pct >= 45


def test_q4_late_close_diff_is_high():
    """Q4 com 18 pts e 3 min restantes → risco alto."""
    pct, level, reason = calculate_blowout_risk(
        period=4, clock="03:00", home_score=110, away_score=92,
    )
    assert level == "high"
    assert pct >= 60
    assert "Q4" in reason or "min" in reason


def test_q4_close_game_low_risk():
    """Q4 com 4 pts — jogo apertado, sem risco."""
    pct, level, _ = calculate_blowout_risk(
        period=4, clock="08:00", home_score=88, away_score=84,
    )
    assert level == "low"
    assert pct <= 15


def test_playoff_dampens_risk():
    """Playoff: mesmo Q4 com 20 pts tem risco menor (titulares ficam)."""
    regular_pct, _, _ = calculate_blowout_risk(
        period=4, clock="04:00", home_score=110, away_score=90,
        is_playoff=False,
    )
    playoff_pct, _, _ = calculate_blowout_risk(
        period=4, clock="04:00", home_score=110, away_score=90,
        is_playoff=True,
    )
    assert playoff_pct < regular_pct
    assert playoff_pct <= regular_pct * 0.5


def test_percentage_clamped_zero_to_hundred():
    pct, _, _ = calculate_blowout_risk(
        period=4, clock="00:01", home_score=140, away_score=80,
    )
    assert 0 <= pct <= 100


def test_reason_mentions_score_diff():
    """Razão sempre menciona a diferença em pontos."""
    _, _, reason = calculate_blowout_risk(
        period=3, clock="05:00", home_score=85, away_score=70,
    )
    assert "15" in reason
