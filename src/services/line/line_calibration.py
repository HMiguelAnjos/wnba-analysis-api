"""
LineCalibrationReport — leitura agregada do line_log.jsonl.

Lê o log estruturado (Fase 6) e produz métricas:
  - Total de cálculos por janela de tempo
  - Distribuição de `our_line` por stat (PTS/REB/AST)
  - Quando há `real_line` populado (The Odds API, ENABLE_REAL_ODDS=1):
      MAE, viés direcional e top 10 divergências vs mercado real
  - Quando há `bet365_line` populado (campo legado): mesmas métricas
  - Quando há `actual_outcome` populado (backfill): MAE vs resultado real

Uso:
  - GET /admin/calibration/report
  - Útil pra monitorar drift sem depender ainda de modelo ML.

Robustez:
  - Streaming line-by-line (não carrega arquivo inteiro em RAM)
  - Skip de linhas malformadas
  - Window default: últimos 7 dias
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class StatBucket:
    """Agregados por stat (PTS/REB/AST)."""
    count: int = 0
    sum_line: float = 0.0
    sum_proj: float = 0.0
    bet365_count: int = 0
    bet365_sum_abs_diff: float = 0.0
    bet365_sum_signed_diff: float = 0.0   # nossa - bet365 (positivo = superestimando)
    # Linha REAL do mercado (The Odds API, campo real_line). É o que de fato
    # temos populado em produção (ENABLE_REAL_ODDS=1) — bet365_line acima é
    # legado e fica zerado. Mesma semântica: positivo = nossa acima do mercado.
    real_count: int = 0
    real_sum_abs_diff: float = 0.0
    real_sum_signed_diff: float = 0.0     # nossa - real (positivo = acima do mercado)
    real_sample_diffs: list[tuple[str, float, float, float]] = field(default_factory=list)
    actual_count: int = 0
    actual_sum_abs_diff: float = 0.0       # |line - actual|
    sample_diffs: list[tuple[str, float, Optional[float], Optional[float]]] = field(default_factory=list)

    def add(
        self,
        line: float,
        proj: Optional[float],
        bet365: Optional[float],
        actual: Optional[float],
        player_name: str,
        real: Optional[float] = None,
    ) -> None:
        self.count += 1
        self.sum_line += line
        if proj is not None:
            self.sum_proj += proj
        if bet365 is not None:
            self.bet365_count += 1
            diff = line - bet365
            self.bet365_sum_abs_diff += abs(diff)
            self.bet365_sum_signed_diff += diff
            # Mantém os 50 maiores |diff| pra inspeção
            self.sample_diffs.append((player_name, line, bet365, diff))
        if real is not None:
            self.real_count += 1
            rdiff = line - real
            self.real_sum_abs_diff += abs(rdiff)
            self.real_sum_signed_diff += rdiff
            self.real_sample_diffs.append((player_name, line, real, rdiff))
            # Bound de memória: top_divergences é o único uso da lista.
            # Sem isto, window_days=0 acumularia ~1M tuplas em RAM.
            if len(self.real_sample_diffs) > 2000:
                self.real_sample_diffs.sort(key=lambda t: abs(t[3]), reverse=True)
                del self.real_sample_diffs[200:]
        if actual is not None:
            self.actual_count += 1
            self.actual_sum_abs_diff += abs(line - actual)

    def to_dict(self) -> dict:
        avg_line = round(self.sum_line / self.count, 2) if self.count else 0
        avg_proj = round(self.sum_proj / self.count, 2) if self.count else 0
        out = {
            "count": self.count,
            "avg_our_line": avg_line,
            "avg_projection": avg_proj,
        }
        if self.bet365_count > 0:
            out["bet365_observations"] = self.bet365_count
            out["bet365_mae"] = round(self.bet365_sum_abs_diff / self.bet365_count, 3)
            out["bet365_bias"] = round(self.bet365_sum_signed_diff / self.bet365_count, 3)
            top = sorted(self.sample_diffs, key=lambda t: abs(t[3]), reverse=True)[:10]
            out["top_divergences"] = [
                {"player": p, "our_line": l, "bet365": b, "diff": round(d, 2)}
                for (p, l, b, d) in top
            ]
        if self.real_count > 0:
            out["real_line_observations"] = self.real_count
            out["real_line_mae"] = round(self.real_sum_abs_diff / self.real_count, 3)
            out["real_line_bias"] = round(self.real_sum_signed_diff / self.real_count, 3)
            rtop = sorted(
                self.real_sample_diffs, key=lambda t: abs(t[3]), reverse=True
            )[:10]
            out["real_line_top_divergences"] = [
                {"player": p, "our_line": l, "real_line": r, "diff": round(d, 2)}
                for (p, l, r, d) in rtop
            ]
        if self.actual_count > 0:
            out["outcome_observations"] = self.actual_count
            out["outcome_mae"] = round(self.actual_sum_abs_diff / self.actual_count, 3)
        return out


def _log_path() -> str:
    from src.config import CACHE_DIR
    return os.path.join(CACHE_DIR, "line_log.jsonl")


def _iter_records(
    path: str,
    since: Optional[datetime] = None,
) -> Iterator[dict]:
    """Stream JSONL, skip malformed."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since:
                ts_raw = rec.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < since:
                    continue
            yield rec


def build_report(window_days: int = 7) -> dict:
    """
    Lê line_log.jsonl e devolve relatório agregado.

    Args:
      window_days: limite temporal (default 7 dias). 0 = tudo.

    Returns:
      dict com `window`, `total_calculations`, `by_stat: {PTS, REB, AST}`,
      ou `{"error": "..."}` se log indisponível.
    """
    path = _log_path()
    if not os.path.exists(path):
        return {
            "error": "line_log.jsonl não encontrado",
            "hint": "ative LOG_LINE_CALC=1 e rode jogos pra acumular dados",
            "expected_path": path,
        }

    since = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
        if window_days > 0 else None
    )

    buckets: dict[str, StatBucket] = {
        "PTS": StatBucket(), "REB": StatBucket(), "AST": StatBucket(),
    }
    total = 0
    skipped = 0
    for rec in _iter_records(path, since=since):
        total += 1
        stat = rec.get("stat", "")
        if stat not in buckets:
            skipped += 1
            continue
        try:
            buckets[stat].add(
                line=float(rec["our_line"]),
                proj=rec.get("projection"),
                bet365=rec.get("bet365_line"),
                actual=rec.get("actual_outcome"),
                player_name=rec.get("player_name", "?"),
                real=rec.get("real_line"),
            )
        except (KeyError, TypeError, ValueError):
            skipped += 1

    return {
        "window_days": window_days,
        "log_path": path,
        "total_records": total,
        "skipped_records": skipped,
        "by_stat": {k: v.to_dict() for k, v in buckets.items()},
    }
