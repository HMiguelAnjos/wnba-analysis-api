"""
Background worker that polls the NBA live scoreboard at a fixed interval
and writes the result to the shared live games cache.

Guarantees:
  - Only one worker instance runs per process (guarded by _worker_started flag).
  - Concurrent fetches are skipped if the previous one hasn't finished.
  - Errors never crash the application; the last valid snapshot is preserved.
  - Configurable via LIVE_POLL_INTERVAL_MS env var (default: 2000 ms).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from src.cache.live_games_cache import AbstractLiveGamesCache
from src.schemas.live_schemas import TodayGamesSchema

logger = logging.getLogger(__name__)

# Guards against duplicate workers if the app initialises more than once
_worker_started = False


async def start_live_games_worker(
    cache: AbstractLiveGamesCache,
    fetch_fn: Callable[[], TodayGamesSchema],
    interval_ms: int = 2000,
) -> None:
    """
    Launch the worker as a long-running asyncio task.
    Call once during application startup.

    Args:
        cache:       Shared cache instance to write snapshots into.
        fetch_fn:    Synchronous callable that fetches live games from the NBA API.
        interval_ms: Polling interval in milliseconds (default: 2000).
    """
    global _worker_started

    if _worker_started:
        logger.warning("Live games worker already running — skipping duplicate start.")
        return

    _worker_started = True
    asyncio.create_task(_run(cache, fetch_fn, interval_ms), name="live_games_worker")
    logger.info("Live games worker started (interval=%d ms).", interval_ms)


async def _run(
    cache: AbstractLiveGamesCache,
    fetch_fn: Callable[[], TodayGamesSchema],
    interval_ms: int,
) -> None:
    """Main worker loop. Runs indefinitely until the task is cancelled."""
    _fetch_in_progress = False

    while True:
        if _fetch_in_progress:
            logger.debug("Previous fetch still in progress — skipping this tick.")
        else:
            _fetch_in_progress = True
            try:
                # Run the blocking NBA API call in a thread pool
                data: TodayGamesSchema = await asyncio.to_thread(fetch_fn)
                cache.set_snapshot(data)

                snapshot = cache.get_snapshot()
                age = snapshot.age_ms if snapshot else 0
                logger.info(
                    "Cache updated — %d game(s), snapshot age: %d ms.",
                    len(data.games), age,
                )
            except asyncio.CancelledError:
                logger.info("Live games worker shutting down.")
                raise
            except Exception as exc:
                logger.error("Worker fetch failed (last snapshot preserved): %s", exc)
            finally:
                _fetch_in_progress = False

        await asyncio.sleep(interval_ms / 1000)
