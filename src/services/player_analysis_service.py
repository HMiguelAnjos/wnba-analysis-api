import logging

from nba_api.stats.static import players as players_static

from src.schemas.analysis_schemas import (
    DashboardGameSchema,
    DashboardPeriodsSchema,
    DashboardSchema,
    DashboardSummarySchema,
    DashboardTrendSchema,
    GameStatSchema,
    PbpErrorSchema,
    PointsByPeriodAverageSchema,
    SeasonAnalysisSchema,
    StatAveragesSchema,
    TrendSchema,
)
from src.schemas.nba_schemas import GameLogSchema
from src.services.nba_service import NbaService
from src.utils.stats import (
    calc_stat_averages,
    calc_trend_status,
    parse_minutes,
    rounded,
)

logger = logging.getLogger(__name__)


class PlayerAnalysisService:
    def __init__(self, nba_service: NbaService) -> None:
        self.nba = nba_service

    def _require_player(self, player_id: int) -> None:
        # Fork WNBA / fonte ESPN: a lista estática do nba_api é só NBA, então
        # não dá pra validar um player_id WNBA por ela. Pula o gate — quem
        # não tiver jogos cai no _require_logs ("nenhum jogo encontrado").
        from src.league import DATA_SOURCE
        if DATA_SOURCE == "espn":
            return
        if not players_static.find_player_by_id(player_id):
            raise ValueError(f"Jogador com id {player_id} não encontrado.")

    def _require_logs(self, logs: list, player_id: int, season: str) -> None:
        if not logs:
            raise ValueError(
                f"Nenhum jogo encontrado para player_id={player_id} na temporada {season}."
            )

    def _period_averages_from_logs(
        self,
        player_id: int,
        logs: list[GameLogSchema],
        last_games: int,
    ) -> tuple[dict[str, float], int, list[PbpErrorSchema]]:
        """Fetches PBP for the last N games and returns (period_averages, games_analyzed, errors)."""
        target = logs[:last_games]
        totals: dict[str, float] = {}
        analyzed = 0
        errors: list[PbpErrorSchema] = []

        for game in target:
            logger.info("PBP analysis: game %s (%s)", game.game_id, game.matchup)
            try:
                result = self.nba.get_points_by_period(player_id, game.game_id)
            except Exception as exc:
                logger.warning("PBP failed for game %s: %s", game.game_id, exc)
                errors.append(
                    PbpErrorSchema(
                        game_id=game.game_id,
                        reason="failed_to_fetch_play_by_play",
                    )
                )
                continue

            analyzed += 1
            for period_str, pts in result.points_by_period.items():
                key = period_str if int(period_str) <= 4 else "OT"
                totals[key] = totals.get(key, 0.0) + pts

        if analyzed == 0:
            return {}, 0, errors

        averages = {k: rounded(v / analyzed) for k, v in sorted(totals.items())}
        return averages, analyzed, errors

    # ------------------------------------------------------------------ #
    # Public methods                                                       #
    # ------------------------------------------------------------------ #

    def get_season_analysis(
        self,
        player_id: int,
        season: str,
        fast: bool = False,
    ) -> SeasonAnalysisSchema:
        """fast=True usa timeout de 6 s e 1 tentativa (para análise ao vivo paralela)."""
        from src.services.nba_service import LIVE_TIMEOUT, LIVE_MAX_RETRIES
        self._require_player(player_id)
        extra = {"timeout": LIVE_TIMEOUT, "max_retries": LIVE_MAX_RETRIES} if fast else {}
        logs = self.nba.get_player_gamelog(player_id, season, **extra)
        self._require_logs(logs, player_id, season)

        avgs = calc_stat_averages(logs)
        last5_avgs = calc_stat_averages(logs[:5])
        last10_avgs = calc_stat_averages(logs[:10])

        return SeasonAnalysisSchema(
            player_id=player_id,
            season=season,
            games_played=len(logs),
            averages=StatAveragesSchema(**avgs),
            last_5_games=StatAveragesSchema(**last5_avgs),
            last_10_games=StatAveragesSchema(**last10_avgs),
            trend=TrendSchema(
                points_vs_season_average=rounded(last5_avgs["points"] - avgs["points"]),
                rebounds_vs_season_average=rounded(last5_avgs["rebounds"] - avgs["rebounds"]),
                assists_vs_season_average=rounded(last5_avgs["assists"] - avgs["assists"]),
            ),
        )

    def get_game_stats(self, player_id: int, season: str) -> list[GameStatSchema]:
        self._require_player(player_id)
        logs = self.nba.get_player_gamelog(player_id, season)
        return [
            GameStatSchema(
                game_id=g.game_id,
                game_date=g.game_date,
                matchup=g.matchup,
                minutes=int(round(parse_minutes(g.minutes))),
                points=g.points,
                rebounds=g.rebounds,
                assists=g.assists,
            )
            for g in logs
        ]

    def get_points_by_period_average(
        self,
        player_id: int,
        season: str,
        last_games: int,
    ) -> PointsByPeriodAverageSchema:
        self._require_player(player_id)
        logs = self.nba.get_player_gamelog(player_id, season)
        period_avgs, analyzed, errors = self._period_averages_from_logs(
            player_id, logs, last_games
        )
        total_avg = rounded(sum(period_avgs.values())) if period_avgs else 0.0
        return PointsByPeriodAverageSchema(
            player_id=player_id,
            season=season,
            games_analyzed=analyzed,
            points_by_period_average=period_avgs,
            total_average=total_avg,
            errors=errors,
        )

    def get_dashboard(
        self,
        player_id: int,
        season: str,
        last_games: int,
    ) -> DashboardSchema:
        self._require_player(player_id)
        # Single gamelog fetch shared across all calculations
        logs = self.nba.get_player_gamelog(player_id, season)
        self._require_logs(logs, player_id, season)

        avgs = calc_stat_averages(logs)
        last5_avgs = calc_stat_averages(logs[:5])
        last10_avgs = calc_stat_averages(logs[:10])

        period_avgs, _, _ = self._period_averages_from_logs(player_id, logs, last_games)
        trend_status = calc_trend_status(last5_avgs["points"], avgs["points"])

        recent_games = [
            DashboardGameSchema(
                game_id=g.game_id,
                game_date=g.game_date,
                matchup=g.matchup,
                points=g.points,
                rebounds=g.rebounds,
                assists=g.assists,
                minutes=int(round(parse_minutes(g.minutes))),
            )
            for g in logs[:last_games]
        ]

        return DashboardSchema(
            player_id=player_id,
            season=season,
            summary=DashboardSummarySchema(
                games_played=len(logs),
                season_points_average=avgs["points"],
                last_5_points_average=last5_avgs["points"],
                last_10_points_average=last10_avgs["points"],
            ),
            periods=DashboardPeriodsSchema(points_by_period_average=period_avgs),
            recent_games=recent_games,
            trend=DashboardTrendSchema(
                status=trend_status,
                points_difference_last_5_vs_season=rounded(
                    last5_avgs["points"] - avgs["points"]
                ),
                points_difference_last_10_vs_season=rounded(
                    last10_avgs["points"] - avgs["points"]
                ),
            ),
        )
