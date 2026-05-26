"""
Testes do BackTester (Fase 10).

Cobre framework + cálculos básicos de hit rate/ROI. Cenários reais
dependem de dataset com `decision` e `actual_outcome` populados, que
hoje só existe em fixture sintética.
"""

import json
import os
import tempfile

from src.services.backtester import BackTester


bt = BackTester()


def _write_log(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_run_with_no_log_returns_empty():
    result = bt.run("/tmp/never_existed.jsonl")
    assert result.total_bets == 0
    assert result.hit_rate == 0.0
    assert result.roi_pct == 0.0


def test_run_filters_records_without_outcome():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "log.jsonl")
    _write_log(path, [
        {"decision": "STRONG_OVER", "our_line": 20, "actual_outcome": 24, "stat": "PTS"},
        {"decision": "STRONG_OVER", "our_line": 20, "actual_outcome": None, "stat": "PTS"},
        {"decision": None, "our_line": 20, "actual_outcome": 24, "stat": "PTS"},
    ])
    result = bt.run(path)
    assert result.total_bets == 1   # só o primeiro tem ambos


def test_strong_over_winning_bet():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "log.jsonl")
    _write_log(path, [
        {"decision": "STRONG_OVER", "our_line": 20, "actual_outcome": 25, "player_name": "A", "stat": "PTS"},
    ])
    result = bt.run(path)
    assert result.total_bets == 1
    assert result.wins == 1
    assert result.losses == 0
    # ROI com -110: vence ganha 100/110 = 0.909
    assert result.roi_pct > 80


def test_strong_under_losing_bet():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "log.jsonl")
    _write_log(path, [
        {"decision": "STRONG_UNDER", "our_line": 20, "actual_outcome": 25, "player_name": "A", "stat": "PTS"},
    ])
    result = bt.run(path)
    assert result.wins == 0
    assert result.losses == 1
    assert result.roi_pct == -100  # 1 unidade perdida


def test_filter_by_min_decision():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "log.jsonl")
    _write_log(path, [
        {"decision": "STRONG_OVER", "our_line": 20, "actual_outcome": 25, "stat": "PTS"},
        {"decision": "LEAN_OVER", "our_line": 20, "actual_outcome": 25, "stat": "PTS"},
        {"decision": "NEUTRAL", "our_line": 20, "actual_outcome": 25, "stat": "PTS"},
    ])
    only_strong = bt.run(path, min_decision="STRONG")
    assert only_strong.total_bets == 1

    strong_and_lean = bt.run(path, min_decision="LEAN")
    assert strong_and_lean.total_bets == 2

    everything = bt.run(path, min_decision="ALL")
    assert everything.total_bets == 3


def test_estimate_runnable_no_log():
    info = BackTester.estimate_runnable("/tmp/missing.jsonl")
    assert info["runnable"] is False


def test_estimate_runnable_insufficient_data():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "log.jsonl")
    _write_log(path, [
        {"decision": "STRONG_OVER", "our_line": 20, "actual_outcome": 22, "stat": "PTS"},
    ])
    info = BackTester.estimate_runnable(path)
    assert info["runnable"] is False
    assert info["with_outcome"] == 1
    assert info["with_decision"] == 1


def test_breakeven_thresholds():
    """Sanity: hit rate breakeven com -110 = 52.4%."""
    from src.services.backtester.backtester import DEFAULT_BREAKEVEN_HIT_RATE
    assert 0.523 < DEFAULT_BREAKEVEN_HIT_RATE < 0.525


def test_buckets_by_decision():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "log.jsonl")
    _write_log(path, [
        {"decision": "STRONG_OVER", "our_line": 20, "actual_outcome": 25, "stat": "PTS"},
        {"decision": "STRONG_OVER", "our_line": 20, "actual_outcome": 18, "stat": "PTS"},
        {"decision": "LEAN_OVER", "our_line": 20, "actual_outcome": 22, "stat": "PTS"},
    ])
    result = bt.run(path, min_decision="LEAN")
    assert result.bets_by_decision["STRONG_OVER"]["bets"] == 2
    assert result.bets_by_decision["STRONG_OVER"]["wins"] == 1
    assert result.bets_by_decision["LEAN_OVER"]["bets"] == 1
    assert result.bets_by_decision["LEAN_OVER"]["wins"] == 1
