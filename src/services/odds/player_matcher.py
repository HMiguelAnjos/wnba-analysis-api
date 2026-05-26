"""
Matching nome do jogador (vindo do The Odds API) → NBA player_id.

The Odds API usa nomes como "LeBron James", "Luka Dončić". O NBA static
players list tem o `full_name` oficial. Strategy:

  1. Normaliza ambos (lowercase, sem diacrítico, sem pontuação)
  2. Lookup direto — bate em > 99% dos casos pra atletas ativos NBA
  3. Sem fuzzy/Levenshtein — risco de matching errado é maior que o
     ganho marginal. Players sem match são logados e ignorados.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from nba_api.stats.static import players as nba_static_players

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """Lowercase + sem diacrítico + sem pontuação + whitespace colapsado."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


class PlayerNameMatcher:
    """
    Lookup case-insensitive de nome → player_id. Lazy: só carrega o índice
    no primeiro `find()` (evita custar import time se o serviço estiver off).
    """

    def __init__(self) -> None:
        self._index: dict[str, int] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            roster = nba_static_players.get_players()
        except Exception as exc:
            logger.warning("PlayerNameMatcher: falhou load do roster (%s)", exc)
            self._loaded = True   # evita retry infinito; index fica vazio
            return
        for p in roster:
            name = p.get("full_name") or ""
            normalized = normalize_name(name)
            if normalized:
                self._index[normalized] = int(p["id"])
        self._loaded = True

    def find(self, name: str) -> Optional[int]:
        """Retorna player_id ou None se não bateu."""
        if not name:
            return None
        self._ensure_loaded()
        return self._index.get(normalize_name(name))
