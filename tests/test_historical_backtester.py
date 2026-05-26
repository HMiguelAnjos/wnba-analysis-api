"""
Testes do HistoricalBacktester e seus componentes.

Foco: snapshot reconstruction + agregação de métricas. NÃO testa
fetch real do nba_api (isso vive em integration tests separados).
"""
from __future__ import annotations

import pytest

from src.services.backtester.historical import (
    BacktestMetrics,
    HistoricalBacktester,
    ProjectionVsActual,
    _compute_prior_as_of,
    _parse_gamelog_date,
    format_report,
)
from src.services.backtester.snapshot import (
    extract_final_stats,
    reconstruct_snapshot,
)


# ─── Helpers pra montar PBP sintético ───────────────────────────────────────


def _shot(period: int, person_id: int, clock: str, three: bool = False, made: bool = True):
    return {
        "actionType": "3pt" if three else "2pt",
        "personId": person_id,
        "period": period,
        "clock": clock,
        "shotResult": "Made" if made else "Missed",
    }


def _sub(period: int, person_id: int, clock: str, sub_type: str):
    return {
        "actionType": "substitution",
        "subType": sub_type,
        "personId": person_id,
        "period": period,
        "clock": clock,
    }


def _rebound(period: int, person_id: int, clock: str):
    return {
        "actionType": "rebound",
        "personId": person_id,
        "period": period,
        "clock": clock,
    }


def _boxscore(home_players: list[dict], away_players: list[dict]) -> dict:
    return {
        "homeTeam": {"teamTricode": "HOM", "players": home_players},
        "awayTeam": {"teamTricode": "AWY", "players": away_players},
    }


def _player(pid: int, name: str, starter: bool = False, **stats) -> dict:
    return {
        "personId": pid,
        "name": name,
        "starter": "1" if starter else "0",
        "statistics": {
            "points": stats.get("points", 0),
            "reboundsTotal": stats.get("rebounds", 0),
            "assists": stats.get("assists", 0),
            "threePointersMade": stats.get("three_pm", 0),
            "minutes": stats.get("minutes", "PT00M00.00S"),
        },
    }


# ─── Snapshot reconstruction ────────────────────────────────────────────────


def test_snapshot_at_q1_end_captures_q1_stats():
    """Snapshot no fim do Q1 captura tudo que aconteceu no Q1, nada do Q2."""
    actions = [
        _shot(1, 100, "PT10M00.00S", made=True),         # 2 pts (Q1)
        _shot(1, 100, "PT05M00.00S", three=True),        # 3 pts (Q1) → 5 total
        _shot(2, 100, "PT11M00.00S", made=True),         # 2 pts (Q2 — DEPOIS do snap)
    ]
    boxscore = _boxscore(
        [_player(100, "Star", starter=True, points=7)],
        [],
    )

    snap = reconstruct_snapshot(
        actions, boxscore, snapshot_period=1, snapshot_clock_min=0.0,
    )

    # Player 100 deve ter 5 pts até fim do Q1 (não 7 — Q2 não conta)
    assert snap.players[100].points == 5
    assert snap.players[100].field_goals_made == 2


def test_snapshot_assists_credited_to_assister():
    """Assist no PBP credita pro `assistPersonId`, não pro shooter."""
    actions = [
        {
            "actionType": "2pt",
            "personId": 100,
            "period": 1,
            "clock": "PT08M00.00S",
            "shotResult": "Made",
            "assistPersonId": 200,
        },
    ]
    boxscore = _boxscore(
        [_player(100, "Scorer", starter=True), _player(200, "Passer", starter=True)],
        [],
    )

    snap = reconstruct_snapshot(actions, boxscore, 1, 0.0)
    assert snap.players[100].points == 2
    assert snap.players[100].assists == 0    # quem fez foi 100, não assistiu
    assert snap.players[200].assists == 1    # 200 assistiu


def test_snapshot_rebounds_count():
    actions = [
        _rebound(1, 100, "PT09M00.00S"),
        _rebound(1, 100, "PT07M00.00S"),
    ]
    boxscore = _boxscore([_player(100, "Rebounder", starter=True)], [])

    snap = reconstruct_snapshot(actions, boxscore, 1, 0.0)
    assert snap.players[100].rebounds == 2


def test_snapshot_starter_played_full_quarter():
    """Sem subs, titular jogou os 12 min completos."""
    boxscore = _boxscore([_player(100, "Starter", starter=True)], [])
    snap = reconstruct_snapshot([], boxscore, 1, 0.0)
    assert snap.players[100].minutes_played == 12.0


def test_snapshot_sub_out_reduces_minutes():
    """Titular sai aos 4:30 do Q1 → 7.5 min jogados."""
    actions = [_sub(1, 100, "PT04M30.00S", "out")]
    boxscore = _boxscore([_player(100, "Starter", starter=True)], [])
    snap = reconstruct_snapshot(actions, boxscore, 1, 0.0)
    assert snap.players[100].minutes_played == 7.5


def test_snapshot_at_mid_quarter():
    """Snapshot em Q1 com 6:00 restantes — fecha em 6.0, não em 0."""
    boxscore = _boxscore([_player(100, "Starter", starter=True)], [])
    # snapshot em 6 min restantes → cara jogou 12-6 = 6 min
    snap = reconstruct_snapshot([], boxscore, 1, 6.0)
    assert snap.players[100].minutes_played == 6.0


# ─── Final stats extraction ─────────────────────────────────────────────────


def test_extract_final_stats():
    boxscore = _boxscore(
        [_player(100, "A", points=25, rebounds=7, assists=5, three_pm=3, minutes="PT34M00.00S")],
        [_player(200, "B", points=18, rebounds=4, assists=2)],
    )
    finals = extract_final_stats(boxscore)
    assert finals[100]["points"] == 25
    assert finals[100]["rebounds"] == 7
    assert finals[100]["assists"] == 5
    assert finals[100]["three_pm"] == 3
    assert finals[100]["minutes"] == 34.0
    assert finals[200]["points"] == 18


