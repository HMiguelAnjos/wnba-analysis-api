import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from src.schemas.analysis_schemas import (
    DashboardSchema,
    GameStatSchema,
    PointsByPeriodAverageSchema,
    SeasonAnalysisSchema,
)
from src.schemas.live_schemas import (
    GamePreviewSchema,
    GameRotationsSchema,
    HotRankingSchema,
    LineupGameSchema,
    LiveBoxscoreSchema,
    LiveGameAnalysisSchema,
    LiveGamesCachedResponseSchema,
    LivePlayerComparisonSchema,
    TodayGamesSchema,
)
from src.schemas.nba_schemas import (
    GameLogSchema,
    HotBoardSchema,
    PlayByPlayEventSchema,
    PlayerSchema,
    PointsByPeriodSchema,
    ProbableLineupSchema,
    TeamBoardSchema,
)
from src.cache.live_games_cache import InMemoryLiveGamesCache
from src.config import ALLOWED_ORIGINS, ENABLE_LIVE_WORKER, LIVE_POLL_INTERVAL_MS, PORT, STATS_PROXY
from src.nba_api_patches import apply_nba_api_patches

# CRÍTICO: aplicar os patches do nba_api ANTES de qualquer service ser
# instanciado. Em maio/2026 a NBA passou a exigir o header Referer no
# cdn.nba.com e stats.nba.com — sem isso devolve 403 e tudo quebra.
apply_nba_api_patches()
from src.services.anomaly_service import AnomalyService
from src.services.live_analysis_service import LiveAnalysisService
from src.services.live_game_service import LiveGameService
from src.services.live_pbp_service import LivePbpService
from src.services.nba_service import NbaService
from src.services.player_analysis_service import PlayerAnalysisService
from src.workers.backfill_worker import start_backfill_worker
from src.workers.live_games_worker import start_live_games_worker
from src.workers.season_cache_warmer import start_season_cache_warmer

# Logging: INFO/WARNING vão pro stdout, ERROR/CRITICAL vão pro stderr.
#
# Por que isso importa: Railway (e maioria das clouds) classifica logs por
# stream — stdout vira "info", stderr vira "error" no painel. Se mandar
# tudo pro stderr (default do basicConfig), todos os logs aparecem como
# error level mesmo com `[INFO]` na mensagem, poluindo filtros de erro.
#
# Splitting por handler resolve: ERROR+ aparece corretamente no filtro de
# erro do Railway; INFO/WARNING ficam no stdout sem falso-positivar.
import sys as _sys

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_stdout_handler = logging.StreamHandler(_sys.stdout)
_stdout_handler.setLevel(logging.INFO)
# Filtra: só INFO e WARNING vão pra stdout (ERROR+ vai pra stderr abaixo)
_stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)
_stdout_handler.setFormatter(_log_formatter)

_stderr_handler = logging.StreamHandler(_sys.stderr)
_stderr_handler.setLevel(logging.ERROR)
_stderr_handler.setFormatter(_log_formatter)

_root = logging.getLogger()
_root.setLevel(logging.INFO)
# Remove handlers default do basicConfig (chamado implicitamente em outros
# imports), garante que só os nossos dois rodam.
for _h in list(_root.handlers):
    _root.removeHandler(_h)
_root.addHandler(_stdout_handler)
_root.addHandler(_stderr_handler)


