"""
Analyzer pra encontrar jogos históricos com início similar.

Pra cada jogo recente do jogador, busca o PBP histórico (cacheado 30d
no NbaService) e extrai o stat do Q1 dele. Filtra jogos onde o Q1
bateu com a referência atual (±tolerância). Agrega stats finais.

Custo: até N fetches de PBP no primeiro uso. Subsequent calls servidas
do cache (PersistentCache 30 dias). Esquenta gradual.

Falha silenciosa: amostra insuficiente (<3 similares) → None.
"""

from __future__ import annotations

import logging
import statistics
from typing import Callable, Optional

from src.schemas.live_schemas import (
    SimilarGameSchema,
    SimilarGamesResultSchema,
)
from src.schemas.nba_schemas import GameLogSchema

logger = logging.getLogger(__name__)

# Tipo dos fetchers injetados (mantém testabilidade)
GamelogFetcher = Callable[[int, str], list[GameLogSchema]]
PbpAggregator = Callable[[str], dict[int, dict[int, dict[str, int]]]]


# Stat key na resposta do _aggregate_v3_dataframe → field no schema
STAT_KEY_MAP = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
}


class SimilarGameAnalyzer:
    """
    Stateless quando os fetchers são puros. Os fetchers tipicamente
    têm cache interno (NbaService gamelog 24h, PBP 30d).
    """

    def __init__(
        self,
        gamelog_fetcher: GamelogFetcher,
        pbp_aggregator: PbpAggregator,
    ) -> None:
        self._gamelog = gamelog_fetcher
        self._pbp = pbp_aggregator

    def analyze(
        self,
        *,
        player_id: int,
        season: str,
        current_q1_stat: int,
        stat: str = "points",
        tolerance: int = 2,
        max_games_to_check: int = 30,
        max_returned: int = 10,
    ) -> Optional[SimilarGamesResultSchema]:
        """
        Encontra jogos onde o Q1 do jogador foi similar ao atual.

        Args:
            player_id: NBA player_id
            season: ex "2024-25"
            current_q1_stat: stat atual do Q1 (referência de match)
            stat: "points" | "rebounds" | "assists"
            tolerance: diferença máxima permitida pra match (±N)
            max_games_to_check: limite de PBP fetches
            max_returned: quantos jogos similares retornar no payload

        Returns:
            SimilarGamesResultSchema com agregado + lista, OU None se
            amostra insuficiente (< 3 jogos similares).
        """
        stat_key = STAT_KEY_MAP.get(stat)
        if stat_key is None:
            return None

        # Pega últimos N jogos do player (gamelog é cacheado 24h)
        try:
            logs = self._gamelog(player_id, season)
        except Exception as exc:
            logger.info(
                "similar_games: gamelog fetch falhou pra player %d (%s)",
                player_id, exc,
            )
            return None
        if not logs:
            return None

        # Pega os mais recentes — gamelog vem desc por padrão
        recent = logs[:max_games_to_check]

        # Pra cada jogo, busca PBP e extrai Q1 stat do player
        similar_games: list[SimilarGameSchema] = []
        final_stats: list[int] = []
        season_total_stat = 0
        season_total_games = 0

        for game in recent:
            try:
                pbp = self._pbp(game.game_id)
            except Exception as exc:
                logger.info(
                    "similar_games: PBP fetch falhou pro game %s (%s)",
                    game.game_id, exc,
                )
                continue
            if not pbp:
                continue

            player_periods = pbp.get(player_id)
            if not player_periods:
                # Player não jogou esse jogo
                continue

            q1_stat = int(player_periods.get(1, {}).get(stat_key, 0))

            # Final stat do gamelog (totais consolidados)
            final_stat = _get_stat_from_gamelog(game, stat_key)
            if final_stat is None:
                continue

            season_total_stat += final_stat
            season_total_games += 1

            # Filtro de similaridade: Q1 dentro da tolerância
            if abs(q1_stat - current_q1_stat) > tolerance:
                continue

            try:
                final_min = float(_parse_minutes(game.minutes))
            except Exception:
                final_min = 0.0

            similar_games.append(SimilarGameSchema(
                game_id=game.game_id,
                game_date=game.game_date,
                matchup=game.matchup,
                first_quarter_stat=q1_stat,
                final_stat=final_stat,
                final_minutes=final_min,
            ))
            final_stats.append(final_stat)

        if len(similar_games) < 3:
            # Sample insuficiente — não devolve dado enganoso
            return None

        season_avg = (
            season_total_stat / season_total_games
            if season_total_games > 0 else 0.0
        )
        avg_final = statistics.mean(final_stats)
        median_final = statistics.median(final_stats)
        recovery_factor = (
            avg_final / season_avg if season_avg > 0 else 1.0
        )

        # Ordena similares por data desc (mais recentes primeiro), pega top N
        similar_games.sort(key=lambda g: g.game_date, reverse=True)
        similar_games = similar_games[:max_returned]

        return SimilarGamesResultSchema(
            stat=stat,
            current_first_quarter=current_q1_stat,
            sample_size=len(final_stats),
            games=similar_games,
            avg_final_stat=round(avg_final, 1),
            median_final_stat=round(median_final, 1),
            season_avg=round(season_avg, 1),
            recovery_factor=round(recovery_factor, 2),
        )


def _get_stat_from_gamelog(game: GameLogSchema, stat_key: str) -> Optional[int]:
    """Mapeia stat_key pro field correto do gamelog."""
    if stat_key == "points":
        return game.points
    if stat_key == "rebounds":
        return game.rebounds
    if stat_key == "assists":
        return game.assists
    return None


def _parse_minutes(min_str: str) -> float:
    """Reuso do parser do utils — evita import circular."""
    s = (min_str or "").strip()
    if ":" in s:
        try:
            mm, ss = s.split(":")
            return int(mm) + int(ss) / 60.0
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0
