"""
LineLog — logging estruturado de cada cálculo de linha.

Persiste 1 linha JSON por cálculo num arquivo append-only. Uso
principal: coletar dataset histórico {features, our_line, real_line,
actual_outcome} pra medir performance REAL em produção (CLV/hit rate
contra a linha do book) e alimentar calibração futura.

Formato (JSON Lines):
  {"ts": "...", "game_id": ..., "player_id": ..., "stat": "PTS",
   "features": {...}, "our_line": 27.5, "projection": ..., "decision":
   ..., "edge": ..., "real_line": ..., "actual_outcome": null}

Path: `CACHE_DIR/line_log.jsonl`.

╭─ TRÊS FREIOS (mai/2026) — pra o arquivo NUNCA crescer sem limite ─────╮
│ 1. DEDUP: 1 registro por (game_id, player_id, stat, minuto-do-jogo). │
│    O feed faz polling; sem isso a MESMA predição era gravada         │
│    centenas de vezes por jogo (incha o arquivo E enviesa o dataset). │
│ 2. RETENÇÃO: prune_log() descarta registros > 90 dias.              │
│ 3. TETO: prune_log() corta os mais antigos se passar de 200 MB.     │
│    prune_log() roda no backfill diário → manutenção automática.     │
╰──────────────────────────────────────────────────────────────────────╯

Best-effort: falha de IO é silenciada — não bloqueia o pipeline live.

Quando usar:
  - Habilitado por env var `LOG_LINE_CALC=1` (default off).
  - Produção: ligar com VOLUME PERSISTENTE (Railway zera FS no deploy).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─── Freio 1: DEDUP (in-memory) ──────────────────────────────────────────────
# Set de chaves (game_id, player_id, stat, minute_bucket) já gravadas.
# In-memory: reseta em restart (aceitável — pior caso, alguns poucos
# duplicados por restart, não centenas por polling). Limite de memória:
# se passar de _DEDUP_MAX chaves, limpa (válvula de segurança — uma
# temporada inteira de jogos cabe bem abaixo disso num processo vivo).
_seen_keys: set[tuple] = set()
_DEDUP_MAX = 200_000


def reset_dedup() -> None:
    """Limpa o estado de dedup. Usado em testes e no boot do processo."""
    _seen_keys.clear()


def _minute_bucket(line_context: Any) -> int:
    """
    Minuto do jogo (0-48) derivado de game_minutes_remaining. É o
    'bucket' do dedup: a mesma predição dentro do mesmo minuto de jogo
    conta UMA vez. Minutos diferentes = registros distintos (queremos
    ver a evolução da predição Q1 vs Q3 — ambas valiosas pra validação).
    """
    gmr = getattr(line_context, "game_minutes_remaining", 0.0) or 0.0
    try:
        return max(0, min(48, int(round(48.0 - float(gmr)))))
    except (TypeError, ValueError):
        return 0


def _log_path() -> str:
    from src.config import CACHE_DIR
    return os.path.join(CACHE_DIR, "line_log.jsonl")


def is_enabled() -> bool:
    return os.environ.get("LOG_LINE_CALC", "0") == "1"


def log_line_calculation(
    *,
    player_id: int,
    player_name: str,
    team_tricode: str,
    stat: str,                      # "PTS" | "REB" | "AST" | "3PM"
    line_context: Any,              # LineContext (dataclass)
    line_result: Any,               # LineResult
    projection: Optional[float] = None,
    game_id: Optional[str] = None,
    decision: Optional[str] = None,
    edge: Optional[float] = None,
    real_line: Optional[float] = None,
    real_edge: Optional[float] = None,
    real_book_count: int = 0,
    bet365_line: Optional[float] = None,    # legacy: mantém compat
    actual_outcome: Optional[float] = None, # populado pós-jogo (backfill)
    extra: Optional[dict] = None,
) -> None:
    """
    Append 1 entrada JSONL no log. Falha silenciosa se IO falhar.

    DEDUP: se já gravou (game_id, player_id, stat, minuto-do-jogo)
    nesta sessão, pula — evita o feed de polling gravar a mesma
    predição N vezes. Quando game_id é None (sem como formar chave
    estável) NÃO deduplica — grava sempre (ex: testes/casos isolados).
    """
    if not is_enabled():
        return

    # ── Freio 1: dedup ────────────────────────────────────────────────
    if game_id is not None:
        key = (game_id, player_id, stat, _minute_bucket(line_context))
        if key in _seen_keys:
            return  # já registrado neste minuto de jogo — pula
        if len(_seen_keys) >= _DEDUP_MAX:
            _seen_keys.clear()  # válvula de memória
        _seen_keys.add(key)

    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "game_id": game_id,
            "player_id": player_id,
            "player_name": player_name,
            "team": team_tricode,
            "stat": stat,
            "features": _serialize_context(line_context),
            "our_line": line_result.line,
            "raw_line": line_result.raw_line,
            "components": line_result.components,
            "reason": line_result.reason,
            "floor_applied": line_result.floor_applied,
            "projection": projection,
            "decision": decision,
            "edge": edge,
            "real_line": real_line,
            "real_edge": real_edge,
            "real_book_count": real_book_count,
            "bet365_line": bet365_line,
            "actual_outcome": actual_outcome,
        }
        if extra:
            record["extra"] = extra
        path = _log_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        logger.debug("LineLog: write failed (%s)", exc)


def prune_log(
    path: Optional[str] = None,
    max_age_days: int = 90,
    max_size_mb: float = 200.0,
) -> dict:
    """
    Freios 2 e 3: aplica RETENÇÃO (descarta > max_age_days) e TETO
    (corta os mais antigos se > max_size_mb). Reescreve atomicamente
    (tmp → os.replace). Idempotente — seguro rodar todo dia.

    Pensado pra ser chamado pelo backfill diário (scripts/
    backfill_outcomes.py) — manutenção automática, zero infra extra.

    Returns dict com estatística: {existed, total, kept, dropped_age,
    dropped_size, final_size_mb}.
    """
    p = path or _log_path()
    if not os.path.exists(p):
        return {"existed": False, "total": 0, "kept": 0,
                "dropped_age": 0, "dropped_size": 0, "final_size_mb": 0.0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    kept_lines: list[str] = []
    total = 0
    dropped_age = 0

    with open(p, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            total += 1
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    dropped_age += 1
                    continue
            except (json.JSONDecodeError, KeyError, ValueError):
                # Linha corrompida / sem ts → descarta (não confiável)
                dropped_age += 1
                continue
            kept_lines.append(line)

    # ── Freio 3: teto de tamanho ──────────────────────────────────────
    # Mantém os MAIS RECENTES (fim da lista) até caber no teto.
    max_bytes = int(max_size_mb * 1024 * 1024)
    dropped_size = 0
    # bytes acumulados de trás pra frente
    acc = 0
    cut_idx = 0
    for i in range(len(kept_lines) - 1, -1, -1):
        acc += len(kept_lines[i].encode("utf-8")) + 1  # +1 = \n
        if acc > max_bytes:
            cut_idx = i + 1
            break
    if cut_idx > 0:
        dropped_size = cut_idx
        kept_lines = kept_lines[cut_idx:]

    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for line in kept_lines:
            f.write(line + "\n")
    os.replace(tmp, p)

    final_size = os.path.getsize(p) / (1024 * 1024) if os.path.exists(p) else 0.0
    return {
        "existed": True,
        "total": total,
        "kept": len(kept_lines),
        "dropped_age": dropped_age,
        "dropped_size": dropped_size,
        "final_size_mb": round(final_size, 2),
    }


def _serialize_context(ctx: Any) -> dict:
    """Converte um dataclass LineContext num dict JSON-friendly."""
    try:
        from dataclasses import asdict
        return asdict(ctx)
    except Exception:
        return {"_repr": str(ctx)}
