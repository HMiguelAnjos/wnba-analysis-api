"""
HotBoardService — "Jogadores Quentes da Liga" (forma recente).

Fonte: NBA stats API via `nba_api.LeagueDashPlayerStats`. UMA chamada
por janela (temporada / últimos 5 / últimos 10) cobre a liga inteira —
mesmo padrão barato do MatchupProvider (que usa LeagueDashTeamStats).

Custo: 3 chamadas a cada ~30 min (cacheado). NÃO é por jogador — não
explode rate-limit nem custo de infra.

IMPORTANTE: isto NÃO toca engine/projeção/picks. É leitura pura de
forma recente (média L5/L10 vs temporada) pra alimentar o dashboard
quando ainda não há escalação publicada / jogo ao vivo.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.schemas.nba_schemas import (
    HotBoardMarketSchema,
    HotBoardPlayerSchema,
    HotBoardSchema,
    ProbableLineupSchema,
    ProbablePlayer,
    ProbableStatLine,
    ProbableTeam,
)
from src.utils.cache import PersistentCache

logger = logging.getLogger(__name__)

HOT_BOARD_TTL = 1_800  # 30 min — forma recente muda no máximo 1x/dia
_MARKETS = ("PTS", "REB", "AST")
# Volume mínimo (média de temporada) pra entrar no ranking de cada
# mercado — corta ruído de quem quase não produz aquele fundamento.
# Pisos mais altos: só produtores reais entram (sem fim-de-banco cuja
# % explode por ter média baixíssima de base).
_FLOOR = {"PTS": 12.0, "REB": 4.5, "AST": 3.0}
_TOP_N = 8

# Gates de RELEVÂNCIA — garantem que o jogador é peça de rotação com
# amostra confiável (não 1-2 jogos de garbage time).
_MIN_SEASON_MPG = 20.0   # minutos/jogo na temporada
_MIN_SEASON_GP = 10      # jogos na temporada (baseline estável)
_MIN_L5_GP = 3           # jogos na janela recente (forma real, não 1 jogo)

# Escalação provável — quem "deve jogar": rotação por minutos. Mais
# permissivo que o hot board (queremos o time todo que joga, não só
# destaques).
_PROB_MIN_MPG = 10.0     # < 10 min/jogo = ponta de banco, fora
_PROB_MIN_GP = 3
_PROB_MAX_PER_TEAM = 12  # rotação típica
_PROB_STARTERS = 5       # top-5 por minutos = provável titular


def _label_for_pct(pct: float) -> str:
    if pct >= 0.20:
        return "on_fire"
    if pct >= 0.08:
        return "heating"
    if pct <= -0.20:
        return "ice_cold"
    if pct <= -0.08:
        return "cold"
    return "stable"


class HotBoardService:
    """Cache 30 min. Falha silenciosa → `available=False` (front faz fallback)."""

    _CACHE_KEY = "hot_board:"

    def __init__(self, cache: Optional[PersistentCache] = None) -> None:
        self._cache = cache or PersistentCache()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _current_season() -> str:
        now = datetime.now()
        if now.month >= 10:
            return f"{now.year}-{str(now.year + 1)[-2:]}"
        return f"{now.year - 1}-{str(now.year)[-2:]}"

    def _fetch_window(self, season: str, last_n: int) -> dict[int, dict]:
        """
        Uma chamada cobre a liga toda. last_n=0 → temporada inteira.
        Retorna {player_id: {name, team, gp, PTS, REB, AST}}.
        Falha → dict vazio (caller cai no fallback).
        """
        try:
            from nba_api.stats.endpoints import leaguedashplayerstats
        except ImportError:
            logger.info("HotBoardService: nba_api ausente — board indisponível")
            return {}

        try:
            from src.league import LEAGUE_ID
            # Headers + proxy iguais ao gamelog (que funciona com ScraperAPI).
            from src.services.nba_service import _ENHANCED_HEADERS, _proxy_kwargs
            resp = leaguedashplayerstats.LeagueDashPlayerStats(
                season=season,
                per_mode_detailed="PerGame",
                last_n_games=last_n,
                league_id_nullable=LEAGUE_ID,
                headers=_ENHANCED_HEADERS,
                timeout=30,
                **_proxy_kwargs(),
            )
            data = resp.get_dict()
        except Exception as exc:
            logger.info(
                "HotBoardService: fetch (last_n=%d) falhou (%s)", last_n, exc
            )
            return {}

        try:
            rs = data["resultSets"][0]
            headers = rs["headers"]
            rows = rs["rowSet"]
            i_id = headers.index("PLAYER_ID")
            i_name = headers.index("PLAYER_NAME")
            i_team = headers.index("TEAM_ABBREVIATION")
            i_gp = headers.index("GP")
            i_min = headers.index("MIN")
            i_pts = headers.index("PTS")
            i_reb = headers.index("REB")
            i_ast = headers.index("AST")
        except (KeyError, ValueError, IndexError) as exc:
            logger.info("HotBoardService: layout inesperado (%s)", exc)
            return {}

        out: dict[int, dict] = {}
        for r in rows:
            try:
                pid = int(r[i_id])
                out[pid] = {
                    "name": str(r[i_name]),
                    "team": str(r[i_team] or ""),
                    "gp": int(r[i_gp] or 0),
                    "min": float(r[i_min] or 0.0),
                    "PTS": float(r[i_pts] or 0.0),
                    "REB": float(r[i_reb] or 0.0),
                    "AST": float(r[i_ast] or 0.0),
                }
            except (TypeError, ValueError):
                continue
        return out

    def _window(self, season: str, last_n: int) -> dict[int, dict]:
        """
        Janela cacheada (30 min) por (season, last_n). Compartilhada entre
        hot board e escalação provável — total de chamadas à NBA fica em
        ~4 (0/3/5/10) a cada 30 min, liga inteira. Custo ~zero.
        """
        key = f"hb_window:{season}:{last_n}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = self._fetch_window(season, last_n)
        # Só cacheia (longo) se veio dado; vazio cacheia curto pra
        # tentar de novo logo (NBA pode ter bloqueado momentaneamente).
        ttl = HOT_BOARD_TTL if data else HOT_BOARD_TTL // 6
        self._cache.set(key, data, ttl)
        return data

    def _build(self, season: str) -> HotBoardSchema:
        season_map = self._window(season, 0)
        l5_map = self._window(season, 5)
        l10_map = self._window(season, 10)

        now = datetime.now(timezone.utc).isoformat()

        if not season_map or not l5_map:
            return HotBoardSchema(
                season=season,
                updated_at=now,
                evaluated=0,
                available=False,
                PTS=HotBoardMarketSchema(hot=[], cold=[]),
                REB=HotBoardMarketSchema(hot=[], cold=[]),
                AST=HotBoardMarketSchema(hot=[], cold=[]),
            )

        per_market: dict[str, list[HotBoardPlayerSchema]] = {
            m: [] for m in _MARKETS
        }
        evaluated = 0

        for pid, sea in season_map.items():
            # Gate de relevância: peça de rotação com amostra confiável.
            if sea.get("min", 0.0) < _MIN_SEASON_MPG:
                continue
            if sea.get("gp", 0) < _MIN_SEASON_GP:
                continue
            l5 = l5_map.get(pid)
            if not l5 or l5["gp"] < _MIN_L5_GP:
                continue  # sem janela recente confiável (≥3 jogos)
            l10 = l10_map.get(pid) or l5
            evaluated += 1
            for m in _MARKETS:
                s = sea[m]
                if s < _FLOOR[m]:
                    continue
                v5 = l5[m]
                v10 = l10[m]
                recent = 0.65 * v5 + 0.35 * v10
                pct = (recent - s) / max(s, 1.0)
                score = max(-1.0, min(1.0, pct / 0.35))
                per_market[m].append(
                    HotBoardPlayerSchema(
                        player_id=pid,
                        name=sea["name"],
                        team=sea["team"],
                        market=m,
                        season=round(s, 1),
                        last5=round(v5, 1),
                        last10=round(v10, 1),
                        pct=round(pct, 4),
                        score=round(score, 3),
                        label=_label_for_pct(pct),
                    )
                )

        def board(m: str) -> HotBoardMarketSchema:
            entries = per_market[m]
            hot = sorted(
                [e for e in entries if e.pct > 0.03],
                key=lambda e: e.pct,
                reverse=True,
            )[:_TOP_N]
            cold = sorted(
                [e for e in entries if e.pct < -0.03],
                key=lambda e: e.pct,
            )[:_TOP_N]
            return HotBoardMarketSchema(hot=hot, cold=cold)

        return HotBoardSchema(
            season=season,
            updated_at=now,
            evaluated=evaluated,
            available=True,
            PTS=board("PTS"),
            REB=board("REB"),
            AST=board("AST"),
        )

    # ------------------------------------------------------------------ #
    def get_hot_board(self, season: Optional[str] = None) -> HotBoardSchema:
        """Nunca levanta exceção. Cacheado 30 min (inclusive o fallback)."""
        season = season or self._current_season()
        key = f"{self._CACHE_KEY}{season}"

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        board = self._build(season)
        # Fallback indisponível: cache curto pra tentar de novo logo.
        ttl = HOT_BOARD_TTL if board.available else HOT_BOARD_TTL // 6
        self._cache.set(key, board, ttl)
        return board

    # ------------------------------------------------------------------ #
    def get_probable_lineup(
        self,
        game_id: str,
        home_tri: str,
        away_tri: str,
        season: Optional[str] = None,
    ) -> ProbableLineupSchema:
        """
        Escalação PROVÁVEL (informativo, sem aposta): quem normalmente
        joga em cada time (rotação por minutos da temporada) + médias
        nos últimos 3/5/10 jogos e na temporada.

        Reusa as janelas cacheadas da liga (0/3/5/10) — ZERO chamada
        extra por jogo. Nunca levanta exceção.
        """
        season = season or self._current_season()
        now = datetime.now(timezone.utc).isoformat()
        home_tri = (home_tri or "").upper()
        away_tri = (away_tri or "").upper()

        sea = self._window(season, 0)
        l5 = self._window(season, 5)
        l10 = self._window(season, 10)
        # L3 removido do produto (NBA não populava de forma confiável).
        # Mantido no schema como zeros só pra compat — front ignora.
        _ZERO3 = ProbableStatLine(games=0, points=0.0, rebounds=0.0, assists=0.0)

        def _stat(m: dict | None) -> ProbableStatLine:
            if not m:
                return ProbableStatLine(
                    games=0, points=0.0, rebounds=0.0, assists=0.0
                )
            return ProbableStatLine(
                games=int(m.get("gp", 0) or 0),
                points=round(float(m.get("PTS", 0.0) or 0.0), 1),
                rebounds=round(float(m.get("REB", 0.0) or 0.0), 1),
                assists=round(float(m.get("AST", 0.0) or 0.0), 1),
            )

        def _team(tri: str) -> ProbableTeam:
            rows = [
                (pid, s)
                for pid, s in sea.items()
                if str(s.get("team", "")).upper() == tri
                and float(s.get("min", 0.0)) >= _PROB_MIN_MPG
                and int(s.get("gp", 0)) >= _PROB_MIN_GP
            ]
            rows.sort(key=lambda kv: float(kv[1].get("min", 0.0)), reverse=True)
            rows = rows[:_PROB_MAX_PER_TEAM]
            players: list[ProbablePlayer] = []
            for idx, (pid, s) in enumerate(rows):
                players.append(
                    ProbablePlayer(
                        player_id=pid,
                        name=str(s.get("name", "")),
                        team=tri,
                        probable_starter=idx < _PROB_STARTERS,
                        season_minutes=round(float(s.get("min", 0.0)), 1),
                        season_games=int(s.get("gp", 0) or 0),
                        season=_stat(s),
                        last3=_ZERO3,
                        last5=_stat(l5.get(pid)),
                        last10=_stat(l10.get(pid)),
                    )
                )
            return ProbableTeam(tricode=tri, players=players)

        home = _team(home_tri)
        away = _team(away_tri)
        available = bool(sea) and bool(home.players or away.players)
        return ProbableLineupSchema(
            game_id=game_id,
            season=season,
            updated_at=now,
            available=available,
            note=(
                "Escalação provável (informativo) — quem costuma jogar "
                "por minutos da temporada. Não é a escalação oficial; a "
                "NBA só publica perto do tip."
            ),
            home=home,
            away=away,
        )
