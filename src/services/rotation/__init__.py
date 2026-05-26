"""
Camada de ROTAÇÃO — minutos esperados por jogador num jogo.

Fonte primária: nbarotations.info (perfis observados minuto-a-minuto).
Fallback: distribuição uniforme baseada em season_minutes.

Validado em 2026-05-09:
  - Sem Cloudflare (curl simples passa)
  - Player ID NBA-nativo (LeBron 2544, etc.)
  - 1846+ jogos do LeBron disponíveis
  - 68 jogos da temporada 2025-26 já catalogados
"""

from src.services.rotation.nbarotations_client import NBARotationsClient
from src.services.rotation.rotation_provider import (
    RotationProfile,
    RotationProvider,
)

__all__ = ["NBARotationsClient", "RotationProfile", "RotationProvider"]
