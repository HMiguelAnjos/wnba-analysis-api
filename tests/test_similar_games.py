"""
Testes do SimilarGameAnalyzer.

Mocka gamelog + PBP aggregator pra simular jogos históricos com Q1
variado. Verifica:
  - Filtragem por tolerância
  - Agregação (avg, median, recovery_factor)
  - Insufficient sample → None
  - Player ausente no PBP é pulado
  - Stats não suportados retornam None
"""

from __future__ import annotations

from src.schemas.nba_schemas import GameLogSchema
from src.services.similar_games import SimilarGameAnalyzer


def _log(game_id: str, date: str, pts: int, reb: int = 5, ast: int = 3, minutes: str = "28:00") -> GameLogSchema:
    """Helper pra criar gamelog mínimo."""
    return GameLogSchema(
        game_id=game_id,
        game_date=date,
        matchup=f"SAS vs OPP",
        minutes=minutes,
        points=pts,
        rebounds=reb,
        assists=ast,
        field_goals_made=0,
        field_goals_attempted=0,
        three_pointers_made=0,
        three_pointers_attempted=0,
        free_throws_made=0,
        free_throws_attempted=0,
    )


def _pbp_factory(pbp_by_game: dict[str, dict[int, dict[int, dict[str, int]]]]):
    """Fake do `aggregate_historical_pbp_per_period`."""
    def _fetcher(game_id: str) -> dict:
        return pbp_by_game.get(game_id, {})
    return _fetcher


def test_returns_none_when_insufficient_similar_games():
    """Menos de 3 jogos similares → None (amostra ruim)."""
    logs = [
        _log("g1", "Mar 01, 2026", pts=15),
        _log("g2", "Mar 03, 2026", pts=18),
        _log("g3", "Mar 05, 2026", pts=20),
    ]
    # Pbp: todos jogos Q1 = 8 pts (longe do current_q1=2)
    pbp = {
        "g1": {100: {1: {"points": 8, "rebounds": 0, "assists": 0}}},
        "g2": {100: {1: {"points": 9, "rebounds": 0, "assists": 0}}},
        "g3": {100: {1: {"points": 10, "rebounds": 0, "assists": 0}}},
    }
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: logs,
        pbp_aggregator=_pbp_factory(pbp),
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,   # nenhum bate em ±2 (tolerance default)
    )
    assert result is None


def test_finds_similar_games_and_aggregates():
    """Casos similares (Q1 ≤ 3 pts) agregados corretamente."""
    logs = [
        _log("g1", "Mar 01, 2026", pts=14),   # Q1=2 → SIMILAR, final 14
        _log("g2", "Mar 03, 2026", pts=8),    # Q1=10 → NÃO similar
        _log("g3", "Mar 05, 2026", pts=18),   # Q1=3 → SIMILAR, final 18
        _log("g4", "Mar 07, 2026", pts=12),   # Q1=4 → NÃO similar (>+2 tolerance)
        _log("g5", "Mar 09, 2026", pts=11),   # Q1=1 → SIMILAR, final 11
        _log("g6", "Mar 11, 2026", pts=22),   # Q1=2 → SIMILAR, final 22
    ]
    pbp = {
        "g1": {100: {1: {"points": 2,  "rebounds": 0, "assists": 0}}},
        "g2": {100: {1: {"points": 10, "rebounds": 0, "assists": 0}}},
        "g3": {100: {1: {"points": 3,  "rebounds": 0, "assists": 0}}},
        "g4": {100: {1: {"points": 5,  "rebounds": 0, "assists": 0}}},
        "g5": {100: {1: {"points": 1,  "rebounds": 0, "assists": 0}}},
        "g6": {100: {1: {"points": 2,  "rebounds": 0, "assists": 0}}},
    }
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: logs,
        pbp_aggregator=_pbp_factory(pbp),
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,
        tolerance=2,
    )
    assert result is not None
    # 4 jogos similares: g1, g3, g5, g6 (Q1 = 2/3/1/2, todos ≤ 2±2)
    assert result.sample_size == 4
    # Finais: 14, 18, 11, 22 → avg = 16.25, median = 16.0
    assert result.avg_final_stat == 16.2 or result.avg_final_stat == 16.3
    assert result.median_final_stat == 16.0
    # Season avg: todos os 6 jogos = (14+8+18+12+11+22)/6 = 14.17
    assert 13.0 <= result.season_avg <= 15.0
    # Recovery: 16.25 / 14.17 ≈ 1.15
    assert result.recovery_factor > 1.0


