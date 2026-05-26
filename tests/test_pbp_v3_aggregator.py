"""
Testes do parser PlayByPlayV3 em NbaService.

Garante que get_play_by_play / get_points_by_period funcionam com o
schema camelCase do V3 (actionType, period, personId, description).
Antes do fix, ambos retornavam stats=0 silenciosamente porque o parser
lia colunas V2 (UPPERCASE_SNAKE: EVENTMSGTYPE, PLAYER1_ID, etc.).

Mocks o `_fetch_pbp_df` pra evitar chamadas reais a stats.nba.com.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src.services.nba_service import _aggregate_v3_dataframe


def _make_v3_row(
    *,
    period: int,
    action_type: str,
    person_id: int,
    description: str = "",
    sub_type: str = "",
    shot_result: str = "",
    player_name: str = "",
) -> dict:
    """Cria um row no formato V3 (camelCase)."""
    return {
        "period": period,
        "actionType": action_type,
        "personId": person_id,
        "description": description,
        "subType": sub_type,
        "shotResult": shot_result,
        "playerName": player_name,
        "playerNameI": player_name,
    }


def test_aggregator_made_2pt_shot_credits_shooter():
    df = pd.DataFrame([
        _make_v3_row(period=1, action_type="Made Shot", person_id=2544,
                     description="James 2' Layup (2 PTS)",
                     player_name="James"),
    ])
    out = _aggregate_v3_dataframe(df)
    assert out[2544][1]["points"] == 2


def test_aggregator_made_3pt_shot_credits_3():
    df = pd.DataFrame([
        _make_v3_row(period=2, action_type="Made Shot", person_id=2544,
                     description="James 27' 3PT Jump Shot (3 PTS)",
                     player_name="James"),
    ])
    out = _aggregate_v3_dataframe(df)
    assert out[2544][2]["points"] == 3


def test_aggregator_free_throw_made_pts_via_description():
    """V3 não popula shot_result pra FT — detecta MADE pela presença de '(N PTS)'."""
    df = pd.DataFrame([
        _make_v3_row(period=3, action_type="Free Throw", person_id=2544,
                     description="James Free Throw 1 of 2 (5 PTS)",
                     player_name="James"),
    ])
    out = _aggregate_v3_dataframe(df)
    assert out[2544][3]["points"] == 1


def test_aggregator_free_throw_missed_no_pts():
    df = pd.DataFrame([
        _make_v3_row(period=3, action_type="Free Throw", person_id=2544,
                     description="MISS James Free Throw 1 of 2",
                     player_name="James"),
    ])
    out = _aggregate_v3_dataframe(df)
    # Player nem entra no dict se não fez nada
    assert 2544 not in out or out[2544][3]["points"] == 0


def test_aggregator_rebound():
    df = pd.DataFrame([
        _make_v3_row(period=2, action_type="Rebound", person_id=1628369,
                     description="Tatum REBOUND (Off:0 Def:1)",
                     player_name="Tatum"),
    ])
    out = _aggregate_v3_dataframe(df)
    assert out[1628369][2]["rebounds"] == 1


def test_aggregator_assist_extracts_from_description():
    """Assist na description: '(<assistente> N AST)' → +1 pro assistente."""
    df = pd.DataFrame([
        # 1ª passada constrói name→pid
        _make_v3_row(period=1, action_type="Made Shot", person_id=1628369,
                     description="Tatum 27' 3PT (3 PTS)",
                     player_name="Tatum"),
        _make_v3_row(period=1, action_type="Made Shot", person_id=201950,
                     description="Holiday 1' Driving Layup (2 PTS) (Tatum 1 AST)",
                     player_name="Holiday"),
    ])
    out = _aggregate_v3_dataframe(df)
    # Tatum recebe assist
    assert out[1628369][1]["assists"] == 1
    # Holiday faz 2 pts
    assert out[201950][1]["points"] == 2


def test_aggregator_skips_invalid_period():
    df = pd.DataFrame([
        _make_v3_row(period=0, action_type="Made Shot", person_id=2544,
                     description="(2 PTS)"),
        _make_v3_row(period=None, action_type="Made Shot", person_id=2544,
                     description="(2 PTS)"),
    ])
    out = _aggregate_v3_dataframe(df)
    # Nenhum válido — out vazio
    assert out == {}


def test_aggregator_handles_full_game_dataframe():
    """Sanity de integração: múltiplos events, players, periods."""
    df = pd.DataFrame([
        _make_v3_row(period=1, action_type="Made Shot", person_id=1,
                     description="P1 (2 PTS)", player_name="P1"),
        _make_v3_row(period=1, action_type="Rebound", person_id=2, player_name="P2"),
        _make_v3_row(period=2, action_type="Made Shot", person_id=1,
                     description="P1 (5 PTS)", player_name="P1"),
        _make_v3_row(period=2, action_type="Free Throw", person_id=1,
                     description="P1 Free Throw 1 of 1 (6 PTS)",
                     player_name="P1"),
        _make_v3_row(period=4, action_type="Rebound", person_id=1, player_name="P1"),
    ])
    out = _aggregate_v3_dataframe(df)
    # P1: 2+2+1 = 5 pts (3 events de pts), 1 reb no Q4
    assert out[1][1]["points"] == 2
    assert out[1][2]["points"] == 3   # 2 do FG + 1 do FT
    assert out[1][4]["rebounds"] == 1
    # P2: 1 reb no Q1
    assert out[2][1]["rebounds"] == 1


# ─── Endpoint smoke (mockado pra evitar fetch real) ────────────────────


def _build_minimal_df() -> pd.DataFrame:
    """DataFrame minimal pra exercitar get_play_by_play e get_points_by_period."""
    return pd.DataFrame([
        _make_v3_row(period=1, action_type="Made Shot", person_id=2544,
                     description="James 2' Layup (2 PTS)",
                     player_name="James"),
        _make_v3_row(period=2, action_type="Made Shot", person_id=2544,
                     description="James 27' 3PT (3 PTS)",
                     player_name="James"),
        _make_v3_row(period=3, action_type="Free Throw", person_id=2544,
                     description="James Free Throw 1 of 1 (6 PTS)",
                     player_name="James"),
    ])


def test_get_points_by_period_uses_v3(tmp_path, monkeypatch):
    """get_points_by_period(LeBron) deveria devolver 6 pts (2+3+1) somados."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    import importlib
    import src.config
    importlib.reload(src.config)

    df = _build_minimal_df()
    with patch("src.services.nba_service._fetch_pbp_df", return_value=df):
        from src.services.nba_service import NbaService
        nba = NbaService()
        # Mock resolution de player_id pra não bater na lib estática
        with patch("src.services.nba_service.players.find_player_by_id",
                   return_value={"id": 2544, "full_name": "LeBron James"}):
            result = nba.get_points_by_period(player_id=2544, game_id="0000000")
    assert result.total_points == 6
    assert result.points_by_period == {"1": 2, "2": 3, "3": 1}


def test_get_play_by_play_returns_v3_events(tmp_path, monkeypatch):
    """get_play_by_play retorna lista de eventos com action_type não-zero."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    import importlib
    import src.config
    importlib.reload(src.config)

    df = _build_minimal_df()
    with patch("src.services.nba_service._fetch_pbp_df", return_value=df):
        from src.services.nba_service import NbaService
        nba = NbaService()
        events = nba.get_play_by_play("0000000")
    assert len(events) == 3
    # Antes do fix, event_type seria str(0); agora deve ser actionType real
    assert events[0].event_type == "Made Shot"
    assert events[0].player_name == "James"
    assert events[1].event_type == "Made Shot"
    assert events[2].event_type == "Free Throw"
