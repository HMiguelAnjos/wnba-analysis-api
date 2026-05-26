"""
Cliente HTTP do nbarotations.info.

API NÃO oficial — site do mrphilroth com perfis de rotação observados
de cada jogador NBA. Diferente do SofaScore: SEM Cloudflare bloqueando,
curl simples passa. Validado em 2026-05-09.

Endpoints usados:
  GET /player/<nba_player_id>
      → HTML com `displayPlayer([...])` inline contendo TODOS os
        jogos históricos do jogador (1846 pra LeBron, etc.).

Robustez:
  - Timeout 10s
  - Retry 1x se status 5xx
  - NÃO levanta exceção — falha vira `None`

Player ID é o NBA-nativo (LeBron 2544, Marcus Smart 203935). Sem mapping.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _base_url() -> str:
    """Late-bound — respeita NBA_ROTATION_BASE_URL no momento do call."""
    from src.config import NBA_ROTATION_BASE_URL
    return NBA_ROTATION_BASE_URL


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class NBARotationsClient:
    """Wrapper fino sobre `requests`. Retorna HTML cru ou None."""

    def __init__(self, timeout: float = _TIMEOUT) -> None:
        self._timeout = timeout

    def fetch_player_html(self, nba_player_id: int) -> Optional[str]:
        """
        Baixa página /player/<id>. Retorna HTML cru ou None em qualquer
        falha (rede, 4xx, 5xx). Sem retry agressivo — fonte é "nice to have".
        """
        url = f"{_base_url()}/player/{nba_player_id}"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=self._timeout)
        except requests.RequestException as exc:
            logger.info("nbarotations: GET %s falhou (%s)", url, exc)
            return None
        if resp.status_code != 200:
            logger.info("nbarotations: GET %s retornou %s", url, resp.status_code)
            return None
        if not resp.text or "displayPlayer" not in resp.text:
            logger.info("nbarotations: %s sem displayPlayer (player desconhecido?)", url)
            return None
        return resp.text
