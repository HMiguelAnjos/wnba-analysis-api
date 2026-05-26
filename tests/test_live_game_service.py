"""
Testes para LiveGameService.get_today_games() — em especial o fallback
para ScoreboardV2 quando a NBA Live API ainda mostra "ontem" ou todos
os jogos estão `final`.

Estratégia: monkey-patch dos métodos `fetch_scoreboard` e
`fetch_scoreboard_for_date` pra evitar I/O com a NBA. Cobrimos cada
ramo do `get_today_games`.
"""

from __future__ import annotations

import pytest

from src.schemas.live_schemas import (
    BlowoutRiskSchema,
    LiveGameSchema,
    LiveTeamSchema,
    TodayGamesSchema,
)
from src.services.live_game_service import LiveGameService


def _make_game(
    game_id: str = "0022300100",
    status: str = "final",
    home_score: int = 110,
    away_score: int = 100,
) -> LiveGameSchema:
    return LiveGameSchema(
        game_id=game_id,
        game_status=status,
        period=4 if status == "final" else 0,
        clock="00:00" if status == "final" else "12:00",
        game_time_utc=None,
        home_team=LiveTeamSchema(team_id=1, name="Home", tricode="HOM", score=home_score),
        away_team=LiveTeamSchema(team_id=2, name="Away", tricode="AWY", score=away_score),
        blowout_risk=None,
    )


def _make_payload(
    date: str,
    games: list[LiveGameSchema],
    source: str = "live",
) -> TodayGamesSchema:
    all_final = bool(games) and all(g.game_status == "final" for g in games)
    return TodayGamesSchema(
        date=date,
        games=games,
        source=source,  # type: ignore[arg-type]
        all_final=all_final,
    )


# ─── Fluxo feliz: live é "hoje" e tem jogo rolando ───────────────────────────

def test_live_today_with_active_game_uses_live(monkeypatch):
    """Live retornou data ET de hoje + tem jogo não-final → usa live direto."""
    svc = LiveGameService()
    live = _make_payload(
        date="2026-05-11",
        games=[_make_game(status="in_progress")],
    )

    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")
    monkeypatch.setattr(svc, "fetch_scoreboard", lambda: live)

    called_scheduled = {"v": False}
    def _no_call(_date):
        called_scheduled["v"] = True
        raise AssertionError("não deveria chamar ScoreboardV2 aqui")
    monkeypatch.setattr(svc, "fetch_scoreboard_for_date", _no_call)

    result = svc.get_today_games()
    assert result.source == "live"
    assert result.date == "2026-05-11"
    assert called_scheduled["v"] is False


# ─── Fluxo fallback: live mostra ontem (todos final) ──────────────────────────

def test_live_yesterday_all_final_uses_scheduled(monkeypatch):
    """
    Cenário do bug do usuário: BRT é dia 11, NBA Live ainda mostra
    dia 10 com jogos finalizados. Service deve buscar o ScoreboardV2
    com data 2026-05-11 e devolver isso no lugar.
    """
    svc = LiveGameService()
    live = _make_payload(
        date="2026-05-10",
        games=[_make_game(status="final")],
    )
    scheduled = _make_payload(
        date="2026-05-11",
        games=[_make_game(game_id="0022300200", status="not_started")],
        source="scheduled",
    )

    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")
    monkeypatch.setattr(svc, "fetch_scoreboard", lambda: live)
    monkeypatch.setattr(svc, "fetch_scoreboard_for_date", lambda d: scheduled)

    result = svc.get_today_games()
    assert result.source == "scheduled"
    assert result.date == "2026-05-11"
    assert result.all_final is False


# ─── Fallback falha → mantém live como degradação suave ──────────────────────

def test_scheduled_fetch_failure_falls_back_to_live(monkeypatch):
    svc = LiveGameService()
    live = _make_payload(
        date="2026-05-10",
        games=[_make_game(status="final")],
    )

    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")
    monkeypatch.setattr(svc, "fetch_scoreboard", lambda: live)

    def _boom(_d):
        raise RuntimeError("stats.nba.com timeout")
    monkeypatch.setattr(svc, "fetch_scoreboard_for_date", _boom)

    result = svc.get_today_games()
    # Não estoura — mantém live com all_final=True. Front renderiza
    # a mensagem "todos finalizados" e usuário não fica na tela vazia.
    assert result.source == "live"
    assert result.all_final is True


