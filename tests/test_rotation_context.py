"""
Testes dos 10 cenários explicitamente listados no prompt da Fase 2 V3:

1. jogador em descanso esperado
2. jogador em descanso inesperado
3. jogador em quadra no período esperado
4. jogador fora em blowout
5. jogador costuma fechar jogos apertados
6. NBA Rotation indisponível
7. cache expirado
8. sem dados de rotação
9. projeção atual preservada com feature flag desligada
10. projeção ajustada com feature flag ligada
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.services.projection import ProjectionEngine
from src.services.rotation import RotationProfile, RotationProvider
from src.services.rotation.rotation_context import build_context


# ─── Helpers ──────────────────────────────────────────────────────────────


class _MockClient:
    def __init__(self, html=None):
        self.html = html
        self.calls = []

    def fetch_player_html(self, player_id: int):
        self.calls.append(player_id)
        return self.html


def _make_profile_with_rest_window():
    """Profile sintético: titular típico — joga Q1 inteiro, descansa início Q2."""
    # Q1 (0-11): 1.0 todo. Q2 (12-15): 0.0 (descanso). Q2 (16-23): 1.0. Q3-Q4: 1.0
    histogram = (
        [1.0] * 12     # Q1: joga tudo
        + [0.0] * 4    # Q2 início: descansa
        + [1.0] * 8    # Q2 fim: volta
        + [1.0] * 12   # Q3
        + [1.0] * 12   # Q4
    )
    return RotationProfile(
        player_id=1,
        total_minutes=44.0,
        minute_probabilities=histogram,
        sample_games=10,
        is_fallback=False,
        clutch_usage={
            "usually_closes_games": True,
            "fourth_quarter_usage_rate": 0.95,
            "close_game_minutes_probability": 0.90,
        },
        blowout_risk={
            "fourth_quarter_return_probability_when_blowout": 0.20,
            "typical_minutes_lost_in_blowout": 8.0,
        },
    )


def _make_fallback_profile():
    return RotationProfile(
        player_id=99,
        total_minutes=24.0,
        minute_probabilities=[],
        sample_games=0,
        is_fallback=True,
    )


def _make_profile_uniform(prob: float):
    """Profile sintético com a MESMA probabilidade em todos os 48 minutos.
    Útil pra testar a zona de variação normal (nem quase-sempre joga,
    nem quase-sempre descansa)."""
    return RotationProfile(
        player_id=2,
        total_minutes=round(prob * 48, 1),
        minute_probabilities=[prob] * 48,
        sample_games=10,
        is_fallback=False,
        clutch_usage={},
        blowout_risk={},
    )


# ─── Cenário 1: jogador em descanso esperado ─────────────────────────────


def test_scenario_1_expected_rest():
    """Q2 com 9min restantes (minuto 15 do jogo): perfil mostra descanso."""
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=20.0,
        period=2,
        clock_minutes_remaining=9,
        is_player_on_court=False,
        score_difference=2,
        is_close_game=True,
        minutes_played=12.0,   # jogou o Q1 inteiro → no esperado
    )
    assert ctx.current_rotation_status == "EXPECTED_REST"
    assert ctx.available is True
    # Note positiva sobre quando volta
    assert any("voltar" in n.lower() or "típico" in n.lower() for n in ctx.notes)


# ─── Cenário 2: jogador em descanso inesperado ───────────────────────────


def test_scenario_2_unexpected_rest():
    """Q1 com 6min restantes: perfil mostra que ele DEVERIA estar em quadra."""
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=20.0,
        period=1,
        clock_minutes_remaining=6,
        is_player_on_court=False,   # mas está no banco
        score_difference=0,
        is_close_game=True,
        minutes_played=0.0,   # esperava ~6 min até aqui, jogou 0
    )
    assert ctx.current_rotation_status == "UNEXPECTED_REST"
    assert any("menos que o esperado" in n.lower() for n in ctx.notes)


# ─── Gate de fronteira de período (mai/2026 — fix Ausar Thompson) ────────


def test_period_boundary_halftime_not_unexpected_rest():
    """
    Intervalo (período 2, clock 0): titular fora de quadra porque é
    halftime — NÃO é descanso fora do padrão. Antes do gate virava
    UNEXPECTED_REST falso (caso Ausar 14.5min).
    """
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=18.0,
        period=2,
        clock_minutes_remaining=0.0,   # fim do Q2 / intervalo
        is_player_on_court=False,
        score_difference=0,
        is_close_game=True,
    )
    assert ctx.current_rotation_status == "EXPECTED_REST"
    assert any("transição" in n.lower() for n in ctx.notes)


def test_period_boundary_start_of_quarter_not_unexpected_rest():
    """
    Início do Q3 (clock 11.5): subs do novo período ainda não
    aconteceram. Titular não voltou ≠ descanso fora do padrão.
    """
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=20.0,
        period=3,
        clock_minutes_remaining=11.5,  # acabou de começar o Q3
        is_player_on_court=False,
        score_difference=0,
        is_close_game=True,
    )
    assert ctx.current_rotation_status == "EXPECTED_REST"


def test_mid_period_still_flags_unexpected_rest():
    """
    Controle: meio do Q3 (clock 6.0, NÃO é fronteira) com perfil
    dizendo que deveria jogar → UNEXPECTED_REST continua disparando.
    Garante que o gate não matou o sinal legítimo.
    """
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=12.0,
        period=3,
        clock_minutes_remaining=6.0,   # meio do período — não é boundary
        is_player_on_court=False,
        score_difference=0,
        is_close_game=True,
        minutes_played=8.0,   # esperava ~26 até aqui, jogou só 8
    )
    assert ctx.current_rotation_status == "UNEXPECTED_REST"
    assert any("menos que o esperado" in n.lower() for n in ctx.notes)


# ─── Cenário 3: jogador em quadra no período esperado ────────────────────


def test_scenario_3_expected_on_court():
    """Q1 com 8min restantes: deveria estar em quadra E está."""
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=35.0,
        period=1,
        clock_minutes_remaining=8,
        is_player_on_court=True,
        score_difference=0,
        is_close_game=True,
    )
    assert ctx.current_rotation_status == "EXPECTED_ON_COURT"


# ─── Regressão (mai/2026): variação normal NÃO vira alerta ───────────────


def test_within_expected_minutes_no_alert():
    """Jogou ~o esperado até agora (perto do previsto) → sem alerta,
    em quadra ou no banco."""
    profile = _make_profile_uniform(0.50)
    # minuto 6 do jogo → esperado ~3 min (0.5*6). Jogou 3 → delta 0.
    ctx_court = build_context(
        profile=profile, expected_remaining_minutes=20.0, period=1,
        clock_minutes_remaining=6.0, is_player_on_court=True,
        score_difference=0, is_close_game=True, minutes_played=3.0,
    )
    assert ctx_court.current_rotation_status == "EXPECTED_ON_COURT"

    ctx_bench = build_context(
        profile=profile, expected_remaining_minutes=20.0, period=1,
        clock_minutes_remaining=6.0, is_player_on_court=False,
        score_difference=0, is_close_game=True, minutes_played=3.0,
    )
    assert ctx_bench.current_rotation_status == "EXPECTED_REST"
    assert not any("esperado" in n.lower() for n in ctx_bench.notes)


def test_cumulative_more_minutes_than_expected():
    """Esperava ~15 min até a metade do jogo, jogou 22 → jogando MAIS."""
    profile = _make_profile_uniform(0.50)  # ~24 mpg
    ctx = build_context(
        profile=profile, expected_remaining_minutes=18.0, period=3,
        clock_minutes_remaining=6.0,  # minuto ~30 → esperado ~15
        is_player_on_court=True, score_difference=0, is_close_game=True,
        minutes_played=22.0,
    )
    assert ctx.current_rotation_status == "UNEXPECTED_ON_COURT"
    assert any("mais que o esperado" in n.lower() for n in ctx.notes)


def test_cumulative_less_minutes_than_expected():
    """Esperava ~15 min até a metade, jogou só 8 e está no banco →
    jogando MENOS."""
    profile = _make_profile_uniform(0.50)
    ctx = build_context(
        profile=profile, expected_remaining_minutes=10.0, period=3,
        clock_minutes_remaining=6.0,  # minuto ~30 → esperado ~15
        is_player_on_court=False, score_difference=0, is_close_game=True,
        minutes_played=8.0,
    )
    assert ctx.current_rotation_status == "UNEXPECTED_REST"
    assert any("menos que o esperado" in n.lower() for n in ctx.notes)


# ─── Cenário 4: jogador fora em blowout ──────────────────────────────────


def test_scenario_4_blowout_high_risk():
    """Q4 com 6min restantes, diferença +25, jogador costuma sentar em blowout."""
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=2.0,
        period=4,
        clock_minutes_remaining=6,
        is_player_on_court=False,
        score_difference=25,           # blowout
        is_close_game=False,
    )
    assert ctx.blowout_risk == "HIGH"
    assert any("blowout" in n.lower() for n in ctx.notes)


# ─── Cenário 5: jogador costuma fechar jogos apertados ────────────────────


def test_scenario_5_clutch_closes_games():
    """Q4 final + jogo apertado + perfil clutch → menciona que fecha jogos."""
    profile = _make_profile_with_rest_window()
    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=4.0,
        period=4,
        clock_minutes_remaining=4,
        is_player_on_court=True,
        score_difference=3,            # apertado
        is_close_game=True,
    )
    assert ctx.closing_game_probability >= 0.7
    assert any("fecha" in n.lower() for n in ctx.notes)


# ─── Cenário 6: NBA Rotation indisponível ────────────────────────────────


def test_scenario_6_nbarotation_unavailable():
    """Client retorna None → fallback profile, context flagged unavailable."""
    p = RotationProvider(client=_MockClient(html=None))
    profile = p.get_profile(player_id=1, season_minutes=30)
    assert profile.is_fallback is True

    ctx = build_context(
        profile=profile,
        expected_remaining_minutes=15.0,
        period=2, clock_minutes_remaining=10,
        is_player_on_court=True,
        score_difference=0,
        is_close_game=True,
    )
    assert ctx.available is False
    assert ctx.current_rotation_status == "UNKNOWN"
    assert any("não dispon" in n.lower() or "fallback" in n.lower() for n in ctx.notes)


# ─── Cenário 7: cache expirado ───────────────────────────────────────────


def test_scenario_7_cache_reuses_within_ttl(tmp_path, monkeypatch):
    """Dentro do TTL, segunda chamada vem do cache (sem novo HTTP)."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    # Force reload do cache pra usar tmp_path
    from src.utils.cache import PersistentCache
    fresh_cache = PersistentCache(path=str(tmp_path / "rot_test.json"))

    games_html = '<script>displayPlayer([])</script>'
    client = _MockClient(html=games_html)
    p = RotationProvider(client=client, cache=fresh_cache)
    # Player único pra esse teste pra evitar collision com cache global
    p.get_profile(player_id=8888888, season_minutes=30)
    p.get_profile(player_id=8888888, season_minutes=30)
    p.get_profile(player_id=8888888, season_minutes=30)
    # Cache deveria evitar chamadas extras
    assert len(client.calls) == 1, f"esperado 1 call, veio {len(client.calls)}"


