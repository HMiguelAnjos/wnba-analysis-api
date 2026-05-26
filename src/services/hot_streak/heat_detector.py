"""
HeatDetector — score composto de "jogador quente / frio".

Combina sinais ortogonais (eficiência, volume, pressão no aro, scoring
run) num número ∈ [-1, +1]. Calibrado pra ser conservador: só dá heat
extremo quando vários sinais concordam.

Por que não usar só o `score` (pts_diff + reb_diff + ...)?
  O score mede desvio numérico vs esperado, mas não distingue:
   - Cara fazendo +5 pts porque pegou 12 FGA (volume alto)
   - Cara fazendo +5 pts em 5 FGA (eficiência absurda — sinal mais forte)

  HeatDetector pega esse contexto e converte em "qualidade do streak",
  algo que oddsmakers usam intuitivamente quando reativam linha ao vivo.

Plug:
  - ProjectionEngine: heat > threshold → boost na produção restante
  - Frontend: pill 🔥 ou 🥶 visível pro user
  - Logging: parte do log_meta da Fase 6
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class HeatSignal:
    """
    Output composto + breakdown pra debug/UI.

    Item 1 (mai/2026): além do `score` composto (compat), expomos sinais
    POR STAT — scoring/playmaking/rebounding. Esse split evita o bug de
    "cara com eFG estourado leva boost em REB/AST" que existia antes.
    Cada um ∈ [-1, +1], independente.
    """
    score: float                # -1..+1 (composto, compat)
    label: str                  # "very_hot" | "hot" | "neutral" | "cold" | "very_cold"
    components: dict[str, float]  # cada sinal individual
    reason: str                 # explicação curta
    # Stat-specific (item 1, mai/2026)
    scoring: float = 0.0        # eFG + FTA + volume + shot quality
    playmaking: float = 0.0     # AST/min vs season + low TOV
    rebounding: float = 0.0     # REB/min vs season

    @property
    def is_hot(self) -> bool:
        return self.score >= 0.6

    @property
    def is_cold(self) -> bool:
        return self.score <= -0.4

    def for_stat(self, stat: str) -> float:
        """Retorna o sub-score apropriado pra cada market."""
        if stat == "points":
            return self.scoring
        if stat == "assists":
            return self.playmaking
        if stat == "rebounds":
            return self.rebounding
        if stat == "three_pm":
            # 3PM herda do scoring (mesma natureza de tiro)
            return self.scoring
        return self.score


# Pesos dos sinais (somam ~1.0 — calibração inicial conservadora,
# refinar com dataset histórico depois).
_W_EFG       = 0.30   # eficiência é o sinal mais limpo
_W_FTA_RATE  = 0.20   # pressão no aro = agressividade
_W_VOLUME    = 0.20   # FGA/min acima da média
_W_SCORING_RUN = 0.20 # consecutivas com pontos (nem sempre disponível)
_W_SHOT_QUALITY = 0.10  # rate de make nos últimos shots (proxy)

# Mínimo de minutos pra ter sinal confiável. Abaixo disso = neutral.
MIN_MINUTES_FOR_HEAT = 6.0


class HeatDetector:
    """
    Stateless. Cada chamada de `score()` é independente.

    Inputs vêm do `LivePlayerStatsSchema` + médias de temporada.
    Quando faltam dados (early game, pre-game), retorna neutro com
    label "neutral" e reason explicativa.
    """

    def score(
        self,
        *,
        # Live state
        minutes_played: float,
        current_points: float,
        current_fga: int,
        current_fgm: int,
        current_3pm: int,
        current_fta: int,
        current_ftm: int,
        # Season averages (pra normalizar)
        season_minutes: float,
        season_fga_per_min: Optional[float] = None,
        season_efg: Optional[float] = None,
        season_fta_per_min: Optional[float] = None,
        # Item 1 (mai/2026): per-stat heat — inputs adicionais
        current_assists: int = 0,
        current_rebounds: int = 0,
        current_turnovers: int = 0,
        season_ast_per_min: Optional[float] = None,
        season_reb_per_min: Optional[float] = None,
        # Optional: scoring run from PBP
        scoring_run_streak: int = 0,    # # de possessions consecutivas com pts
    ) -> HeatSignal:
        """Calcula heat composto."""
        components: dict[str, float] = {}

        # Gate de amostra: amostra pequena = sem sinal
        if minutes_played < MIN_MINUTES_FOR_HEAT:
            return HeatSignal(
                score=0.0,
                label="neutral",
                components={"sample_factor": 0.0},
                reason=f"amostra pequena ({minutes_played:.1f}min, mínimo {MIN_MINUTES_FOR_HEAT})",
                scoring=0.0,
                playmaking=0.0,
                rebounding=0.0,
            )

        # ── Sinal 1: eFG delta ──────────────────────────────────────────
        # Diferença de eFG% atual vs temporada. Range típico: ±0.20.
        # Normaliza pra [-1, +1] dividindo por 0.15 (1 std-dev típica).
        efg_signal = 0.0
        if current_fga >= 4 and season_efg is not None and season_efg > 0:
            current_efg = (current_fgm + 0.5 * current_3pm) / current_fga
            efg_delta = current_efg - season_efg
            efg_signal = max(-1.0, min(1.0, efg_delta / 0.15))
        components["efg"] = round(efg_signal, 3)

        # ── Sinal 2: FTA rate (pressão no aro) ──────────────────────────
        # FTA/min vs temporada. >1.5x = atacando agressivo (sinal de chama).
        fta_signal = 0.0
        if season_fta_per_min is not None and season_fta_per_min > 0:
            current_fta_per_min = current_fta / minutes_played
            fta_ratio = current_fta_per_min / season_fta_per_min
            # Mapeia 0.5×→-1, 1.0×→0, 2.0×→+1, cap nos extremos
            fta_signal = max(-1.0, min(1.0, (fta_ratio - 1.0) / 1.0))
        components["fta_rate"] = round(fta_signal, 3)

        # ── Sinal 3: volume (FGA/min) ───────────────────────────────────
        volume_signal = 0.0
        if season_fga_per_min is not None and season_fga_per_min > 0:
            current_fga_per_min = current_fga / minutes_played
            volume_ratio = current_fga_per_min / season_fga_per_min
            volume_signal = max(-1.0, min(1.0, (volume_ratio - 1.0) / 0.5))
        components["volume"] = round(volume_signal, 3)

        # ── Sinal 4: scoring run ────────────────────────────────────────
        # Sequência de possessions com pontos. 3+ é raro = chama.
        # 0 ou 1 = neutro.
        scoring_run_signal = 0.0
        if scoring_run_streak >= 4:
            scoring_run_signal = 1.0
        elif scoring_run_streak >= 3:
            scoring_run_signal = 0.6
        elif scoring_run_streak >= 2:
            scoring_run_signal = 0.3
        components["scoring_run"] = round(scoring_run_signal, 3)

        # ── Sinal 5: shot quality (rate de make) ────────────────────────
        # Make rate atual no jogo. Alto = sinal forte de chama.
        # Diferente do eFG porque não pondera 3PT.
        shot_quality_signal = 0.0
        if current_fga >= 3:
            make_rate = current_fgm / current_fga
            # 50% = neutral. 70%+ = quente, 30%- = frio.
            shot_quality_signal = max(-1.0, min(1.0, (make_rate - 0.5) / 0.20))
        components["shot_quality"] = round(shot_quality_signal, 3)

        # ── Composição final ────────────────────────────────────────────
        weighted = (
            _W_EFG          * efg_signal
            + _W_FTA_RATE   * fta_signal
            + _W_VOLUME     * volume_signal
            + _W_SCORING_RUN * scoring_run_signal
            + _W_SHOT_QUALITY * shot_quality_signal
        )
        # Cap final
        final = max(-1.0, min(1.0, weighted))

        # Label
        if final >= 0.75:
            label = "very_hot"
        elif final >= 0.6:
            label = "hot"
        elif final <= -0.6:
            label = "very_cold"
        elif final <= -0.4:
            label = "cold"
        else:
            label = "neutral"

        # Reason — sinal dominante
        sorted_signals = sorted(
            ((k, v) for k, v in components.items() if k != "sample_factor"),
            key=lambda kv: abs(kv[1]),
            reverse=True,
        )
        if sorted_signals and abs(sorted_signals[0][1]) >= 0.4:
            top_name, top_value = sorted_signals[0]
            direction = "alto" if top_value > 0 else "baixo"
            reason = f"{top_name} {direction} ({top_value:+.2f}) dominou o sinal"
        else:
            reason = "sinais distribuídos — sem dominância clara"

        # ── Per-stat sub-scores (item 1, mai/2026) ──────────────────────
        # SCORING: combina eFG + FTA + volume + shot_quality. O composto
        # já é dominado por sinais de tiro, então scoring ≈ score sem
        # scoring_run (que é mais conjuntural que stat-specific).
        scoring_signal = max(-1.0, min(1.0,
            (_W_EFG * efg_signal
             + _W_FTA_RATE * fta_signal
             + _W_VOLUME * volume_signal
             + _W_SHOT_QUALITY * shot_quality_signal)
            / (_W_EFG + _W_FTA_RATE + _W_VOLUME + _W_SHOT_QUALITY)
        ))

        # PLAYMAKING: AST/min vs season - TOV penalty.
        # Cara com 7 ast e 1 TOV em 18min cooking; 4 ast com 5 TOV não.
        playmaking_signal = 0.0
        if season_ast_per_min is not None and season_ast_per_min > 0:
            current_ast_per_min = current_assists / minutes_played
            ast_ratio = current_ast_per_min / season_ast_per_min
            # Mapeia 0.6×→-0.7, 1.0×→0, 1.6×→+0.7, cap nos extremos
            playmaking_signal = max(-1.0, min(1.0, (ast_ratio - 1.0) / 0.6))
            # TOV penalty: 1 TOV = -0.05, cap em -0.30
            tov_penalty = min(current_turnovers * 0.05, 0.30)
            playmaking_signal = max(-1.0, playmaking_signal - tov_penalty)

        # REBOUNDING: REB/min vs season. Sinal mais limpo (menos contexto).
        rebounding_signal = 0.0
        if season_reb_per_min is not None and season_reb_per_min > 0:
            current_reb_per_min = current_rebounds / minutes_played
            reb_ratio = current_reb_per_min / season_reb_per_min
            rebounding_signal = max(-1.0, min(1.0, (reb_ratio - 1.0) / 0.5))

        return HeatSignal(
            score=round(final, 3),
            label=label,
            components=components,
            reason=reason,
            scoring=round(scoring_signal, 3),
            playmaking=round(playmaking_signal, 3),
            rebounding=round(rebounding_signal, 3),
        )
