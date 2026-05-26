import logging
import time
from typing import Any, Callable, Optional

import pandas as pd
import requests
import urllib3
from nba_api.stats.endpoints import PlayerGameLog, PlayByPlayV3, CommonAllPlayers

from src.config import STATS_PROXY
from src.league import LEAGUE_ID


# When STATS_PROXY is configured (e.g. ScraperAPI), the proxy itself does
# its own TLS termination and presents a non-public certificate, so the
# client *must* skip verification. Without this, any HTTPS call through
# the proxy fails with SSLError (CERTIFICATE_VERIFY_FAILED). Patch is
# applied once at import time and only when a proxy is in use.
def _disable_ssl_verification_for_proxy() -> None:
    """Make every requests.Session.request default to verify=False.

    This affects all outbound HTTPS in this process, which is acceptable
    in our context: we don't accept user-controlled target URLs, and the
    cdn.nba.com paths still benefit from the proxy operator's TLS
    handling. The InsecureRequestWarning spam is silenced too.
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _orig_request = requests.Session.request

    def _patched_request(self, method, url, **kwargs):
        kwargs.setdefault("verify", False)
        return _orig_request(self, method, url, **kwargs)

    requests.Session.request = _patched_request


# ScraperAPI terminates TLS itself and presents its own certificate, so
# SSL verification must be disabled when routing through it. Other proxies
# (e.g. Webshare residential) use standard CONNECT tunneling and do NOT
# need this — applying it there breaks the connection.
_PROXY_NEEDS_SSL_BYPASS = STATS_PROXY and "scraperapi" in STATS_PROXY.lower()

if _PROXY_NEEDS_SSL_BYPASS:
    _disable_ssl_verification_for_proxy()
    logging.getLogger(__name__).info(
        "ScraperAPI proxy detected — SSL verification disabled."
    )
elif STATS_PROXY:
    logging.getLogger(__name__).info(
        "STATS_PROXY detected (non-ScraperAPI) — SSL verification kept enabled."
    )
from src.schemas.nba_schemas import (
    GameLogSchema,
    PlayerSchema,
    PlayByPlayEventSchema,
    PointsByPeriodSchema,
)
from src.utils.cache import PersistentCache
from src.utils.converters import (
    EVENT_TYPE_MAP,
    normalize_player_name,
    points_from_event,
    safe_str,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_DELAY = 5.0

# Timeout curto para contexto de análise ao vivo (parallel workers)
LIVE_TIMEOUT = 6
LIVE_MAX_RETRIES = 1

# Gamelogs barely change after a game ends; 24h is more than enough
# and gives huge resilience when stats.nba.com is blocking the host.
GAMELOG_TTL = 86_400


def _proxy_kwargs() -> dict:
    """Returns {'proxy': STATS_PROXY} when configured, else empty dict.

    nba_api's stats endpoints accept a `proxy` kwarg (a single URL string).
    On Railway/cloud, stats.nba.com routinely blocks datacenter IPs; routing
    via a residential proxy works around this. Set the STATS_PROXY env var
    (e.g. http://user:pass@host:port) to enable. If unset, calls go direct.
    """
    return {"proxy": STATS_PROXY} if STATS_PROXY else {}


# nba_api 1.5.x already sends Chrome User-Agent + Referer, but misses the
# NBA-specific tokens that nba.com's own frontend includes. Adding them makes
# the request indistinguishable from a real browser session on the site.
_ENHANCED_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}


def _with_retry(fn: Callable, *args, max_retries: int = MAX_RETRIES, **kwargs) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            logger.warning("Attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(RETRY_DELAY)
    raise last_error


def _fetch_pbp_df(game_id: str) -> pd.DataFrame:
    def _fetch():
        return PlayByPlayV3(
            game_id=game_id,
            timeout=DEFAULT_TIMEOUT,
            headers=_ENHANCED_HEADERS,
            **_proxy_kwargs(),
        ).get_data_frames()[0]

    return _with_retry(_fetch)


# Regex pra extrair pts e assistente de descriptions do V3:
#   "Holiday 1' Driving Layup (2 PTS) (Tatum 1 AST)"
#   "Brown Free Throw 1 of 2 (5 PTS)"
import re as _re
_RE_POINTS = _re.compile(r"\((\d+)\s*PTS\)")
_RE_ASSIST = _re.compile(r"\(([\w\.\-' ]+?)\s+\d+\s*AST\)")


def _extract_final_margin(df: pd.DataFrame) -> Optional[int]:
    """
    Extrai a margem absoluta do placar final do PBP V3.

    Itera de trás pra frente até achar um row com scoreHome/scoreAway
    populados (alguns rows como "End of Period" não têm placar). Retorna
    None se ninguém tem placar ou DF vazio.
    """
    if df.empty:
        return None

    for _, row in df.iloc[::-1].iterrows():
        sh_raw = safe_str(row.get("scoreHome"))
        sa_raw = safe_str(row.get("scoreAway"))
        if not sh_raw or not sa_raw:
            continue
        try:
            sh = int(sh_raw)
            sa = int(sa_raw)
        except (TypeError, ValueError):
            continue
        return abs(sh - sa)

    return None


def _aggregate_v3_dataframe(df: pd.DataFrame) -> dict[int, dict[int, dict[str, int]]]:
    """
    Parser específico pro PlayByPlayV3 (stats.nba.com). Diferente do live
    PBP (cdn.nba.com) que tem actionType "2pt"/"3pt" em camelCase, V3 usa
    actionType "Made Shot"/"Missed Shot"/"Free Throw"/"Rebound" + descrição
    com "(N PTS)" e "(<assistente> N AST)".
    """
    from collections import defaultdict
    out: dict[int, dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {
            "points": 0, "assists": 0, "rebounds": 0, "three_pm": 0,
        })
    )

    # Pra mapear nome do assistente → personId, cacheamos personId×nome
    # local (mesmo jogo). Nem sempre sucesso — fallback ignora silencioso.
    name_to_pid: dict[str, int] = {}

    # 1ª passada: indexa nomes
    for _, row in df.iterrows():
        try:
            name = (row.get("playerNameI") or row.get("playerName") or "").strip()
            pid = int(row.get("personId") or 0)
            if pid > 0 and name:
                name_to_pid[name] = pid
                # Variação última-palavra (sobrenome) também
                last = name.split()[-1] if " " in name else name
                if last and last not in name_to_pid:
                    name_to_pid[last] = pid
        except (TypeError, ValueError):
            continue

    # 2ª passada: agrega
    for _, row in df.iterrows():
        try:
            period = int(row.get("period") or 0)
            if period <= 0:
                continue
            pid = int(row.get("personId") or 0)
        except (TypeError, ValueError):
            continue

        action_type = str(row.get("actionType") or "")
        shot_result = str(row.get("shotResult") or "")
        description = str(row.get("description") or "")

        # Made Shot — pts vão pro shooter
        if action_type == "Made Shot" and pid > 0:
            m = _RE_POINTS.search(description)
            # description vem com "(2 PTS)" cumulativo do jogo, não a
            # ação; precisamos do delta. Mas V3 traz `shotValue` ou
            # podemos inferir do subType (3PT vs 2PT).
            sub = str(row.get("subType") or "")
            is_three = "3PT" in description.upper() or "3PT" in sub.upper()
            pts = 3 if is_three else 2
            out[pid][period]["points"] += pts
            if is_three:
                out[pid][period]["three_pm"] += 1

            # Assist?
            ast_m = _RE_ASSIST.search(description)
            if ast_m:
                ast_name = ast_m.group(1).strip()
                ast_pid = name_to_pid.get(ast_name)
                if ast_pid is None and " " in ast_name:
                    ast_pid = name_to_pid.get(ast_name.split()[-1])
                if ast_pid:
                    out[ast_pid][period]["assists"] += 1

        # Free Throw — V3 não popula shotResult pra FTs. Detecta MADE
        # pela presença de "(N PTS)" na description; MISS aparece como
        # prefixo "MISS" na description.
        elif action_type == "Free Throw" and pid > 0:
            desc_upper = description.upper()
            if not desc_upper.startswith("MISS") and _RE_POINTS.search(description):
                out[pid][period]["points"] += 1

        # Rebound — 1 reb
        elif action_type == "Rebound" and pid > 0:
            out[pid][period]["rebounds"] += 1

    # Converte defaultdict → dict puro (cacheable)
    return {pid: {p: dict(s) for p, s in periods.items()}
            for pid, periods in out.items()}


class NbaService:
    def __init__(self) -> None:
        # Persistent on-disk cache for player gamelogs. Survives container
        # restarts and means a single successful fetch unlocks the data for
        # 24 h regardless of stats.nba.com availability.
        # Path uses CACHE_DIR (default /tmp); aponte pra um volume persistente
        # em produção pra que esse cache também sobreviva a deploys.
        from src.config import CACHE_DIR
        import os
        self._gamelog_cache = PersistentCache(
            path=os.path.join(CACHE_DIR, "nba_gamelog_cache.json"),
        )
        # Cache de stats por período por jogo histórico (PBP). Histórico
        # é imutável → TTL longo (30 dias). Cache por game_id retorna
        # mapping completo {player_id: {period: stats}}, todos os players
        # do jogo de uma vez — qualquer player que peça depois pega cached.
        self._pbp_period_cache = PersistentCache(
            path=os.path.join(CACHE_DIR, "nba_pbp_period_cache.json"),
        )
        # Cache da margem final (|home - away|) por game_id. Mesmo TTL longo
        # que o PBP cache — placar final é imutável. Separado pra acesso
        # barato sem reler o JSON do per-period.
        self._final_margin_cache = PersistentCache(
            path=os.path.join(CACHE_DIR, "nba_final_margin_cache.json"),
        )

        # Fork WNBA: fonte ESPN (responde do IP local sem proxy). Quando
        # ativa, busca e gamelog vêm da ESPN; o resto da análise é o mesmo
        # código (construído em cima do gamelog).
        from src.league import DATA_SOURCE
        self._espn = None
        if DATA_SOURCE == "espn":
            from src.services.espn_wnba import EspnWnbaSource
            self._espn = EspnWnbaSource(cache_dir=CACHE_DIR)
            logger.info("NbaService: fonte de dados = ESPN (WNBA local)")

    def search_players(self, name: str) -> list[PlayerSchema]:
        if self._espn is not None:
            return self._espn.search_players(name)
        query = normalize_player_name(name)
        logger.info("Searching players with query: %s", query)

        # WNBA fork: a lista estática do nba_api é só NBA. Pra WNBA
        # (LeagueID=10) buscamos via CommonAllPlayers da liga, cacheado.
        all_players = self._get_all_players()
        matches = [
            p for p in all_players
            if query in normalize_player_name(p["full_name"])
        ]

        return [
            PlayerSchema(
                id=p["id"],
                full_name=p["full_name"],
                first_name=p["first_name"],
                last_name=p["last_name"],
                is_active=p["is_active"],
            )
            for p in matches
        ]

    def _get_all_players(self) -> list[dict]:
        """
        Índice de jogadoras(es) da liga (CommonAllPlayers). Cacheado 24h —
        a lista muda raramente. Cobre WNBA (LeagueID=10) e NBA (00).
        Cada item: {id, full_name, first_name, last_name, is_active}.
        """
        from src.league import DEFAULT_SEASON
        cache_key = f"all_players:{LEAGUE_ID}:{DEFAULT_SEASON}"
        cached = self._gamelog_cache.get(cache_key)
        if cached is not None:
            return cached

        def _fetch():
            return CommonAllPlayers(
                league_id=LEAGUE_ID,
                season=DEFAULT_SEASON,
                is_only_current_season=0,
                timeout=DEFAULT_TIMEOUT,
                headers=_ENHANCED_HEADERS,
                **_proxy_kwargs(),
            ).get_data_frames()[0]

        df: pd.DataFrame = _with_retry(_fetch, max_retries=MAX_RETRIES)
        out: list[dict] = []
        for _, row in df.iterrows():
            full = str(row.get("DISPLAY_FIRST_LAST") or "").strip()
            if not full:
                continue
            # ROSTERSTATUS: 1 = na ativa nesta temporada
            is_active = bool(int(row.get("ROSTERSTATUS", 0) or 0))
            # DISPLAY_LAST_COMMA_FIRST = "Sobrenome, Nome" → separa
            first, last = "", ""
            lcf = str(row.get("DISPLAY_LAST_COMMA_FIRST") or "")
            if "," in lcf:
                last, first = [s.strip() for s in lcf.split(",", 1)]
            else:
                parts = full.split()
                first = parts[0] if parts else full
                last = " ".join(parts[1:]) if len(parts) > 1 else ""
            out.append({
                "id": int(row["PERSON_ID"]),
                "full_name": full,
                "first_name": first,
                "last_name": last,
                "is_active": is_active,
            })
        if out:
            self._gamelog_cache.set(cache_key, out, 24 * 3600)
        return out

    def get_player_gamelog(
        self,
        player_id: int,
        season: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> list[GameLogSchema]:
        if self._espn is not None:
            return self._espn.get_player_gamelog(player_id, season)
        cache_key = f"gamelog:{player_id}:{season}"
        cached = self._gamelog_cache.get(cache_key)
        if cached is not None:
            logger.info("Gamelog cache HIT for player %d, season %s", player_id, season)
            return [GameLogSchema(**g) for g in cached]

        logger.info("Gamelog cache MISS — fetching from stats.nba.com (player %d, season %s)", player_id, season)

        def _fetch():
            return PlayerGameLog(
                player_id=player_id,
                season=season,
                league_id_nullable=LEAGUE_ID,
                timeout=timeout,
                headers=_ENHANCED_HEADERS,
                **_proxy_kwargs(),
            ).get_data_frames()[0]

        df: pd.DataFrame = _with_retry(_fetch, max_retries=max_retries)

        if df.empty:
            return []

        results = []
        for _, row in df.iterrows():
            results.append(
                GameLogSchema(
                    game_id=str(row["Game_ID"]),
                    game_date=str(row["GAME_DATE"]),
                    matchup=str(row["MATCHUP"]),
                    minutes=str(row["MIN"]),
                    points=int(row["PTS"]),
                    rebounds=int(row["REB"]),
                    assists=int(row["AST"]),
                    field_goals_made=int(row["FGM"]),
                    field_goals_attempted=int(row["FGA"]),
                    three_pointers_made=int(row["FG3M"]),
                    three_pointers_attempted=int(row["FG3A"]),
                    free_throws_made=int(row["FTM"]),
                    free_throws_attempted=int(row["FTA"]),
                )
            )

        # Persist for 24h so subsequent endpoints (analysis/season,
        # stats/games, dashboard, etc.) survive any stats.nba.com outage.
        if results:
            self._gamelog_cache.set(
                cache_key,
                [g.model_dump() for g in results],
                GAMELOG_TTL,
            )
            logger.info("Gamelog cached for player %d (%d games)", player_id, len(results))
        return results

    def get_play_by_play(self, game_id: str) -> list[PlayByPlayEventSchema]:
        """
        Lista de eventos do PBP histórico do jogo.

        PlayByPlayV3 retorna camelCase: actionType, period, clock,
        playerName, description (combinada), scoreHome/scoreAway. O
        schema histórico antigo separava description em home/visitor —
        hoje V3 já entrega combinada. Mantemos a description em ambos
        os campos pra compat (front trata).
        """
        # WNBA/ESPN: os game ids são da ESPN e NÃO existem no PlayByPlayV3
        # do stats.nba.com → chamada falharia (e via proxy, lenta + custosa).
        # PBP histórico fica indisponível no fork WNBA.
        if self._espn is not None:
            return []
        logger.info("Fetching play-by-play for game %s", game_id)

        df = _fetch_pbp_df(game_id)

        if df.empty:
            return []

        events = []
        for _, row in df.iterrows():
            try:
                period = int(row.get("period") or 0)
            except (TypeError, ValueError):
                period = 0

            action_type = safe_str(row.get("actionType")) or "unknown"
            player_name = safe_str(row.get("playerName")) or None
            description = safe_str(row.get("description")) or None
            clock = safe_str(row.get("clock")) or ""

            # V3 separa scoreHome/scoreAway. Reconstrói "H-A" se ambos
            # presentes; senão deixa null.
            score_home = safe_str(row.get("scoreHome"))
            score_away = safe_str(row.get("scoreAway"))
            score: str | None = None
            if score_home and score_away:
                score = f"{score_away}-{score_home}"

            events.append(
                PlayByPlayEventSchema(
                    period=period,
                    clock=clock,
                    event_type=action_type,
                    player_name=player_name,
                    # V3 já entrega description combinada; populamos ambos
                    # campos pra compat com clients antigos
                    description_home=description,
                    description_visitor=description,
                    score=score,
                )
            )
        return events

    def get_points_by_period(self, player_id: int, game_id: str) -> PointsByPeriodSchema:
        """
        Pontos do jogador por período num jogo específico.

        Usa `aggregate_historical_pbp_per_period` (V3-aware) e fatia o
        player. Retorna 0 pra períodos sem ação. Raises ValueError se o
        player_id não existe.
        """
        # WNBA/ESPN: sem PBP histórico (ids ESPN ≠ stats.nba.com). Devolve
        # vazio — caller trata como "sem dado por período".
        if self._espn is not None:
            return PointsByPeriodSchema(
                player_id=player_id,
                game_id=game_id,
                points_by_period={},
                total_points=0,
            )
        logger.info("Calculating points by period for player %d in game %s", player_id, game_id)

        if not players.find_player_by_id(player_id):
            raise ValueError(f"Player with id {player_id} not found.")

        full = self.aggregate_historical_pbp_per_period(game_id)
        player_periods = full.get(player_id, {})

        # Mantém formato {str(period): int} pra compat com schema antigo
        points_by_period: dict[str, int] = {
            str(period): stats.get("points", 0)
            for period, stats in player_periods.items()
            if stats.get("points", 0) > 0
        }
        total = sum(points_by_period.values())

        return PointsByPeriodSchema(
            player_id=player_id,
            game_id=game_id,
            points_by_period=points_by_period,
            total_points=total,
        )

    # ------------------------------------------------------------------ #
    # Aggregation: PBP histórico → stats por (player_id, period)         #
    # ------------------------------------------------------------------ #

    def aggregate_historical_pbp_per_period(
        self, game_id: str
    ) -> dict[int, dict[int, dict[str, int]]]:
        """
        Lê PlayByPlayV3 do jogo e devolve mapping completo:
            {player_id: {period: {points, assists, rebounds}}}

        Cacheado 30 dias em disco (PBP histórico é imutável).

        V3 tem actionType com strings ("Made Shot", "Missed Shot", "Rebound",
        "Free Throw") e a quantidade de pts vem na `description` como "(N PTS)".
        Formato diferente do live PBP do cdn.nba.com — daí parser próprio.

        Lógica:
          - Made Shot: shooter recebe pts (extraídos de "(N PTS)" da description)
          - Free Throw + shotResult=Made: 1 pt pro shooter
          - Rebound: 1 reb pro personId
          - Made Shot + "(<name> N AST)" na description: 1 ast pro assistente

        Falha silenciosa: retorna {} em qualquer erro.
        """
        # WNBA/ESPN: game ids da ESPN não existem no PlayByPlayV3 da NBA.
        # Curto-circuito instantâneo — evita falha lenta + retries via proxy
        # (era o que travava a análise ao vivo no fork WNBA).
        if self._espn is not None:
            return {}
        cache_key = f"hist_pbp_period:{game_id}"
        cached = self._pbp_period_cache.get(cache_key)
        if cached is not None:
            return {
                int(pid): {int(p): dict(s) for p, s in periods.items()}
                for pid, periods in cached.items()
            }

        try:
            df = _fetch_pbp_df(game_id)
        except Exception as exc:
            logger.info("aggregate_historical_pbp: fetch falhou pro game %s (%s)", game_id, exc)
            return {}

        if df.empty:
            return {}

        agg = _aggregate_v3_dataframe(df)

        # Serializa pra JSON-friendly
        result_dict = {
            str(pid): {str(p): dict(stats) for p, stats in periods.items()}
            for pid, periods in agg.items()
        }
        self._pbp_period_cache.set(cache_key, result_dict, ttl=30 * 86_400)

        # Placar final: última linha do PBP V3 com scoreHome/scoreAway
        # populados. Cacheia em paralelo pra evitar fetch separado quando
        # `get_final_margin` for chamado depois.
        final_margin = _extract_final_margin(df)
        if final_margin is not None:
            self._final_margin_cache.set(
                f"final_margin:{game_id}", final_margin, ttl=30 * 86_400
            )

        return agg

    def get_final_margin(self, game_id: str) -> Optional[int]:
        """
        Margem final |home - away| do jogo. None se PBP indisponível ou
        scores ausentes. Cacheia 30 dias.

        Usado pelo RotationProvider pra classificar jogos como close/blowout
        com placar real, em vez do self-clustering pelo Q4 do próprio jogador.
        """
        cache_key = f"final_margin:{game_id}"
        cached = self._final_margin_cache.get(cache_key)
        if cached is not None:
            return int(cached)

        # Aproveita o aggregate (popula nosso cache de margin como side-effect)
        try:
            self.aggregate_historical_pbp_per_period(game_id)
        except Exception as exc:
            logger.info("get_final_margin: aggregate falhou pro game %s (%s)", game_id, exc)
            return None

        cached = self._final_margin_cache.get(cache_key)
        return int(cached) if cached is not None else None
