"""
Anomaly detection engine for live NBA player stats.

Rules
-----
Bench players (minutes < 15)
  - Microwave scorer: ppm > 1.0 AND pts >= 5  → HIGH
  - Quick shooter:    3PM >= 2 AND minutes < 8 → HIGH

Star players (minutes >= 15)
  - Pts 30+                                    → EXTREME
  - Scoring pace: ppm > 0.8 AND pts >= 12      → HIGH
  - Assist machine: ast >= 10                  → HIGH
  - Assist rate:    ast/minutes > 0.35 AND
                    ast >= 5                   → MEDIUM
  - Rebound monster: reb >= 12                 → HIGH
  - Rebound rate:    reb/minutes > 0.4 AND
                     reb >= 6                  → MEDIUM
  - Steal spree:     stl >= 4                  → EXTREME

Specialists (any minutes)
  - 3PM >= 5                                   → HIGH
  - BLK >= 4                                   → EXTREME
  - Foul trouble: fouls == 5                   → EXTREME
                  fouls == 4                   → HIGH
                  fouls == 3                   → MEDIUM

Dedup: keep highest severity per (player_id, stat_type).
Sort:  severity desc (EXTREME > HIGH > MEDIUM > LOW), then value desc.
"""
from __future__ import annotations

import math
from typing import List

from src.schemas.anomaly_schemas import AnomalyPlayerStatsSchema, HotStatSchema

_SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "EXTREME": 3}

# Total regulation minutes (4 × 12)
_FULL_GAME_MINUTES = 48


def _pace(value: float, minutes: float) -> float:
    return round((value / max(minutes, 1)) * 36, 2)


def _projected(value: float, elapsed: int) -> float:
    clamped = max(5, min(elapsed, _FULL_GAME_MINUTES))
    progress = clamped / _FULL_GAME_MINUTES
    return round(value / progress, 1)


def _anomaly_score(value: float, minutes: float) -> float:
    """Simple score: pace per minute, scaled."""
    return round((value / max(minutes, 1)) * 10, 2)


def _alert(
    player: AnomalyPlayerStatsSchema,
    stat_type: str,
    value: float,
    severity: str,
    description: str,
) -> HotStatSchema:
    return HotStatSchema(
        player_id=player.player_id,
        player_name=player.player_name,
        team_abbr=player.team_abbr,
        stat_type=stat_type,  # type: ignore[arg-type]
        value=value,
        pace=_pace(value, player.minutes),
        projected_total=_projected(value, player.minute_of_game),
        anomaly_score=_anomaly_score(value, player.minutes),
        severity=severity,  # type: ignore[arg-type]
        description=description,
        minute_of_game=player.minute_of_game,
    )


class AnomalyService:
    """Stateless service — instantiate once and call detect() per tick."""

    def detect(self, players: List[AnomalyPlayerStatsSchema]) -> List[HotStatSchema]:
        raw: list[HotStatSchema] = []

        for p in players:
            mins = p.minutes
            pts = p.points
            reb = p.rebounds
            ast = p.assists
            stl = p.steals
            blk = p.blocks
            tpm = p.three_pointers_made
            fouls = p.fouls_personal or 0
            ppm = pts / max(mins, 1)

            # ── Bench rules (minutes < 15) ──────────────────────────────
            if mins < 15:
                if ppm > 1.0 and pts >= 5:
                    raw.append(_alert(
                        p, "PTS", pts, "HIGH",
                        f"{p.player_name} está explodindo no banco: "
                        f"{pts} pts em {mins:.1f} min ({ppm:.2f} ppm)",
                    ))
                if tpm >= 2 and mins < 8:
                    raw.append(_alert(
                        p, "3PM", tpm, "HIGH",
                        f"{p.player_name} com {tpm} triplos em menos de 8 min — tiro quente",
                    ))

            # ── Star rules (minutes >= 15) ───────────────────────────────
            else:
                if pts >= 30:
                    raw.append(_alert(
                        p, "PTS", pts, "EXTREME",
                        f"{p.player_name} com {pts} pontos — jogo histórico em andamento",
                    ))
                elif ppm > 0.8 and pts >= 12:
                    raw.append(_alert(
                        p, "PTS", pts, "HIGH",
                        f"{p.player_name} marcando em alto ritmo: {pts} pts ({ppm:.2f} ppm)",
                    ))

                if ast >= 10:
                    raw.append(_alert(
                        p, "AST", ast, "HIGH",
                        f"{p.player_name} caminhando para double-double em assistências: {ast} ast",
                    ))
                elif ast / max(mins, 1) > 0.35 and ast >= 5:
                    raw.append(_alert(
                        p, "AST", ast, "MEDIUM",
                        f"{p.player_name} distribuindo bem: {ast} ast em {mins:.1f} min",
                    ))

                if reb >= 12:
                    raw.append(_alert(
                        p, "REB", reb, "HIGH",
                        f"{p.player_name} dominando o garrafão: {reb} rebotes",
                    ))
                elif reb / max(mins, 1) > 0.4 and reb >= 6:
                    raw.append(_alert(
                        p, "REB", reb, "MEDIUM",
                        f"{p.player_name} reboteiro em alta: {reb} reb em {mins:.1f} min",
                    ))

                if stl >= 4:
                    raw.append(_alert(
                        p, "STL", stl, "EXTREME",
                        f"{p.player_name} com {stl} roubos de bola — pressão defensiva elite",
                    ))

            # ── Specialist rules (any minutes) ───────────────────────────
            if tpm >= 5:
                raw.append(_alert(
                    p, "3PM", tpm, "HIGH",
                    f"{p.player_name} é a chama de três: {tpm} triplos no jogo",
                ))

            if blk >= 4:
                raw.append(_alert(
                    p, "BLK", blk, "EXTREME",
                    f"{p.player_name} com {blk} tocos — guardião do garrafão",
                ))

            if fouls == 5:
                raw.append(_alert(
                    p, "FOUL", fouls, "EXTREME",
                    f"{p.player_name} com 5 faltas — próxima falta elimina do jogo",
                ))
            elif fouls == 4:
                raw.append(_alert(
                    p, "FOUL", fouls, "HIGH",
                    f"{p.player_name} com 4 faltas — em sério risco de eliminação",
                ))
            elif fouls == 3:
                raw.append(_alert(
                    p, "FOUL", fouls, "MEDIUM",
                    f"{p.player_name} com 3 faltas — atenção ao foul trouble",
                ))

        # ── Dedup: keep highest severity per (player_id, stat_type) ────
        best: dict[tuple[int, str], HotStatSchema] = {}
        for alert in raw:
            key = (alert.player_id, alert.stat_type)
            existing = best.get(key)
            if existing is None or (
                _SEVERITY_RANK[alert.severity] > _SEVERITY_RANK[existing.severity]
            ):
                best[key] = alert

        # ── Sort: severity desc, then value desc ────────────────────────
        return sorted(
            best.values(),
            key=lambda a: (_SEVERITY_RANK[a.severity], a.value),
            reverse=True,
        )