def test_returns_max_10_games_in_payload():
    """Apenas top 10 mais recentes retornados (ordenados desc por data)."""
    logs = [
        _log(f"g{i}", f"Mar {i:02d}, 2026", pts=15) for i in range(1, 16)
    ]
    # Todos com Q1 = 2 (todos similares)
    pbp = {
        f"g{i}": {100: {1: {"points": 2, "rebounds": 0, "assists": 0}}}
        for i in range(1, 16)
    }
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: logs,
        pbp_aggregator=_pbp_factory(pbp),
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,
    )
    assert result is not None
    # 15 similares no total, mas só top 10 retornados
    assert result.sample_size == 15
    assert len(result.games) == 10


def test_player_absent_from_pbp_is_skipped():
    """Jogos onde o player não aparece no PBP (inactive) são pulados."""
    logs = [
        _log("g1", "Mar 01, 2026", pts=14),
        _log("g2", "Mar 03, 2026", pts=8),
        _log("g3", "Mar 05, 2026", pts=18),
        _log("g4", "Mar 07, 2026", pts=12),
    ]
    pbp = {
        "g1": {100: {1: {"points": 2, "rebounds": 0, "assists": 0}}},
        "g2": {200: {1: {"points": 5, "rebounds": 0, "assists": 0}}},  # outro player
        "g3": {100: {1: {"points": 3, "rebounds": 0, "assists": 0}}},
        "g4": {100: {1: {"points": 2, "rebounds": 0, "assists": 0}}},
    }
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: logs,
        pbp_aggregator=_pbp_factory(pbp),
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,
    )
    assert result is not None
    # 3 similares (g2 pulado pq player 100 não tá no PBP)
    assert result.sample_size == 3


def test_pbp_fetch_failure_continues_with_other_games():
    """Falha de PBP num jogo não derruba os outros."""
    logs = [
        _log("g1", "Mar 01, 2026", pts=14),
        _log("broken", "Mar 03, 2026", pts=8),
        _log("g3", "Mar 05, 2026", pts=18),
        _log("g4", "Mar 07, 2026", pts=12),
    ]
    pbp = {
        "g1": {100: {1: {"points": 2, "rebounds": 0, "assists": 0}}},
        "g3": {100: {1: {"points": 3, "rebounds": 0, "assists": 0}}},
        "g4": {100: {1: {"points": 2, "rebounds": 0, "assists": 0}}},
    }
    def failing_pbp(gid):
        if gid == "broken":
            raise RuntimeError("forced failure")
        return pbp.get(gid, {})
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: logs,
        pbp_aggregator=failing_pbp,
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,
    )
    assert result is not None
    assert result.sample_size == 3


def test_unsupported_stat_returns_none():
    """Stat fora de {points, rebounds, assists} → None."""
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: [],
        pbp_aggregator=lambda gid: {},
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,
        stat="steals",
    )
    assert result is None


def test_recovery_factor_below_1_when_similar_games_underperform():
    """
    Cenário: jogador tem season avg 16 pts. Em jogos com Q1 baixo, ele
    típicamente NÃO recupera (final ≈ 8 pts). Recovery factor < 1.
    """
    logs = [
        _log("g1", "Mar 01, 2026", pts=8),    # season normal
        _log("g2", "Mar 03, 2026", pts=10),
        _log("g3", "Mar 05, 2026", pts=20),
        _log("g4", "Mar 07, 2026", pts=24),
        _log("g5", "Mar 09, 2026", pts=18),
    ]
    pbp = {
        "g1": {100: {1: {"points": 2, "rebounds": 0, "assists": 0}}},  # similar
        "g2": {100: {1: {"points": 3, "rebounds": 0, "assists": 0}}},  # similar
        "g3": {100: {1: {"points": 8, "rebounds": 0, "assists": 0}}},  # NOT similar
        "g4": {100: {1: {"points": 9, "rebounds": 0, "assists": 0}}},  # NOT similar
        "g5": {100: {1: {"points": 2, "rebounds": 0, "assists": 0}}},  # similar
    }
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: logs,
        pbp_aggregator=_pbp_factory(pbp),
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,
    )
    assert result is not None
    # 3 similares: finais 8, 10, 18 → avg 12.0
    # Season: (8+10+20+24+18)/5 = 16.0
    # Recovery: 12/16 = 0.75 → cara NÃO se recupera em inícios ruins
    assert result.recovery_factor < 1.0
    assert result.recovery_factor > 0.5


def test_empty_gamelog_returns_none():
    """Sem histórico → não dá pra analisar."""
    analyzer = SimilarGameAnalyzer(
        gamelog_fetcher=lambda pid, season: [],
        pbp_aggregator=lambda gid: {},
    )
    result = analyzer.analyze(
        player_id=100, season="2025-26",
        current_q1_stat=2,
    )
    assert result is None