# ─── Prior computation a partir de gamelog ──────────────────────────────────


def test_compute_prior_filters_by_date():
    """Computa average usando só jogos ANTES da data alvo."""
    gamelog = [
        {"GAME_DATE": "MAR 10, 2026", "PTS": 20, "REB": 5, "AST": 3, "MIN": 30},
        {"GAME_DATE": "MAR 12, 2026", "PTS": 25, "REB": 6, "AST": 4, "MIN": 32},
        {"GAME_DATE": "MAR 15, 2026", "PTS": 100, "REB": 100, "AST": 100, "MIN": 100},  # jogo alvo — deve ser EXCLUÍDO
    ]
    prior = _compute_prior_as_of(gamelog, before_date="2026-03-15")

    assert prior is not None
    assert prior["games_played"] == 2  # só os 2 antes
    assert prior["pts_avg"] == 22.5    # (20+25)/2 = 22.5


def test_compute_prior_returns_none_when_no_previous_games():
    gamelog = [{"GAME_DATE": "MAR 15, 2026", "PTS": 20, "REB": 5, "AST": 3, "MIN": 30}]
    prior = _compute_prior_as_of(gamelog, before_date="2026-03-15")
    assert prior is None


def test_parse_gamelog_date_formats():
    # Formato NBA padrão
    assert _parse_gamelog_date("MAR 15, 2026") == "2026-03-15"
    # ISO direto
    assert _parse_gamelog_date("2026-03-15") == "2026-03-15"
    # ISO com timestamp
    assert _parse_gamelog_date("2026-03-15T19:00:00") == "2026-03-15"
    # Inválido
    assert _parse_gamelog_date("garbage") is None
    assert _parse_gamelog_date("") is None


# ─── Metrics aggregation ────────────────────────────────────────────────────


def _make_pva(stat: str, projection: float, actual: float, decision: str = "NEUTRAL",
              over_won=None) -> ProjectionVsActual:
    return ProjectionVsActual(
        game_id="0022500001",
        game_date="2026-03-15",
        player_id=100,
        player_name="Test",
        stat=stat,
        snapshot_period=3,
        snapshot_clock_min=0.0,
        minutes_at_snapshot=24.0,
        current_at_snapshot=15.0,
        prior_avg=20.0,
        projection=projection,
        actual_final=actual,
        error=projection - actual,
        abs_error=abs(projection - actual),
        decision_synthetic=decision,
        over_won=over_won,
    )


def test_aggregation_computes_mae_per_stat():
    bt = HistoricalBacktester()
    metrics = BacktestMetrics()
    metrics.by_stat = {"PTS": {"mae": 0.0, "n": 0, "total_err": 0.0}}
    metrics.by_decision = {}
    metrics.caps_fired = {}

    for proj, actual in [(20, 22), (15, 17), (25, 20)]:
        bt._aggregate_one(metrics, _make_pva("PTS", proj, actual))

    # Total abs errors: |20-22| + |15-17| + |25-20| = 2 + 2 + 5 = 9
    # MAE = 9/3 = 3
    assert metrics.by_stat["PTS"]["n"] == 3
    assert metrics.by_stat["PTS"]["total_err"] == 9


def test_aggregation_hit_rate_by_decision():
    bt = HistoricalBacktester()
    metrics = BacktestMetrics()
    metrics.by_stat = {"PTS": {"mae": 0.0, "n": 0, "total_err": 0.0}}
    metrics.by_decision = {}
    metrics.caps_fired = {}

    # 3 STRONG_OVER: 2 ganharam, 1 perdeu → hit rate 66.7%
    bt._aggregate_one(metrics, _make_pva("PTS", 25, 28, decision="STRONG_OVER", over_won=True))
    bt._aggregate_one(metrics, _make_pva("PTS", 25, 26, decision="STRONG_OVER", over_won=True))
    bt._aggregate_one(metrics, _make_pva("PTS", 25, 20, decision="STRONG_OVER", over_won=False))

    d = metrics.by_decision["STRONG_OVER"]
    assert d["n"] == 3
    assert d["wins"] == 2
    assert d["losses"] == 1


# ─── Inferência de season pelo game_id ──────────────────────────────────────


def test_season_inferred_from_game_id():
    bt = HistoricalBacktester()
    assert bt._infer_season_from_game_id("0022500001") == "2025-26"
    assert bt._infer_season_from_game_id("0042400001") == "2024-25"  # playoff 24-25
    assert bt._infer_season_from_game_id("0021900001") == "2019-20"


# ─── Format report básico ───────────────────────────────────────────────────


def test_format_report_empty():
    metrics = BacktestMetrics()
    report = format_report(metrics)
    assert "Nenhuma" in report


def test_format_report_with_data():
    bt = HistoricalBacktester()
    metrics = BacktestMetrics()
    metrics.by_stat = {"PTS": {"mae": 0.0, "n": 0, "total_err": 0.0}}
    metrics.by_decision = {}
    metrics.caps_fired = {}
    bt._aggregate_one(metrics, _make_pva("PTS", 25, 28, decision="STRONG_OVER", over_won=True))
    bt._aggregate_one(metrics, _make_pva("PTS", 15, 17, decision="NEUTRAL"))
    # Finaliza MAE
    metrics.by_stat["PTS"]["mae"] = metrics.by_stat["PTS"]["total_err"] / metrics.by_stat["PTS"]["n"]

    report = format_report(metrics)
    assert "2 projeções" in report
    assert "PTS" in report
    assert "STRONG_OVER" in report
