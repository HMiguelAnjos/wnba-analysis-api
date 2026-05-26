"""
MatchupProvider — DRtg e Pace por time NBA, cache 24h.

Fonte: NBA stats API via `nba_api.LeagueDashTeamStats(measure_type='Advanced')`.
Esse endpoint pode ser bloqueado (stats.nba.com filtra IPs, especialmente
de DC). Quando falha, o provider retorna `MatchupContext.neutral()` —
sistema continua funcionando, só sem ajuste de matchup.

Métricas usadas:
  - DRtg (Defensive Rating): pts permitidos por 100 possessions.
    Liga média ~110-115. Bom defensor: <108. Ruim: >120.
  - Pace: possessions por 48 minutos.
    Liga média ~100. Rápido: 105+. Devagar: 95-.
  - ORtg (Offensive Rating): exposto também pra possíveis usos futuros.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.utils.cache import PersistentCache

logger = logging.getLogger(__name__)

# League averages — atualizar 1x por temporada se quisermos precisão maior.
# Valores típicos modernos NBA (2023-2026):
LEAGUE_AVG_DRTG = 113.0
LEAGUE_AVG_PACE = 100.0
LEAGUE_AVG_ORTG = 113.0

# TTL de 24h: stats agregados de temporada não mudam intra-dia.
MATCHUP_CACHE_TTL = 86_400


@dataclass
class MatchupContext:
    """Métricas defensivas e de pace de um time, mais flag de fallback."""
    team_tricode: str
    drtg: float
    pace: float
    ortg: float
    is_neutral: bool = False  # True quando origem é fallback

    @classmethod
    def neutral(cls, team_tricode: str = "") -> "MatchupContext":
        """Retorna contexto neutro — usado como fallback ou pré-temporada."""
        return cls(
            team_tricode=team_tricode,
            drtg=LEAGUE_AVG_DRTG,
            pace=LEAGUE_AVG_PACE,
            ortg=LEAGUE_AVG_ORTG,
            is_neutral=True,
        )

    @property
    def drtg_factor(self) -> float:
        """
        Fator multiplicativo aplicado à linha quando ESTE time é o adversário.
        > 1.0  → defesa pior que média (linha do jogador adversário sobe)
        < 1.0  → defesa melhor (linha desce)
        """
        # Clamp em ±15% (cap de segurança contra outliers).
        ratio = self.drtg / LEAGUE_AVG_DRTG
        return max(0.85, min(1.15, ratio))

    @property
    def pace_factor(self) -> float:
        """Fator multiplicativo do pace deste time (vs média)."""
        ratio = self.pace / LEAGUE_AVG_PACE
        return max(0.90, min(1.10, ratio))


class MatchupProvider:
    """
    Cache 24h por tricode. Lookup lazy: 1ª chamada busca da NBA API,
    chamadas seguintes vêm do cache. Falha = fallback neutro silencioso.
    """

    _CACHE_KEY_PREFIX = "matchup:"

    def __init__(self, cache: Optional[PersistentCache] = None) -> None:
        self._cache = cache or PersistentCache()
        # Marca se já tentamos popular o cache nesta sessão pra evitar
        # tempestade de fetches paralelos. Se um fetch global falha,
        # marcamos pra não tentar de novo no mesmo deploy.
        self._fetch_attempted = False

    def get(self, team_tricode: str, season: Optional[str] = None) -> MatchupContext:
        """
        Retorna `MatchupContext` pro time. Nunca levanta exceção.

        Args:
          team_tricode: ex "LAL", "BOS"
          season: "2025-26" — se None, infere da data atual
        """
        if not team_tricode:
            return MatchupContext.neutral()

        season = season or self._current_season()
        cache_key = f"{self._CACHE_KEY_PREFIX}{season}:{team_tricode.upper()}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Tentar fetch global e popular tudo
        if not self._fetch_attempted:
            self._fetch_attempted = True
            self._populate_all(season)
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        # Fallback neutro — também cacheado pra evitar refetch
        neutral = MatchupContext.neutral(team_tricode.upper())
        self._cache.set(cache_key, neutral, MATCHUP_CACHE_TTL // 4)  # TTL menor
        return neutral

    def get_pair(
        self,
        home_tricode: str,
        away_tricode: str,
        season: Optional[str] = None,
    ) -> tuple[MatchupContext, MatchupContext]:
        """Atalho pra buscar os dois times de um confronto."""
        return self.get(home_tricode, season), self.get(away_tricode, season)

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _current_season() -> str:
        from datetime import datetime
        now = datetime.now()
        if now.month >= 10:
            return f"{now.year}-{str(now.year + 1)[-2:]}"
        return f"{now.year - 1}-{str(now.year)[-2:]}"

    def _populate_all(self, season: str) -> None:
        """
        Fetch único da liga inteira. Mais barato que 30 chamadas.
        Falha silenciosa: se rolar exceção, deixa o cache vazio e o
        caller cai pro fallback neutro.
        """
        try:
            from nba_api.stats.endpoints import leaguedashteamstats
        except ImportError:
            logger.info("MatchupProvider: nba_api não instalado, fallback neutro")
            return

        try:
            resp = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                timeout=15,
            )
            data = resp.get_dict()
        except Exception as exc:
            logger.info(
                "MatchupProvider: fetch falhou (%s) — fallback neutro pra todos os times",
                exc,
            )
            return

        try:
            result_set = data["resultSets"][0]
            headers = result_set["headers"]
            rows = result_set["rowSet"]
            idx_abbr = headers.index("TEAM_NAME")    # nome completo
            # NBA API tem TEAM_ID + TEAM_NAME mas nem sempre TEAM_ABBREVIATION
            # nesse endpoint advanced. Vamos tentar ambos.
            idx_drtg = headers.index("DEF_RATING") if "DEF_RATING" in headers else None
            idx_pace = headers.index("PACE") if "PACE" in headers else None
            idx_ortg = headers.index("OFF_RATING") if "OFF_RATING" in headers else None
            if idx_drtg is None or idx_pace is None:
                logger.info("MatchupProvider: headers inesperados %s", headers)
                return
        except (KeyError, IndexError, ValueError) as exc:
            logger.info("MatchupProvider: parse falhou (%s)", exc)
            return

        from src.utils.converters import team_name_to_tricode
        cached_count = 0
        for row in rows:
            try:
                team_name = row[idx_abbr]
                tricode = team_name_to_tricode(team_name)
                if not tricode:
                    continue
                ctx = MatchupContext(
                    team_tricode=tricode,
                    drtg=float(row[idx_drtg]),
                    pace=float(row[idx_pace]),
                    ortg=float(row[idx_ortg]) if idx_ortg is not None else LEAGUE_AVG_ORTG,
                    is_neutral=False,
                )
                cache_key = f"{self._CACHE_KEY_PREFIX}{season}:{tricode}"
                self._cache.set(cache_key, ctx, MATCHUP_CACHE_TTL)
                cached_count += 1
            except (ValueError, TypeError) as exc:
                logger.debug("MatchupProvider: skip row (%s)", exc)
                continue

        logger.info("MatchupProvider: cacheou %d times pra temporada %s", cached_count, season)
