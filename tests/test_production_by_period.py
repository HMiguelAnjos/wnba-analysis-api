"""
Testes do production_by_period (Fase 11).

Cobre:
  - compute_production_by_period com fetch_pbp mock
  - Filtragem por temporada
  - Agregação minutos × stats em rates
  - lookup_rate helper
  - production_to_dict / from_dict roundtrip
  - Plug no ProjectionEngine via period_production_rate
"""

from __future__ import annotations

import pytest

from src.services.projection import ProjectionEngine
from src.services.rotation.nbarotations_parser import GameRotationEntry
from src.services.rotation.production_by_period import (
    compute_production_by_period,
    lookup_rate,
    production_from_dict,
    production_to_dict,
)


def _make_game(date: str, code: str, histogram_pattern: str) -> GameRotationEntry:
    """
    histogram_pattern: 'qN' onde N = quantos minutos por quarter.
    Ex: 'q12121212' = 12 min cada quarter (full game).
    Pra simplicidade, usamos 1.0 nos minutos jogados, 0.0 nos demais.
    """
    parts = histogram_pattern.replace("q", "").split("|")
    # parts[i] = minutos jogados no quarter i+1 (1..4)
    histogram = []
    for q_mins_str in parts[:4]:
        q_mins = int(q_mins_str)
        # q_mins de 1.0 (jogou) seguidos por (12 - q_mins) de 0.0
        histogram.extend([1.0] * q_mins + [0.0] * (12 - q_mins))
    while len(histogram) < 48:
        histogram.append(0.0)
    return GameRotationEntry(
        gamedate=date, gamecode=code, opponent="OPP", histogram=histogram[:48],
    )


def test_compute_production_returns_none_with_no_games():
    result = compute_production_by_period(
        [],
        player_id=1,
        fetch_pbp_per_period=lambda gid: {},
        season_start="2025-10-01",
    )
    assert result is None


def test_compute_production_filters_by_season():
    games = [
        _make_game("2024-12-01", "old_g1", "12|12|12|12"),  # temporada antiga
        _make_game("2026-01-01", "new_g1", "12|12|12|12"),
    ]
    fetched = []

    def mock_fetch(gid: str) -> dict:
        fetched.append(gid)
        return {1: {1: {"points": 6, "assists": 1, "rebounds": 2}}}

    compute_production_by_period(
        games, player_id=1,
        fetch_pbp_per_period=mock_fetch,
        season_start="2025-10-01",
    )
    # Só o jogo de 2026 deveria ser fetcheado
    assert "old_g1" not in fetched
    assert "new_g1" in fetched


def test_compute_production_requires_min_3_games():
    games = [
        _make_game("2026-01-01", "g1", "12|12|12|12"),
        _make_game("2026-01-02", "g2", "12|12|12|12"),
    ]
    result = compute_production_by_period(
        games, player_id=1,
        fetch_pbp_per_period=lambda gid: {1: {1: {"points": 5, "assists": 1, "rebounds": 1}}},
        season_start="2025-10-01",
    )
    assert result is None


def test_compute_production_aggregates_rates():
    """Player faz 6 pts em 12 min de Q1 em 5 jogos = 0.5 ppm."""
    games = [
        _make_game(f"2026-01-{d:02d}", f"g{d}", "12|12|12|12")
        for d in range(1, 6)
    ]

    def mock_fetch(gid: str) -> dict:
        return {1: {
            1: {"points": 6, "assists": 1, "rebounds": 2},   # Q1
            2: {"points": 8, "assists": 2, "rebounds": 3},   # Q2
            3: {"points": 4, "assists": 1, "rebounds": 2},   # Q3
            4: {"points": 12, "assists": 3, "rebounds": 4},  # Q4 — clutch
        }}

    result = compute_production_by_period(
        games, player_id=1,
        fetch_pbp_per_period=mock_fetch,
        season_start="2025-10-01",
    )
    assert result is not None
    assert len(result) == 4

    q1 = result[0]
    assert q1.quarter == 1
    assert q1.points_per_min == 0.5    # 6 pts / 12 min
    assert q1.sample_games == 5

    q4 = result[3]
    assert q4.points_per_min == 1.0    # 12 pts / 12 min — clutch player


def test_compute_production_skips_games_without_player_data():
    """Se PBP não tem entry pro player, pula esse jogo."""
    games = [_make_game(f"2026-01-{d:02d}", f"g{d}", "12|12|12|12") for d in range(1, 6)]
    call_count = [0]

    def mock_fetch(gid: str) -> dict:
        call_count[0] += 1
        # Só metade dos jogos tem dado pro player
        if call_count[0] <= 2:
            return {}  # PBP vazio
        return {1: {1: {"points": 8, "assists": 1, "rebounds": 1}}}

    result = compute_production_by_period(
        games, player_id=1,
        fetch_pbp_per_period=mock_fetch,
        season_start="2025-10-01",
    )
    # 5 jogos - 2 vazios = 3 com dado. Mínimo 3 — passa.
    assert result is not None
    assert result[0].sample_games == 3


