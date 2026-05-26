"""
Testes do aggregate_per_period_with_court_time — rastreia tempo em quadra
por jogador por período via state machine de substituições do PBP.
"""
from __future__ import annotations

import pytest

from src.utils.pbp_aggregator import (
    aggregate_per_period_with_court_time,
    _parse_clock_to_minutes,
)


def _shot(period: int, person_id: int, clock: str, made: bool = True, three: bool = False):
    return {
        "actionType": "3pt" if three else "2pt",
        "personId": person_id,
        "period": period,
        "clock": clock,
        "shotResult": "Made" if made else "Missed",
    }


def _sub(period: int, person_id: int, clock: str, sub_type: str):
    """sub_type: 'in' ou 'out'"""
    return {
        "actionType": "substitution",
        "subType": sub_type,
        "personId": person_id,
        "period": period,
        "clock": clock,
    }


# ─── parse de clock ─────────────────────────────────────────────────────────


def test_parse_clock_full_quarter():
    assert _parse_clock_to_minutes("PT12M00.00S") == 12.0


def test_parse_clock_mid_quarter():
    # 8m 30s = 8.5 min
    assert _parse_clock_to_minutes("PT08M30.00S") == 8.5


def test_parse_clock_seconds_only():
    # 0m 45s = 0.75 min
    assert _parse_clock_to_minutes("PT00M45.00S") == 0.75


def test_parse_clock_invalid_returns_zero():
    assert _parse_clock_to_minutes("") == 0.0
    assert _parse_clock_to_minutes("invalid") == 0.0


# ─── Q1 starters jogam o quarter inteiro sem subs ──────────────────────────


def test_starter_no_subs_plays_full_q1():
    """Sem nenhuma sub, titular do Q1 joga os 12 min completos."""
    starters = {100}
    actions = []  # nenhuma ação

    result = aggregate_per_period_with_court_time(actions, starters)

    p1 = result[100][1]
    assert p1["minutes_played"] == 12.0
    assert p1["intervals"] == [[12.0, 0.0]]


# ─── Q1 starter sai no meio do quarter ─────────────────────────────────────


def test_starter_subbed_out_mid_q1():
    """Titular sai aos 4:30 do Q1 → jogou 7.5 min."""
    starters = {100}
    actions = [
        _sub(1, 100, "PT04M30.00S", "out"),
    ]

    result = aggregate_per_period_with_court_time(actions, starters)

    p1 = result[100][1]
    # Entrou em 12:00, saiu em 4:30 → 7.5 min
    assert p1["minutes_played"] == 7.5
    assert p1["intervals"] == [[12.0, 4.5]]


# ─── Player entra, sai, volta no mesmo quarter ─────────────────────────────


def test_player_multiple_stints_in_q1():
    """Reserva entra 8:00, sai 4:00, volta 1:30 até o fim → 2 intervalos."""
    starters = set()  # esse cara não é titular
    pid = 200
    actions = [
        _sub(1, pid, "PT08M00.00S", "in"),
        _sub(1, pid, "PT04M00.00S", "out"),
        _sub(1, pid, "PT01M30.00S", "in"),
    ]

    result = aggregate_per_period_with_court_time(actions, starters)

    p1 = result[pid][1]
    # Intervalos: [8:00 → 4:00] (4 min) + [1:30 → 0:00] (1.5 min) = 5.5
    assert p1["minutes_played"] == 5.5
    assert p1["intervals"] == [[8.0, 4.0], [1.5, 0.0]]


# ─── Carryover de estado entre quartos ─────────────────────────────────────


def test_q1_starter_continues_in_q2():
    """Titular que terminou Q1 em quadra continua em Q2."""
    starters = {100}
    actions = [
        # Q1: cara joga inteiro (sem subs)
        # Q2: ação qualquer pra cruzar o boundary do período
        _shot(2, 100, "PT10M00.00S"),
        # Q2: cara sai aos 5:00
        _sub(2, 100, "PT05M00.00S", "out"),
    ]

    result = aggregate_per_period_with_court_time(actions, starters)

    # Q1: jogou completo
    assert result[100][1]["minutes_played"] == 12.0
    # Q2: começou em quadra (carryover), saiu aos 5:00 → 7 min
    assert result[100][2]["minutes_played"] == 7.0
    assert result[100][2]["intervals"] == [[12.0, 5.0]]


