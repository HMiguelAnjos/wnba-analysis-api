"""
Camada de PROJEÇÃO — quanto a gente acha que o jogador VAI FAZER hoje.

Diferente da LINHA (que tenta replicar o bookmaker), a PROJEÇÃO é a
nossa estimativa contextual da realidade. Pode divergir radicalmente
da linha — é exatamente nessa divergência que mora o EDGE de aposta.

Filosofia da separação:
  - ProjectionEngine NÃO conhece a LINHA. É puramente forward-looking.
  - Sinais usados: ritmo do jogo, fouls, blowout, cold/hot start, etc.
  - Sinais que a linha usa mas a projeção não: vig de bookmaker, regressão
    forçada à média de mercado, normalização .5 padrão.
"""

from src.services.projection.projection_engine import ProjectionEngine

__all__ = ["ProjectionEngine"]
