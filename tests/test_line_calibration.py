"""
Testes para build_report (Fase 8).

Cobre o caminho feliz (jsonl com mix de stats), edge cases (arquivo
vazio, linhas malformadas) e cálculo de MAE/bias.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def tmp_cache_dir(monkeypatch):
    """Setup temp CACHE_DIR e reload de config + module."""
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("CACHE_DIR", tmp)
    # Force reload do módulo de config
    import importlib
    import src.config
    importlib.reload(src.config)
    import src.services.line.line_calibration as lc
    importlib.reload(lc)
    return tmp, lc


def _write_log(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_report_no_log_returns_error(tmp_cache_dir):
    tmp, lc = tmp_cache_dir
    report = lc.build_report()
    assert "error" in report
    assert "não encontrado" in report["error"]


def test_report_empty_log(tmp_cache_dir):
    tmp, lc = tmp_cache_dir
    open(os.path.join(tmp, "line_log.jsonl"), "w").close()
    report = lc.build_report()
    assert report["total_records"] == 0
    assert all(b["count"] == 0 for b in report["by_stat"].values())


def test_report_aggregates_by_stat(tmp_cache_dir):
    tmp, lc = tmp_cache_dir
    now = datetime.now(timezone.utc).isoformat()
    _write_log(
        os.path.join(tmp, "line_log.jsonl"),
        [
            {"ts": now, "stat": "PTS", "our_line": 20.5, "projection": 21, "player_name": "A"},
            {"ts": now, "stat": "PTS", "our_line": 24.5, "projection": 23.8, "player_name": "B"},
            {"ts": now, "stat": "REB", "our_line": 5.5, "projection": 6, "player_name": "A"},
            {"ts": now, "stat": "AST", "our_line": 4.5, "projection": 4, "player_name": "B"},
        ],
    )
    report = lc.build_report()
    assert report["total_records"] == 4
    assert report["by_stat"]["PTS"]["count"] == 2
    assert report["by_stat"]["REB"]["count"] == 1
    assert report["by_stat"]["AST"]["count"] == 1


def test_report_computes_bet365_metrics(tmp_cache_dir):
    tmp, lc = tmp_cache_dir
    now = datetime.now(timezone.utc).isoformat()
    _write_log(
        os.path.join(tmp, "line_log.jsonl"),
        [
            # Caso real: Brunson (nossa 29.0, b365 30.5 → diff -1.5)
            {"ts": now, "stat": "PTS", "our_line": 29.0, "projection": 28.7,
             "bet365_line": 30.5, "player_name": "Brunson"},
            # Oubre (nossa 24.5, b365 24.5 → diff 0)
            {"ts": now, "stat": "PTS", "our_line": 24.5, "projection": 23.8,
             "bet365_line": 24.5, "player_name": "Oubre"},
        ],
    )
    report = lc.build_report()
    pts = report["by_stat"]["PTS"]
    assert pts["bet365_observations"] == 2
    # MAE = (1.5 + 0.0) / 2 = 0.75
    assert pts["bet365_mae"] == 0.75
    # Bias signed = (-1.5 + 0.0) / 2 = -0.75 (estamos subestimando)
    assert pts["bet365_bias"] == -0.75
    # Top divergence
    assert len(pts["top_divergences"]) == 2
    assert pts["top_divergences"][0]["player"] == "Brunson"


def test_report_skips_malformed_lines(tmp_cache_dir):
    tmp, lc = tmp_cache_dir
    log_path = os.path.join(tmp, "line_log.jsonl")
    now = datetime.now(timezone.utc).isoformat()
    with open(log_path, "w") as f:
        f.write(json.dumps({"ts": now, "stat": "PTS", "our_line": 20, "player_name": "A"}) + "\n")
        f.write("not json\n")
        f.write(json.dumps({"ts": now, "stat": "REB", "our_line": 5, "player_name": "B"}) + "\n")
    report = lc.build_report()
    assert report["total_records"] == 2  # malformada não conta


def test_report_filters_by_window(tmp_cache_dir):
    tmp, lc = tmp_cache_dir
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    _write_log(
        os.path.join(tmp, "line_log.jsonl"),
        [
            {"ts": old_ts, "stat": "PTS", "our_line": 20, "player_name": "Old"},
            {"ts": new_ts, "stat": "PTS", "our_line": 25, "player_name": "New"},
        ],
    )
    report = lc.build_report(window_days=7)
    assert report["total_records"] == 1
    assert report["by_stat"]["PTS"]["count"] == 1
