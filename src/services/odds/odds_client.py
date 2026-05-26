"""
HTTP client pra The Odds API (https://the-odds-api.com).

Camada fina sobre `requests`: dois endpoints, payload cru retornado como
dict, falha silenciosa (None ou []). A camada de serviço é que decide o
que fazer com isso (cache, agregação, fallback).

Headers de quota (`x-requests-remaining`, `x-requests-used`) são logados
pra dar visibilidade do consumo sem precisar abrir o dashboard.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from src.league import ODDS_SPORT_KEY

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
DEFAULT_TIMEOUT = 10


class OddsApiClient:
    """Wrapper minimalista. Stateless além do api_key/regions."""

    def __init__(self, api_key: str, regions: str = "us") -> None:
        self._api_key = api_key
        self._regions = regions

    def list_events(self, sport: str = "basketball_nba") -> list[dict]:
        """
        Lista de eventos (jogos) ativos pro sport. Cada item tem
        {id, home_team, away_team, commence_time, ...}.

        Endpoint barato (1 crédito por chamada, sem multiplicador).
        """
        url = f"{BASE_URL}/sports/{sport}/events"
        params = {"apiKey": self._api_key}
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            self._log_quota(resp)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("OddsApi list_events falhou: %s", exc)
            return []

    def event_odds(
        self,
        *,
        event_id: str,
        markets: list[str],
        sport: str = ODDS_SPORT_KEY,
        bookmakers: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Odds de UM evento. Retorna {id, bookmakers: [{key, title, markets:
        [{key, outcomes: [{name, description, price, point}]}]}], ...} ou
        None em qualquer falha.

        Cobra: 10 créditos × len(markets) × len(regions) por chamada.
        Pra 3 markets × 1 region = 30 créditos por evento.
        """
        url = f"{BASE_URL}/sports/{sport}/events/{event_id}/odds"
        params = {
            "apiKey": self._api_key,
            "regions": self._regions,
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            self._log_quota(resp)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("OddsApi event_odds falhou pro %s: %s", event_id, exc)
            return None

    @staticmethod
    def _log_quota(resp: requests.Response) -> None:
        """Loga quota restante — útil pra rastrear consumo sem dashboard."""
        remaining = resp.headers.get("x-requests-remaining")
        used = resp.headers.get("x-requests-used")
        if remaining is not None:
            logger.info(
                "OddsApi quota: %s used, %s remaining", used, remaining
            )
