"""
Camada de cálculo da LINHA estimada (synthetic bookmaker line).

A LINHA é o número que apostamos que o bookmaker abriria pra Over/Under
desse prop específico. NÃO é a nossa projeção — é a estimativa do mercado.

Filosofia da separação:
  - LineEngine NÃO conhece o EDGE. Ele só calcula a linha.
  - LineEngine NÃO chama ProjectionEngine. Recebe `projected_end` como
    input se quem invocou já tem; é referência, não dependência.
  - O EDGE (decisão de aposta) nasce só depois, no caller, comparando
    LINE × PROJECTION.

Esta segregação permite calibrar e testar a linha contra o mercado
(Bet365, DraftKings) sem misturar com nossa visão da realidade.
"""

from src.services.line.line_engine import LineEngine

__all__ = ["LineEngine"]
