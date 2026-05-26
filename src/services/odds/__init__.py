"""
The Odds API integration — linhas reais de player props.

Exports:
  - OddsService: API estável que o LiveAnalysisService consome.
  - PlayerOdds: linha agregada (média entre books) por stat.
"""
from src.services.odds.odds_service import (
    GameOdds,
    OddsService,
    PlayerOdds,
    compute_odds_ttl,
)

__all__ = ["OddsService", "PlayerOdds", "GameOdds", "compute_odds_ttl"]
