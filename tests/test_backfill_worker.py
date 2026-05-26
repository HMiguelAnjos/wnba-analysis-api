"""
Testes do backfill_worker — agendamento + guard de instância única.
Não testa o backfill em si (coberto em test_backfill_outcomes).
"""
from datetime import datetime, timezone
from unittest.mock import patch

from src.workers import backfill_worker
from src.workers.backfill_worker import _seconds_until_next_run


def _at(h, m=0):
    return datetime(2026, 5, 16, h, m, 0, tzinfo=timezone.utc)


def test_next_run_later_today_when_before_target():
    """09:00 UTC, alvo 12:00 → faltam 3h (no mesmo dia)."""
    with patch("src.workers.backfill_worker.datetime") as mock_dt:
        mock_dt.now.return_value = _at(9)
        secs = _seconds_until_next_run(hour_utc=12)
    assert secs == 3 * 3600


def test_next_run_tomorrow_when_past_target():
    """15:00 UTC, alvo 12:00 → rola pro dia seguinte (21h)."""
    with patch("src.workers.backfill_worker.datetime") as mock_dt:
        mock_dt.now.return_value = _at(15)
        secs = _seconds_until_next_run(hour_utc=12)
    assert secs == 21 * 3600


def test_next_run_always_positive_at_exact_target():
    """Exatamente no alvo → agenda pro próximo dia (não dispara loop)."""
    with patch("src.workers.backfill_worker.datetime") as mock_dt:
        mock_dt.now.return_value = _at(12, 0)
        secs = _seconds_until_next_run(hour_utc=12)
    assert secs == 24 * 3600


def test_start_worker_guard_single_instance():
    """Segundo start é no-op (não cria task duplicada)."""
    import asyncio

    async def _run_twice():
        backfill_worker._worker_started = False
        created = []
        real_create = asyncio.create_task

        def _spy(coro, **kw):
            created.append(kw.get("name"))
            t = real_create(coro, **kw)
            t.cancel()  # não deixa o loop infinito rodar de verdade
            return t

        with patch("asyncio.create_task", side_effect=_spy):
            await backfill_worker.start_backfill_worker()
            await backfill_worker.start_backfill_worker()
        return created

    created = asyncio.run(_run_twice())
    assert created.count("backfill_worker") == 1