# ─── ScoreboardV2 também vazio → mantém live (evita "piscar" pra nada) ──────

def test_scheduled_empty_keeps_live(monkeypatch):
    """
    Caso de offseason / dia sem jogos: live mostra jogos antigos
    ainda como "final", ScoreboardV2 do dia atual está vazio.
    Preferimos o live (pelo menos dá contexto histórico).
    """
    svc = LiveGameService()
    live = _make_payload(
        date="2026-05-10",
        games=[_make_game(status="final")],
    )
    scheduled = _make_payload(date="2026-05-11", games=[], source="scheduled")

    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")
    monkeypatch.setattr(svc, "fetch_scoreboard", lambda: live)
    monkeypatch.setattr(svc, "fetch_scoreboard_for_date", lambda d: scheduled)

    result = svc.get_today_games()
    assert result.source == "live"
    assert result.date == "2026-05-10"


# ─── ScoreboardV2 também todo final → mantém live (mesmo motivo) ─────────────

def test_scheduled_all_final_keeps_live(monkeypatch):
    svc = LiveGameService()
    live = _make_payload(
        date="2026-05-10",
        games=[_make_game(status="final")],
    )
    scheduled = _make_payload(
        date="2026-05-11",
        games=[_make_game(status="final")],
        source="scheduled",
    )

    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")
    monkeypatch.setattr(svc, "fetch_scoreboard", lambda: live)
    monkeypatch.setattr(svc, "fetch_scoreboard_for_date", lambda d: scheduled)

    result = svc.get_today_games()
    assert result.source == "live"


# ─── Cache: segunda chamada não reexecuta fetch ─────────────────────────────

def test_cache_hits_dont_refetch(monkeypatch):
    svc = LiveGameService()
    live = _make_payload(
        date="2026-05-11",
        games=[_make_game(status="in_progress")],
    )

    calls = {"n": 0}
    def _fetch():
        calls["n"] += 1
        return live
    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")
    monkeypatch.setattr(svc, "fetch_scoreboard", _fetch)

    svc.get_today_games()
    svc.get_today_games()
    assert calls["n"] == 1, "Segunda chamada deveria vir do cache"


# ─── Resilience: live falha mas ScoreboardV2 dá certo → usa scheduled ──────

def test_live_fails_scheduled_succeeds(monkeypatch):
    """
    Se a NBA Live API estiver fora mas o ScoreboardV2 responder, o smart
    fetch deve usar o scheduled em vez de propagar a falha do live.
    """
    svc = LiveGameService()
    scheduled = _make_payload(
        date="2026-05-11",
        games=[_make_game(status="not_started")],
        source="scheduled",
    )

    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")
    def _live_fails():
        raise RuntimeError("NBA Live timeout")
    monkeypatch.setattr(svc, "fetch_scoreboard", _live_fails)
    monkeypatch.setattr(svc, "fetch_scoreboard_for_date", lambda d: scheduled)

    result = svc.fetch_scoreboard_smart()
    assert result.source == "scheduled"
    assert result.date == "2026-05-11"


def test_both_fail_raises(monkeypatch):
    """Ambas as fontes off → propaga erro pro worker (preserva snapshot anterior)."""
    svc = LiveGameService()
    monkeypatch.setattr("src.services.live_game_service.today_in_et", lambda: "2026-05-11")

    def _boom():
        raise RuntimeError("live off")
    def _boom2(_d):
        raise RuntimeError("stats off")
    monkeypatch.setattr(svc, "fetch_scoreboard", _boom)
    monkeypatch.setattr(svc, "fetch_scoreboard_for_date", _boom2)

    with pytest.raises(RuntimeError):
        svc.fetch_scoreboard_smart()


# ─── today_in_et: smoke test (formato + dentro de janela razoável) ──────────

def test_today_in_et_returns_yyyy_mm_dd():
    from datetime import datetime, timedelta, timezone

    from src.utils.time_utils import today_in_et

    today = today_in_et()
    # YYYY-MM-DD
    assert len(today) == 10
    assert today[4] == "-" and today[7] == "-"
    # Deve estar dentro de 1 dia da data UTC (ET é UTC-5/-4).
    parsed = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    delta = abs((parsed - now_utc).total_seconds())
    assert delta < 86400 * 2, f"Data ET muito longe do UTC agora: {today}"