# ─── Stats + court time integrados ─────────────────────────────────────────


def test_stats_and_court_time_combined():
    """O resultado contém TANTO os counters tradicionais QUANTO court time."""
    starters = {100}
    actions = [
        _shot(1, 100, "PT10M00.00S", made=True, three=False),  # 2 pts
        _shot(1, 100, "PT05M00.00S", made=True, three=True),   # 3 pts
        _sub(1, 100, "PT02M00.00S", "out"),
    ]

    result = aggregate_per_period_with_court_time(actions, starters)
    p1 = result[100][1]

    # Counters
    assert p1["points"] == 5
    assert p1["two_pt_made"] == 1
    assert p1["three_pt_made"] == 1

    # Court time
    assert p1["minutes_played"] == 10.0  # 12:00 → 2:00
    assert p1["intervals"] == [[12.0, 2.0]]


# ─── Edge case: nenhum starter, nenhuma sub ────────────────────────────────


def test_empty_starters_no_subs_returns_empty():
    """Sem starters e sem subs, resultado é vazio (não tem dado)."""
    result = aggregate_per_period_with_court_time([], set())
    assert result == {}


# ─── Reserva que nunca jogou (sem subs in) ─────────────────────────────────


def test_player_never_in_court_no_intervals():
    """Reserva sem nenhuma sub 'in' não aparece no resultado."""
    starters = {100, 101, 102, 103, 104}
    # Player 200 não está em starters E não tem subs
    actions = []

    result = aggregate_per_period_with_court_time(actions, starters)

    assert 200 not in result
    # Mas todos os starters aparecem
    for pid in starters:
        assert pid in result


# ─── Bug Gobert: jogo recém-começado, starter não pode ter 12 min ─────────


def test_live_q1_starter_only_played_for_current_clock():
    """
    Bug reportado: Gobert começou Q1 mas jogo está em 11:54
    (apenas 0.1 min jogados). Sistema mostrava 12.0 min jogados
    porque fechava intervalo em 0.0 em vez do clock atual.

    Fix: passa live_period + live_clock_minutes pro aggregator.
    """
    starters = {100}
    # Sem actions ainda (jogo começou agora)
    actions = []

    # Q1 com 11:54 restantes (0.1 min jogados)
    result = aggregate_per_period_with_court_time(
        actions, starters,
        live_period=1,
        live_clock_minutes=11.9,
    )

    p1 = result[100][1]
    # Jogou de 12.0 → 11.9 = 0.1 min
    assert abs(p1["minutes_played"] - 0.1) < 0.01
    assert p1["intervals"] == [[12.0, 11.9]]


def test_live_q2_starter_q1_full_q2_partial():
    """
    Live em Q2 com 5:00 no relógio. Starter jogou Q1 inteiro +
    parte do Q2.
    Q1: 12 min (completo)
    Q2: 12 - 5 = 7 min (até agora)
    """
    starters = {100}
    actions = []  # nenhuma sub

    result = aggregate_per_period_with_court_time(
        actions, starters,
        live_period=2,
        live_clock_minutes=5.0,
    )

    # Q1 fecha em 0 (período anterior)
    assert result[100][1]["minutes_played"] == 12.0
    # Q2 fecha em 5.0 (live clock)
    assert abs(result[100][2]["minutes_played"] - 7.0) < 0.01
    assert result[100][2]["intervals"] == [[12.0, 5.0]]


def test_finalized_game_closes_at_zero():
    """
    Jogo finalizado: live_period=None → fecha em 0.0 normalmente.
    """
    starters = {100}
    actions = []

    result = aggregate_per_period_with_court_time(
        actions, starters,
        live_period=None,
        live_clock_minutes=None,
    )

    # Jogo finalizado, starter "jogou" 12 min (fechou em 0)
    assert result[100][1]["minutes_played"] == 12.0
