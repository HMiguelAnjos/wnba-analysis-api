"""
Camada de detecção de HOT STREAK — quanto o jogador está realmente quente.

Diferente do `score` interno (que mede desvio vs esperado), o HeatDetector
combina sinais qualitativos de "estado de chama":
  - Eficiência (eFG% acima da temporada)
  - Pressão no aro (FTA/min acima do habitual)
  - Volume de arremessos (FGA/min acima do habitual)
  - Scoring run (sequência de possessions com pontos)

Output: heat ∈ [-1, +1]
   +1.0 = chama total (quase impossível esfriar — minutos restantes valem ouro)
    0.0 = neutro (sem sinal)
   -1.0 = frio total (mesmo que tenha pts, está jogando mal)

Plug em ProjectionEngine: heat > 0.6 dá boost gradual na produção esperada
do tempo restante (até +15%); heat < -0.4 reduz (até -20%).
"""

from src.services.hot_streak.heat_detector import HeatDetector, HeatSignal

__all__ = ["HeatDetector", "HeatSignal"]
