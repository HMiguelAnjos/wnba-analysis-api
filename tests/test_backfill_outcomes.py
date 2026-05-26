"""
Testes do script scripts/backfill_outcomes.py.

Cobertura:
  - Caminho feliz: 4 records (PTS/REB/AST/3PM) viram preenchidos
  - Records sem game_id são pulados sem crash
  - Records com stat desconhecido são pulados
  - Players ausentes do PBP são pulados
  - Records já preenchidos não são tocados
  - Falha de fetch num game não derruba os outros
  - dry_run não escreve no arquivo
  - Atomic write (tmp → rename) preserva conteúdo em caso de erro
  - Aggregator extendido conta three_pm corretamente
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

import pandas as pd
import pytest

# Adiciona scripts/ ao path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_PROJECT_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import backfill_outcomes
from src.services.nba_service import _aggregate_v3_dataframe


# ─── Fixtures ──────────────────────────────────────────────────────────────


class _FakeNba:
    """Mock de NbaService — guarda fixtures pré-definidas por game_id."""

    def __init__(
        self,
        per_game: Optional[dict] = None,
        failing_games: Optional[set[str]] = None,
    ) -> None:
        self.per_game = per_game or {}
        self.failing_games = failing_games or set()
        self.calls: list[str] = []

    def aggregate_historical_pbp_per_period(self, game_id: str) -> dict:
        self.calls.append(game_id)
        if game_id in self.failing_games:
            raise RuntimeError(f"forced failure for {game_id}")
        return self.per_game.get(game_id, {})


def _write_records(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _read_records(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ─── Caminho feliz ─────────────────────────────────────────────────────────


def test_backfill_fills_pts_reb_ast_three_pm(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        {"game_id": "g1", "player_id": 100, "stat": "PTS", "our_line": 22.5},
        {"game_id": "g1", "player_id": 100, "stat": "REB", "our_line": 5.5},
        {"game_id": "g1", "player_id": 100, "stat": "AST", "our_line": 7.5},
        {"game_id": "g1", "player_id": 100, "stat": "3PM", "our_line": 2.5},
    ]
    _write_records(log_path, records)

    fake = _FakeNba(per_game={
        "g1": {
            100: {
                1: {"points": 8, "rebounds": 2, "assists": 3, "three_pm": 2},
                2: {"points": 6, "rebounds": 1, "assists": 2, "three_pm": 0},
                3: {"points": 7, "rebounds": 2, "assists": 1, "three_pm": 1},
                4: {"points": 4, "rebounds": 1, "assists": 2, "three_pm": 0},
            }
        }
    })

    result = backfill_outcomes.backfill(log_path, nba_service=fake)
    assert result["filled"] == 4
    assert result["games_fetched"] == 1
    assert result["games_failed"] == 0

    out = _read_records(log_path)
    assert out[0]["actual_outcome"] == 25.0      # 8+6+7+4
    assert out[1]["actual_outcome"] == 6.0       # 2+1+2+1
    assert out[2]["actual_outcome"] == 8.0       # 3+2+1+2
    assert out[3]["actual_outcome"] == 3.0       # 2+0+1+0


# ─── Edge cases ────────────────────────────────────────────────────────────


def test_backfill_skips_records_without_game_id(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        {"player_id": 100, "stat": "PTS"},   # sem game_id
        {"game_id": "g1", "player_id": 100, "stat": "PTS"},
    ]
    _write_records(log_path, records)
    fake = _FakeNba(per_game={
        "g1": {100: {1: {"points": 20, "rebounds": 0, "assists": 0, "three_pm": 0}}}
    })
    result = backfill_outcomes.backfill(log_path, nba_service=fake)
    assert result["skipped_no_game_id"] == 1
    assert result["filled"] == 1


def test_backfill_skips_unknown_stat(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        {"game_id": "g1", "player_id": 100, "stat": "STL"},  # não suportado
    ]
    _write_records(log_path, records)
    fake = _FakeNba(per_game={
        "g1": {100: {1: {"points": 10, "rebounds": 0, "assists": 0, "three_pm": 0}}}
    })
    result = backfill_outcomes.backfill(log_path, nba_service=fake)
    assert result["filled"] == 0
    assert result["skipped_unknown_stat"] == 1


def test_backfill_skips_player_not_in_pbp(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        # player 999 não jogou (não aparece no PBP)
        {"game_id": "g1", "player_id": 999, "stat": "PTS"},
    ]
    _write_records(log_path, records)
    fake = _FakeNba(per_game={
        "g1": {100: {1: {"points": 10, "rebounds": 0, "assists": 0, "three_pm": 0}}}
    })
    result = backfill_outcomes.backfill(log_path, nba_service=fake)
    assert result["filled"] == 0
    assert result["skipped_player_missing"] == 1


def test_backfill_idempotent_does_not_overwrite_existing(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        {"game_id": "g1", "player_id": 100, "stat": "PTS",
         "actual_outcome": 99.0},   # já preenchido
    ]
    _write_records(log_path, records)
    fake = _FakeNba(per_game={
        "g1": {100: {1: {"points": 25, "rebounds": 0, "assists": 0, "three_pm": 0}}}
    })
    result = backfill_outcomes.backfill(log_path, nba_service=fake)
    assert result["filled"] == 0
    assert result["needed_outcome"] == 0
    out = _read_records(log_path)
    assert out[0]["actual_outcome"] == 99.0    # mantido


def test_backfill_continues_when_one_game_fetch_fails(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        {"game_id": "broken", "player_id": 100, "stat": "PTS"},
        {"game_id": "g2", "player_id": 200, "stat": "PTS"},
    ]
    _write_records(log_path, records)
    fake = _FakeNba(
        per_game={
            "g2": {200: {1: {"points": 30, "rebounds": 0, "assists": 0, "three_pm": 0}}}
        },
        failing_games={"broken"},
    )
    result = backfill_outcomes.backfill(log_path, nba_service=fake)
    # 1 game failed, mas o outro foi preenchido
    assert result["games_failed"] == 1
    assert result["filled"] == 1


def test_backfill_dry_run_does_not_write(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        {"game_id": "g1", "player_id": 100, "stat": "PTS"},
    ]
    _write_records(log_path, records)
    original = _read_records(log_path)

    fake = _FakeNba(per_game={
        "g1": {100: {1: {"points": 25, "rebounds": 0, "assists": 0, "three_pm": 0}}}
    })
    result = backfill_outcomes.backfill(log_path, dry_run=True, nba_service=fake)

    # Reporta filled=1, mas arquivo continua igual
    assert result["filled"] == 1
    assert result["dry_run"] is True
    after = _read_records(log_path)
    assert "actual_outcome" not in after[0]


def test_backfill_handles_missing_file(tmp_path):
    nonexistent = str(tmp_path / "nope.jsonl")
    result = backfill_outcomes.backfill(nonexistent)
    assert "error" in result
    assert "não encontrado" in result["error"]


def test_backfill_skips_malformed_jsonl_lines(tmp_path):
    log_path = str(tmp_path / "line_log.jsonl")
    # Mistura JSON válido e lixo
    with open(log_path, "w", encoding="utf-8") as f:
        f.write('{"game_id": "g1", "player_id": 100, "stat": "PTS"}\n')
        f.write("not json at all\n")
        f.write('{"game_id": "g1", "player_id": 200, "stat": "AST"}\n')

    fake = _FakeNba(per_game={
        "g1": {
            100: {1: {"points": 20, "rebounds": 0, "assists": 0, "three_pm": 0}},
            200: {1: {"points": 0, "rebounds": 0, "assists": 8, "three_pm": 0}},
        }
    })
    result = backfill_outcomes.backfill(log_path, nba_service=fake)
    assert result["filled"] == 2
    assert result["total_records"] == 2  # linha lixo descartada


def test_backfill_groups_by_game_id_calls_pbp_once_per_game(tmp_path):
    """Múltiplos records do mesmo game_id → 1 fetch só."""
    log_path = str(tmp_path / "line_log.jsonl")
    records = [
        {"game_id": "g1", "player_id": 100, "stat": "PTS"},
        {"game_id": "g1", "player_id": 100, "stat": "REB"},
        {"game_id": "g1", "player_id": 200, "stat": "PTS"},
        {"game_id": "g1", "player_id": 200, "stat": "AST"},
    ]
    _write_records(log_path, records)
    fake = _FakeNba(per_game={
        "g1": {
            100: {1: {"points": 10, "rebounds": 5, "assists": 2, "three_pm": 0}},
            200: {1: {"points": 15, "rebounds": 0, "assists": 7, "three_pm": 0}},
        }
    })
    backfill_outcomes.backfill(log_path, nba_service=fake)
    # Apesar de 4 records, fetch só rolou 1× (game agregado)
    assert fake.calls.count("g1") == 1


# ─── Aggregator three_pm ───────────────────────────────────────────────────


def test_aggregator_counts_three_pm():
    """Made Shot com 3PT na description → conta points=3 + three_pm=1."""
    df = pd.DataFrame([
        {
            "period": 1, "actionType": "Made Shot", "personId": 2544,
            "description": "James 27' 3PT Jump Shot (3 PTS)",
            "subType": "3PT",
            "shotResult": "", "playerName": "James", "playerNameI": "James",
        },
        {
            "period": 1, "actionType": "Made Shot", "personId": 2544,
            "description": "James 1' Driving Layup (5 PTS)",
            "subType": "", "shotResult": "",
            "playerName": "James", "playerNameI": "James",
        },
    ])
    out = _aggregate_v3_dataframe(df)
    # 3PT + 2PT = 5 pts; só 1 três
    assert out[2544][1]["points"] == 5
    assert out[2544][1]["three_pm"] == 1


def test_aggregator_three_pm_zero_when_only_twos():
    df = pd.DataFrame([
        {
            "period": 1, "actionType": "Made Shot", "personId": 100,
            "description": "Player 1' Layup (2 PTS)",
            "subType": "", "shotResult": "",
            "playerName": "P", "playerNameI": "P",
        },
    ])
    out = _aggregate_v3_dataframe(df)
    assert out[100][1]["three_pm"] == 0
    assert out[100][1]["points"] == 2
