"""
Testes do RotationProvider (Fase 2 V2 — nbarotations.info real).

Cobertura:
  - Fallback uniforme quando client retorna None
  - Profile real construído quando HTML válido vem do client
  - expected_minutes_remaining usa minute_probabilities quando disponível
  - Cache reusa entre chamadas (com mock do client contando hits)
  - Parser de HTML válido / inválido / sem displayPlayer
  - aggregate_recent_games filtra temporada e exige sample mínimo
"""

from __future__ import annotations

import json
from typing import Optional

import pytest

from src.services.rotation import RotationProfile, RotationProvider
from src.services.rotation.nbarotations_parser import (
    aggregate_recent_games,
    parse_player_html,
    GameRotationEntry,
)


# ─── Mock client pra evitar rede ──────────────────────────────────────────


class _MockClient:
    """Cliente fake — guarda fixture pré-definida e conta calls."""
    def __init__(self, html: Optional[str] = None) -> None:
        self.html = html
        self.calls: list[int] = []

    def fetch_player_html(self, player_id: int) -> Optional[str]:
        self.calls.append(player_id)
        return self.html


def _make_html(games: list[dict]) -> str:
    """Constrói HTML mínimo com displayPlayer embutido."""
    return f"<html><body><script>displayPlayer({json.dumps(games)});</script></body></html>"


def _make_game(date: str, histogram: list[float]) -> dict:
    return {
        "date": date,
        "gamecode": "0042500001",
        "gamedate": date,
        "histogram": histogram,
        "name": "Test Player",
        "opponent": "OPP",
        "teamcode": 1610612747,
        "year": 2025,
        "yearteam": "2025LAL",
    }


# ─── Parser ──────────────────────────────────────────────────────────────


def test_parse_player_html_extracts_games():
    games = [_make_game("2026-01-01", [1.0] * 48)]
    entries = parse_player_html(_make_html(games))
    assert len(entries) == 1
    assert entries[0].gamedate == "2026-01-01"
    assert len(entries[0].histogram) == 48


def test_parse_player_html_empty_when_no_marker():
    assert parse_player_html("<html></html>") == []


def test_parse_player_html_empty_when_invalid_json():
    bad = "<script>displayPlayer([{badjson]);</script>"
    assert parse_player_html(bad) == []


def test_parse_player_html_handles_nested_brackets():
    """O array tem objects com `histogram` que é também array — bracket-balance correto."""
    games = [_make_game("2026-01-01", [0.5, 1.0, 0.0] * 16)]
    entries = parse_player_html(_make_html(games))
    assert len(entries) == 1
    assert len(entries[0].histogram) == 48


# ─── aggregate_recent_games ──────────────────────────────────────────────


def test_aggregate_filters_by_season():
    games = [
        GameRotationEntry("2024-12-01", "g1", "OPP", [1.0] * 48),  # temporada antiga
        GameRotationEntry("2025-11-01", "g2", "OPP", [0.5] * 48),
        GameRotationEntry("2025-12-01", "g3", "OPP", [0.5] * 48),
        GameRotationEntry("2026-01-01", "g4", "OPP", [0.5] * 48),
    ]
    agg = aggregate_recent_games(games, season_start="2025-10-01", last_n=10)
    assert agg is not None
    assert agg["sample_games"] == 3   # apenas 2025-26
    # Histograma médio = 0.5 em cada minuto → 24 min total
    assert abs(agg["total_minutes_avg"] - 24.0) < 0.01


def test_aggregate_returns_none_when_sample_too_small():
    games = [
        GameRotationEntry("2026-01-01", "g1", "OPP", [1.0] * 48),
        GameRotationEntry("2026-01-02", "g2", "OPP", [1.0] * 48),
    ]
    # Apenas 2 jogos da temporada — abaixo do mínimo de 3
    assert aggregate_recent_games(games, season_start="2025-10-01") is None


def test_aggregate_takes_only_last_n():
    games = [
        GameRotationEntry(f"2026-01-{d:02d}", "g", "OPP", [1.0 if i == d else 0.0 for i in range(48)])
        for d in range(1, 16)
    ]
    agg = aggregate_recent_games(games, season_start="2025-10-01", last_n=5)
    assert agg is not None
    assert agg["sample_games"] == 5  # apenas os 5 mais recentes


# ─── Provider — fallback path ────────────────────────────────────────────


def test_provider_fallback_when_client_returns_none():
    p = RotationProvider(client=_MockClient(html=None))
    profile = p.get_profile(player_id=1234, season_minutes=32.0)
    assert profile.is_fallback is True
    assert profile.total_minutes == 32.0
    assert profile.minute_probabilities == []