def test_lookup_rate_returns_correct_stat():
    raw = [
        {"quarter": 1, "points_per_min": 0.5, "assists_per_min": 0.1,
         "rebounds_per_min": 0.2, "sample_minutes": 60, "sample_games": 5},
        {"quarter": 2, "points_per_min": 0.7, "assists_per_min": 0.2,
         "rebounds_per_min": 0.15, "sample_minutes": 60, "sample_games": 5},
    ]
    assert lookup_rate(raw, period=1, stat="points") == 0.5
    assert lookup_rate(raw, period=2, stat="assists") == 0.2
    assert lookup_rate(raw, period=3, stat="points") is None  # period sem dado
    assert lookup_rate(raw, period=1, stat="invalid") is None
    assert lookup_rate(None, period=1, stat="points") is None
    assert lookup_rate([], period=1, stat="points") is None


def test_lookup_rate_skips_zero_rates():
    """Rate 0 = sem sinal (provavelmente quarter sem amostra)."""
    raw = [{"quarter": 1, "points_per_min": 0.0, "assists_per_min": 0.0,
            "rebounds_per_min": 0.0, "sample_minutes": 0, "sample_games": 0}]
    assert lookup_rate(raw, period=1, stat="points") is None


def test_production_dict_roundtrip():
    games = [_make_game(f"2026-01-{d:02d}", f"g{d}", "12|12|12|12") for d in range(1, 6)]
    fetched = compute_production_by_period(
        games, player_id=1,
        fetch_pbp_per_period=lambda gid: {1: {1: {"points": 5, "assists": 1, "rebounds": 1}}},
        season_start="2025-10-01",
    )
    serialized = production_to_dict(fetched)
    deserialized = production_from_dict(serialized)
    assert deserialized == fetched


# ─── Integração com ProjectionEngine ────────────────────────────────────


def test_projection_uses_period_production_rate():
    """Quando period_production_rate é alto, projeção sobe vs fallback."""
    pe = ProjectionEngine()
    # Player no Q4 com 30 min jogados, 20 pts (0.67 ppm)
    base = pe.project(
        stat=20, minutes=30, avg_stat=22, avg_minutes=32,
        period=4, game_minutes_remaining=8,
        last_10_avg=22, last_5_avg=22,
        is_starter=True, expected_minutes_remaining=6.0,
        period_production_rate=None,
    )
    # Mesma chamada com rate Q4 alto (clutch player faz 1.0 ppm no Q4)
    boosted = pe.project(
        stat=20, minutes=30, avg_stat=22, avg_minutes=32,
        period=4, game_minutes_remaining=8,
        last_10_avg=22, last_5_avg=22,
        is_starter=True, expected_minutes_remaining=6.0,
        period_production_rate=1.0,    # 1 pt/min no Q4 (alto)
    )
    assert boosted["expected"] > base["expected"]


def test_projection_period_rate_low_reduces_projection():
    """Player com rate baixo no período → projeção menor."""
    pe = ProjectionEngine()
    base = pe.project(
        stat=8, minutes=12, avg_stat=18, avg_minutes=30,
        period=2, game_minutes_remaining=24,
        last_10_avg=18, last_5_avg=18,
        is_starter=True, expected_minutes_remaining=18.0,
        period_production_rate=None,
    )
    cut = pe.project(
        stat=8, minutes=12, avg_stat=18, avg_minutes=30,
        period=2, game_minutes_remaining=24,
        last_10_avg=18, last_5_avg=18,
        is_starter=True, expected_minutes_remaining=18.0,
        period_production_rate=0.20,    # bem abaixo do prior_rate (~0.6)
    )
    assert cut["expected"] < base["expected"]


def test_projection_period_rate_none_preserves_old_behavior():
    """None deve produzir resultado idêntico a sem o param."""
    pe = ProjectionEngine()
    a = pe.project(
        stat=10, minutes=20, avg_stat=20, avg_minutes=32,
        period=3, game_minutes_remaining=20,
        last_10_avg=20, last_5_avg=20,
        is_starter=True,
        period_production_rate=None,
    )
    b = pe.project(
        stat=10, minutes=20, avg_stat=20, avg_minutes=32,
        period=3, game_minutes_remaining=20,
        last_10_avg=20, last_5_avg=20,
        is_starter=True,
    )
    assert a["expected"] == b["expected"]
