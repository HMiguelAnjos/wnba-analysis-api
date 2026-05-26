"""
backfill_outcomes.py — popula `actual_outcome` em line_log.jsonl

Lê o JSONL de cálculos de linha gerado por LOG_LINE_CALC=1, identifica
records sem `actual_outcome`, e busca o stat real do jogador no jogo
via PBP histórico (NbaService.aggregate_historical_pbp_per_period).

Reescreve o arquivo atomicamente: tmp file → rename. Falha de fetch
em UM jogo não interrompe os outros — logs e continua.

Uso:
    python scripts/backfill_outcomes.py
    python scripts/backfill_outcomes.py --path /custom/line_log.jsonl
    python scripts/backfill_outcomes.py --dry-run
    python scripts/backfill_outcomes.py --since-days 7

Pré-requisito do log:
    Cada record precisa ter `game_id`, `player_id`, `stat`. Records
    gerados antes de mai/2026 (sem `game_id`) são pulados — esses ficam
    para sempre sem outcome (não há como mapear retroativamente).

Stats suportados (case-insensitive):
    PTS  → points    (somado de todos os quarters do PBP)
    REB  → rebounds
    AST  → assists
    3PM  → three_pm  (precisa do aggregator estendido pós mai/2026)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from typing import Optional

# Adiciona o root do projeto ao path pra permitir imports `src.*`
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.services.nba_service import NbaService

logger = logging.getLogger(__name__)


STAT_KEY_MAP = {
    "PTS": "points",
    "REB": "rebounds",
    "AST": "assists",
    "3PM": "three_pm",
}


def _read_records(path: str) -> list[dict]:
    """Carrega todos os records do JSONL. Linhas malformadas são puladas."""
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _write_records_atomic(path: str, records: list[dict]) -> None:
    """Atomic write: tmp file → rename, evita corrupção se Ctrl+C."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    os.replace(tmp, path)


def _fetch_game_totals(
    nba: NbaService, game_id: str
) -> Optional[dict[int, dict[str, int]]]:
    """
    Busca stats finais por jogador no jogo, somando todos os quarters
    do PBP histórico. Retorna {player_id: {points, rebounds, assists,
    three_pm}} ou None em falha.
    """
    try:
        agg = nba.aggregate_historical_pbp_per_period(game_id)
    except Exception as exc:
        logger.warning("PBP fetch falhou pro game %s: %s", game_id, exc)
        return None
    if not agg:
        return None

    totals: dict[int, dict[str, int]] = {}
    for pid, periods in agg.items():
        t = {"points": 0, "rebounds": 0, "assists": 0, "three_pm": 0}
        for _, stats in periods.items():
            t["points"] += stats.get("points", 0)
            t["rebounds"] += stats.get("rebounds", 0)
            t["assists"] += stats.get("assists", 0)
            t["three_pm"] += stats.get("three_pm", 0)
        totals[pid] = t
    return totals


def backfill(
    path: str,
    *,
    dry_run: bool = False,
    nba_service: Optional[NbaService] = None,
) -> dict:
    """
    Roda o backfill no arquivo `path`.

    Returns:
        {
          "total_records": int,
          "needed_outcome": int,
          "filled": int,
          "skipped_no_game_id": int,
          "skipped_unknown_stat": int,
          "skipped_player_missing": int,
          "games_fetched": int,
          "games_failed": int,
          "dry_run": bool,
        }
    """
    if not os.path.exists(path):
        return {
            "total_records": 0,
            "filled": 0,
            "error": f"arquivo não encontrado: {path}",
        }

    records = _read_records(path)
    nba = nba_service or NbaService()

    # 1ª passada: identifica game_ids únicos com records que precisam de outcome
    games_needed: set[str] = set()
    skipped_no_game_id = 0
    needed_outcome = 0
    for rec in records:
        if rec.get("actual_outcome") is not None:
            continue
        needed_outcome += 1
        gid = rec.get("game_id")
        if not gid:
            skipped_no_game_id += 1
            continue
        games_needed.add(str(gid))

    # 2ª passada: fetch totais por game_id
    game_stats: dict[str, dict[int, dict[str, int]]] = {}
    games_failed = 0
    for gid in sorted(games_needed):
        totals = _fetch_game_totals(nba, gid)
        if totals is None:
            games_failed += 1
            continue
        game_stats[gid] = totals
        logger.info("game %s: %d players agregados", gid, len(totals))

    # 3ª passada: preenche actual_outcome
    filled = 0
    skipped_unknown_stat = 0
    skipped_player_missing = 0
    for rec in records:
        if rec.get("actual_outcome") is not None:
            continue
        gid = rec.get("game_id")
        if not gid or gid not in game_stats:
            continue
        try:
            pid = int(rec.get("player_id") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        stat = (rec.get("stat") or "").upper()
        key = STAT_KEY_MAP.get(stat)
        if key is None:
            skipped_unknown_stat += 1
            continue
        player_totals = game_stats[gid].get(pid)
        if player_totals is None:
            skipped_player_missing += 1
            continue
        outcome = player_totals.get(key)
        if outcome is None:
            continue
        rec["actual_outcome"] = float(outcome)
        filled += 1

    if not dry_run and filled > 0:
        _write_records_atomic(path, records)

    return {
        "total_records": len(records),
        "needed_outcome": needed_outcome,
        "filled": filled,
        "skipped_no_game_id": skipped_no_game_id,
        "skipped_unknown_stat": skipped_unknown_stat,
        "skipped_player_missing": skipped_player_missing,
        "games_fetched": len(game_stats),
        "games_failed": games_failed,
        "dry_run": dry_run,
    }


def _default_log_path() -> str:
    cache_dir = os.environ.get("CACHE_DIR", "/tmp")
    return os.path.join(cache_dir, "line_log.jsonl")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Popula actual_outcome em line_log.jsonl"
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Path do JSONL (default: $CACHE_DIR/line_log.jsonl)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não escreve no arquivo, só reporta",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Pula a manutenção (retenção 90d + teto 200MB)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=90,
        help="Retenção: descarta registros mais antigos que isso (default 90)",
    )
    parser.add_argument(
        "--max-size-mb",
        type=float,
        default=200.0,
        help="Teto: corta os mais antigos se passar disso (default 200)",
    )
    args = parser.parse_args()

    path = args.path or _default_log_path()
    print(f"Backfill em: {path}")
    if args.dry_run:
        print("(--dry-run: sem escrita)")

    result = backfill(path, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # ── Manutenção (freios 2 e 3): retenção + teto ───────────────────
    # Roda no mesmo cron diário do backfill — sem infra extra. O arquivo
    # fica BOUNDED pra sempre (nunca cresce sem limite → custo previsível).
    if not args.dry_run and not args.no_prune:
        from src.services.line.line_log import prune_log
        prune_stats = prune_log(
            path=path,
            max_age_days=args.max_age_days,
            max_size_mb=args.max_size_mb,
        )
        print("Prune (retenção + teto):")
        print(json.dumps(prune_stats, indent=2, ensure_ascii=False))

    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