def test_provider_fallback_when_no_season_data():
    p = RotationProvider(client=_MockClient(html=None))
    profile = p.get_profile(player_id=999, season_minutes=0)
    assert profile.is_fallback is True
    assert profile.total_minutes == 8.0   # bench default


def test_provider_uses_real_data_when_available():
    games = [_make_game(f"2026-01-{i:02d}", [1.0] * 24 + [0.0] * 24) for i in range(1, 11)]
    p = RotationProvider(client=_MockClient(html=_make_html(games)))
    profile = p.get_profile(player_id=2544, season_minutes=35.0)
    assert profile.is_fallback is False
    assert profile.sample_games == 10
    assert len(profile.minute_probabilities) == 48
    # Primeira metade = 1.0, segunda = 0.0
    assert profile.minute_probabilities[0] == 1.0
    assert profile.minute_probabilities[24] == 0.0


def test_provider_caches_real_profile(tmp_path):
    """Cache fresh por teste pra evitar contaminação entre tests."""
    from src.utils.cache import PersistentCache
    cache = PersistentCache(path=str(tmp_path / "cache.json"))
    games = [_make_game(f"2026-01-{i:02d}", [1.0] * 48) for i in range(1, 6)]
    client = _MockClient(html=_make_html(games))
    p = RotationProvider(client=client, cache=cache)
    p.get_profile(player_id=1, season_minutes=32)
    p.get_profile(player_id=1, season_minutes=32)
    p.get_profile(player_id=1, season_minutes=32)
    # Deve ter chamado o client apenas 1x
    assert len(client.calls) == 1


# ─── Provider — expected_minutes_remaining ────────────────────────────────


def test_expected_remaining_uses_real_profile_when_available():
    """
    Profile sintético: jogador joga só Q1 (12 min) e nada depois.
    No início do Q1, esperado = 12. No início do Q2, esperado = 0.
    """
    profile = RotationProfile(
        player_id=1,
        total_minutes=12.0,
        minute_probabilities=[1.0] * 12 + [0.0] * 36,
        sample_games=10,
        is_fallback=False,
    )
    p = RotationProvider(client=_MockClient(html=None))

    # Q1 start: rotation diz 12, naive=48 × (12/48)=12 → blended ≈ 12.
    rem_q1 = p.expected_minutes_remaining(profile, period=1, clock_minutes_remaining=12)
    assert abs(rem_q1 - 12.0) < 0.5

    # Q2 start: rotation diz 0; naive = 36 × (12/48) = 9 → blended = 0.35×9 = 3.15.
    # 65/35 blend (mai/2026): rotation tratado como sugestão, não regra.
    rem_q2 = p.expected_minutes_remaining(profile, period=2, clock_minutes_remaining=12)
    assert 1.0 < rem_q2 < 5.0


def test_expected_remaining_blowout_reduces():
    profile = RotationProfile(
        player_id=1,
        total_minutes=24.0,
        minute_probabilities=[1.0] * 24 + [0.0] * 24,
        sample_games=10,
        is_fallback=False,
    )
    p = RotationProvider(client=_MockClient(html=None))
    no_blow = p.expected_minutes_remaining(profile, period=1, clock_minutes_remaining=12, blowout_severity=0)
    blowout = p.expected_minutes_remaining(profile, period=1, clock_minutes_remaining=12, blowout_severity=1.0)
    assert blowout < no_blow * 0.8


def test_expected_remaining_falls_back_in_overtime():
    """OT (period 5+) sempre usa fallback, ignora minute_probabilities (que só cobre 48 min)."""
    profile = RotationProfile(
        player_id=1,
        total_minutes=36.0,
        minute_probabilities=[1.0] * 48,
        sample_games=10,
        is_fallback=False,
    )
    p = RotationProvider(client=_MockClient(html=None))
    rem = p.expected_minutes_remaining(profile, period=5, clock_minutes_remaining=5, minutes_already_played=36)
    # Profile já satisfeito — fallback retorna 0
    assert rem == 0.0


# ─── Cache serialization (PersistentCache JSON) ──────────────────────────


def test_profile_roundtrip_via_dict():
    original = RotationProfile(
        player_id=42,
        total_minutes=33.5,
        minute_probabilities=[0.5] * 48,
        sample_games=10,
        is_fallback=False,
    )
    raw = original.to_cache_dict()
    # Garantia de JSON-serializable
    json.dumps(raw)
    restored = RotationProfile.from_cache_dict(raw)
    assert restored == original