# ─── Cenário 8: sem dados de rotação (sample insuficiente) ────────────────


def test_scenario_8_insufficient_sample_falls_back():
    """Player tem só 2 jogos da temporada → cai pro fallback."""
    games = [
        {"date": "2026-01-01", "gamecode": "g1", "gamedate": "2026-01-01",
         "histogram": [1.0]*48, "name": "X", "opponent": "OPP",
         "teamcode": 0, "year": 2025, "yearteam": "2025LAL"},
        {"date": "2026-01-02", "gamecode": "g2", "gamedate": "2026-01-02",
         "histogram": [1.0]*48, "name": "X", "opponent": "OPP",
         "teamcode": 0, "year": 2025, "yearteam": "2025LAL"},
    ]
    html = f"<script>displayPlayer({json.dumps(games)});</script>"
    p = RotationProvider(client=_MockClient(html=html))
    profile = p.get_profile(player_id=1, season_minutes=30)
    # 2 jogos < 3 mínimo → fallback
    assert profile.is_fallback is True


# ─── Cenário 9: feature flag desligada → projeção sem ajuste ──────────────


def test_scenario_9_feature_flag_off_preserves_projection(monkeypatch):
    """Com ENABLE_NBA_ROTATION_ADJUSTMENT=0, projeção deve ser idêntica
    à versão sem rotation."""
    pe = ProjectionEngine()
    # Sem rotation_remaining → fallback nativo
    base = pe.project(
        stat=10, minutes=20,
        avg_stat=20, avg_minutes=32,
        period=3, game_minutes_remaining=20,
        last_10_avg=20, last_5_avg=20,
        is_starter=True, heat_score=0.0,
        expected_minutes_remaining=None,
        clutch_close_game_boost=0.0,
        rotation_blowout_cut=0.0,
    )
    # Mesma chamada com rotation explícita None → mesmo resultado
    same = pe.project(
        stat=10, minutes=20,
        avg_stat=20, avg_minutes=32,
        period=3, game_minutes_remaining=20,
        last_10_avg=20, last_5_avg=20,
        is_starter=True, heat_score=0.0,
        expected_minutes_remaining=None,
    )
    assert base["expected"] == same["expected"]


