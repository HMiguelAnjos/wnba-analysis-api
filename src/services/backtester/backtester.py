"""
BackTester — simula ROI hipotético de apostar em sinais STRONG/LEAN.

Lê line_log.jsonl com bet365_line + actual_outcome populados e replica
decisões que o sistema gerou. Mede:

  - Hit rate por nível de decisão (STRONG vs LEAN)
  - ROI assumindo odds -110 padrão (51.6% breakeven hit rate)
  - Edge distribution dos vencedores vs perdedores
  - Equity curve (cumulative P&L)

V1 (esqueleto): framework + cálculo de hit rate + ROI básico.
V2 (futuro): odds reais por aposta, Kelly fractional sizing,
filtros por contexto (foul trouble, blowout, etc).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Default odds americano -110 implica 47.6% pra cobrir o vig.
# Hit rate breakeven com -110 = 110/(110+100) = 0.524 (52.4%).
DEFAULT_AMERICAN_ODDS = -110
DEFAULT_BREAKEVEN_HIT_RATE = 110 / 210  # ≈ 0.5238


@dataclass
class BacktestResult:
    """Output agregado do backtest."""
    total_bets: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0  # quando outcome == line (raríssimo com .5)
    bets_by_decision: dict[str, dict] = field(default_factory=dict)
    roi_pct: float = 0.0
    hit_rate: float = 0.0
    breakeven_hit_rate: float = DEFAULT_BREAKEVEN_HIT_RATE
    sample_bets: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "pushes": self.pushes,
            "hit_rate": round(self.hit_rate, 4),
            "breakeven_hit_rate": round(self.breakeven_hit_rate, 4),
            "roi_pct": round(self.roi_pct, 2),
            "by_decision": self.bets_by_decision,
            "sample_bets": self.sample_bets[:20],
        }


class BackTester:
    """
    Roda backtest sobre line_log.jsonl. Stateless — cada `run()` é
    independente e relê o arquivo.
    """

    def run(
        self,
        jsonl_path: str,
        *,
        min_decision: str = "LEAN",     # "STRONG" só pega apostas mais fortes
        odds_american: int = DEFAULT_AMERICAN_ODDS,
    ) -> BacktestResult:
        """
        Args:
          jsonl_path: line_log.jsonl path
          min_decision: "STRONG" | "LEAN" | "ALL" — filtro mínimo de força
          odds_american: assumido pra todas apostas; -110 default

        Returns:
          BacktestResult com métricas. Vazio (todos zeros) se sem dados.
        """
        if not os.path.exists(jsonl_path):
            return BacktestResult()

        result = BacktestResult()
        result.bets_by_decision = {}

        for rec in _iter_records(jsonl_path):
            outcome = rec.get("actual_outcome")
            line = rec.get("our_line")
            decision = rec.get("decision") or self._infer_decision(rec)
            if outcome is None or line is None or decision is None:
                continue
            if not self._passes_filter(decision, min_decision):
                continue

            # Determina win/loss
            is_over = "OVER" in decision
            won = (outcome > line) if is_over else (outcome < line)
            push = outcome == line

            result.total_bets += 1
            if push:
                result.pushes += 1
            elif won:
                result.wins += 1
            else:
                result.losses += 1

            # Bucket por decisão
            bucket = result.bets_by_decision.setdefault(
                decision, {"bets": 0, "wins": 0, "losses": 0, "pushes": 0},
            )
            bucket["bets"] += 1
            if push:
                bucket["pushes"] += 1
            elif won:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1

            # Sample (mantém os 50 mais recentes pra inspeção)
            if len(result.sample_bets) < 50:
                result.sample_bets.append({
                    "player": rec.get("player_name", "?"),
                    "stat": rec.get("stat", "?"),
                    "decision": decision,
                    "line": line,
                    "outcome": outcome,
                    "won": won and not push,
                })

        # Compute ROI: cada aposta vence paga 100/110 da unidade (odds -110).
        # Cada aposta perdida custa 1 unidade.
        if result.total_bets > 0:
            payout_per_win = 100.0 / abs(odds_american) if odds_american < 0 else odds_american / 100.0
            total_pnl = result.wins * payout_per_win - result.losses
            risked = result.total_bets - result.pushes  # pushes devolvem stake
            result.roi_pct = (total_pnl / max(risked, 1)) * 100
            result.hit_rate = result.wins / max(result.wins + result.losses, 1)

        return result

    @staticmethod
    def _infer_decision(rec: dict) -> Optional[str]:
        """
        Quando o log não tem `decision` salva, infere do `components`
        ou do reason. Hoje sempre None — log só popula quando explicitamente.
        """
        return None

    @staticmethod
    def _passes_filter(decision: str, min_decision: str) -> bool:
        if min_decision == "ALL":
            return True
        if min_decision == "STRONG":
            return "STRONG" in decision
        if min_decision == "LEAN":
            return "STRONG" in decision or "LEAN" in decision
        return False

    @staticmethod
    def estimate_runnable(jsonl_path: str) -> dict:
        """
        Diz se há dados suficientes pra rodar backtest. Útil pra UI
        mostrar "ainda não dá pra backtest, faltam X registros".
        """
        if not os.path.exists(jsonl_path):
            return {"runnable": False, "reason": "log não existe"}
        total = 0
        with_outcome = 0
        with_decision = 0
        for rec in _iter_records(jsonl_path):
            total += 1
            if rec.get("actual_outcome") is not None:
                with_outcome += 1
            if rec.get("decision") is not None:
                with_decision += 1
        runnable = with_outcome >= 100 and with_decision >= 100
        return {
            "runnable": runnable,
            "total": total,
            "with_outcome": with_outcome,
            "with_decision": with_decision,
            "min_required": 100,
        }


def _iter_records(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
