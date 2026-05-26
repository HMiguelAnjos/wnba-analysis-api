"""
Background worker que roda o backfill de outcomes + manutenção do
line_log 1×/dia, dentro do próprio processo da API.

Por que in-app (e não Railway Cron):
  - O backend já fica Online 24/7 → não precisa de serviço/cron extra.
  - Roda no MESMO processo que tem o volume montado → zero config de
    volume compartilhado.
  - Custo adicional: zero.

Garantias (mesma filosofia do live_games_worker):
  - Uma instância por processo (guard _worker_started).
  - Erro NUNCA derruba a aplicação — loga e tenta de novo no dia seguinte.
  - O backfill faz I/O de rede (PBP histórico) → roda em thread separada
    (asyncio.to_thread) pra não travar o event loop.
  - Só inicia se LOG_LINE_CALC=1 (sem logging não há o que preencher).

Agendamento: alvo 12:00 UTC (~09:00 BRT) — toda a rodada da NBA da
noite anterior já encerrou. Loop recalcula o próximo alvo a cada volta.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_worker_started = False

# Hora-alvo (UTC) do run diário. 12:00 UTC ≈ 09:00 BRT — jogos da NBA
# (que terminam ~05:00 UTC no pior caso) já encerraram com folga.
_TARGET_HOUR_UTC = 12


def _seconds_until_next_run(hour_utc: int = _TARGET_HOUR_UTC) -> float:
    """Segundos até o próximo HH:00 UTC alvo (sempre no futuro)."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _do_backfill_and_prune() -> None:
    """
    Executado numa thread (bloqueante: faz fetch de PBP). Best-effort —
    qualquer exceção é capturada pelo caller.
    """
    from scripts.backfill_outcomes import _default_log_path, backfill
    from src.services.line.line_log import prune_log

    path = _default_log_path()
    # Prune ANTES do backfill (de propósito): prune_log lê o arquivo em
    # streaming (seguro de memória) e descarta o backlog > 90d / acima do
    # teto. Encolher primeiro evita que o backfill — que carrega TODOS os
    # registros na RAM de uma vez — estoure memória no 1º run com backlog
    # grande, e reduz fetches de PBP de jogos antigos.
    prune_stats = prune_log(path=path)
    logger.info("Prune diário (retenção+teto): %s", prune_stats)
    res = backfill(path, dry_run=False)
    logger.info("Backfill diário concluído: %s", res)


async def _run() -> None:
    while True:
        wait_s = _seconds_until_next_run()
        logger.info(
            "Backfill worker: próximo run em %.1f h", wait_s / 3600.0
        )
        await asyncio.sleep(wait_s)
        try:
            await asyncio.to_thread(_do_backfill_and_prune)
        except Exception as exc:
            # Nunca derruba o app — tenta de novo no próximo ciclo.
            logger.warning("Backfill worker: falha no run (%s)", exc)
        # Pequeno respiro pra garantir que sai da janela do minuto-alvo
        # e o próximo _seconds_until_next_run aponte pro dia seguinte.
        await asyncio.sleep(60)


async def start_backfill_worker() -> None:
    """
    Inicia o worker como task asyncio de longa duração.
    Chamar uma vez no startup. No-op se já iniciado.
    """
    global _worker_started
    if _worker_started:
        logger.warning(
            "Backfill worker já rodando — ignorando start duplicado."
        )
        return
    _worker_started = True
    asyncio.create_task(_run(), name="backfill_worker")
    logger.info(
        "Backfill worker iniciado (diário ~%02d:00 UTC).", _TARGET_HOUR_UTC
    )