# ── Uvicorn / FastAPI: forçar uso dos NOSSOS handlers ───────────────────────
# Uvicorn instala loggers próprios que vão direto pro stderr ignorando o
# root handler, daí no Railway aparecia "INFO: Started server process" como
# error level. Limpamos os handlers do Uvicorn e fazemos propagar pro root
# (que tem o split stdout/stderr correto).
class _SuppressBindAddrFilter(logging.Filter):
    """
    Suprime mensagem padrão do Uvicorn 'Uvicorn running on http://0.0.0.0:PORT'
    porque (a) o 0.0.0.0 é um bind interno feio de mostrar e (b) a gente
    loga uma versão amigável logo abaixo no lifespan.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "0.0.0.0" in msg or "127.0.0.1" in msg:
            return False
        return True


for _uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _ulog = logging.getLogger(_uvicorn_logger_name)
    _ulog.handlers.clear()
    _ulog.propagate = True

logging.getLogger("uvicorn.error").addFilter(_SuppressBindAddrFilter())

# ------------------------------------------------------------------ #
# Shared instances                                                    #
# ------------------------------------------------------------------ #

nba = NbaService()
analysis = PlayerAnalysisService(nba)
live_game = LiveGameService()
live_pbp = LivePbpService()
# RotationProvider injeta NbaService.aggregate_historical_pbp_per_period
# como fetcher pra computar production_by_period (rates pts/ast/reb por
# período, derivado do PBP histórico × histograma do nbarotations).
# Falha silenciosa: se PBP indisponível, production_by_period fica None.
from src.services.rotation import RotationProvider
rotation_provider = RotationProvider(
    pbp_fetcher=nba.aggregate_historical_pbp_per_period,
    final_margin_fetcher=nba.get_final_margin,
)

# OddsService (mai/2026, The Odds API): lê linhas reais de player props.
# Off por padrão — ativa setando ENABLE_REAL_ODDS=1 + ODDS_API_KEY.
# Sem credenciais ou flag desligada, fica None e o schema devolve
# real_line=None (front renderiza só o synthetic).
from src.config import (
    ENABLE_REAL_ODDS,
    ODDS_API_KEY,
    ODDS_BOOKMAKERS,
    ODDS_REGIONS,
)
from src.services.odds import OddsService
odds_service: OddsService | None = None
if ENABLE_REAL_ODDS and ODDS_API_KEY:
    odds_service = OddsService(
        api_key=ODDS_API_KEY,
        regions=ODDS_REGIONS,
        bookmakers=ODDS_BOOKMAKERS,
    )

live_analysis = LiveAnalysisService(
    live_game, analysis,
    pbp_service=live_pbp,
    rotation_provider=rotation_provider,
    odds_service=odds_service,
)
anomaly = AnomalyService()
from src.services.hot_board_service import HotBoardService
hot_board = HotBoardService()
from src.services.team_board_service import TeamBoardService
team_board = TeamBoardService()
live_cache = InMemoryLiveGamesCache()

MAX_LAST_GAMES = 20

# WNBA fork: temporada/formato vêm do config de liga (src/league.py).
# WNBA = 'YYYY' (mai–out); NBA = 'YYYY-YY' (out–jun). Override via env
# DEFAULT_SEASON (ex.: 2025 = última temporada completa pra teste).
from src.league import DEFAULT_SEASON, current_season as _current_season  # noqa: E402,F401


# ------------------------------------------------------------------ #
# App lifespan (startup / shutdown)                                   #
# ------------------------------------------------------------------ #

@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_logger = logging.getLogger(__name__)
    from src.league import LEAGUE_NAME
    # ASCII puro: evita UnicodeEncodeError no console Windows (cp1252).
    _startup_logger.info(
        "[%s] Analysis API ready - port %d - season %s",
        LEAGUE_NAME, PORT, _current_season(),
    )
    if ENABLE_LIVE_WORKER:
        # fetch_scoreboard_smart faz fallback pra ScoreboardV2 quando o
        # NBA Live ainda mostra "ontem" (a NBA rola o dia só ao meio-dia
        # ET — antes disso, em BRT, o usuário veria só jogos finalizados).
        await start_live_games_worker(
            cache=live_cache,
            fetch_fn=live_game.fetch_scoreboard_smart,
            interval_ms=LIVE_POLL_INTERVAL_MS,
        )
        # Pre-warm season averages so user requests don't depend on
        # stats.nba.com being available right that second.
        await start_season_cache_warmer(
            live_cache=live_cache,
            live_game=live_game,
            live_analysis=live_analysis,
            season=_current_season(),
        )
    else:
        _startup_logger.info("Live games worker disabled (ENABLE_LIVE_WORKER=false).")

    # Backfill diário (predição×resultado real) — só se o logging estiver
    # ligado. Roda no próprio processo (tem o volume) — sem cron/serviço
    # extra no Railway. Best-effort: erro não derruba o app.
    from src.services.line.line_log import is_enabled as _line_log_enabled
    if _line_log_enabled():
        await start_backfill_worker()
    else:
        _startup_logger.info(
            "Backfill worker desligado (LOG_LINE_CALC != 1)."
        )
    yield


app = FastAPI(
    title="WNBA Analysis API",
    description="Estatísticas da WNBA para inteligência de apostas (fork do NBA Analysis API)",
    version="0.4.0-wnba",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # Aceita qualquer subdomínio do Railway (front em produção,
    # PR previews, etc.) sem precisar atualizar a env var manualmente.
    # *.up.railway.app cobre os deploys gerados; *.railway.app cobre
    # domínios custom mais curtos. Vercel também incluído por garantia.
    # nine6.com.br cobre o domínio OFICIAL do produto (clutchpro.nine6.com.br
    # e qualquer subdomínio/apex), sem depender de env var.
    allow_origin_regex=(
        r"https://.*\.(up\.)?railway\.app"
        r"|https://.*\.vercel\.app"
        r"|https://([a-z0-9-]+\.)*nine6\.com\.br"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _season_query(default: str = DEFAULT_SEASON) -> str:
    return Query(default, description="Temporada no formato YYYY-YY, ex: 2024-25")


def _last_games_query() -> int:
    return Query(
        10,
        ge=1,
        le=MAX_LAST_GAMES,
        description=f"Número de jogos a analisar (máx: {MAX_LAST_GAMES})",
    )


# ------------------------------------------------------------------ #
# Basic routes                                                        #
# ------------------------------------------------------------------ #

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/server-ip")
def server_ip():
    """Retorna o IP público do servidor (Railway). Use para configurar whitelist de proxy."""
    import requests as req
    try:
        r = req.get("https://api.ipify.org?format=json", timeout=5)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/debug/proxy-test")
def proxy_test():
    """Testa o proxy em HTTP e HTTPS separadamente para diagnosticar problemas de tunneling."""
    import requests as req
    proxies = {"http": STATS_PROXY, "https": STATS_PROXY} if STATS_PROXY else None
    results = {"proxy_configured": bool(STATS_PROXY)}

    # Teste 1: HTTP simples pelo proxy (sem CONNECT tunneling)
    try:
        r = req.get("http://httpbin.org/ip", proxies=proxies, timeout=10, verify=False)
        results["http_test"] = {"status": r.status_code, "body": r.json()}
    except Exception as exc:
        results["http_test"] = {"error": type(exc).__name__, "detail": str(exc)}

    # Teste 2: HTTPS pelo proxy (requer CONNECT tunneling)
    try:
        r = req.get("https://httpbin.org/ip", proxies=proxies, timeout=10, verify=False)
        results["https_test"] = {"status": r.status_code, "body": r.json()}
    except Exception as exc:
        results["https_test"] = {"error": type(exc).__name__, "detail": str(exc)}

    return results


@app.get("/debug/nba-stats")
def debug_nba_stats():
    """
    Quick diagnostic: hits stats.nba.com directly with browser-like headers.

    Use this to confirm whether the current host (e.g. Railway) is being
    blocked. If status != 200 here but works locally, stats.nba.com is
    blocking the cloud IP — set STATS_PROXY to route through a residential
    proxy.
    """
    import time
    import requests

    url = "https://stats.nba.com/stats/playergamelog"
    params = {"PlayerID": "2544", "Season": "2024-25", "SeasonType": "Regular Season"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
    }
    proxies = {"http": STATS_PROXY, "https": STATS_PROXY} if STATS_PROXY else None

    # ScraperAPI terminates TLS itself — must skip cert verification.
    # Other proxies (Webshare residential, etc.) use CONNECT tunneling
    # and work fine with normal SSL verification.
    verify_ssl = not (STATS_PROXY and "scraperapi" in STATS_PROXY.lower())

    started = time.monotonic()
    try:
        r = requests.get(
            url, params=params, headers=headers, proxies=proxies,
            timeout=15, verify=verify_ssl,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "status": r.status_code,
            "elapsed_ms": elapsed_ms,
            "via_proxy": bool(STATS_PROXY),
            "ssl_verified": verify_ssl,
            "body_preview": r.text[:300],
        }
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "status": "error",
            "elapsed_ms": elapsed_ms,
            "via_proxy": bool(STATS_PROXY),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


@app.get("/admin/calibration/report")
def calibration_report(window_days: int = 7):
    """
    Relatório agregado de calibração da linha (Fase 8).

    Lê `$CACHE_DIR/line_log.jsonl` e produz métricas:
      - Total de cálculos por stat (PTS/REB/AST)
      - Médias da nossa linha e da projeção
      - Quando há `bet365_line` populado: MAE, viés direcional, top 10 divergências
      - Quando há `actual_outcome` populado: MAE vs resultado real

    Pré-requisito: LOG_LINE_CALC=1 + jogos rodados pra acumular dados.

    Query params:
      window_days: limite temporal (default 7). Use 0 pra ler tudo.
    """
    from src.services.line.line_calibration import build_report
    return build_report(window_days=window_days)


@app.get("/admin/calibration/dataset-status")
def calibration_dataset_status():
    """
    Quanto dado já temos pra treinar o LineCalibrator (Fase 7).

    Retorna contagem de registros totais, com bet365_line populado, com
    actual_outcome populado, e flag `ready_to_train` (True quando atinge
    o mínimo de samples necessários).
    """
    import os
    from src.config import CACHE_DIR
    from src.services.line.line_calibrator import LineCalibrator
    return LineCalibrator.estimate_dataset_size(
        os.path.join(CACHE_DIR, "line_log.jsonl")
    )


@app.get("/admin/backtest")
def admin_backtest(min_decision: str = "LEAN", odds_american: int = -110):
    """
    Backtest hipotético sobre line_log.jsonl (Fase 10).

    Pré-requisitos:
      - LOG_LINE_CALC=1 ligado durante jogos
      - actual_outcome + decision populados via job pós-jogo
        (ainda não implementado — esqueleto da Fase 10)

    Query params:
      min_decision: "STRONG" | "LEAN" | "ALL"
      odds_american: default -110
    """
    import os
    from src.config import CACHE_DIR
    from src.services.backtester import BackTester
    bt = BackTester()
    log_path = os.path.join(CACHE_DIR, "line_log.jsonl")
    runnable = BackTester.estimate_runnable(log_path)
    if not runnable["runnable"]:
        return {"runnable": False, "diagnostic": runnable}
    return bt.run(
        log_path,
        min_decision=min_decision,
        odds_american=odds_american,
    ).to_dict()


@app.get("/live/cache/status")
def cache_status():
    """
    Estado atual do cache de jogos ao vivo.

    Retorna metadados do último snapshot gravado pelo worker:
    updated_at, age_ms, quantidade de jogos em cache.
    """
    snapshot = live_cache.get_snapshot()
    if snapshot is None:
        return {
            "status": "initializing",
            "last_update": None,
            "games_cached": 0,
            "age_ms": None,
        }
    return {
        "status": "running",
        "last_update": snapshot.updated_at.isoformat(),
        "games_cached": len(snapshot.data.games),
        "age_ms": snapshot.age_ms,
    }


@app.get("/players/search", response_model=list[PlayerSchema])
def search_players(name: str = Query(..., min_length=2, description="Nome do jogador")):
    try:
        results = nba.search_players(name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao buscar jogadores: {exc}")
    if not results:
        raise HTTPException(status_code=404, detail=f"Nenhum jogador encontrado para '{name}'.")
    return results


@app.get("/players/{player_id}/gamelog", response_model=list[GameLogSchema])
def player_gamelog(
    player_id: int,
    season: str = _season_query(),
):
    try:
        logs = nba.get_player_gamelog(player_id, season)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao buscar game log: {exc}")
    if not logs:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum jogo encontrado para player_id={player_id} na temporada {season}.",
        )
    return logs


@app.get("/players/hot-board", response_model=HotBoardSchema)
def players_hot_board(season: str = _season_query()):
    """
    "Jogadores Quentes da Liga" — forma recente (L5/L10 vs média da
    temporada) da liga inteira. 3 chamadas cacheadas (30 min) à NBA API,
    NÃO por jogador — barato. Leitura pura: não é projeção nem pick.

    Nunca falha pro cliente: se a NBA bloquear, devolve available=False
    e o front faz fallback elegante.
    """
    try:
        return hot_board.get_hot_board(season)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Erro ao montar hot board: {exc}"
        )


@app.get("/teams/board", response_model=TeamBoardSchema)
def teams_board(season: str = _season_query()):
    """
    Pontos marcados/sofridos por time (liga inteira). 2 chamadas
    cacheadas (30 min) — NÃO por jogo. Alimenta o hero do Dashboard
    pré-jogo (nunca fica vazio). available=False → front faz fallback.
    """
    try:
        return team_board.get_team_board(season)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Erro ao montar team board: {exc}"
        )


@app.get("/games/{game_id}/play-by-play", response_model=list[PlayByPlayEventSchema])
def play_by_play(game_id: str):
    try:
        events = nba.get_play_by_play(game_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao buscar play-by-play: {exc}")
    if not events:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum evento encontrado para game_id={game_id}.",
        )
    return events


@app.get(
    "/players/{player_id}/games/{game_id}/points-by-period",
    response_model=PointsByPeriodSchema,
)
def points_by_period(player_id: int, game_id: str):
    try:
        result = nba.get_points_by_period(player_id, game_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao calcular pontos por período: {exc}")
    return result


# ------------------------------------------------------------------ #
# Analysis routes                                                     #
# ------------------------------------------------------------------ #

@app.get("/players/{player_id}/analysis/season", response_model=SeasonAnalysisSchema)
def season_analysis(
    player_id: int,
    season: str = _season_query(),
):
    try:
        return analysis.get_season_analysis(player_id, season)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao calcular análise da temporada: {exc}")


@app.get("/players/{player_id}/stats/games", response_model=list[GameStatSchema])
def game_stats(
    player_id: int,
    season: str = _season_query(),
):
    try:
        stats = analysis.get_game_stats(player_id, season)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao buscar estatísticas por jogo: {exc}")
    if not stats:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum jogo encontrado para player_id={player_id} na temporada {season}.",
        )
    return stats


@app.get(
    "/players/{player_id}/analysis/points-by-period",
    response_model=PointsByPeriodAverageSchema,
)
def points_by_period_average(
    player_id: int,
    season: str = _season_query(),
    last_games: int = _last_games_query(),
):
    try:
        return analysis.get_points_by_period_average(player_id, season, last_games)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao calcular média de pontos por período: {exc}",
        )


@app.get("/players/{player_id}/dashboard", response_model=DashboardSchema)
def dashboard(
    player_id: int,
    season: str = _season_query(),
    last_games: int = _last_games_query(),
):
    try:
        return analysis.get_dashboard(player_id, season, last_games)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao montar dashboard: {exc}")


# ------------------------------------------------------------------ #
# Live routes  (register /games/live/today BEFORE /games/{game_id}/…)#
# ------------------------------------------------------------------ #

@app.get("/games/live/today", response_model=LiveGamesCachedResponseSchema)
def today_games():
    # Fork WNBA: scoreboard ao vivo vem da ESPN (sem worker/cache, busca na
    # hora — a tela já faz polling). NBA segue pelo cache do worker.
    if getattr(nba, "_espn", None) is not None:
        try:
            return nba._espn.get_today_games()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Erro ao buscar jogos (ESPN): {exc}")
    snapshot = live_cache.get_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=503,
            detail="Live games data not ready yet. Worker is initializing, try again in a moment.",
        )
    return LiveGamesCachedResponseSchema(
        date=snapshot.data.date,
        games=snapshot.data.games,
        updated_at=snapshot.updated_at.isoformat(),
        age_ms=snapshot.age_ms,
        # Repassa pra o front saber se está vendo "ontem ainda final" ou
        # "hoje via ScoreboardV2" — alimenta os badges contextuais.
        scoreboard_source=snapshot.data.source,
        all_final=snapshot.data.all_final,
    )


@app.get("/games/{game_id}/live-boxscore", response_model=LiveBoxscoreSchema)
def live_boxscore(game_id: str):
    try:
        return live_game.get_live_boxscore(game_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/games/{game_id}/lineups", response_model=LineupGameSchema)
def lineups(game_id: str):
    """
    Elenco completo do jogo: titulares, reservas e inativos pra cada time.
    Inclui foto, posição, status, motivo de não jogar e nota 0–10 de
    desempenho. Reaproveita o mesmo cache do boxscore (TTL 15s) — não
    duplica chamadas à NBA Live API.
    """
    try:
        # Mai/2026: passa live_pbp pro lineup ter split por quarter
        # (mesmo formato do Hot Picks). Silencioso em falha.
        return live_game.get_lineup(game_id, pbp_service=live_pbp)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao montar lineup: {exc}")


@app.get("/games/{game_id}/rotations", response_model=GameRotationsSchema)
def game_rotations(game_id: str):
    """
    Padrão histórico de rotação de cada jogador do jogo (nbarotations):
    heatmap dos 48 minutos (prob. de estar em quadra), janelas de
    descanso típicas e flag "quase voltando" pro jogador no banco que
    costuma voltar nos próximos minutos. Perfis cacheados 7d no provider.
    """
    try:
        return live_game.get_game_rotations(
            game_id, rotation_provider=rotation_provider
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Erro ao montar rotações: {exc}"
        )


@app.get("/games/{game_id}/preview", response_model=GamePreviewSchema)
def game_preview(game_id: str, season: str = _season_query()):
    """
    Briefing pré-jogo (mai/2026): médias da temporada + recents +
    linhas dos books pra prováveis titulares e bench top.

    Quando game_status == "not_started" e estamos > 5 min do tipoff,
    `real_lines_available` vem False e linhas vêm null. Dentro dos
    últimos 5 min antes do tipoff, OddsService prefetch popula as linhas.

    Em jogo ao vivo, recomenda-se usar `/live-hot-ranking` em vez disso
    (tem ritmo atual + edge + heat + recomendação de aposta).

    Cacheado indiretamente: stats da temporada (24h), lineup (15s do
    boxscore), odds (TTL dinâmico), matchup (24h).
    """
    try:
        return live_analysis.get_game_preview(game_id, season)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao montar preview: {exc}")


@app.get(
    "/games/{game_id}/probable-lineup",
    response_model=ProbableLineupSchema,
)
def probable_lineup(game_id: str, season: str = _season_query()):
    """
    Escalação PROVÁVEL (informativo, SEM aposta): quem normalmente joga
    em cada time + performance nos últimos 3/5/10 jogos e na temporada.

    Disponível desde que o jogo entra na lista (1 dia antes) — não
    depende da NBA publicar a escalação oficial. Reusa as janelas
    cacheadas da liga (mesmo custo do hot board, ~zero por jogo).
    """
    home_tri = away_tri = ""
    snapshot = live_cache.get_snapshot()
    if snapshot is not None:
        for g in snapshot.data.games:
            if g.game_id == game_id:
                home_tri = g.home_team.tricode
                away_tri = g.away_team.tricode
                break
    try:
        return hot_board.get_probable_lineup(
            game_id, home_tri, away_tri, season
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Erro ao montar escalação provável: {exc}"
        )


@app.get("/games/{game_id}/live-analysis", response_model=LiveGameAnalysisSchema)
def live_game_analysis(
    game_id: str,
    season: str = _season_query(),
):
    try:
        return live_analysis.get_game_analysis(game_id, season)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro na análise live: {exc}")


@app.get(
    "/players/{player_id}/games/{game_id}/live-comparison",
    response_model=LivePlayerComparisonSchema,
)
def live_player_comparison(
    player_id: int,
    game_id: str,
    season: str = _season_query(),
):
    try:
        return live_analysis.get_player_live_comparison(player_id, game_id, season)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro na comparação live: {exc}")


@app.get("/games/{game_id}/live-hot-ranking", response_model=HotRankingSchema)
def live_hot_ranking(
    game_id: str,
    season: str = _season_query(),
    limit: int = Query(5, ge=1, le=50, description="Quantidade de jogadores no ranking"),
    consider_blowout: bool | None = Query(
        None,
        description=(
            "Considerar ajuste de blowout na projeção. "
            "Padrão: auto-detecta (playoffs=False, resto=True). "
            "Use True/False para forçar."
        ),
    ),
):
    try:
        return live_analysis.get_hot_ranking(
            game_id, season, limit, consider_blowout=consider_blowout
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao gerar hot ranking: {exc}")
