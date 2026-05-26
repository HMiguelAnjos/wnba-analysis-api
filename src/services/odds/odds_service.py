"""
OddsService — linhas reais de player props agregadas dos books US.

Produz `{player_id: {market: PlayerOdds}}` por jogo, pronto pro caller
parear contra a projeção e obter `real_edge = projection − real_line`.

Decisões de design:
  - Cache em MEMÓRIA (sem persistência). Odds são voláteis; perder cache
    no restart é OK — próxima request refaz fetch.
  - TTL DINÂMICO baseado no estado do jogo (Q1-Q3 normal: 90s; crunch:
    30s; pré-jogo: 5min; final: ~24h). Calibrado pra economizar créditos
    sem perder responsividade onde importa.
  - Falha silenciosa: qualquer erro de fetch/parse devolve dict vazio.
    O caller (LiveAnalysisService) já trata `real_line=None` como
    "linha real indisponível, mostra só o synthetic".
  - Linha = MÉDIA dos books que oferecem aquele mercado pra aquele
    jogador. Mais robusto que pegar 1 book — outliers são suavizados.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.services.odds.odds_client import OddsApiClient
from src.services.odds.player_matcher import PlayerNameMatcher
from src.services.odds.team_mapping import NBA_NAME_TO_TRICODE

logger = logging.getLogger(__name__)

# Markets do The Odds API que mapeamos
MARKET_PTS = "player_points"
MARKET_REB = "player_rebounds"
MARKET_AST = "player_assists"
ALL_MARKETS = [MARKET_PTS, MARKET_REB, MARKET_AST]

# TTL pro índice de events do dia (NBA tem ~12 jogos, lista é estável).
EVENTS_INDEX_TTL = 6 * 3600

# Janela pré-jogo (mai/2026): se faltam ≤ X minutos pro tipoff, fazemos
# UMA fetch antecipada pra carregar o cache. Mais cedo = lineups confirmados
# e injury news precificada; muito cedo = linhas ainda movem. 5 min é o
# sweet spot de mercado real.
PREGAME_PREFETCH_WINDOW_MIN = 5.0
PREGAME_PREFETCH_TTL = 5 * 60   # 5 min — cobre o resto do pré-jogo + entrada no Q1


def compute_odds_ttl(
    period: int, clock_minutes_remaining: float, game_status: str
) -> int:
    """
    TTL em segundos pra cache de odds, dinâmico por estado do jogo.

    Cadência calibrada (mai/2026):
      not_started     → ∞ (não chama API; ver get_player_lines)
      Q1              → 120s   (linhas começam estáveis)
      Q2              → 90s
      Q3 (>5min)      → 45s    (decisões começam a contar — linha precisa frescor)
      Q3 (≤5min) + Q4 + OT → 30s   (crunch time)
      final           → 24h   (cache permanente)

    Args:
        period: 1-4 (ou 5+ pra OT)
        clock_minutes_remaining: minutos no relógio do quarter atual
        game_status: 'not_started' | 'in_progress' | 'final'
    """
    if game_status == "final":
        return 24 * 3600
    if game_status == "not_started":
        # Sentinel grande — get_player_lines pula fetch pré-jogo de qualquer jeito.
        return 24 * 3600

    # Crunch time: Q3 últimos 5 min, Q4 inteiro, OT
    if (period == 3 and clock_minutes_remaining <= 5.0) or period >= 4:
        return 30

    # Q3 inicial (>5min restantes): cadência mais agressiva (mai/2026).
    # Linhas começam a importar pra decisão real, custo de credit aceitável.
    if period == 3:
        return 45

    # Q1 tem cadência mais lenta — odds movem pouco no início
    if period == 1:
        return 120

    # Q2
    return 90


@dataclass
class _EventInfo:
    """Tupla nomeada com event_id + commence_time. Usada no índice."""
    event_id: str
    commence_time: str       # ISO 8601 UTC, ex: "2026-05-09T23:00:00Z"


@dataclass
class PlayerOdds:
    """Linha agregada (média entre books) pra UM stat de UM jogador."""
    line: float            # média das linhas
    book_count: int        # quantos books contribuíram
    books: list[str] = field(default_factory=list)   # debug
    # Epoch time (segundos) do fetch que produziu essa linha.
    # Permite o front mostrar "linha atualizada há Xs" pro usuário
    # avaliar frescor. 0.0 = não preenchido (compat com testes antigos).
    fetched_at: float = 0.0


@dataclass
class GameOdds:
    """Snapshot completo de odds de um jogo."""
    fetched_at: float                                  # epoch (time.time)
    by_player: dict[int, dict[str, PlayerOdds]]


class OddsService:
    """
    Stateful: cache em memória por game_id. Falha silenciosa.

    Uso típico:
        svc = OddsService(api_key="...")
        odds = svc.get_player_lines(
            game_id="0042500001",
            period=3, clock_minutes_remaining=4.0, game_status="in_progress",
            home_tricode="BOS", away_tricode="LAL",
        )
        line_pts = odds.get(player_id, {}).get(MARKET_PTS)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        regions: str = "us",
        bookmakers: str = "",
        client: Optional[OddsApiClient] = None,
        matcher: Optional[PlayerNameMatcher] = None,
    ) -> None:
        if client is None:
            if not api_key:
                raise ValueError(
                    "OddsService precisa de api_key quando client não é injetado"
                )
            client = OddsApiClient(api_key=api_key, regions=regions)
        self._client = client
        self._bookmakers = bookmakers
        self._matcher = matcher or PlayerNameMatcher()

        # Cache de odds por game_id. Volátil (in-memory).
        self._game_cache: dict[str, GameOdds] = {}
        # game_id → expires_at (time.monotonic). Separado do cache pra
        # invalidar TTL sem mexer no payload.
        self._game_expires_at: dict[str, float] = {}

        # Mapping (home_tri, away_tri) → _EventInfo, refrescado por dia.
        self._events_index: dict[tuple[str, str], _EventInfo] = {}
        self._events_index_loaded_at: float = 0.0

    def get_player_lines(
        self,
        *,
        game_id: str,
        period: int,
        clock_minutes_remaining: float,
        game_status: str,
        home_tricode: str,
        away_tricode: str,
    ) -> dict[int, dict[str, PlayerOdds]]:
        """
        Devolve {player_id: {market: PlayerOdds}} pro jogo. Dict vazio em
        qualquer falha. NUNCA levanta — chamada barata pro caller.
        """
        # Jogo encerrado (mai/2026): não fetcha odds novas. Linhas pós-jogo
        # são só histórico — sem valor pra +EV. Se cache existe (do tempo
        # em que o jogo estava in_progress), devolve. Senão, vazio.
        # Skip antes de resolve_event pra economizar até a chamada barata
        # de events index quando o cache de eventos expira.
        if game_status == "final":
            cached_final = self._game_cache.get(game_id)
            return cached_final.by_player if cached_final else {}

        now = time.monotonic()

        # Resolve event do The Odds API a partir das tricodes
        event = self._resolve_event(home_tricode, away_tricode)
        if event is None:
            logger.info(
                "OddsService: sem event_id pro jogo %s (%s vs %s)",
                game_id, home_tricode, away_tricode,
            )
            return {}

        # Pré-jogo (mai/2026): faz UMA fetch antecipada nos últimos
        # PREGAME_PREFETCH_WINDOW_MIN antes do tipoff pra carregar o cache.
        # Mais cedo = lineups confirmados; muito tarde = perdemos a
        # oportunidade. Fora dessa janela, retorna {} sem custo.
        ttl_override: Optional[int] = None
        if game_status == "not_started":
            mins_to_tip = self._minutes_until(event.commence_time)
            if (
                mins_to_tip is None
                or not 0.0 <= mins_to_tip <= PREGAME_PREFETCH_WINDOW_MIN
            ):
                return {}
            # Dentro da janela — usa TTL fixo de 5min pra cobrir o
            # restante do pré-jogo + entrada do Q1
            ttl_override = PREGAME_PREFETCH_TTL

        # Cache hit dentro do TTL?
        cached = self._game_cache.get(game_id)
        expires = self._game_expires_at.get(game_id, 0.0)
        if cached is not None and now < expires:
            return cached.by_player

        # Fetch de odds
        payload = self._client.event_odds(
            event_id=event.event_id,
            markets=ALL_MARKETS,
            bookmakers=self._bookmakers or None,
        )
        if payload is None:
            return {}

        # Parse + agrega. fetched_at é o tempo real (epoch) do fetch —
        # propagado pra cada PlayerOdds pra o caller saber a idade da linha.
        fetched_at_epoch = time.time()
        by_player = self._aggregate(payload, fetched_at_epoch)

        # Cacheia com TTL dinâmico do estado do jogo (ou override pré-jogo)
        ttl = ttl_override if ttl_override is not None else compute_odds_ttl(
            period, clock_minutes_remaining, game_status,
        )
        self._game_cache[game_id] = GameOdds(
            fetched_at=fetched_at_epoch, by_player=by_player,
        )
        self._game_expires_at[game_id] = now + ttl

        logger.info(
            "OddsService: cached %d players pro jogo %s (TTL %ds)",
            len(by_player), game_id, ttl,
        )
        return by_player

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    def _resolve_event(
        self, home_tricode: str, away_tricode: str
    ) -> Optional[_EventInfo]:
        """Mapping (home_tri, away_tri) → _EventInfo, com refresh do índice."""
        now = time.monotonic()
        if (now - self._events_index_loaded_at) > EVENTS_INDEX_TTL:
            self._refresh_events_index()
        return self._events_index.get((home_tricode, away_tricode))

    def _refresh_events_index(self) -> None:
        """Recarrega lista de events e remonta lookup por (home, away)."""
        events = self._client.list_events()
        if not events:
            return

        new_index: dict[tuple[str, str], _EventInfo] = {}
        for ev in events:
            home_full = ev.get("home_team", "")
            away_full = ev.get("away_team", "")
            home_tri = NBA_NAME_TO_TRICODE.get(home_full)
            away_tri = NBA_NAME_TO_TRICODE.get(away_full)
            event_id = ev.get("id")
            commence_time = ev.get("commence_time", "")
            if home_tri and away_tri and event_id:
                new_index[(home_tri, away_tri)] = _EventInfo(
                    event_id=event_id, commence_time=commence_time,
                )

        self._events_index = new_index
        self._events_index_loaded_at = time.monotonic()
        logger.info(
            "OddsService: events index refreshed — %d jogos mapeados",
            len(new_index),
        )

    @staticmethod
    def _minutes_until(commence_time_iso: str) -> Optional[float]:
        """
        Minutos faltando pro tipoff. Negativo = já passou da hora marcada.
        None = parse falhou (fica conservador, caller skipa fetch).
        """
        if not commence_time_iso:
            return None
        try:
            # "2026-05-09T23:00:00Z" → tira o Z, parseia como UTC
            ts = commence_time_iso.rstrip("Z")
            tipoff = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return None
        now = datetime.now(timezone.utc)
        delta = (tipoff - now).total_seconds()
        return delta / 60.0

    def _aggregate(
        self, payload: dict, fetched_at: float = 0.0,
    ) -> dict[int, dict[str, PlayerOdds]]:
        """
        Agrega outcomes dos books num único line por (player, market) =
        média aritmética. The Odds API entrega Over+Under separados, mas
        o `point` é o mesmo nos dois — pegamos UM por book/player/market.

        fetched_at é propagado pra cada PlayerOdds — permite o caller
        (e o front) mostrar "linha atualizada há Xs".
        """
        # raw[player_id][market] = list[(line, book_name)]
        raw: dict[int, dict[str, list[tuple[float, str]]]] = {}

        for book in payload.get("bookmakers", []):
            book_name = book.get("title") or book.get("key", "")
            for market in book.get("markets", []):
                m_key = market.get("key", "")
                if m_key not in ALL_MARKETS:
                    continue
                seen_in_market: set[str] = set()
                for outcome in market.get("outcomes", []):
                    player_name = outcome.get("description") or ""
                    if not player_name or player_name in seen_in_market:
                        continue
                    seen_in_market.add(player_name)

                    point = outcome.get("point")
                    if point is None:
                        continue

                    pid = self._matcher.find(player_name)
                    if pid is None:
                        continue

                    try:
                        line_val = float(point)
                    except (TypeError, ValueError):
                        continue

                    raw.setdefault(pid, {}).setdefault(m_key, []).append(
                        (line_val, book_name)
                    )

        result: dict[int, dict[str, PlayerOdds]] = {}
        for pid, markets in raw.items():
            for market, entries in markets.items():
                lines = [e[0] for e in entries]
                books = [e[1] for e in entries]
                result.setdefault(pid, {})[market] = PlayerOdds(
                    line=round(statistics.mean(lines), 1),
                    book_count=len(lines),
                    books=books,
                    fetched_at=fetched_at,
                )
        return result
