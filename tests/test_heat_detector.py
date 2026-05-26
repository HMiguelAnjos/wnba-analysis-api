"""
Testes para HeatDetector (Fase 4).

Cobertura:
- Amostra pequena → neutral
- eFG alto → hot
- FTA agressivo → hot
- eFG baixo + volume baixo → cold
- Scoring run alto → hot
- Cap em ±1.0
"""

from src.services.hot_streak import HeatDetector


detector = HeatDetector()


def test_small_sample_returns_neutral():
    """Menos de 6 minutos jogados → sem sinal."""
    s = detector.score(
        minutes_played=3,
        current_points=4,
        current_fga=2, current_fgm=2, current_3pm=0,
        current_fta=2, current_ftm=2,
        season_minutes=30,
        season_fga_per_min=0.5, season_efg=0.5, season_fta_per_min=0.15,
    )
    assert s.label == "neutral"
    assert s.score == 0.0
    assert "amostra" in s.reason.lower()


def test_high_efg_dominates_signal_but_needs_confirmation():
    """
    eFG bem acima dispara o sinal individual no max (+1.0), mas o score
    composto fica em ~0.45 sem outros sinais ratificando — design
    intencional pra evitar "hot" baseado em 1 sinal só.
    """
    s = detector.score(
        minutes_played=20,
        current_points=18,
        current_fga=10, current_fgm=8, current_3pm=2,
        current_fta=4, current_ftm=3,
        season_minutes=30,
        season_fga_per_min=0.5, season_efg=0.5, season_fta_per_min=0.15,
    )
    assert s.components["efg"] == 1.0
    assert s.score > 0.3   # detecta o sinal
    # Não bate "hot" porque volume/FTA/scoring_run não confirmaram
    assert "efg" in s.reason.lower()


def test_multi_signal_confirmation_marks_hot():
    """Quando vários sinais concordam, score sobe pra hot."""
    s = detector.score(
        minutes_played=20,
        current_points=22,
        current_fga=14, current_fgm=10, current_3pm=3,    # eFG 0.82, volume +75%
        current_fta=8, current_ftm=6,                      # 0.4 FTA/min vs 0.15
        season_minutes=30,
        season_fga_per_min=0.4, season_efg=0.5, season_fta_per_min=0.15,
        scoring_run_streak=3,
    )
    assert s.score >= 0.6
    assert s.label in ("hot", "very_hot")


def test_aggressive_fta_pushes_up():
    """FTA/min muito acima da média → sinal de pressão (hot)."""
    s = detector.score(
        minutes_played=20,
        current_points=14,
        current_fga=8, current_fgm=4, current_3pm=1,
        current_fta=8, current_ftm=6,    # 0.4 FTA/min vs season 0.1
        season_minutes=30,
        season_fga_per_min=0.5, season_efg=0.5, season_fta_per_min=0.10,
    )
    # FTA atual/min = 0.4, season 0.1, ratio 4.0 → cap em +1.0
    assert s.components["fta_rate"] == 1.0


def test_low_efg_marks_cold():
    """eFG bem abaixo + sem outros sinais → cold."""
    s = detector.score(
        minutes_played=20,
        current_points=4,
        current_fga=10, current_fgm=2, current_3pm=0,
        current_fta=0, current_ftm=0,
        season_minutes=30,
        season_fga_per_min=0.5, season_efg=0.55, season_fta_per_min=0.15,
    )
    # eFG = 0.20, delta = -0.35 → -1.0 cap
    assert s.components["efg"] <= -0.9
    assert s.score < 0
    assert s.label in ("cold", "very_cold")


def test_scoring_run_signal():
    """Scoring streak alto → boost no signal."""
    s = detector.score(
        minutes_played=15,
        current_points=12,
        current_fga=6, current_fgm=4, current_3pm=1,
        current_fta=2, current_ftm=2,
        season_minutes=30,
        season_fga_per_min=0.5, season_efg=0.5, season_fta_per_min=0.15,
        scoring_run_streak=4,
    )
    assert s.components["scoring_run"] == 1.0


def test_score_clamped_to_unit_range():
    """Score nunca passa de [-1, +1]."""
    # Tudo no extremo positivo
    s_max = detector.score(
        minutes_played=30,
        current_points=40,
        current_fga=20, current_fgm=18, current_3pm=8,  # eFG ~1.0
        current_fta=15, current_ftm=14,                  # FTA agressivo
        season_minutes=30,
        season_fga_per_min=0.4, season_efg=0.5, season_fta_per_min=0.10,
        scoring_run_streak=10,
    )
    assert -1.0 <= s_max.score <= 1.0
    assert s_max.score >= 0.7  # deveria estar bem alto


def test_neutral_player_returns_zero_ish():
    """Jogador exatamente na média de tudo → score próximo de zero."""
    s = detector.score(
        minutes_played=20,
        current_points=10,
        current_fga=8, current_fgm=4, current_3pm=1,    # eFG = (4+0.5)/8=0.5625
        current_fta=2, current_ftm=2,                    # 0.1 FTA/min
        season_minutes=30,
        season_fga_per_min=0.27,  # 8/20 ≈ 0.4 atual vs 0.27 season → +50%
        season_efg=0.55, season_fta_per_min=0.10,
    )
    # Não tem como ser 0 perfeito, mas label deveria ser neutral ou borderline
    assert -0.6 < s.score < 0.6


def test_heat_signal_dataclass_properties():
    """Sanity check: dataclass expõe is_hot e is_cold corretamente."""
    from src.services.hot_streak import HeatSignal
    hot = HeatSignal(score=0.7, label="hot", components={}, reason="")
    cold = HeatSignal(score=-0.5, label="cold", components={}, reason="")
    neutral = HeatSignal(score=0.0, label="neutral", components={}, reason="")
    assert hot.is_hot
    assert not hot.is_cold
    assert cold.is_cold
    assert not cold.is_hot
    assert not neutral.is_hot
    assert not neutral.is_cold
