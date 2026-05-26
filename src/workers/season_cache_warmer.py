"""
Background worker that pre-warms the season-averages cache for players in
today's games.

Why:
    stats.nba.com periodically blocks cloud IPs (Railway/AWS/etc). When a
    user clicks a game and we hit stats.nba.com on the request path, the
    request can hang or fail. By pre-fetching season averages once per day,
    the PersistentCache will be warm before any user request — so user-facing
    requests never depend on stats.nba.com being available right that second.

How it works:
    - Runs ONCE per day at 09:00 UTC (configurable). At that hour every NBA
      game is over (~07:00 UTC end of west-coast tip-offs) and next round
      doesn't start for ~14h, so the cache stays fresh through the next
      day's games.
    - On startup (after a short delay) does an immediate first pass — gets
      the cache warm if the container was just deployed.
    - For each in-progress/final game today, reads the boxscore and calls
      _get_season_averages for every active player. Caps at MAX_PLAYERS to
      avoid unbounded ScraperAPI burn during high-volume nights.
    - On failure (stats.nba.com block, ScraperAPI quota, etc.), retries with
      backoff: 30min, 1h, 2h, 4h. Then waits for the next daily slot.

ScraperAPI economics:
    Each player → 1 stats.nba.com call → 1-10 ScraperAPI credits (residential
    proxy is more expensive). With ~50 unique players/day × ~5 credits each =
    ~250 credits/day from warming. The bulk of remaining usage comes from
    on-demand cache MISS when users open players not yet warmed.

Guarantees:
    - Single instance per process (guarded by _warmer_started).
    - Errors never crash the application.
    - Skips not_started games (no boxscore rosters available yet).
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta, timezone

from src.cache.live_games_cache import AbstractLiveGamesCache
from src.services.live_analysis_service import LiveAnalysisService
from src.services.live_game_service import LiveGameService
from src.schemas.live_schemas import LivePlayerStatsSchema

logger = logging.getLogger(__name__)

_warmer_started = False

# ── Knobs (calibrados pra ScraperAPI Hobby = 100k créditos/mês) ─────────────
# Slot UTC pra warm diário. 09:00 UTC = 06:00 ET / 03:00 PT — todos jogos
# acabaram (last west-coast tip-off termina ~07:00 UTC), próximo jogo começa
# ~23:00 UTC. 14h de margem antes da próxima rodada.
DEFAULT_WARM_HOUR_UTC = 9

# Tentativas em caso de falha do warm. Backoff progressivo até dar certo
# ou esgotar; depois espera o próximo slot diário.
RETRY_BACKOFFS_S = [30 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60]   # 30min → 4h

# Cap de jogadores pra warm. Em playoffs (1-4 jogos/noite) raramente passa
# de 60-80 jogadores; o cap protege contra uma noite incomum + reduz ScraperAPI.
# Prioriza titulares + alta minutagem (rotação principal).
MAX_PLAYERS_TO_WARM = 50


async def start_season_cache_warmer(
    live_cache: AbstractLiveGamesCache,
    live_game: LiveGameService,
    live_analysis: LiveAnalysisService,
    season: str,
    initial_delay_s: int = 30,      # let live games worker populate first
    warm_hour_utc: int = DEFAULT_WARM_HOUR_UTC,
) -> None:
    """Launch the warmer as a long-running asyncio task. Call once on startup."""
    global _warmer_started
    if _warmer_started:
        logger.warning("Season cache warmer already running — skipping duplicate start.")
        return
    _warmer_started = True
    asyncio.create_task(
        _run(live_cache, live_game, live_analysis, season, initial_delay_s, warm_hour_utc),
        name="season_cache_warmer",
    )
    logger.info(
        "Season cache warmer scheduled: initial pass in %ds, then daily at %02d:00 UTC (season=%s).",
        initial_delay_s, warm_hour_utc, season,
    )


async def _run(
    live_cache: AbstractLiveGamesCache,
    live_game: LiveGameService,
    live_analysis: LiveAnalysisService,
    season: str,
    initial_delay_s: int,
    warm_hour_utc: int,
) -> None:
    """Main warmer loop. Runs indefinitely until cancelled."""
    # Initial warm shortly after startup — cobre cold start / redeploy.
    # PORÉM: se o cache do disco já carregou entradas frescas (via volume
    # persistente), pula o initial warm — todo deploy economiza ~350
    # créditos ScraperAPI. Fundamental quando deploys são frequentes (dev).
    await asyncio.sleep(initial_delay_s)
    if _cache_is_warm_enough(live_analysis):
        logger.info(
            "Warmer: cache do disco já está populado (%d entradas) — "
            "pulando initial warm, próximo só no slot diário das %02d:00 UTC.",
            live_analysis._cache.count_prefix("season_avg:"), warm_hour_utc,
        )
    else:
        await _warm_with_retries(live_cache, live_game, live_analysis, season, "initial")

    while True:
        sleep_s = _seconds_until_next_slot(warm_hour_utc)
        next_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_s)
        logger.info(
            "Warmer: dormindo até próximo slot diário em %s (UTC) (em %.1f h).",
            next_at.strftime("%Y-%m-%d %H:%M"), sleep_s / 3600,
        )
        try:
            await asyncio.sleep(sleep_s)
            await _warm_with_retries(live_cache, live_game, live_analysis, season, "daily")
        except asyncio.CancelledError:
            logger.info("Season cache warmer shutting down.")
            raise
        except Exception as exc:
            logger.error("Cache warmer cycle failed (will retry tomorrow): %s", exc)


def _cache_is_warm_enough(live_analysis: LiveAnalysisService) -> bool:
    """
    Heurística: se o cache do disco já tem >= MIN_WARM_THRESHOLD entradas
    válidas pro prefix `season_avg:`, considera "warm o suficiente" e
    pula o initial fetch. Threshold conservador pra cobrir noites com
    poucos jogos (playoffs em rodada única = ~16 jogadores ativos).
    """
    MIN_WARM_THRESHOLD = 16
    count = live_analysis._cache.count_prefix("season_avg:")
    return count >= MIN_WARM_THRESHOLD


def _seconds_until_next_slot(warm_hour_utc: int) -> float:
    """Calcula quantos segundos até o próximo slot {warm_hour_utc}:00 UTC."""
    now = datetime.now(timezone.utc)
    today_slot = now.replace(hour=warm_hour_utc, minute=0, second=0, microsecond=0)
    next_slot = today_slot if now < today_slot else today_slot + timedelta(days=1)
    return (next_slot - now).total_seconds()


async def _warm_with_retries(
    live_cache: AbstractLiveGamesCache,
    live_game: LiveGameService,
    live_analysis: LiveAnalysisService,
    season: str,
    label: str,
) -> None:
    """
    Tenta warm com backoff progressivo. Se a 1ª tentativa falhar
    (stats.nba.com bloqueado / ScraperAPI fora / etc.), tenta de novo
    em 30min, 1h, 2h, 4h. Total ~7.5h de janela — depois disso ainda
    sobram horas até o próximo slot.
    """
    for attempt, backoff_s in enumerate([0, *RETRY_BACKOFFS_S]):
        if backoff_s > 0:
            logger.info(
                "Warmer (%s): retry %d/%d em %d min após falha anterior.",
                label, attempt, len(RETRY_BACKOFFS_S), backoff_s // 60,
            )
            await asyncio.sleep(backoff_s)
        try:
            ok = await asyncio.to_thread(
                _warm_once, live_cache, live_game, live_analysis, season,
            )
            if ok:
                return
            # Sem dados pra warming (ex: nenhum jogo hoje) NÃO é falha — sai.
            logger.info("Warmer (%s): nada pra fazer (sem jogos elegíveis).", label)
            return
        except Exception as exc:
            logger.error("Warmer (%s) attempt %d falhou: %s", label, attempt + 1, exc)
    logger.error(
        "Warmer (%s): esgotou todas as %d retries — esperando próximo slot diário.",
        label, len(RETRY_BACKOFFS_S) + 1,
    )


def _warm_once(
    live_cache: AbstractLiveGamesCache,
    live_game: LiveGameService,
    live_analysis: LiveAnalysisService,
    season: str,
) -> bool:
    """
    Single warming pass. Synchronous (runs in thread).
    Returns True se executou pelo menos 1 fetch, False se não havia jogos.
    Levanta exceção se houver falha real (será capturada pelo retry).
    """
    snapshot = live_cache.get_snapshot()
    if snapshot is None:
        logger.info("Warmer: nenhum snapshot de jogos ainda — pulando.")
        return False

    games = [g for g in snapshot.data.games if g.game_status in ("in_progress", "final")]
    if not games:
        logger.info("Warmer: nenhum jogo in-progress/final hoje.")
        return False

    # Coleta todos os jogadores ativos (que jogaram pelo menos 1 minuto)
    # com seus minutos pra ranking.
    all_players: list[LivePlayerStatsSchema] = []
    for g in games:
        try:
            bs = live_game.get_live_boxscore(g.game_id)
            for team in (bs.home_team, bs.away_team):
                all_players.extend(team.players)
        except Exception as exc:
            logger.warning("Warmer: falha ao buscar boxscore %s: %s", g.game_id, exc)

    if not all_players:
        logger.info("Warmer: sem jogadores ativos nos boxscores.")
        return False

    # Prioriza titulares + alta minutagem (rotação principal). Limita ao top
    # MAX_PLAYERS_TO_WARM pra evitar burn em noite atípica de muitos jogos.
    # Critério de ordenação: titular primeiro, depois minutos descendente.
    deduped = {p.player_id: p for p in all_players}.values()
    ranked = sorted(deduped, key=lambda p: (not p.is_starter, -p.minutes))
    selected = list(ranked)[:MAX_PLAYERS_TO_WARM]
    skipped = len(deduped) - len(selected)
    if skipped > 0:
        logger.info(
            "Warmer: %d jogadores total, warmando top %d (cap), pulando %d de fim de banco.",
            len(deduped), len(selected), skipped,
        )

    logger.info(
        "Warmer: começando season-avg fetch pra %d jogadores em %d jogos.",
        len(selected), len(games),
    )

    warmed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=min(len(selected), 16)) as pool:
        futures = {
            pool.submit(live_analysis._get_season_averages, p.player_id, season): p.player_id
            for p in selected
        }
        for fut in as_completed(futures):
            try:
                if fut.result() is not None:
                    warmed += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                logger.debug("Warmer: jogador %d falhou: %s", futures[fut], exc)

    # Estimativa de créditos ScraperAPI gastos (cada call ~5-10 créditos
    # em residential premium). Usa 7 como média conservadora pra reportagem.
    est_credits = warmed * 7
    logger.info(
        "Warmer: cycle completo — %d cached, %d falharam. ~%d créditos ScraperAPI estimados.",
        warmed, failed, est_credits,
    )
    return warmed + failed > 0
