"""
Carregador de dados históricos pro HistoricalBacktester.

Responsabilidade ÚNICA: buscar PBP + box score + game logs do `nba_api`
e cachear em disco. Cache em disco = zero custo de re-execução, custo
único na primeira vez.

Estrutura do cache em `data/backtester/`:
  games/<game_id>/
    pbp.json            # play-by-play completo
    boxscore.json       # box score final (com starters)
    meta.json           # game_date, teams, etc.
  player_logs/<season>/<player_id>.json  # gamelog completo da temp
  game_index/<season>.json  # lista de game_ids por data

Tudo idempotente — se já tem em disco, não bate na API.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Diretório raiz do cache do backtester. Fica fora de tests/fixtures
# pra não inflar o repo — gitignored.
DEFAULT_CACHE_DIR = Path("data/backtester")

# Delay entre chamadas ao nba_api pra não levar rate-limit.
# stats.nba.com é loose mas 0.6s por request é seguro pra batches longos.
API_DELAY_S = 0.6


@dataclass
class GameMeta:
    """Metadados básicos do jogo."""
    game_id: str
    game_date: str        # "YYYY-MM-DD"
    home_tricode: str
    away_tricode: str
    season: str           # "2025-26"
    final_period: int     # 4 (regulação) ou 5+ (OT)


class HistoricalLoader:
    """
    Loader idempotente: chama API só na 1ª vez, depois disco.

    Uso típico:
        loader = HistoricalLoader()
        for game_id in loader.list_games_in_range("2026-04-01", "2026-04-30"):
            pbp = loader.load_pbp(game_id)
            box = loader.load_boxscore(game_id)
            # ... roda o backtester
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self.cache_dir = (cache_dir or DEFAULT_CACHE_DIR).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "games").mkdir(exist_ok=True)
        (self.cache_dir / "player_logs").mkdir(exist_ok=True)
        (self.cache_dir / "game_index").mkdir(exist_ok=True)

    # ─── Game index (lista de jogos por temporada) ─────────────────────

    def list_games_in_season(self, season: str) -> list[GameMeta]:
        """
        Lista todos os jogos finalizados de uma temporada (regular season +
        playoffs). Cacheado em `game_index/<season>.json`.

        season: "2025-26" (formato padrão NBA — primeiro ano-segundo ano).
        """
        path = self.cache_dir / "game_index" / f"{season}.json"
        if path.exists():
            with path.open() as f:
                raw = json.load(f)
            return [GameMeta(**g) for g in raw]

        logger.info("Buscando lista de jogos da temporada %s...", season)
        try:
            from nba_api.stats.endpoints import leaguegamefinder
        except ImportError:
            raise RuntimeError("nba_api não instalado")

        # Bate em todos os season types relevantes (regular + playoffs +
        # play-in). Em maio/junho, sem playoffs aqui a lista fica vazia
        # — bug observado em 2026-05-12.
        season_types = ["Regular Season", "Playoffs", "PlayIn"]
        games: dict[str, GameMeta] = {}

        for st in season_types:
            try:
                finder = leaguegamefinder.LeagueGameFinder(
                    season_nullable=season,
                    season_type_nullable=st,
                    league_id_nullable="00",
                    timeout=30,
                )
                df = finder.get_data_frames()[0]
            except Exception as exc:
                logger.warning("Falha ao buscar %s da temp %s: %s", st, season, exc)
                continue
            time.sleep(API_DELAY_S)  # rate limit
            self._index_dataframe_into(games, df, season)

        ordered = sorted(games.values(), key=lambda g: g.game_date)
        with path.open("w") as f:
            json.dump([g.__dict__ for g in ordered], f)
        logger.info("Indexed %d jogos da temporada %s (todos os tipos)",
                    len(ordered), season)
        return ordered

    @staticmethod
    def _index_dataframe_into(
        games: dict[str, "GameMeta"], df, season: str,
    ) -> None:
        """Helper: parseia DataFrame do leaguegamefinder pra dict de GameMeta."""
        for _, row in df.iterrows():
            gid = str(row["GAME_ID"])
            if gid in games:
                continue
            # Determina home/away via MATCHUP ("LAL @ MIN" = LAL away, MIN home)
            matchup = str(row["MATCHUP"])
            tricode = str(row["TEAM_ABBREVIATION"])
            if " @ " in matchup:
                away = tricode
                home = matchup.split(" @ ")[1].strip()
            elif " vs. " in matchup:
                home = tricode
                away = matchup.split(" vs. ")[1].strip()
            else:
                continue
            games[gid] = GameMeta(
                game_id=gid,
                game_date=str(row["GAME_DATE"]),
                home_tricode=home,
                away_tricode=away,
                season=season,
                final_period=4,   # default — corrige depois quando carregar boxscore
            )

    def list_games_in_range(
        self, season: str, start_date: str, end_date: str,
    ) -> list[GameMeta]:
        """Filtra jogos por janela de datas. Inclusivo nos dois lados."""
        all_games = self.list_games_in_season(season)
        return [g for g in all_games if start_date <= g.game_date <= end_date]

    # ─── PBP de jogo único ─────────────────────────────────────────────

    def load_pbp(self, game_id: str) -> Optional[list[dict]]:
        """
        Retorna lista de actions do PBP V3 (cdn.nba.com).
        Cacheado em `games/<game_id>/pbp.json`. None se falhar.
        """
        path = self.cache_dir / "games" / game_id / "pbp.json"
        if path.exists():
            with path.open() as f:
                return json.load(f)

        path.parent.mkdir(exist_ok=True)
        logger.info("Fetching PBP for %s...", game_id)
        try:
            from nba_api.live.nba.endpoints import playbyplay
            pbp = playbyplay.PlayByPlay(game_id=game_id, timeout=15)
            data = pbp.get_dict()
            actions = (data.get("game") or {}).get("actions") or []
        except Exception as exc:
            logger.warning("PBP fetch falhou pra %s: %s", game_id, exc)
            return None
        finally:
            time.sleep(API_DELAY_S)

        if not isinstance(actions, list) or not actions:
            return None
        with path.open("w") as f:
            json.dump(actions, f)
        return actions

    # ─── Box score final de um jogo ────────────────────────────────────

    def load_boxscore(self, game_id: str) -> Optional[dict]:
        """
        Box score completo (com starters). Cacheado.
        Formato: NBA Live API boxscore (cdn.nba.com).
        """
        path = self.cache_dir / "games" / game_id / "boxscore.json"
        if path.exists():
            with path.open() as f:
                return json.load(f)

        path.parent.mkdir(exist_ok=True)
        logger.info("Fetching boxscore for %s...", game_id)
        try:
            from nba_api.live.nba.endpoints import boxscore
            bs = boxscore.BoxScore(game_id=game_id, timeout=15)
            data = bs.get_dict().get("game", {})
        except Exception as exc:
            logger.warning("Boxscore fetch falhou pra %s: %s", game_id, exc)
            return None
        finally:
            time.sleep(API_DELAY_S)

        if not data:
            return None
        with path.open("w") as f:
            json.dump(data, f)
        return data

    # ─── Gamelog de jogador (pra calcular prior season+L10+L5) ─────────

    def load_player_gamelog(self, player_id: int, season: str) -> Optional[list[dict]]:
        """
        Gamelog completo do jogador na temporada. Cada item: {GAME_DATE, PTS,
        REB, AST, MIN, ...}. Usado pra computar season_avg / L10 / L5
        AS OF uma data específica (filtra games <= alvo).
        """
        path = self.cache_dir / "player_logs" / season / f"{player_id}.json"
        if path.exists():
            with path.open() as f:
                return json.load(f)

        path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("Fetching gamelog player=%d season=%s", player_id, season)
        try:
            from nba_api.stats.endpoints import playergamelog
            gl = playergamelog.PlayerGameLog(
                player_id=player_id, season=season, timeout=15,
            )
            df = gl.get_data_frames()[0]
        except Exception as exc:
            logger.warning("Gamelog fetch falhou (player=%d): %s", player_id, exc)
            return None
        finally:
            time.sleep(API_DELAY_S)

        # Converte pra lista de dicts. Datas como string "MMM DD, YYYY".
        rows = df.to_dict(orient="records")
        with path.open("w") as f:
            json.dump(rows, f, default=str)
        return rows

    # ─── Stats utilitários ─────────────────────────────────────────────

    def stats(self) -> dict:
        """Retorna contadores do cache (debug + observabilidade)."""
        games_dir = self.cache_dir / "games"
        n_games = sum(1 for p in games_dir.iterdir() if p.is_dir()) if games_dir.exists() else 0
        logs_dir = self.cache_dir / "player_logs"
        n_logs = 0
        if logs_dir.exists():
            for season_dir in logs_dir.iterdir():
                if season_dir.is_dir():
                    n_logs += len(list(season_dir.glob("*.json")))
        return {
            "games_cached": n_games,
            "player_logs_cached": n_logs,
            "cache_dir": str(self.cache_dir),
        }
