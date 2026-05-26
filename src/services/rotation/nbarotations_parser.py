"""
Parser do HTML do nbarotations.info.

O dado vive embutido como JS inline:
    displayPlayer([{"date": "Nov 03", "gamecode": "...",
                    "histogram": [48 floats], ...}, ...]);

O array tem TODOS os jogos do jogador na carreira. Nosso uso filtra os
últimos N da temporada atual e agrega os 48 valores minuto-a-minuto.

Cada valor do histogram é a fração [0..1] do minuto que o jogador esteve
em quadra. `sum(histogram)` = total de minutos jogados naquele jogo.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GameRotationEntry:
    """Uma linha do array displayPlayer — rotação de UM jogo do jogador."""
    gamedate: str        # ISO YYYY-MM-DD
    gamecode: str        # NBA game_id
    opponent: str        # tricode (com '@' prefix se away)
    histogram: list[float]  # 48 valores, prob em quadra cada minuto


def parse_player_html(html: str) -> list[GameRotationEntry]:
    """
    Extrai o array de displayPlayer do HTML. Retorna [] se não encontrou
    ou JSON malformado. Nunca levanta.
    """
    marker = "displayPlayer("
    try:
        start_idx = html.index(marker) + len(marker)
    except ValueError:
        return []

    # Bracket-balance: encontra ']' que fecha o array externo
    depth = 0
    end_idx = -1
    i = start_idx
    n = len(html)
    while i < n:
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
        i += 1

    if end_idx < 0:
        return []

    raw = html[start_idx:end_idx]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.info("nbarotations parser: JSON invalido (%s)", exc)
        return []

    out: list[GameRotationEntry] = []
    for entry in data:
        try:
            out.append(
                GameRotationEntry(
                    gamedate=entry.get("gamedate", ""),
                    gamecode=entry.get("gamecode", ""),
                    opponent=entry.get("opponent", ""),
                    histogram=list(entry.get("histogram") or []),
                )
            )
        except (TypeError, AttributeError):
            continue

    return out


def aggregate_recent_games(
    games: list[GameRotationEntry],
    *,
    season_start: str,    # ISO YYYY-MM-DD ex "2025-10-01"
    last_n: int = 10,
    histogram_size: int = 48,
) -> Optional[dict]:
    """
    Agrega últimos N jogos da temporada num perfil de rotação.

    Returns dict ou None se não há sample suficiente:
      {
        "minute_probabilities": [48 floats],  # p(em quadra) por minuto do jogo
        "total_minutes_avg": float,            # média de minutos por jogo
        "sample_games": int,                   # quantos jogos agregados
      }

    Filtra:
      - jogos da temporada atual (gamedate >= season_start)
      - apenas os `last_n` mais recentes
      - precisa de >= 3 jogos pra considerar profile válido
    """
    season_games = [g for g in games if g.gamedate >= season_start]
    if not season_games:
        return None

    # Ordena por data ASC (já costuma estar, mas garantia)
    season_games.sort(key=lambda g: g.gamedate)
    recent = season_games[-last_n:]

    # Filtra entradas com histogram completo
    valid = [g for g in recent if len(g.histogram) >= histogram_size]
    if len(valid) < 3:
        # Amostra muito pequena — não confiar no perfil
        return None

    # Agregação minuto-a-minuto
    avg_hist: list[float] = []
    for minute_idx in range(histogram_size):
        values = [g.histogram[minute_idx] for g in valid]
        avg_hist.append(round(statistics.mean(values), 4))

    total_minutes = [sum(g.histogram[:histogram_size]) for g in valid]
    avg_total = round(statistics.mean(total_minutes), 2)

    return {
        "minute_probabilities": avg_hist,
        "total_minutes_avg": avg_total,
        "sample_games": len(valid),
    }
