"""
Testes do enriquecimento do LineLog (mai/2026): garante que cada record
gravado contém os campos novos (game_id, decision, edge, real_line,
real_edge, real_book_count) que o BackTester precisa.
"""

from __future__ import annotations

import json
import os

import pytest

from src.services.line.line_log import log_line_calculation
from src.utils.stats import LineContext, LineResult


@pytest.fixture
def enabled_log(tmp_path, monkeypatch):
    """Liga LOG_LINE_CALC=1 e aponta CACHE_DIR pra tmp."""
    monkeypatch.setenv("LOG_LINE_CALC", "1")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    # `src.config.CACHE_DIR` é avaliado at import time — reload
    # garante que `_log_path()` no LineLog enxerga o tmp_path.
    import importlib
    import src.config
    importlib.reload(src.config)
    # Dedup é in-memory e persiste entre testes no mesmo processo —
    # zera pra cada teste ver um estado limpo.
    from src.services.line.line_log import reset_dedup
    reset_dedup()
    yield tmp_path


def _read_log(cache_dir) -> list[dict]:
    path = os.path.join(cache_dir, "line_log.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _ctx() -> LineContext:
    return LineContext(
        season_avg=20.0, last_10_avg=22.0, last_5_avg=24.0,
        season_minutes=32.0, current_stat=12, minutes_played=18.0,
        projected_end=24.0, current_fga=10, current_fgm=5,
    )


def _result() -> LineResult:
    return LineResult(line=22.5, raw_line=22.7, components={"prior": 20.0})


def test_log_writes_new_fields(enabled_log):
    log_line_calculation(
        player_id=2544, player_name="LeBron James", team_tricode="LAL",
        stat="PTS",
        line_context=_ctx(), line_result=_result(),
        projection=24.5,
        game_id="0042500001",
        decision="LEAN_OVER",
        edge=2.0,
        real_line=23.5,
        real_edge=1.0,
        real_book_count=1,
    )
    records = _read_log(enabled_log)
    assert len(records) == 1
    rec = records[0]
    # Campos novos que o BackTester precisa pra rodar
    assert rec["game_id"] == "0042500001"
    assert rec["decision"] == "LEAN_OVER"
    assert rec["edge"] == 2.0
    assert rec["real_line"] == 23.5
    assert rec["real_edge"] == 1.0
    assert rec["real_book_count"] == 1
    # Existentes
    assert rec["player_id"] == 2544
    assert rec["stat"] == "PTS"
    assert rec["our_line"] == 22.5


def test_log_handles_missing_optional_fields(enabled_log):
    """Sem real_*/decision (jogo sem odds API) → campos viram None/0."""
    log_line_calculation(
        player_id=100, player_name="X", team_tricode="LAL",
        stat="REB",
        line_context=_ctx(), line_result=_result(),
        projection=8.0,
        game_id="0042500002",
        decision="NEUTRAL",
        edge=0.5,
        # real_line/real_edge/real_book_count não passados
    )
    records = _read_log(enabled_log)
    rec = records[0]
    assert rec["real_line"] is None
    assert rec["real_edge"] is None
    assert rec["real_book_count"] == 0
    assert rec["decision"] == "NEUTRAL"


def test_log_disabled_writes_nothing(tmp_path, monkeypatch):
    """LOG_LINE_CALC=0 → nada escrito mesmo passando todos os args."""
    monkeypatch.setenv("LOG_LINE_CALC", "0")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    log_line_calculation(
        player_id=100, player_name="X", team_tricode="LAL", stat="PTS",
        line_context=_ctx(), line_result=_result(),
        game_id="g1", decision="LEAN_OVER",
    )
    assert _read_log(tmp_path) == []


# ─── Freio 1: DEDUP ──────────────────────────────────────────────────────────


def test_dedup_same_minute_writes_once(enabled_log):
    """
    Mesma predição (game/player/stat/minuto) chamada 5× = 1 registro.
    Simula o polling do feed gravando a mesma coisa repetidamente.
    """
    for _ in range(5):
        log_line_calculation(
            player_id=42, player_name="P", team_tricode="LAL", stat="PTS",
            line_context=_ctx(), line_result=_result(),
            game_id="0042500099", decision="LEAN_OVER", edge=2.0,
        )
    assert len(_read_log(enabled_log)) == 1


def test_dedup_different_minute_writes_each(enabled_log):
    """Minutos de jogo diferentes = registros distintos (evolução)."""
    from src.utils.stats import LineContext

    def ctx_at(gmr: float) -> LineContext:
        return LineContext(
            season_avg=20.0, last_10_avg=22.0, last_5_avg=24.0,
            season_minutes=32.0, current_stat=12, minutes_played=18.0,
            projected_end=24.0, game_minutes_remaining=gmr,
        )
    for gmr in (36.0, 24.0, 12.0):  # Q1, meio, Q3 → 3 buckets distintos
        log_line_calculation(
            player_id=7, player_name="P", team_tricode="LAL", stat="PTS",
            line_context=ctx_at(gmr), line_result=_result(),
            game_id="0042500098", decision="LEAN_OVER", edge=2.0,
        )
    assert len(_read_log(enabled_log)) == 3


def test_dedup_skipped_when_no_game_id(enabled_log):
    """Sem game_id não dá pra formar chave estável → grava sempre."""
    for _ in range(3):
        log_line_calculation(
            player_id=9, player_name="P", team_tricode="LAL", stat="PTS",
            line_context=_ctx(), line_result=_result(),
            game_id=None, decision="LEAN_OVER",
        )
    assert len(_read_log(enabled_log)) == 3


# ─── Freios 2 e 3: RETENÇÃO + TETO ───────────────────────────────────────────


def test_prune_drops_old_records(enabled_log):
    """Registros > max_age_days são descartados."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    from src.services.line.line_log import prune_log

    path = os.path.join(enabled_log, "line_log.jsonl")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=120)).isoformat()
    recent = (now - timedelta(days=5)).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        f.write(_json.dumps({"ts": old, "game_id": "g1"}) + "\n")
        f.write(_json.dumps({"ts": recent, "game_id": "g2"}) + "\n")

    stats = prune_log(path=path, max_age_days=90, max_size_mb=200)
    assert stats["total"] == 2
    assert stats["kept"] == 1
    assert stats["dropped_age"] == 1
    remaining = _read_log(enabled_log)
    assert len(remaining) == 1
    assert remaining[0]["game_id"] == "g2"


def test_prune_enforces_size_cap(enabled_log):
    """Passou do teto → corta os MAIS ANTIGOS, mantém os recentes."""
    import json as _json
    from datetime import datetime, timezone

    from src.services.line.line_log import prune_log

    path = os.path.join(enabled_log, "line_log.jsonl")
    now = datetime.now(timezone.utc).isoformat()
    # ~1KB por linha (padding) × 50 = ~50KB. Teto 0.02MB (~20KB) → corta.
    pad = "x" * 1000
    with open(path, "w", encoding="utf-8") as f:
        for i in range(50):
            f.write(_json.dumps({"ts": now, "i": i, "pad": pad}) + "\n")

    stats = prune_log(path=path, max_age_days=90, max_size_mb=0.02)
    assert stats["dropped_size"] > 0
    assert stats["final_size_mb"] <= 0.02 + 0.001  # dentro do teto
    remaining = _read_log(enabled_log)
    # Mantém os mais recentes (fim do arquivo) — último i preservado
    assert remaining[-1]["i"] == 49


def test_prune_noop_when_file_absent(tmp_path):
    from src.services.line.line_log import prune_log
    stats = prune_log(path=os.path.join(tmp_path, "nope.jsonl"))
    assert stats["existed"] is False
