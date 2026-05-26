"""
HistoricalBacktester — roda o motor de projeção em jogos passados e
mede accuracy contra resultado final.

Diferente do BackTester clássico (que lê `line_log.jsonl` do live system),
esse rebobina jogos completos e simula o motor "como se estivéssemos
projetando no Q3 do jogo X de 30 dias atrás".

Métricas que produz:
  - **MAE** (Mean Absolute Error): média de |projeção - final| em pts
  - **Hit rate por decisão**: dos cards que viraram STRONG_OVER, quantos %
    o jogador realmente terminou acima da linha?
  - **Calibração**: cara projetado pra terminar com X, em média terminou
    com quanto?
  - **% caps disparando**: que % das projeções tiveram soft_floor /
    in_deficit_cap / unexpected_rest_cap aplicado?

Saída como dataclass + função `format_report()` que retorna markdown
legível pra terminal.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.services.backtester.historical_loader import HistoricalLoader, GameMeta
from src.services.backtester.snapshot import (
    extract_final_stats,
    reconstruct_snapshot,
)
from src.services.projection import ProjectionEngine
from src.utils.stats import (
    calculate_edge_decision,
    dampen_decision_for_low_sample,
)

logger = logging.getLogger(__name__)


# ─── Tipos de saída ──────────────────────────────────────────────────────────


@dataclass
class ProjectionVsActual:
    """Uma única projeção (player × stat × snapshot) com resultado real."""
    game_id: str
    game_date: str
    player_id: int
    player_name: str
    stat: str                     # "PTS" | "REB" | "AST"
    snapshot_period: int
    snapshot_clock_min: float
    minutes_at_snapshot: float
    current_at_snapshot: float
    prior_avg: float
    projection: float
    actual_final: float
    error: float                  # projection - actual
    abs_error: float              # |projection - actual|
    # Caps disparados (do breakdown)
    soft_floor_applied: bool = False
    in_deficit_cap_applied: bool = False
    sanity_cap_applied: bool = False
    unexpected_rest_cap_applied: bool = False
    # Pra decisão hit rate: precisa de uma linha. Aqui usamos a linha
    # sintética estimada (= prior_avg como proxy simples) já que não temos
    # historical Bet365 odds.
    synthetic_line: float = 0.0
    edge_synthetic: float = 0.0
    decision_synthetic: str = "NEUTRAL"
    # Did the player actually beat the line?
    over_won: Optional[bool] = None      # final > line
    # P-final (variance penalty): tier do bet_recommendation
    # (PASS/SMALL/MEDIUM/LARGE). Permite medir efeito da variance
    # penalty (que afeta tier, não edge/decision).
    bet_tier: str = "PASS"
    variance_factor: float = 1.0
    # Mai/2026 v3: rastrear modifiers do engine que podem influenciar
    # projeção (pra investigar viés de +1.0 em LARGE). Cada bool indica
    # se o modifier disparou nesse projection.
    heat_boost_applied: bool = False    # heat_score >= 0.30 + future_value>0
    heat_cut_applied: bool = False      # heat_score <= -0.30
    hot_shrinkage_applied: bool = False # minutes < threshold + hot ratio
    rate_decay_applied: bool = False    # archetype excess_minutes > 0
    heat_multiplier_value: float = 1.0  # valor exato aplicado


@dataclass
class BacktestMetrics:
    """Agregado de todas as projeções."""
    total_projections: int = 0
    by_stat: dict[str, dict] = field(default_factory=dict)
    by_decision: dict[str, dict] = field(default_factory=dict)
    caps_fired: dict[str, int] = field(default_factory=dict)
    # Mai/2026: MAE por estado de cap. Permite saber se um cap especifico
    # ESTÁ AJUDANDO ou ATRAPALHANDO. Estrutura:
    #   {cap_name: {"fired_mae": x, "fired_n": n, "not_fired_mae": y, "not_fired_n": m,
    #               "fired_signed_err": s, "fired_signed_n": ...}}
    # Erro com SINAL: positivo = projeção > final (super-estimou),
    #                negativo = projeção < final (sub-estimou).
    # Combinado com MAE diz: cap atrapalha pra cima (corta demais) ou
    # pra baixo (não corta o suficiente).
    caps_mae: dict[str, dict] = field(default_factory=dict)
    # P-final: distribuição por tier (PASS/SMALL/MEDIUM/LARGE) com hit rate.
    # Permite medir efeito da variance penalty (tier varia, decisão não).
    by_tier: dict[str, dict] = field(default_factory=dict)
    # Mai/2026: tier × stat. Pergunta: REB e AST estão tão bem calibrados
    # quanto PTS? Estrutura: {tier: {stat: {wins, losses, pushes, n}}}.
    # Espera-se hit rate monotônico DENTRO de cada stat (LARGE > MEDIUM > SMALL).
    # Se um stat ficar muito atrás (ex: LARGE AST 75% vs LARGE PTS 95%),
    # é sinal de que precisa calibração específica.
    by_tier_stat: dict[str, dict[str, dict]] = field(default_factory=dict)
    # Mai/2026: MAE por stat × prior bucket. Quantifica se o engine erra
    # mais em jogadores low-volume (prior <5) vs high-volume.
    by_stat_prior_bucket: dict[str, dict[str, dict]] = field(default_factory=dict)
    # Mai/2026 v3: signed error por tier × modifier. Pra identificar
    # qual modifier do engine está contribuindo pro viés de +1.0 em LARGE.
    # Estrutura: {tier: {modifier: {fired_signed, fired_n, off_signed, off_n}}}
    tier_modifier_signed: dict[str, dict[str, dict]] = field(default_factory=dict)
    samples: list[ProjectionVsActual] = field(default_factory=list)


# ─── HistoricalBacktester ────────────────────────────────────────────────────


class HistoricalBacktester:
    """
    Stateless. Use:
        bt = HistoricalBacktester()
        metrics = bt.run(
            game_ids=["0022400123", "0022400124"],
            snapshot_period=3,
            snapshot_clock_min=0.0,  # final do Q3
        )
        print(format_report(metrics))
    """

    def __init__(
        self,
        loader: Optional[HistoricalLoader] = None,
        engine: Optional[ProjectionEngine] = None,
    ) -> None:
        self.loader = loader or HistoricalLoader()
        self.engine = engine or ProjectionEngine()

    def run(
        self,
        game_ids: list[str],
        *,
        snapshot_period: int = 3,
        snapshot_clock_min: float = 0.0,
        min_minutes_at_snapshot: float = 6.0,
    ) -> BacktestMetrics:
        """
        Roda backtest em N jogos. Pra cada jogo:
          1. Carrega PBP + boxscore + game meta
          2. Reconstrói estado dos jogadores no snapshot
          3. Pra cada jogador (>= min_minutes_at_snapshot), roda o motor
          4. Coleta projection vs actual
          5. Agrega métricas

        snapshot_period + clock define quando "olharíamos" o jogo.
        Default: final do Q3 (clock=0, period=3) → projeta o Q4 inteiro.
        """
        metrics = BacktestMetrics()
        metrics.by_stat = defaultdict(lambda: {"mae": 0.0, "n": 0, "total_err": 0.0})
        metrics.by_decision = defaultdict(lambda: {
            "wins": 0, "losses": 0, "pushes": 0, "n": 0,
        })
        metrics.caps_fired = defaultdict(int)

        for game_id in game_ids:
            try:
                results = self._run_one_game(
                    game_id,
                    snapshot_period=snapshot_period,
                    snapshot_clock_min=snapshot_clock_min,
                    min_minutes_at_snapshot=min_minutes_at_snapshot,
                )
            except Exception as exc:
                logger.warning("Backtest falhou pra %s: %s", game_id, exc)
                continue
            for r in results:
                self._aggregate_one(metrics, r)

        # Finaliza médias
        for stat, bucket in metrics.by_stat.items():
            if bucket["n"] > 0:
                bucket["mae"] = round(bucket["total_err"] / bucket["n"], 3)
            bucket.pop("total_err", None)

        return metrics

    def _run_one_game(
        self,
        game_id: str,
        snapshot_period: int,
        snapshot_clock_min: float,
        min_minutes_at_snapshot: float,
    ) -> list[ProjectionVsActual]:
        actions = self.loader.load_pbp(game_id)
        boxscore = self.loader.load_boxscore(game_id)
        if not actions or not boxscore:
            return []

        # Game meta (data, season) — pega do índice se já cacheado
        game_date = boxscore.get("gameTimeUTC", "")[:10] or "unknown"

        snap = reconstruct_snapshot(
            actions, boxscore, snapshot_period, snapshot_clock_min,
        )
        finals = extract_final_stats(boxscore)

        # Determine season from game_id prefix (NBA convention)
        # game_id "0022500xxx" = 2025-26 regular season
        # game_id "0042500xxx" = 2025-26 playoffs
        season = self._infer_season_from_game_id(game_id)

        results: list[ProjectionVsActual] = []
        for pid, ps in snap.players.items():
            if ps.minutes_played < min_minutes_at_snapshot:
                continue
            final = finals.get(pid)
            if final is None or final["minutes"] == 0:
                continue

            # Carrega gamelog do jogador pra computar prior AS OF game_date.
            # Skip se sem gamelog (jogador novo, lesão prolongada, etc.).
            gamelog = self.loader.load_player_gamelog(pid, season)
            if not gamelog:
                continue

            prior_stats = _compute_prior_as_of(gamelog, before_date=game_date)
            if prior_stats is None or prior_stats["games_played"] < 3:
                # Sample insuficiente — não dá pra projetar com confiança
                continue

            # Mai/2026: enriquecer com heat_score, rest_days e variance.
            heat = _compute_heat_score(ps, prior_stats)
            rest_days = _compute_rest_days(prior_stats, game_date)
            variances = _compute_variance_factors(gamelog, before_date=game_date)

            # Roda projeção pra cada stat
            for stat_label, current_val, prior_val, final_val in [
                ("PTS", ps.points, prior_stats["pts_avg"], final["points"]),
                ("REB", ps.rebounds, prior_stats["reb_avg"], final["rebounds"]),
                ("AST", ps.assists, prior_stats["ast_avg"], final["assists"]),
            ]:
                if prior_val < 1.0:
                    continue  # cara não produz nessa stat, skip
                # Heat per-stat (scoring pra pts, rebounding pra reb, etc.)
                stat_heat = heat.get(stat_label, 0.0)
                # Variance per-stat
                stat_variance = variances.get(stat_label, 1.0)
                # Mai/2026 v2: shrinkage threshold por stat (AST mais
                # ruidoso → threshold maior). Espelha live_analysis_service.
                stat_shrinkage_threshold = (
                    14.0 if stat_label == "AST"
                    else 10.0 if stat_label == "REB"
                    else 8.0
                )
                proj_dict = self.engine.project(
                    stat=current_val,
                    minutes=ps.minutes_played,
                    avg_stat=prior_val,
                    avg_minutes=prior_stats["min_avg"],
                    fouls=ps.fouls,
                    period=snapshot_period,
                    game_minutes_remaining=(4 - snapshot_period) * 12.0 + snapshot_clock_min,
                    is_final=False,
                    is_starter=ps.is_starter,
                    last_10_avg=prior_stats["pts_avg"] if stat_label == "PTS"
                                else prior_stats["reb_avg"] if stat_label == "REB"
                                else prior_stats["ast_avg"],
                    last_5_avg=prior_stats["pts_avg"] if stat_label == "PTS"
                               else prior_stats["reb_avg"] if stat_label == "REB"
                               else prior_stats["ast_avg"],
                    heat_score=stat_heat,
                    rest_days=rest_days,
                    variance_factor=stat_variance,
                    shrinkage_min_threshold=stat_shrinkage_threshold,
                )
                # Proxy de linha sintética = prior_avg (book bate perto disso)
                synthetic_line = round(prior_val * 2) / 2  # arredonda pra .0/.5
                edge = round(proj_dict["expected"] - synthetic_line, 1)
                decision = calculate_edge_decision(edge)
                # Mai/2026: espelha o dampener do live (_fair_line) —
                # sample pequeno rebaixa STRONG→LEAN (6-10min) ou
                # →NEUTRAL (<6min). Sem isso o backtester não refletiria
                # o comportamento real de produção.
                decision = dampen_decision_for_low_sample(
                    decision, ps.minutes_played
                )

                error = proj_dict["expected"] - final_val
                bd = proj_dict.get("breakdown", {}) or {}

                # Did the OVER bet win? final > line
                over_won = (
                    final_val > synthetic_line if "OVER" in decision
                    else final_val < synthetic_line if "UNDER" in decision
                    else None
                )

                # P-final: calcula bet_tier (PASS/SMALL/MEDIUM/LARGE).
                # Backtester sintetiza betting_confidence + usage pra
                # poder chamar bet_recommendation e medir o tier.
                bet_tier = _compute_bet_tier(
                    edge=edge,
                    proj_dict=proj_dict,
                    prior_stats=prior_stats,
                    snapshot_minutes=ps.minutes_played,
                    variance_factor=stat_variance,
                    stat_heat=stat_heat,
                )

                # Mai/2026 v3: extrai estado dos modifiers do breakdown
                heat_mult = bd.get("heat_multiplier")
                results.append(ProjectionVsActual(
                    game_id=game_id,
                    game_date=game_date,
                    player_id=pid,
                    player_name=ps.name,
                    stat=stat_label,
                    snapshot_period=snapshot_period,
                    snapshot_clock_min=snapshot_clock_min,
                    minutes_at_snapshot=ps.minutes_played,
                    current_at_snapshot=current_val,
                    prior_avg=prior_val,
                    projection=proj_dict["expected"],
                    actual_final=final_val,
                    error=round(error, 2),
                    abs_error=round(abs(error), 2),
                    soft_floor_applied="soft_floor_applied" in bd,
                    in_deficit_cap_applied="in_deficit_cap_applied" in bd,
                    sanity_cap_applied=bool(bd.get("sanity_cap_applied")),
                    unexpected_rest_cap_applied=bool(bd.get("unexpected_rest_cap_applied")),
                    synthetic_line=synthetic_line,
                    edge_synthetic=edge,
                    decision_synthetic=decision,
                    over_won=over_won,
                    bet_tier=bet_tier,
                    variance_factor=stat_variance,
                    heat_boost_applied=(heat_mult is not None and heat_mult > 1.0),
                    heat_cut_applied=(heat_mult is not None and heat_mult < 1.0),
                    hot_shrinkage_applied="hot_start_shrinkage_applied" in bd,
                    rate_decay_applied="rate_decay_applied" in bd,
                    heat_multiplier_value=float(heat_mult) if heat_mult is not None else 1.0,
                ))

        return results

    def _aggregate_one(self, metrics: BacktestMetrics, r: ProjectionVsActual) -> None:
        metrics.total_projections += 1
        # By stat: MAE (usa setdefault pra tolerar dicts não-defaultdict)
        b = metrics.by_stat.setdefault(
            r.stat, {"mae": 0.0, "n": 0, "total_err": 0.0},
        )
        b["n"] += 1
        b["total_err"] += r.abs_error
        # By decision: hit rate
        d = metrics.by_decision.setdefault(
            r.decision_synthetic,
            {"wins": 0, "losses": 0, "pushes": 0, "n": 0},
        )
        d["n"] += 1
        if r.over_won is True:
            d["wins"] += 1
        elif r.over_won is False:
            d["losses"] += 1
        else:
            d["pushes"] += 1
        # Caps fired + MAE por cap (helping vs hurting)
        for cap_name, fired in (
            ("soft_floor", r.soft_floor_applied),
            ("in_deficit_cap", r.in_deficit_cap_applied),
            ("sanity_cap", r.sanity_cap_applied),
            ("unexpected_rest_cap", r.unexpected_rest_cap_applied),
        ):
            if fired:
                metrics.caps_fired[cap_name] = metrics.caps_fired.get(cap_name, 0) + 1
            # MAE + erro signed por cap state (fired vs not_fired)
            cap_bucket = metrics.caps_mae.setdefault(cap_name, {
                "fired_total_err": 0.0, "fired_n": 0, "fired_signed_err": 0.0,
                "not_fired_total_err": 0.0, "not_fired_n": 0, "not_fired_signed_err": 0.0,
            })
            if fired:
                cap_bucket["fired_total_err"] += r.abs_error
                cap_bucket["fired_signed_err"] += r.error  # proj - actual
                cap_bucket["fired_n"] += 1
            else:
                cap_bucket["not_fired_total_err"] += r.abs_error
                cap_bucket["not_fired_signed_err"] += r.error
                cap_bucket["not_fired_n"] += 1
        # By tier (variance penalty effect)
        t = metrics.by_tier.setdefault(
            r.bet_tier,
            {"wins": 0, "losses": 0, "pushes": 0, "n": 0},
        )
        t["n"] += 1
        if r.over_won is True:
            t["wins"] += 1
        elif r.over_won is False:
            t["losses"] += 1
        else:
            t["pushes"] += 1
        # By tier × stat (pergunta: REB/AST tão calibrados quanto PTS?)
        tier_bucket = metrics.by_tier_stat.setdefault(r.bet_tier, {})
        ts = tier_bucket.setdefault(
            r.stat,
            {"wins": 0, "losses": 0, "pushes": 0, "n": 0,
             "total_abs_err": 0.0, "total_signed_err": 0.0},
        )
        ts["n"] += 1
        ts["total_abs_err"] += r.abs_error
        ts["total_signed_err"] += r.error
        if r.over_won is True:
            ts["wins"] += 1
        elif r.over_won is False:
            ts["losses"] += 1
        else:
            ts["pushes"] += 1
        # Tier × modifier signed err (investigar fonte do viés)
        tier_mod_bucket = metrics.tier_modifier_signed.setdefault(r.bet_tier, {})
        for mod_name, fired in (
            ("heat_boost", r.heat_boost_applied),
            ("heat_cut", r.heat_cut_applied),
            ("hot_shrinkage", r.hot_shrinkage_applied),
            ("rate_decay", r.rate_decay_applied),
            ("soft_floor", r.soft_floor_applied),
            ("in_deficit_cap", r.in_deficit_cap_applied),
        ):
            mod = tier_mod_bucket.setdefault(
                mod_name,
                {"fired_signed": 0.0, "fired_n": 0,
                 "off_signed": 0.0, "off_n": 0},
            )
            if fired:
                mod["fired_signed"] += r.error
                mod["fired_n"] += 1
            else:
                mod["off_signed"] += r.error
                mod["off_n"] += 1
        # By stat × prior bucket (low/mid/high volume calibration)
        if r.prior_avg < 5:
            bucket_name = "low (<5)"
        elif r.prior_avg < 15:
            bucket_name = "mid (5-15)"
        else:
            bucket_name = "high (15+)"
        sb = metrics.by_stat_prior_bucket.setdefault(r.stat, {})
        sbb = sb.setdefault(
            bucket_name,
            {"n": 0, "total_abs_err": 0.0, "total_signed_err": 0.0},
        )
        sbb["n"] += 1
        sbb["total_abs_err"] += r.abs_error
        sbb["total_signed_err"] += r.error
        # Sample
        if len(metrics.samples) < 30:
            metrics.samples.append(r)

    @staticmethod
    def _infer_season_from_game_id(game_id: str) -> str:
        """
        NBA game_id convention: "00X YY NNNN"
          - X: regular(2)/playoff(4)/etc.
          - YY: 2 últimos dígitos do ano de INÍCIO da temporada
        Ex: "0022500001" → temporada 2025-26.
        """
        if len(game_id) < 5:
            return "2025-26"  # fallback
        try:
            yy = int(game_id[3:5])
            start_year = 2000 + yy
            return f"{start_year}-{str(start_year + 1)[-2:]}"
        except ValueError:
            return "2025-26"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _compute_prior_as_of(
    gamelog: list[dict],
    *,
    before_date: str,
) -> Optional[dict]:
    """
    Computa season_avg / L10 / L5 / min_avg usando jogos do gamelog
    estritamente ANTES de `before_date`.

    gamelog: lista de dicts do nba_api playergamelog. Cada item tem
             GAME_DATE em formato "MMM DD, YYYY" (ex: "MAR 15, 2026")
             ou similar — converte pra "YYYY-MM-DD" pra comparar.
    before_date: "YYYY-MM-DD" (data do jogo do snapshot)
    """
    eligible = []
    for row in gamelog:
        d = _parse_gamelog_date(str(row.get("GAME_DATE", "")))
        if d and d < before_date:
            eligible.append(row)
    if not eligible:
        return None

    # Sort por data desc (mais recente primeiro) pra pegar L10/L5
    eligible.sort(key=lambda r: _parse_gamelog_date(str(r.get("GAME_DATE", ""))) or "", reverse=True)
    last_5 = eligible[:5]
    last_10 = eligible[:10]

    def _avg(rows: list[dict], key: str) -> float:
        vals = [float(r.get(key, 0) or 0) for r in rows]
        return sum(vals) / len(vals) if vals else 0.0

    # Computa rates POR MINUTO da temporada (necessários pro HeatDetector
    # no backtester — mai/2026 expansão).
    total_min = sum(float(r.get("MIN", 0) or 0) for r in eligible)
    total_fga = sum(float(r.get("FGA", 0) or 0) for r in eligible)
    total_fgm = sum(float(r.get("FGM", 0) or 0) for r in eligible)
    total_3pm = sum(float(r.get("FG3M", 0) or 0) for r in eligible)
    total_fta = sum(float(r.get("FTA", 0) or 0) for r in eligible)
    total_ftm = sum(float(r.get("FTM", 0) or 0) for r in eligible)
    total_ast = sum(float(r.get("AST", 0) or 0) for r in eligible)
    total_reb = sum(float(r.get("REB", 0) or 0) for r in eligible)

    season_fga_per_min = total_fga / total_min if total_min > 0 else None
    season_efg = (
        (total_fgm + 0.5 * total_3pm) / total_fga
        if total_fga > 0 else None
    )
    season_fta_per_min = total_fta / total_min if total_min > 0 else None
    season_ast_per_min = total_ast / total_min if total_min > 0 else None
    season_reb_per_min = total_reb / total_min if total_min > 0 else None

    # Data do último jogo (pra calcular rest_days)
    last_game_date = _parse_gamelog_date(
        str(eligible[0].get("GAME_DATE", ""))
    ) if eligible else None

    return {
        "games_played": len(eligible),
        "pts_avg": _avg(eligible, "PTS"),
        "reb_avg": _avg(eligible, "REB"),
        "ast_avg": _avg(eligible, "AST"),
        "min_avg": _avg(eligible, "MIN"),
        "l5_pts": _avg(last_5, "PTS"),
        "l10_pts": _avg(last_10, "PTS"),
        # Rates pra HeatDetector
        "season_fga_per_min": season_fga_per_min,
        "season_efg": season_efg,
        "season_fta_per_min": season_fta_per_min,
        "season_ast_per_min": season_ast_per_min,
        "season_reb_per_min": season_reb_per_min,
        # Última data (pra rest_days)
        "last_game_date": last_game_date,
    }


def _compute_heat_score(
    ps,
    prior_stats: dict,
) -> dict[str, float]:
    """
    Roda HeatDetector com o estado do snapshot + médias da temporada.
    Retorna dict {stat_label: heat_score} pra cada mercado (PTS/REB/AST).

    Retorna 0 (neutro) se HeatDetector falhar ou sample for muito pequeno.
    """
    from src.services.hot_streak.heat_detector import HeatDetector

    detector = HeatDetector()
    try:
        signal = detector.score(
            minutes_played=ps.minutes_played,
            current_points=ps.points,
            current_fga=ps.field_goals_attempted,
            current_fgm=ps.field_goals_made,
            current_3pm=ps.three_pointers_made,
            current_fta=ps.free_throws_attempted,
            current_ftm=ps.free_throws_made,
            season_minutes=prior_stats.get("min_avg", 0) * prior_stats.get("games_played", 1),
            season_fga_per_min=prior_stats.get("season_fga_per_min"),
            season_efg=prior_stats.get("season_efg"),
            season_fta_per_min=prior_stats.get("season_fta_per_min"),
            current_assists=ps.assists,
            current_rebounds=ps.rebounds,
            current_turnovers=0,  # snapshot não rastreia TOV
            season_ast_per_min=prior_stats.get("season_ast_per_min"),
            season_reb_per_min=prior_stats.get("season_reb_per_min"),
        )
        return {
            "PTS": signal.for_stat("points"),
            "REB": signal.for_stat("rebounds"),
            "AST": signal.for_stat("assists"),
        }
    except Exception:
        return {"PTS": 0.0, "REB": 0.0, "AST": 0.0}


def _compute_bet_tier(
    *,
    edge: float,
    proj_dict: dict,
    prior_stats: dict,
    snapshot_minutes: float,
    variance_factor: float,
    stat_heat: float,
) -> str:
    """
    Sintetiza inputs e chama bet_recommendation pra obter o tier
    (PASS/SMALL/MEDIUM/LARGE). Backtester não tem usage/betting_confidence
    diretamente; derivamos de proxies:

    - betting_confidence ≈ projection_confidence × sample_factor × alignment
    - usage ≈ 0.5 (default — sem season FGA reliable em todos gamelogs)

    Não é idêntico ao live (não tem heat full breakdown), mas é
    suficiente pra medir o efeito da variance penalty.
    """
    from src.utils.stats import (
        bet_recommendation,
        betting_confidence_from_signals,
        projection_confidence_from_label,
        sample_confidence_from_minutes,
    )

    sample_conf = sample_confidence_from_minutes(snapshot_minutes)
    proj_conf = projection_confidence_from_label(
        proj_dict.get("confidence", "medium")
    ) * variance_factor

    betting_conf = betting_confidence_from_signals(
        edge=edge,
        heat_score=stat_heat,
        sample_confidence=sample_conf,
        projection_confidence=proj_conf,
    )

    # Usage default 0.5 — backtester não tem dado granular de FGA por player.
    # Em produção, usage_proxy é calculado de season_fga / season_minutes.
    rec_label, _ = bet_recommendation(
        edge=edge,
        betting_confidence=betting_conf,
        usage=0.5,
        has_real_book=False,
        variance_factor=variance_factor,
    )
    return rec_label


def _compute_variance_factors(
    gamelog: list[dict],
    *,
    before_date: str,
) -> dict[str, float]:
    """
    Computa variance_factor por stat usando os games anteriores ao
    snapshot. Retorna {PTS, REB, AST}: cada um em 0..1.

    1.0 = consistente (CV ≤ 0.20)
    0.2 = muito volátil (CV ≥ 0.80)
    """
    from src.utils.stats import player_variance_factor

    eligible = []
    for row in gamelog:
        d = _parse_gamelog_date(str(row.get("GAME_DATE", "")))
        if d and d < before_date:
            eligible.append(row)
    if len(eligible) < 3:
        return {"PTS": 0.5, "REB": 0.5, "AST": 0.5}

    return {
        "PTS": player_variance_factor([float(r.get("PTS", 0) or 0) for r in eligible]),
        "REB": player_variance_factor([float(r.get("REB", 0) or 0) for r in eligible]),
        "AST": player_variance_factor([float(r.get("AST", 0) or 0) for r in eligible]),
    }


def _compute_rest_days(prior_stats: dict, current_game_date: str) -> Optional[int]:
    """
    Dias desde o último jogo do jogador. None se sem histórico.
    0 = back-to-back. Default None passa pro engine não aplicar rest_factor.
    """
    last_date = prior_stats.get("last_game_date")
    if not last_date or not current_game_date:
        return None
    try:
        last_dt = datetime.strptime(last_date, "%Y-%m-%d")
        curr_dt = datetime.strptime(current_game_date, "%Y-%m-%d")
        delta = (curr_dt - last_dt).days
        return max(0, delta - 1)  # 1 dia entre jogos = 0 rest (B2B)
    except (ValueError, TypeError):
        return None


def _parse_gamelog_date(s: str) -> Optional[str]:
    """Converte 'MAR 15, 2026' → '2026-03-15'. Aceita ISO também."""
    if not s:
        return None
    # ISO direto
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    # Formato "MAR 15, 2026"
    try:
        dt = datetime.strptime(s, "%b %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    return None


# ─── Report ──────────────────────────────────────────────────────────────────


def format_report(metrics: BacktestMetrics) -> str:
    """Markdown legível com as métricas agregadas."""
    if metrics.total_projections == 0:
        return "## Backtest\n\nNenhuma projeção rodada — verifique os dados.\n"

    lines = []
    lines.append(f"# Backtest Report — {metrics.total_projections} projeções")
    lines.append("")

    # MAE por stat
    lines.append("## Accuracy (MAE)")
    lines.append("")
    lines.append("| Stat | N | MAE (pts) |")
    lines.append("|------|---|-----------|")
    for stat in ("PTS", "REB", "AST"):
        b = metrics.by_stat.get(stat, {})
        if b.get("n", 0) > 0:
            lines.append(f"| {stat} | {b['n']} | {b['mae']:.2f} |")
    lines.append("")

    # Hit rate por decisão
    lines.append("## Hit rate por decisão (linha sintética)")
    lines.append("")
    lines.append("| Decisão | N | Wins | Losses | Hit rate |")
    lines.append("|---------|---|------|--------|----------|")
    for dec in ("STRONG_OVER", "LEAN_OVER", "NEUTRAL", "LEAN_UNDER", "STRONG_UNDER"):
        d = metrics.by_decision.get(dec, {})
        n = d.get("n", 0)
        if n == 0:
            continue
        wins = d.get("wins", 0)
        losses = d.get("losses", 0)
        resolved = wins + losses
        hr = (wins / resolved * 100) if resolved > 0 else 0.0
        lines.append(f"| {dec} | {n} | {wins} | {losses} | {hr:.1f}% |")
    lines.append("")

    # Caps fired + MAE por cap (ajuda ou atrapalha?)
    if metrics.caps_fired:
        lines.append("## Caps disparando + impacto em MAE + direção do erro")
        lines.append("")
        lines.append("| Cap | % | MAE on | MAE off | Δ MAE | Erro signed on |")
        lines.append("|---|---|---|---|---|---|")
        for cap_name, count in metrics.caps_fired.items():
            pct = count / metrics.total_projections * 100
            mae_bucket = metrics.caps_mae.get(cap_name, {})
            fired_n = mae_bucket.get("fired_n", 0)
            not_fired_n = mae_bucket.get("not_fired_n", 0)
            fired_mae = (
                mae_bucket.get("fired_total_err", 0) / fired_n
                if fired_n > 0 else 0
            )
            not_fired_mae = (
                mae_bucket.get("not_fired_total_err", 0) / not_fired_n
                if not_fired_n > 0 else 0
            )
            fired_signed = (
                mae_bucket.get("fired_signed_err", 0) / fired_n
                if fired_n > 0 else 0
            )
            delta = fired_mae - not_fired_mae
            delta_indicator = (
                "🔴 atrapalha" if delta > 0.3
                else "🟢 ajuda" if delta < -0.3
                else "⚪ neutro"
            )
            # Direção do erro: positivo = projetou MAIS que real,
            # negativo = projetou MENOS que real
            direction = (
                "→ super" if fired_signed > 0.3
                else "→ sub" if fired_signed < -0.3
                else "→ ~0"
            )
            lines.append(
                f"| **{cap_name}** | {pct:.1f}% ({count}) | "
                f"{fired_mae:.2f} | {not_fired_mae:.2f} | "
                f"{delta:+.2f} {delta_indicator} | "
                f"{fired_signed:+.2f} {direction} |"
            )
        lines.append("")
        lines.append(
            "> **Δ MAE**: positivo = cap atrapalha (projeção pior quando dispara). "
            "Negativo = cap ajuda (salva casos extremos)."
        )
        lines.append(
            "> **Erro signed on**: positivo = projeção SUPER-estimou (cap não cortou o "
            "suficiente). Negativo = projeção SUB-estimou (cap cortou demais)."
        )
        lines.append("")

    # Hit rate por tier (variance penalty + book bypass effect)
    if metrics.by_tier:
        lines.append("## Hit rate por TIER (bet_recommendation)")
        lines.append("")
        lines.append("| Tier | N | Wins | Losses | Hit rate | % do total |")
        lines.append("|------|---|------|--------|----------|------------|")
        for tier in ("LARGE", "MEDIUM", "SMALL", "PASS"):
            t = metrics.by_tier.get(tier, {})
            n = t.get("n", 0)
            if n == 0:
                continue
            wins = t.get("wins", 0)
            losses = t.get("losses", 0)
            resolved = wins + losses
            hr = (wins / resolved * 100) if resolved > 0 else 0.0
            pct_total = n / metrics.total_projections * 100
            lines.append(
                f"| **{tier}** | {n} | {wins} | {losses} | "
                f"{hr:.1f}% | {pct_total:.1f}% |"
            )
        lines.append("")
        lines.append(
            "> Variance penalty rebaixa tier de caras voláteis. Hit rate "
            "deve **subir conforme o tier** (LARGE > MEDIUM > SMALL)."
        )
        lines.append("")

    # Hit rate por tier × stat (PTS vs REB vs AST calibrados igual?)
    if metrics.by_tier_stat:
        lines.append("## Hit rate por TIER × STAT (calibração por mercado)")
        lines.append("")
        lines.append(
            "| Tier | Stat | N | Wins | Losses | Hit rate | MAE | Signed err |"
        )
        lines.append(
            "|------|------|---|------|--------|----------|-----|------------|"
        )
        for tier in ("LARGE", "MEDIUM", "SMALL", "PASS"):
            tier_bucket = metrics.by_tier_stat.get(tier, {})
            if not tier_bucket:
                continue
            for stat in ("PTS", "REB", "AST"):
                ts = tier_bucket.get(stat, {})
                n = ts.get("n", 0)
                if n == 0:
                    continue
                wins = ts.get("wins", 0)
                losses = ts.get("losses", 0)
                resolved = wins + losses
                hr = (wins / resolved * 100) if resolved > 0 else 0.0
                mae = ts.get("total_abs_err", 0) / n
                signed = ts.get("total_signed_err", 0) / n
                lines.append(
                    f"| **{tier}** | {stat} | {n} | {wins} | {losses} | "
                    f"{hr:.1f}% | {mae:.2f} | {signed:+.2f} |"
                )
        lines.append("")
        lines.append(
            "> **Pergunta**: dentro de um mesmo tier, os 3 stats têm hit "
            "rate parecido? Se LARGE PTS = 95% mas LARGE AST = 70%, "
            "AST precisa de calibração específica (threshold de tier, "
            "variance, ou priors)."
        )
        lines.append(
            "> **Signed err**: positivo = projeção SUPER-estima esse mercado. "
            "Negativo = SUB-estima. Ideal ≈ 0."
        )
        lines.append("")

    # MAE por stat × prior bucket (low/mid/high volume)
    if metrics.by_stat_prior_bucket:
        lines.append("## MAE por STAT × volume do jogador")
        lines.append("")
        lines.append("| Stat | Bucket prior | N | MAE | Signed err | % err |")
        lines.append("|------|--------------|---|-----|------------|-------|")
        for stat in ("PTS", "REB", "AST"):
            buckets = metrics.by_stat_prior_bucket.get(stat, {})
            for bucket_name in ("low (<5)", "mid (5-15)", "high (15+)"):
                sbb = buckets.get(bucket_name, {})
                n = sbb.get("n", 0)
                if n == 0:
                    continue
                mae = sbb.get("total_abs_err", 0) / n
                signed = sbb.get("total_signed_err", 0) / n
                # % err: MAE / midpoint do bucket pra dar noção relativa
                midpoint = (
                    2.5 if bucket_name == "low (<5)"
                    else 10.0 if bucket_name == "mid (5-15)"
                    else 20.0
                )
                pct_err = (mae / midpoint) * 100
                lines.append(
                    f"| {stat} | {bucket_name} | {n} | {mae:.2f} | "
                    f"{signed:+.2f} | {pct_err:.0f}% |"
                )
        lines.append("")
        lines.append(
            "> **% err**: MAE relativo ao midpoint do bucket. AST tende "
            "a ter % maior por ter base menor (1 ast errado em projeção "
            "de 4 ast = 25% err)."
        )
        lines.append("")

    # Signed err por TIER × MODIFIER (diagnóstico de viés)
    if metrics.tier_modifier_signed:
        lines.append("## Signed err por TIER × MODIFIER (diagnóstico de viés)")
        lines.append("")
        lines.append(
            "| Tier | Modifier | N fired | Signed fired | N off | Signed off | Δ |"
        )
        lines.append(
            "|------|----------|---------|--------------|-------|------------|---|"
        )
        # Foco em LARGE primeiro (onde está o viés), depois MEDIUM
        for tier in ("LARGE", "MEDIUM", "SMALL", "PASS"):
            tier_bucket = metrics.tier_modifier_signed.get(tier, {})
            if not tier_bucket:
                continue
            for mod_name in (
                "heat_boost", "heat_cut", "hot_shrinkage",
                "rate_decay", "soft_floor", "in_deficit_cap",
            ):
                mod = tier_bucket.get(mod_name, {})
                fn = mod.get("fired_n", 0)
                on_ = mod.get("off_n", 0)
                if fn == 0 and on_ == 0:
                    continue
                f_signed = mod.get("fired_signed", 0) / fn if fn > 0 else 0.0
                o_signed = mod.get("off_signed", 0) / on_ if on_ > 0 else 0.0
                delta = f_signed - o_signed
                # Marca o "culpado" — modifier que infla quando dispara
                # vs quando não dispara, dentro do mesmo tier
                marker = (
                    "🚨" if delta > 0.5 and fn >= 30
                    else "🔻" if delta < -0.5 and fn >= 30
                    else ""
                )
                lines.append(
                    f"| **{tier}** | {mod_name} | {fn} | {f_signed:+.2f} | "
                    f"{on_} | {o_signed:+.2f} | {delta:+.2f} {marker} |"
                )
        lines.append("")
        lines.append(
            "> **Δ**: signed err quando o modifier dispara MENOS quando não "
            "dispara, **dentro do mesmo tier**. Positivo grande (🚨) = esse "
            "modifier infla projeção quando dispara. Negativo grande (🔻) = "
            "modifier corta demais."
        )
        lines.append(
            "> N fired pequeno (<30) torna a estatística pouco confiável."
        )
        lines.append("")

    # Sample de outliers (maior erro absoluto)
    if metrics.samples:
        worst = sorted(metrics.samples, key=lambda r: -r.abs_error)[:5]
        lines.append("## Top 5 piores projeções")
        lines.append("")
        lines.append("| Player | Stat | Proj | Final | Erro |")
        lines.append("|--------|------|------|-------|------|")
        for s in worst:
            lines.append(
                f"| {s.player_name} | {s.stat} | {s.projection:.1f} | "
                f"{s.actual_final:.0f} | {s.error:+.1f} |"
            )
        lines.append("")

    return "\n".join(lines)