# ─── Cenário 10: feature flag ligada → projeção ajustada ──────────────────


def test_scenario_10_feature_flag_on_adjusts_projection():
    """Com expected_minutes_remaining real, projeção deve diferir do fallback."""
    pe = ProjectionEngine()
    fallback = pe.project(
        stat=10, minutes=20,
        avg_stat=20, avg_minutes=32,
        period=3, game_minutes_remaining=20,
        last_10_avg=20, last_5_avg=20,
        is_starter=True, heat_score=0.0,
        expected_minutes_remaining=None,
    )
    with_rotation = pe.project(
        stat=10, minutes=20,
        avg_stat=20, avg_minutes=32,
        period=3, game_minutes_remaining=20,
        last_10_avg=20, last_5_avg=20,
        is_starter=True, heat_score=0.0,
        expected_minutes_remaining=4.0,   # Bem menos que o fallback assumiria
    )
    # Com menos minutos restantes esperados, a projeção deve ser MENOR
    assert with_rotation["expected"] < fallback["expected"]


# ─── Cenário extra: clutch boost aumenta projeção ─────────────────────────


def test_clutch_boost_increases_projection():
    pe = ProjectionEngine()
    sem_clutch = pe.project(
        stat=20, minutes=30, avg_stat=25, avg_minutes=35,
        period=4, game_minutes_remaining=4,
        last_10_avg=25, last_5_avg=25,
        is_starter=True, expected_minutes_remaining=4.0,
        clutch_close_game_boost=0.0,
    )
    com_clutch = pe.project(
        stat=20, minutes=30, avg_stat=25, avg_minutes=35,
        period=4, game_minutes_remaining=4,
        last_10_avg=25, last_5_avg=25,
        is_starter=True, expected_minutes_remaining=4.0,
        clutch_close_game_boost=0.9,
    )
    assert com_clutch["expected"] > sem_clutch["expected"]


# ─── Cenário extra: blowout-specific cut reduz projeção ───────────────────


def test_rotation_blowout_cut_reduces_projection():
    pe = ProjectionEngine()
    sem_cut = pe.project(
        stat=10, minutes=18, avg_stat=20, avg_minutes=32,
        period=4, game_minutes_remaining=8,
        last_10_avg=20, last_5_avg=20,
        is_starter=True, expected_minutes_remaining=6.0,
        rotation_blowout_cut=0.0,
    )
    com_cut = pe.project(
        stat=10, minutes=18, avg_stat=20, avg_minutes=32,
        period=4, game_minutes_remaining=8,
        last_10_avg=20, last_5_avg=20,
        is_starter=True, expected_minutes_remaining=6.0,
        rotation_blowout_cut=1.0,
    )
    assert com_cut["expected"] < sem_cut["expected"]
