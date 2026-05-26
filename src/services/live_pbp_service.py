"""
LivePbpService — busca play-by-play do `cdn.nba.com` (mesma fonte do
boxscore live, gratuita, não usa ScraperAPI) e expõe stats agregadas
por jogador POR período.

Cache TTL alinhado com boxscore (5s) — mesma cadência de poll do front.
Se o fetch falhar (cdn temporariamente offline, jogo não tem PBP), o
service retorna dict vazio em vez de levantar exception. O caller
decide o fallback (mostrar "—" no front).

Uso:
    pbp_service = LivePbpService()
    per_period = pbp_service.get_per_period_stats(game_id)
    # → {player_id: {period: {points, assists, rebounds, ...}}}
"""
from __future__ import annotations

import logging
from typing import Optional

from nba_api.live.nba.endpoints import playbyplay

from src.utils.cache import SimpleCache
from src.utils.pbp_aggregator import (
    aggregate_per_period,
    aggregate_per_period_with_court_time,
)

logger = logging.getLogger(__name__)

PBP_TTL = 5  # segundos — alinha com boxscore (front bate a cada 5s)


class LivePbpService:
    def __init__(self) -> None:
        # Cache em memória local (não persiste em disco — PBP é volátil
        # por natureza; restart do container basta limpar).
        self._cache = SimpleCache()

    def get_per_period_stats(
        self,
        game_id: str,
    ) -> dict[int, dict[int, dict[str, int]]]:
        """
        Retorna o map {player_id: {period: counters}} pro game_id.
        Cacheado por PBP_TTL. Em caso de falha, retorna {} (graceful).
        """
        cache_key = f"pbp_per_period:{game_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            actions = self._fetch_actions(game_id)
        except Exception as exc:
            logger.warning("LivePbp: falha ao buscar PBP %s: %s", game_id, exc)
            # Cacheia o vazio também por TTL curto pra não martelar a API
            # quando ela está fora.
            self._cache.set(cache_key, {}, ttl=PBP_TTL)
            return {}

        result = aggregate_per_period(actions)
        # Convert defaultdicts pra dicts puros pra evitar comportamento
        # surpresa em caller que faz `result[id]` (criava entrada vazia).
        clean: dict[int, dict[int, dict[str, int]]] = {
            pid: dict(periods) for pid, periods in result.items()
        }
        self._cache.set(cache_key, clean, ttl=PBP_TTL)
        return clean

    def get_per_period_stats_with_court_time(
        self,
        game_id: str,
        q1_starters: set[int],
        live_period: int | None = None,
        live_clock_minutes: float | None = None,
    ) -> dict[int, dict[int, dict]]:
        """
        Versão estendida do `get_per_period_stats` que TAMBÉM rastreia
        minutos jogados + intervalos em quadra via state machine de subs.

        Args:
            game_id: id do jogo (ex: "0042500224")
            q1_starters: set de player_ids que começaram o Q1 (do boxscore)
            live_period: período atual do jogo (pra fechar intervalos
                         abertos no clock correto em jogos ao vivo)
            live_clock_minutes: minutos restantes do quarter atual

        Returns:
            {player_id: {period: {points, ..., minutes_played, intervals}}}
            Cacheado com TTL diferente (chave separada) pra não conflitar
            com o get_per_period_stats clássico.

        Em caso de falha: retorna {} (graceful).
        """
        # Cache key inclui starters + live state pra não cachear estado
        # stale (o clock muda toda hora).
        starters_hash = ",".join(str(s) for s in sorted(q1_starters))
        clock_hash = (
            f"{live_period or 0}:{int((live_clock_minutes or 0) * 10)}"
            if live_period is not None else "static"
        )
        cache_key = f"pbp_per_period_ct:{game_id}:{starters_hash}:{clock_hash}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            actions = self._fetch_actions(game_id)
        except Exception as exc:
            logger.warning(
                "LivePbp: falha ao buscar PBP %s pra court time: %s",
                game_id, exc,
            )
            self._cache.set(cache_key, {}, ttl=PBP_TTL)
            return {}

        result = aggregate_per_period_with_court_time(
            actions, q1_starters,
            live_period=live_period,
            live_clock_minutes=live_clock_minutes,
        )
        clean: dict[int, dict[int, dict]] = {
            pid: dict(periods) for pid, periods in result.items()
        }
        self._cache.set(cache_key, clean, ttl=PBP_TTL)
        return clean

    def _fetch_actions(self, game_id: str) -> list[dict]:
        """Faz o request de PBP e retorna lista de actions. Pode levantar."""
        pbp = playbyplay.PlayByPlay(game_id=game_id, timeout=10)
        data = pbp.get_dict()
        # Estrutura padrão: {meta: ..., game: {gameId, actions: [...]}}
        actions = (data.get("game") or {}).get("actions") or []
        if not isinstance(actions, list):
            return []
        return actions
