"""
CLI do HistoricalBacktester.

Uso:
    python -m src.services.backtester --season 2025-26 --days 30
    python -m src.services.backtester --games 0022500001,0022500002 --period 3
    python -m src.services.backtester --season 2025-26 --days 30 --period 2 --clock 6.0

Flags:
    --season       Temporada NBA (formato "YYYY-YY", default "2025-26")
    --days         Quantos dias finalizados pra trás (alternativa a --games)
    --games        Lista de game_ids separados por vírgula
    --period       Período do snapshot (1-4, default 3)
    --clock        Minutos restantes no clock do snapshot (default 0)
    --min-minutes  Min de minutos jogados pra considerar player (default 6)
    --output       json | markdown (default markdown)
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta

from src.services.backtester import (
    HistoricalBacktester,
    HistoricalLoader,
    format_report,
)
from src.nba_api_patches import apply_nba_api_patches


def main() -> None:
    apply_nba_api_patches()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Historical projection backtester")
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--days", type=int, default=None,
                        help="Pega últimos N dias finalizados da temporada")
    parser.add_argument("--games", default=None,
                        help="Lista de game_ids separada por vírgula")
    parser.add_argument("--period", type=int, default=3)
    parser.add_argument("--clock", type=float, default=0.0)
    parser.add_argument("--min-minutes", type=float, default=6.0)
    parser.add_argument("--output", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args()

    loader = HistoricalLoader()
    bt = HistoricalBacktester(loader=loader)

    # Resolve lista de jogos
    if args.games:
        game_ids = [g.strip() for g in args.games.split(",") if g.strip()]
    elif args.days:
        # Pega jogos da janela [today - days, today]
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        metas = loader.list_games_in_range(args.season, start, end)
        game_ids = [m.game_id for m in metas]
        print(f"Encontrados {len(game_ids)} jogos entre {start} e {end}.")
    else:
        parser.error("Forneça --days ou --games")

    if not game_ids:
        print("Nenhum jogo pra analisar.")
        return

    print(f"\nRodando backtest em {len(game_ids)} jogos "
          f"(snapshot: Q{args.period} @ {args.clock:.1f}min)...")
    metrics = bt.run(
        game_ids=game_ids,
        snapshot_period=args.period,
        snapshot_clock_min=args.clock,
        min_minutes_at_snapshot=args.min_minutes,
    )

    if args.output == "json":
        # Schema simples pra parsear
        payload = {
            "total_projections": metrics.total_projections,
            "by_stat": dict(metrics.by_stat),
            "by_decision": dict(metrics.by_decision),
            "caps_fired": dict(metrics.caps_fired),
            "samples": [s.__dict__ for s in metrics.samples[:20]],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print()
        print(format_report(metrics))

    cache = loader.stats()
    print(f"\nCache: {cache['games_cached']} jogos, "
          f"{cache['player_logs_cached']} gamelogs em {cache['cache_dir']}")


if __name__ == "__main__":
    main()
