"""
TeamBoardService — pontos marcados/sofridos por time (liga inteira).

Fonte: nba_api.LeagueDashTeamStats (Base = pontos marcados;
Opponent = pontos sofridos). DUAS chamadas cobrem a liga inteira,
cacheadas 30 min — mesmo padrão barato do HotBoardService /
MatchupProvider. NÃO é por jogo. Custo ~zero.

Alimenta o hero do Dashboard pré-jogo (nunca fica vazio): mostra os
times que jogam hoje rankeados por ataque/defesa.

NÃO toca engine/projeção — leitura pura de stats agregados.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.schemas.nba_schemas import TeamBoardSchema, TeamBoardTeam
from src.utils.cache import PersistentCache

logger = logging.getLogger(__name__)

TEAM_BOARD_TTL = 1_800  # 30 min — stats de temporada mudam 1x/dia


def _team_abbr_map() -> dict[int, dict]:
    try:
        from nba_api.stats.static import teams as _t
        return {
            int(x["id"]): {"abbr": x["abbreviation"], "name": x["full_name"]}
            for x in _t.get_teams()
        }
    except Exception:
        return {}


class TeamBoardService:
    """Cache 30 min. Falha silenciosa → available=False (front faz fallback)."""

    _KEY = "team_board:"

    def __init__(self, cache: Optional[PersistentCache] = None) -> None:
        self._cache = cache or PersistentCache()
        # Fork WNBA: stats da liga vêm da ESPN (LeagueDash é stats.nba.com).
        from src.league import DATA_SOURCE
        self._espn = None
        if DATA_SOURCE == "espn":
            from src.services.espn_wnba import EspnWnbaSource
            self._espn = EspnWnbaSource()

    @staticmethod
    def _current_season() -> str:
        now = datetime.now()
        if now.month >= 10:
            return f"{now.year}-{str(now.year + 1)[-2:]}"
        return f"{now.year - 1}-{str(now.year)[-2:]}"

    def _fetch(self, season: str, measure: str) -> dict[int, dict]:
        """1 chamada, liga toda. measure='Base'|'Opponent'. {team_id: row}."""
        try:
            from nba_api.stats.endpoints import leaguedashteamstats
        except ImportError:
            return {}
        try:
            from src.league import LEAGUE_ID
            resp = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                measure_type_detailed_defense=measure,
                per_mode_detailed="PerGame",
                league_id_nullable=LEAGUE_ID,
                timeout=15,
            )
            data = resp.get_dict()
        except Exception as exc:
            logger.info("TeamBoard fetch (%s) falhou: %s", measure, exc)
            return {}
        try:
            rs = data["resultSets"][0]
            h = rs["headers"]
            rows = rs["rowSet"]
            i_id = h.index("TEAM_ID")
            i_gp = h.index("GP")
            # Base → PTS / PLUS_MINUS ; Opponent → OPP_PTS
            i_pts = h.index("OPP_PTS") if measure == "Opponent" else h.index("PTS")
            i_pm = h.index("PLUS_MINUS") if "PLUS_MINUS" in h else None
        except (KeyError, ValueError, IndexError) as exc:
            logger.info("TeamBoard layout inesperado (%s): %s", measure, exc)
            return {}
        out: dict[int, dict] = {}
        for r in rows:
            try:
                tid = int(r[i_id])
                out[tid] = {
                    "gp": int(r[i_gp] or 0),
                    "pts": float(r[i_pts] or 0.0),
                    "pm": float(r[i_pm] or 0.0) if i_pm is not None else 0.0,
                }
            except (TypeError, ValueError):
                continue
        return out

    def _build(self, season: str) -> TeamBoardSchema:
        if self._espn is not None:
            return self._espn.get_team_board(season)
        base = self._fetch(season, "Base")
        opp = self._fetch(season, "Opponent")
        now = datetime.now(timezone.utc).isoformat()
        if not base:
            return TeamBoardSchema(
                season=season, updated_at=now, available=False, teams=[]
            )
        amap = _team_abbr_map()
        teams: list[TeamBoardTeam] = []
        for tid, b in base.items():
            meta = amap.get(tid, {})
            o = opp.get(tid, {})
            teams.append(
                TeamBoardTeam(
                    team_id=tid,
                    tricode=meta.get("abbr", ""),
                    name=meta.get("name", ""),
                    games=int(b.get("gp", 0)),
                    points=round(float(b.get("pts", 0.0)), 1),
                    opp_points=round(float(o.get("pts", 0.0)), 1),
                    plus_minus=round(float(b.get("pm", 0.0)), 1),
                )
            )
        teams.sort(key=lambda t: t.points, reverse=True)
        return TeamBoardSchema(
            season=season, updated_at=now, available=True, teams=teams
        )

    def get_team_board(self, season: Optional[str] = None) -> TeamBoardSchema:
        """Nunca levanta exceção. Cacheado 30 min (inclusive fallback)."""
        season = season or self._current_season()
        key = f"{self._KEY}{season}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        board = self._build(season)
        ttl = TEAM_BOARD_TTL if board.available else TEAM_BOARD_TTL // 6
        self._cache.set(key, board, ttl)
        return board
