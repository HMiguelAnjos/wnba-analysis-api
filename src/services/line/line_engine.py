"""
LineEngine — fachada da estimativa de linha (synthetic bookmaker).

Hoje delega 100% da matemática pra `calculate_estimated_sportsbook_line`
em `utils/stats.py`. A indireção existe pra permitir evolução incremental
(ex: injeção de matchup context, rotação, calibração por mercado) sem
churn nos callers.

Logging estruturado (Fase 6) MIGROU pro caller (`live_analysis_service.
_fair_line`) em mai/2026 — caller tem o contexto completo (decision, real
line, edge) que o engine não conhece. LineEngine virou puro: input→output.

Quando expandir:
  - Fase 7: substituir o kernel por modelo aprendido (LightGBM) mantendo
            mesma API pública.
"""

from __future__ import annotations

from typing import Optional

from src.utils.stats import (
    LineContext,
    LineResult,
    calculate_estimated_sportsbook_line,
)


class LineEngine:
    """
    Estima a linha de um prop específico (player_points/rebounds/assists).

    Não é stateful entre chamadas — cada `calculate()` é independente.
    Mas a classe existe (em vez de função pura) pra:
      - Permitir injeção de dependências futuras (matchup provider, etc.)
      - Facilitar mocking em testes
      - Convergir API com `ProjectionEngine` (mesmo shape de uso)
    """

    def calculate(
        self,
        ctx: LineContext,
        *,
        log_meta: Optional[dict] = None,   # legacy — ignorado, logging migrou
    ) -> LineResult:
        """Calcula linha + breakdown a partir do contexto. Pure function."""
        return calculate_estimated_sportsbook_line(ctx)
