"""
Patches aplicados ao `nba_api` no startup.

Por que existe: em maio/2026 a NBA adicionou proteção anti-bot no
`cdn.nba.com` e no `stats.nba.com` que retorna **403 Access Denied**
quando a requisição não envia o header `Referer: https://www.nba.com/`.

O `nba_api` (versão atual ao escrever este código) envia um User-Agent
de browser mas NÃO envia `Referer` — o que dispara o bloqueio. Sem isso,
TUDO que depende da NBA quebra: scoreboard, boxscore, lineups, season
stats, etc.

Fix: monkey-patch nos dicionários `STATS_HEADERS` de ambos os módulos
HTTP do nba_api (live + stats), adicionando o Referer.

Aplicado uma vez no startup do app via `apply_nba_api_patches()` —
chamado pelo main.py antes de qualquer service ser instanciado.

Se o nba_api atualizar e remover essa necessidade, esse patch vira
no-op (já tem o header). Se outro header passar a ser obrigatório,
adicionar aqui.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Headers que a NBA passou a exigir. Mantemos um dict pequeno e claro
# pra facilitar atualização se mais um header virar obrigatório.
_REQUIRED_HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}


def apply_nba_api_patches() -> None:
    """
    Injeta headers obrigatórios nos clientes HTTP do nba_api.

    Idempotente — chamar várias vezes não causa efeito colateral.
    """
    patched_any = False

    # 1. Live API (cdn.nba.com): usado por scoreboard, boxscore, etc.
    try:
        from nba_api.live.nba.library import http as live_http  # type: ignore
        for k, v in _REQUIRED_HEADERS.items():
            if live_http.STATS_HEADERS.get(k) != v:
                live_http.STATS_HEADERS[k] = v
                patched_any = True
        # Algumas versões do nba_api copiam o dict pra um atributo de classe
        # no momento da definição — atualizamos lá também por garantia.
        if hasattr(live_http, "NBALiveHTTP"):
            live_http.NBALiveHTTP.headers = live_http.STATS_HEADERS
    except Exception as exc:
        logger.warning("Não foi possível patchear nba_api.live HTTP: %s", exc)

    # 2. Stats API (stats.nba.com): usado por ScoreboardV2, PlayerCareerStats,
    #    PlayByPlayV3, etc. Pode usar um dict diferente do live.
    try:
        from nba_api.stats.library import http as stats_http  # type: ignore
        if hasattr(stats_http, "STATS_HEADERS"):
            for k, v in _REQUIRED_HEADERS.items():
                if stats_http.STATS_HEADERS.get(k) != v:
                    stats_http.STATS_HEADERS[k] = v
                    patched_any = True
            if hasattr(stats_http, "NBAStatsHTTP"):
                stats_http.NBAStatsHTTP.headers = stats_http.STATS_HEADERS
    except Exception as exc:
        logger.warning("Não foi possível patchear nba_api.stats HTTP: %s", exc)

    if patched_any:
        logger.info(
            "nba_api headers patched (Referer/Origin) — bypass 403 do CDN/Stats NBA"
        )
    else:
        logger.debug("nba_api headers já tinham os campos obrigatórios — no-op")
