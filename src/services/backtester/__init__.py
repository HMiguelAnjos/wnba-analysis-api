"""
Camada de BACKTESTING — simula decisões e mede ROI hipotético.

Quando tivermos:
  1. line_log.jsonl com bet365_line + actual_outcome populados
  2. histórico de pelo menos algumas semanas

…o BackTester replica "o que teria acontecido" se apostássemos em todos
os sinais STRONG_OVER/STRONG_UNDER que nosso sistema gerou.

Métricas:
  - hit_rate (% de apostas vencedoras)
  - ROI (assumindo odds padrão -110)
  - distribuição de edge nas apostas vencedoras vs perdedoras
  - Sharpe da curva de equity

Hoje é esqueleto: classe + tests do framework. Implementação real
depende de dados que ainda não temos.
"""

from src.services.backtester.backtester import BackTester, BacktestResult
from src.services.backtester.historical import (
    BacktestMetrics,
    HistoricalBacktester,
    ProjectionVsActual,
    format_report,
)
from src.services.backtester.historical_loader import (
    GameMeta,
    HistoricalLoader,
)
from src.services.backtester.snapshot import (
    GameSnapshot,
    PlayerSnapshot,
    extract_final_stats,
    reconstruct_snapshot,
)

__all__ = [
    "BackTester",
    "BacktestResult",
    "BacktestMetrics",
    "HistoricalBacktester",
    "HistoricalLoader",
    "ProjectionVsActual",
    "GameMeta",
    "GameSnapshot",
    "PlayerSnapshot",
    "extract_final_stats",
    "reconstruct_snapshot",
    "format_report",
]
