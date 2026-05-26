"""
RotationProvider — minutos esperados restantes por jogador no jogo.

Fonte primária (V2, mai/2026): nbarotations.info — perfis observados de
rotação minuto-a-minuto, agregados sobre os últimos N jogos da temporada
atual. Validado vivo: HTTP 200, sem Cloudflare, player_id é NBA-nativo.

Fallback (sempre disponível): distribuição uniforme baseada apenas em
`season_minutes` do jogador. Usado quando:
  - nbarotations cair / 404 pro player
  - sample < 3 jogos na temporada atual
  - parser falhar

API estável: caller só chama `provider.expected_minutes_remaining(...)`
e recebe um número. Internamente decide se usa profile real ou fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

# Type alias: dado um game_id, devolve a margem absoluta do placar final
# ou None quando indisponível. Tipicamente NbaService.get_final_margin.
FinalMarginFetcher = Callable[[str], Optional[int]]

from src.services.rotation.nbarotations_client import NBARotationsClient
from src.services.rotation.nbarotations_parser import (
    aggregate_recent_games,
    parse_player_html,
)
from src.services.rotation.production_by_period import (
    PbpAggregateFn,
    compute_production_by_period,
    production_to_dict,
)
from src.services.rotation.rotation_derivation import (
    derive_clutch_and_blowout,
    derive_quarter_patterns,
)
from src.utils.cache import PersistentCache

logger = logging.getLogger(__name__)

# TTL menor pra fallbacks (negative cache) — caso jogador hoje não tem
# perfil mas semana que vem volta pra rotação.
ROTATION_FALLBACK_TTL = 6 * 3600


def _rotation_cache_ttl() -> int:
    """Late-bound — lê NBA_ROTATION_CACHE_TTL_SECONDS na hora do uso."""
    from src.config import NBA_ROTATION_CACHE_TTL_SECONDS
    return NBA_ROTATION_CACHE_TTL_SECONDS


@dataclass
class RotationProfile:
    """
    Padrão de minutos jogados por um jogador (perfil RICO).

    Mínimo:
      `minute_probabilities`: 48 floats, prob de estar em quadra cada minuto.
      Vazio = fallback (caller usa distribuição uniforme).

    Derivados (presentes apenas quando is_fallback=False):
      `quarter_patterns`: rest/play windows + usual start/end por quarter.
      `clutch_usage`: dict serializado (usually_closes_games, etc.).
      `blowout_risk`: dict serializado (return_probability, minutes_lost).

    Pendente:
      `production_by_period`: pts/min, ast/min, reb/min por quarter.
      Não populado pela camada de rotação (precisa PBP histórico).
    """
    player_id: int
    total_minutes: float
    minute_probabilities: list[float]   # vazio = fallback
    sample_games: int = 0
    is_fallback: bool = True
    # Derivados (vazios quando is_fallback=True)
    quarter_patterns: list = field(default_factory=list)
    clutch_usage: Optional[dict] = None
    blowout_risk: Optional[dict] = None
    production_by_period: Optional[list] = None

    def to_cache_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "total_minutes": self.total_minutes,
            "minute_probabilities": self.minute_probabilities,
            "sample_games": self.sample_games,
            "is_fallback": self.is_fallback,
            "quarter_patterns": [
                _quarter_pattern_to_dict(qp) for qp in self.quarter_patterns
            ],
            "clutch_usage": self.clutch_usage,
            "blowout_risk": self.blowout_risk,
            "production_by_period": self.production_by_period,
        }

    @classmethod
    def from_cache_dict(cls, raw: dict) -> "RotationProfile":
        return cls(
            player_id=raw["player_id"],
            total_minutes=raw["total_minutes"],
            minute_probabilities=raw.get("minute_probabilities", []),
            sample_games=raw.get("sample_games", 0),
            is_fallback=raw.get("is_fallback", True),
            quarter_patterns=[
                _quarter_pattern_from_dict(d)
                for d in raw.get("quarter_patterns") or []
            ],
            clutch_usage=raw.get("clutch_usage"),
            blowout_risk=raw.get("blowout_risk"),
            production_by_period=raw.get("production_by_period"),
        )


def _quarter_pattern_to_dict(qp) -> dict:
    return {
        "quarter": qp.quarter,
        "expected_minutes": qp.expected_minutes,
        "usual_start_minute": qp.usual_start_minute,
        "usual_end_minute": qp.usual_end_minute,
        "rest_windows": [
            {"from_minute": w.from_minute, "to_minute": w.to_minute,
             "probability": w.probability}
            for w in qp.rest_windows
        ],
        "play_windows": [
            {"from_minute": w.from_minute, "to_minute": w.to_minute,
             "probability": w.probability}
            for w in qp.play_windows
        ],
    }


def _quarter_pattern_from_dict(d: dict):
    from src.services.rotation.rotation_derivation import QuarterPattern, TimeWindow
    return QuarterPattern(
        quarter=d["quarter"],
        expected_minutes=d["expected_minutes"],
        usual_start_minute=d.get("usual_start_minute"),
        usual_end_minute=d.get("usual_end_minute"),
        rest_windows=[
            TimeWindow(**w) for w in d.get("rest_windows") or []
        ],
        play_windows=[
            TimeWindow(**w) for w in d.get("play_windows") or []
        ],
    )


def _current_season_start() -> str:
    """ISO date do começo da temporada atual NBA (1º de Outubro)."""
    now = datetime.now()
    year = now.year if now.month >= 10 else now.year - 1
    return f"{year}-10-01"


class RotationProvider:
    """
    Stateful: carrega perfis sob demanda do nbarotations.info, cacheia 7d.
    Falha silenciosa em qualquer ponto da cadeia → fallback uniforme.
    """

    _CACHE_KEY_PREFIX = "rotation:"

    def __init__(
        self,
        client: Optional[NBARotationsClient] = None,
        cache: Optional[PersistentCache] = None,
        pbp_fetcher: Optional[PbpAggregateFn] = None,
        final_margin_fetcher: Optional[FinalMarginFetcher] = None,
    ) -> None:
        self._client = client or NBARotationsClient()
        self._cache = cache or PersistentCache()
        # Optional fetcher pra agregar PBP histórico por período. Quando
        # None, production_by_period fica vazio. Tipicamente é o método
        # NbaService.aggregate_historical_pbp_per_period bound.
        self._pbp_fetcher = pbp_fetcher
        # Optional fetcher pra placar final por game_id. Quando fornecido,
        # `derive_clutch_and_blowout` usa margem real em vez de
        # self-clustering pelo Q4 do jogador. Tipicamente
        # NbaService.get_final_margin bound.
        self._final_margin_fetcher = final_margin_fetcher

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def get_profile(
        self,
        player_id: int,
        season_minutes: float,
    ) -> RotationProfile:
        """
        Retorna profile do jogador. Tenta nbarotations.info; cai no
        fallback uniforme se a fonte não tem dado suficiente.
        """
        if player_id <= 0:
            return self._build_fallback(player_id, season_minutes)

        cache_key = f"{self._CACHE_KEY_PREFIX}{player_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            # Reconstitui dataclass se cache devolveu dict (persistido em disco)
            if isinstance(cached, dict):
                return RotationProfile.from_cache_dict(cached)
            return cached

        # Tenta fetch
        profile = self._fetch_real_profile(player_id, season_minutes)
        if profile is not None:
            self._cache.set(cache_key, profile.to_cache_dict(), _rotation_cache_ttl())
            return profile

        # Fallback — cacheado com TTL menor
        fallback = self._build_fallback(player_id, season_minutes)
        self._cache.set(cache_key, fallback.to_cache_dict(), ROTATION_FALLBACK_TTL)
        return fallback

    def expected_minutes_remaining(
        self,
        profile: RotationProfile,
        period: int,
        clock_minutes_remaining: float,
        minutes_already_played: float = 0.0,
        blowout_severity: float = 0.0,
    ) -> float:
        """
        Estima quantos minutos a mais o jogador deve jogar a partir do
        ponto atual. Usa profile real quando disponível; fallback uniforme
        caso contrário.

        Args:
          period: 1-4 (5+ = OT — usa fallback automático)
          clock_minutes_remaining: minutos no relógio do quarter atual
          minutes_already_played: minutos que o jogador já jogou
          blowout_severity: 0..1, reduz minutos esperados
        """
        if (
            profile.is_fallback
            or not profile.minute_probabilities
            or period > 4
        ):
            return self._fallback_remaining(
                total=profile.total_minutes,
                period=period,
                clock_minutes_remaining=clock_minutes_remaining,
                minutes_already_played=minutes_already_played,
                blowout_severity=blowout_severity,
            )

        # Usa minute_probabilities pra estimativa precisa.
        # Index do minuto atual: cada quarter tem 12 min.
        # Q1 12:00 restantes = minuto 0 do jogo (não começou).
        # Q3 8:00 restantes = (3-1)*12 + (12-8) = 28 (jogo está no 28º minuto).
        elapsed_in_quarter = max(0.0, 12.0 - clock_minutes_remaining)
        current_minute_idx = int(round((period - 1) * 12 + elapsed_in_quarter))
        current_minute_idx = max(
            0, min(current_minute_idx, len(profile.minute_probabilities))
        )

        # Soma probabilidades dos minutos restantes = expected minutes
        # do perfil real do jogador (nbarotations).
        rotation_remaining = sum(
            profile.minute_probabilities[current_minute_idx:]
        )

        # Mistura 65% rotation real + 35% naive ("season minutes × fração
        # de tempo restante"). Tratar rotation como SUGESTÃO, não regra:
        # padrões mudam em B2B, lesão, injury report, foul trouble — naive
        # captura "se ele jogasse a temporada média daqui pra frente, X min".
        # Ratio testado contra Bet365 no fim de mai/2026 — 65/35 minimiza
        # erro absoluto vs estimativa real do mercado.
        game_minutes_remaining = (
            (4 - period) * 12.0 + max(0.0, clock_minutes_remaining)
        )
        on_court_fraction = (
            profile.total_minutes / 48.0
            if profile.total_minutes > 0
            else 0.7
        )
        naive_remaining = max(0.0, game_minutes_remaining) * on_court_fraction

        blended_remaining = 0.65 * rotation_remaining + 0.35 * naive_remaining

        # Aplica desconto de blowout (treinador descansa cedo).
        if blowout_severity > 0:
            blended_remaining *= (1.0 - 0.30 * blowout_severity)

        return max(blended_remaining, 0.0)

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    def _fetch_real_profile(
        self,
        player_id: int,
        season_minutes: float,
    ) -> Optional[RotationProfile]:
        """
        Baixa do nbarotations.info, parseia, agrega últimos 10 jogos
        + deriva windows/clutch/blowout. Retorna None se fonte indisponível
        ou sample insuficiente.
        """
        html = self._client.fetch_player_html(player_id)
        if html is None:
            return None

        games = parse_player_html(html)
        if not games:
            return None

        season_start = _current_season_start()
        agg = aggregate_recent_games(
            games,
            season_start=season_start,
            last_n=10,
        )
        if agg is None:
            return None

        # Filtra games da temporada atual pra derivar clutch/blowout
        season_games = [g for g in games if g.gamedate >= season_start][-20:]

        # Margens reais quando fetcher está disponível. Se fetch falhar pra
        # algum game, fica fora do dict — derive_clutch_and_blowout cai no
        # self-clustering pra esses individualmente.
        margins: Optional[dict[str, int]] = None
        if self._final_margin_fetcher is not None and season_games:
            margins = {}
            for g in season_games:
                if not g.gamecode:
                    continue
                try:
                    m = self._final_margin_fetcher(g.gamecode)
                except Exception as exc:
                    logger.info(
                        "rotation: final_margin fetch falhou pro game %s (%s)",
                        g.gamecode, exc,
                    )
                    continue
                if m is not None and m >= 0:
                    margins[g.gamecode] = int(m)

        quarter_patterns = derive_quarter_patterns(agg["minute_probabilities"])
        clutch, blowout = derive_clutch_and_blowout(season_games, margins=margins)

        # Production by period — combina histograma (minutos por quarter)
        # com PBP histórico (stats por quarter). Lazy: só roda se temos
        # pbp_fetcher injetado. Se PBP falhar pra todos os jogos da
        # amostra, devolve None e o caller usa rate uniforme.
        production = None
        if self._pbp_fetcher is not None:
            try:
                prods = compute_production_by_period(
                    games,
                    player_id=player_id,
                    fetch_pbp_per_period=self._pbp_fetcher,
                    season_start=season_start,
                    last_n=5,
                )
                production = production_to_dict(prods)
            except Exception as exc:
                logger.info(
                    "rotation: production_by_period falhou pra player %d (%s)",
                    player_id, exc,
                )

        logger.info(
            "rotation: profile real player %d — %d jogos, %.1f min/jogo, "
            "closes=%s, blowout_lost=%.1f, prod_by_period=%s",
            player_id, agg["sample_games"], agg["total_minutes_avg"],
            clutch.usually_closes_games, blowout.typical_minutes_lost_in_blowout,
            "yes" if production else "no",
        )
        return RotationProfile(
            player_id=player_id,
            total_minutes=agg["total_minutes_avg"],
            minute_probabilities=agg["minute_probabilities"],
            sample_games=agg["sample_games"],
            is_fallback=False,
            quarter_patterns=quarter_patterns,
            clutch_usage={
                "usually_closes_games": clutch.usually_closes_games,
                "fourth_quarter_usage_rate": clutch.fourth_quarter_usage_rate,
                "close_game_minutes_probability": clutch.close_game_minutes_probability,
            },
            blowout_risk={
                "fourth_quarter_return_probability_when_blowout":
                    blowout.fourth_quarter_return_probability_when_blowout,
                "typical_minutes_lost_in_blowout":
                    blowout.typical_minutes_lost_in_blowout,
            },
            production_by_period=production,
        )

    @staticmethod
    def _build_fallback(player_id: int, season_minutes: float) -> RotationProfile:
        if season_minutes <= 0:
            # Sem dado de temporada — assume rotação mínima de bench.
            return RotationProfile(
                player_id=player_id,
                total_minutes=8.0,
                minute_probabilities=[],
                is_fallback=True,
            )
        return RotationProfile(
            player_id=player_id,
            total_minutes=season_minutes,
            minute_probabilities=[],
            is_fallback=True,
        )

    @staticmethod
    def _fallback_remaining(
        *,
        total: float,
        period: int,
        clock_minutes_remaining: float,
        minutes_already_played: float,
        blowout_severity: float,
    ) -> float:
        """V1: assume distribuição uniforme. Mantido como safety net."""
        if period <= 4:
            game_minutes_remaining = (
                (4 - period) * 12.0 + max(0.0, clock_minutes_remaining)
            )
        else:
            game_minutes_remaining = max(0.0, clock_minutes_remaining)

        remaining_by_profile = max(total - minutes_already_played, 0.0)
        on_court_max = game_minutes_remaining * 0.85
        result = min(remaining_by_profile, on_court_max)

        if blowout_severity > 0:
            result *= (1.0 - 0.30 * blowout_severity)

        return max(result, 0.0)
