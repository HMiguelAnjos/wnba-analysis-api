"""
Production by period — pts/min, ast/min, reb/min por quarter, agregados
sobre os últimos N jogos da temporada.

Combina 2 fontes:
  1. nbarotations.info `histogram` por jogo → minutos do player em cada
     bloco de 12 min (Q1/Q2/Q3/Q4).
  2. NBA stats PlayByPlayV3 por jogo → stats do player por período.

Resultado: rate empírico ESPECÍFICO por quarter, sem assumir que o ritmo
é uniforme. Útil pra capturar que "X faz 0.6 ppm no Q1 mas 0.9 ppm no
Q4 quando joga clutch".

Custo: 1 fetch de PBP histórico por jogo da amostra (lazy + cache 30d
no NbaService garante reuso entre players do mesmo jogo).
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Callable, Optional

from src.services.rotation.nbarotations_parser import GameRotationEntry

logger = logging.getLogger(__name__)


@dataclass
class ProductionPeriod:
    """Rate por minuto que o player produziu num determinado quarter."""
    quarter: int               # 1..4
    points_per_min: float
    assists_per_min: float
    rebounds_per_min: float
    sample_minutes: float      # total de minutos (todos jogos) usados pra média
    sample_games: int          # quantos jogos contribuíram


# Tipo do fetcher injetado: recebe game_id, devolve {player_id: {period: stats}}
PbpAggregateFn = Callable[[str], dict[int, dict[int, dict[str, int]]]]


def compute_production_by_period(
    games: list[GameRotationEntry],
    *,
    player_id: int,
    fetch_pbp_per_period: PbpAggregateFn,
    season_start: str,
    last_n: int = 5,
) -> Optional[list[ProductionPeriod]]:
    """
    Args:
      games: histórico cru do nbarotations (já parsed)
      player_id: NBA player_id (mesmo usado no rotation profile)
      fetch_pbp_per_period: função que, dado game_id, devolve mapping
        {player_id: {period: {points, assists, rebounds}}}
        Tipicamente NbaService.aggregate_historical_pbp_per_period bound.
      season_start: ISO YYYY-MM-DD (filtro temporal)
      last_n: quantos jogos da temporada usar (default 5 — limita custo)

    Returns:
      list[ProductionPeriod] (4 entradas, Q1-Q4) ou None se sample insuficiente.

    Falha silenciosa em jogos individuais — pula PBP indisponível e
    continua com o resto.
    """
    season_games = [g for g in games if g.gamedate >= season_start]
    if not season_games:
        return None

    # Ordenar por data ASC e pegar os mais recentes
    season_games.sort(key=lambda g: g.gamedate)
    recent = season_games[-last_n:]

    # Acumuladores: por quarter, soma de stats e soma de minutos
    stats_sum = {q: {"points": 0.0, "assists": 0.0, "rebounds": 0.0}
                 for q in range(1, 5)}
    minutes_sum = {q: 0.0 for q in range(1, 5)}
    games_used = 0

    for game in recent:
        # Fallback se histograma está incompleto
        if len(game.histogram) < 48:
            continue

        # 1) Stats do player nesse jogo (do PBP histórico)
        try:
            full = fetch_pbp_per_period(game.gamecode)
        except Exception as exc:
            logger.info(
                "production_by_period: PBP fetch falhou pro game %s (%s)",
                game.gamecode, exc,
            )
            continue
        if not full:
            continue

        player_stats = full.get(player_id)
        if not player_stats:
            # Player não aparece no PBP — pode ter sido inactive nesse jogo
            continue

        # 2) Minutos do player por período (do histograma do nbarotations)
        # Cada slot do histograma vale 1 minuto com prob `p` → contribui `p` min.
        any_period_data = False
        for q in range(1, 5):
            slice_probs = game.histogram[(q - 1) * 12 : q * 12]
            mins_in_q = sum(slice_probs)
            if mins_in_q < 0.5:
                # Player praticamente não jogou esse quarter — pula pra
                # evitar dividir por número minúsculo e amplificar ruído.
                continue
            period_stats = player_stats.get(q, {})
            stats_sum[q]["points"] += period_stats.get("points", 0)
            stats_sum[q]["assists"] += period_stats.get("assists", 0)
            stats_sum[q]["rebounds"] += period_stats.get("rebounds", 0)
            minutes_sum[q] += mins_in_q
            any_period_data = True

        if any_period_data:
            games_used += 1

    # Exige pelo menos 3 jogos com dados pra ter sinal estatístico
    if games_used < 3:
        return None

    out: list[ProductionPeriod] = []
    for q in range(1, 5):
        mins = minutes_sum[q]
        if mins <= 0:
            # Quarter sem dados — preenche com zeros (rate 0 = neutro,
            # caller tem que tratar como "sem sinal")
            out.append(ProductionPeriod(
                quarter=q,
                points_per_min=0.0,
                assists_per_min=0.0,
                rebounds_per_min=0.0,
                sample_minutes=0.0,
                sample_games=0,
            ))
            continue
        out.append(ProductionPeriod(
            quarter=q,
            points_per_min=round(stats_sum[q]["points"] / mins, 4),
            assists_per_min=round(stats_sum[q]["assists"] / mins, 4),
            rebounds_per_min=round(stats_sum[q]["rebounds"] / mins, 4),
            sample_minutes=round(mins, 1),
            sample_games=games_used,
        ))

    return out


def production_to_dict(prods: Optional[list[ProductionPeriod]]) -> Optional[list[dict]]:
    """Serializa pra dict (JSON cache friendly)."""
    if not prods:
        return None
    return [
        {
            "quarter": p.quarter,
            "points_per_min": p.points_per_min,
            "assists_per_min": p.assists_per_min,
            "rebounds_per_min": p.rebounds_per_min,
            "sample_minutes": p.sample_minutes,
            "sample_games": p.sample_games,
        }
        for p in prods
    ]


def production_from_dict(raw: Optional[list[dict]]) -> Optional[list[ProductionPeriod]]:
    if not raw:
        return None
    return [
        ProductionPeriod(
            quarter=d["quarter"],
            points_per_min=d.get("points_per_min", 0.0),
            assists_per_min=d.get("assists_per_min", 0.0),
            rebounds_per_min=d.get("rebounds_per_min", 0.0),
            sample_minutes=d.get("sample_minutes", 0.0),
            sample_games=d.get("sample_games", 0),
        )
        for d in raw
    ]


def lookup_rate(
    production_by_period: Optional[list[dict]],
    *,
    period: int,
    stat: str,
) -> Optional[float]:
    """
    Helper pra extrair rate de UM stat pra UM período do dict serializado.

    Args:
      production_by_period: lista de dicts (formato to_cache_dict)
      period: 1..4 (5+ retorna None — OT não tem perfil)
      stat: "points" | "assists" | "rebounds"

    Returns float ou None se sem dado.
    """
    if not production_by_period or period < 1 or period > 4:
        return None
    key_map = {
        "points": "points_per_min",
        "assists": "assists_per_min",
        "rebounds": "rebounds_per_min",
    }
    key = key_map.get(stat)
    if not key:
        return None
    for entry in production_by_period:
        if entry.get("quarter") == period:
            rate = entry.get(key, 0.0)
            return rate if rate > 0 else None
    return None
