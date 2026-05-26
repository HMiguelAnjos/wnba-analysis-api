"""
Camada de MATCHUP — contexto do confronto pra ajustar linha/projeção.

Bookmakers (Bet365, etc.) ajustam linhas baseado em quem é o adversário:
defesa boa baixa a linha; pace alto sobe; jogo home/away matter.

Hoje carregamos só DRtg + Pace via NBA stats API (1x/dia, cache 24h).
Fallback neutro garante que o sistema funciona se o fetch falhar.

Quando expandir:
  - Splits home/away (defesa muda fora de casa)
  - Pace por quarter (alguns times jogam pacey só no Q1)
  - DRtg por position (defensa de alas vs guards)
"""

from src.services.matchup.matchup_provider import (
    MatchupContext,
    MatchupProvider,
)

__all__ = ["MatchupContext", "MatchupProvider"]
