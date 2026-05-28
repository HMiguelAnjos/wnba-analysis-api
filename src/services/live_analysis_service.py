import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from datetime import datetime, timezone

from src.schemas.live_schemas import (
    BlowoutRiskSchema,
    CashoutAlertSchema,
    ConfidenceBreakdownSchema,
    FairLineSchema,
    GamePreviewMatchupSchema,
    GamePreviewPlayerSchema,
    GamePreviewSchema,
    HotRankingPlayerSchema,
    HotRankingSchema,
    LiveAnalysisErrorSchema,
    LiveBoxscoreSchema,
    LiveCurrentStatsSchema,
    LiveDifferenceSchema,
    LiveExpectedStatsSchema,
    LiveGameAnalysisSchema,
    LivePlayerAnalysisSchema,
    LivePlayerComparisonSchema,
    LivePlayerStatsSchema,
    LiveSeasonAverageSchema,
    LiveTeamSchema,
    PaceProjectionSchema,
    PlayerBlowoutImpactSchema,
    QuarterStatsSchema,
    RotationContextSchema,
    SimilarGamesResultSchema,
    TodayHotRankingErrorSchema,
    TodayHotRankingItemSchema,
    TodayHotRankingsSchema,
)
from src.schemas.anomaly_schemas import (
    AnomalyPlayerStatsSchema,
    HotStatSchema,
)
from src.services.anomaly_service import AnomalyService
from src.services.cashout import detect_cashout
from src.services.hot_streak import HeatDetector
from src.services.line import LineEngine
from src.services.live_game_service import LiveGameService
from src.services.live_pbp_service import LivePbpService
from src.services.matchup import MatchupProvider
from src.services.odds import OddsService, PlayerOdds
from src.services.similar_games import SimilarGameAnalyzer
from src.services.odds.odds_service import (
    MARKET_AST as _ODDS_MARKET_AST,
    MARKET_PTS as _ODDS_MARKET_PTS,
    MARKET_REB as _ODDS_MARKET_REB,
)
from src.services.player_analysis_service import PlayerAnalysisService
from src.services.projection import ProjectionEngine
from src.services.rotation import RotationProvider
from src.utils.cache import PersistentCache
from src.utils.stats import (
    bet_recommendation,
    betting_confidence_from_signals,
    calc_per_stat_status,
    calc_player_score,
    calc_player_status,
    calc_shooting_impact,
    calculate_player_performance_rating,
    confidence_label,
    projection_confidence_from_label,
    rounded,
    sample_confidence_from_minutes,
    usage_label,
    usage_proxy,
)

logger = logging.getLogger(__name__)

SEASON_AVG_TTL = 86_400    # 24 hours — médias mudam no máximo 1x/dia
ANALYSIS_TYPE = "experimental_live_analysis"


class LiveAnalysisService:
    def __init__(
        self,
        live_game_service: LiveGameService,
        player_analysis_service: PlayerAnalysisService,
        pbp_service: Optional[LivePbpService] = None,
        line_engine: Optional[LineEngine] = None,
        projection_engine: Optional[ProjectionEngine] = None,
        matchup_provider: Optional[MatchupProvider] = None,
        heat_detector: Optional[HeatDetector] = None,
        rotation_provider: Optional[RotationProvider] = None,
        odds_service: Optional[OddsService] = None,
        anomaly_service: Optional[AnomalyService] = None,
    ) -> None:
        self.live = live_game_service
        self.player_analysis = player_analysis_service
        # PBP é opcional — fallback pra service novo se não injetado.
        # Usado pra split por período no card live.
        self.pbp = pbp_service or LivePbpService()
        # Engines de linha e projeção. Defaults criam instâncias internas;
        # injetáveis pra testes / configuração futura (calibração por mercado,
        # rotação, matchup context).
        self.line_engine = line_engine or LineEngine()
        self.projection_engine = projection_engine or ProjectionEngine()
        # Matchup provider — DRtg + pace por team, cache 24h.
        # Falha silenciosa: se nba_api/stats.nba.com bloquear, retorna neutro.
        self.matchup = matchup_provider or MatchupProvider()
        # HeatDetector — sinal composto de "jogador quente". Stateless.
        self.heat = heat_detector or HeatDetector()
        # RotationProvider — busca padrão minuto-a-minuto do nbarotations.info
        # (Fase 2 V2). Falha silenciosa: cai em fallback uniforme se site fora
        # ou jogador sem dado suficiente.
        self.rotation = rotation_provider or RotationProvider()
        # OddsService — linhas reais do The Odds API (opcional). None = não
        # populamos `real_line`. Wiring fica em main.py atrás de feature flag.
        self.odds = odds_service
        # AnomalyService — regras determinísticas pra destacar perfomances
        # anormais (microwave scorer, double-double, foul trouble). Default
        # interna; injetável pra testes.
        self.anomaly = anomaly_service or AnomalyService()

        # SimilarGameAnalyzer (mai/2026) — busca jogos históricos com Q1
        # similar ao atual. Surfaceia "quando ele teve um início assim,
        # o que aconteceu". Usa o gamelog + PBP do NbaService (ambos
        # cacheados). Só chamamos quando player está em underperformance
        # detectada — ver _build_player.
        self.similar_games = SimilarGameAnalyzer(
            gamelog_fetcher=self.player_analysis.nba.get_player_gamelog,
            pbp_aggregator=self.player_analysis.nba.aggregate_historical_pbp_per_period,
        )
        self._cache = PersistentCache()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _get_season_averages(
        self, player_id: int, season: str
    ) -> Optional[dict[str, float]]:
        """
        Fetch and cache season averages for a player. Returns None on failure.

        Inclui também last_5 e last_10 averages (PTS/REB/AST) — usado pelo
        synthetic fair line. Esses dados já são calculados pelo
        get_season_analysis, só precisamos propagar.
        """
        cache_key = f"season_avg:{player_id}:{season}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            result = self.player_analysis.get_season_analysis(player_id, season, fast=True)
            avgs = {
                "points": result.averages.points,
                "rebounds": result.averages.rebounds,
                "assists": result.averages.assists,
                "minutes": result.averages.minutes,
                "field_goals_made": result.averages.field_goals_made,
                "field_goals_attempted": result.averages.field_goals_attempted,
                "three_pointers_made": result.averages.three_pointers_made,
                "three_pointers_attempted": result.averages.three_pointers_attempted,
                "free_throws_made": result.averages.free_throws_made,
                "free_throws_attempted": result.averages.free_throws_attempted,
                # Recências — pra synthetic fair line
                "last_5_points":    result.last_5_games.points,
                "last_5_rebounds":  result.last_5_games.rebounds,
                "last_5_assists":   result.last_5_games.assists,
                "last_5_three_pm":  result.last_5_games.three_pointers_made,
                "last_10_points":   result.last_10_games.points,
                "last_10_rebounds": result.last_10_games.rebounds,
                "last_10_assists":  result.last_10_games.assists,
                "last_10_three_pm": result.last_10_games.three_pointers_made,
            }
            self._cache.set(cache_key, avgs, SEASON_AVG_TTL)
            logger.info("Season averages cached for player %d (%s)", player_id, season)
            return avgs
        except Exception as exc:
            logger.warning(
                "Could not fetch season averages for player %d: %s", player_id, exc
            )
            return None

    def _get_variance_factors(
        self, player_id: int, season: str
    ) -> dict[str, float]:
        """
        Item 2 (mai/2026): fator de confidence ∈ [0, 1] por stat baseado
        na variância dos últimos 10 jogos. CV alto = projeção menos
        confiável → confidence cai.

        Returns dict com chaves "points", "rebounds", "assists". Defaults
        em 0.5 (neutro) quando gamelog falha ou amostra insuficiente.
        Cacheado 24h junto com o gamelog (que já tem cache).
        """
        cache_key = f"variance:{player_id}:{season}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        from src.utils.stats import player_variance_factor

        result = {
            "points": 0.5, "rebounds": 0.5, "assists": 0.5, "three_pm": 0.5,
        }
        try:
            logs = self.player_analysis.nba.get_player_gamelog(player_id, season)
        except Exception as exc:
            logger.info(
                "variance: gamelog falhou pra %d (%s)", player_id, exc
            )
            self._cache.set(cache_key, result, SEASON_AVG_TTL)
            return result

        if not logs:
            self._cache.set(cache_key, result, SEASON_AVG_TTL)
            return result

        # Últimos 10 jogos (gamelog vem ordenado desc por padrão)
        recent = logs[:10]
        if len(recent) < 3:
            self._cache.set(cache_key, result, SEASON_AVG_TTL)
            return result

        result = {
            "points": player_variance_factor([float(g.points) for g in recent]),
            "rebounds": player_variance_factor([float(g.rebounds) for g in recent]),
            "assists": player_variance_factor([float(g.assists) for g in recent]),
            "three_pm": player_variance_factor(
                [float(g.three_pointers_made) for g in recent]
            ),
        }
        self._cache.set(cache_key, result, SEASON_AVG_TTL)
        return result

    def _get_rest_days(
        self, player_id: int, season: str, today_iso: Optional[str]
    ) -> Optional[int]:
        """
        Dias decorridos entre o jogo de HOJE (today_iso = "YYYY-MM-DD") e o
        último jogo do gamelog. None = não computável (sem today, sem gamelog,
        ou erro de parse).

        Cap em 30 — descansos longos (>30d) saem da curva normal e ficam ruim
        de generalizar; tratamos como rest=3 (alto) sem distinguir.
        """
        if not today_iso:
            return None
        try:
            today = datetime.strptime(today_iso, "%Y-%m-%d").date()
        except ValueError:
            return None
        try:
            logs = self.player_analysis.nba.get_player_gamelog(player_id, season)
        except Exception as exc:
            logger.warning(
                "rest_days: gamelog falhou pra %d: %s", player_id, exc
            )
            return None
        if not logs:
            return None

        last_dt = None
        for g in logs:
            raw = (g.game_date or "").strip()
            if not raw:
                continue
            parsed = None
            for fmt in ("%b %d, %Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(raw, fmt).date()
                    break
                except ValueError:
                    continue
            if parsed is None:
                continue
            if parsed >= today:
                continue
            if last_dt is None or parsed > last_dt:
                last_dt = parsed
        if last_dt is None:
            return None

        delta = (today - last_dt).days
        if delta < 0:
            return None
        return min(delta, 30)

    def _analyze_player(
        self,
        player: LivePlayerStatsSchema,
        team_tricode: str,
        season: str,
    ) -> tuple[Optional[LivePlayerAnalysisSchema], Optional[str]]:
        """Returns (analysis, error_reason). Exactly one of them is None."""
        season_avgs = self._get_season_averages(player.player_id, season)
        if season_avgs is None:
            return None, "failed_to_fetch_season_averages"

        avg_minutes = season_avgs["minutes"]
        if avg_minutes <= 0:
            return None, "season_avg_minutes_is_zero"

        ratio = player.minutes / avg_minutes

        expected = LiveExpectedStatsSchema(
            points=rounded(season_avgs["points"] * ratio),
            rebounds=rounded(season_avgs["rebounds"] * ratio),
            assists=rounded(season_avgs["assists"] * ratio),
            field_goals_made=rounded(season_avgs["field_goals_made"] * ratio),
            field_goals_attempted=rounded(season_avgs["field_goals_attempted"] * ratio),
            three_pointers_made=rounded(season_avgs["three_pointers_made"] * ratio),
            three_pointers_attempted=rounded(season_avgs["three_pointers_attempted"] * ratio),
            free_throws_made=rounded(season_avgs["free_throws_made"] * ratio),
            free_throws_attempted=rounded(season_avgs["free_throws_attempted"] * ratio),
        )
        diff = LiveDifferenceSchema(
            points=rounded(player.points - expected.points),
            rebounds=rounded(player.rebounds - expected.rebounds),
            assists=rounded(player.assists - expected.assists),
            field_goals_made=rounded(player.field_goals_made - expected.field_goals_made),
            field_goals_attempted=rounded(
                player.field_goals_attempted - expected.field_goals_attempted
            ),
            three_pointers_made=rounded(
                player.three_pointers_made - expected.three_pointers_made
            ),
            three_pointers_attempted=rounded(
                player.three_pointers_attempted - expected.three_pointers_attempted
            ),
            free_throws_made=rounded(player.free_throws_made - expected.free_throws_made),
            free_throws_attempted=rounded(
                player.free_throws_attempted - expected.free_throws_attempted
            ),
        )

        field_goal_misses_diff = rounded(
            (player.field_goals_attempted - player.field_goals_made)
            - (expected.field_goals_attempted - expected.field_goals_made)
        )
        free_throw_misses_diff = rounded(
            (player.free_throws_attempted - player.free_throws_made)
            - (expected.free_throws_attempted - expected.free_throws_made)
        )
        shooting_impact = calc_shooting_impact(
            diff.field_goals_made,
            diff.field_goals_attempted,
            diff.three_pointers_made,
            diff.free_throws_made,
            field_goal_misses_diff,
            free_throw_misses_diff,
        )
        score = calc_player_score(
            diff.points,
            diff.rebounds,
            diff.assists,
            shooting_impact,
        )
        status = calc_player_status(score)

        # Nota 0–10 da partida — mesma fórmula da aba Lineups.
        rating, label, low_conf = calculate_player_performance_rating(
            points=player.points,
            rebounds=player.rebounds,
            assists=player.assists,
            steals=player.steals,
            blocks=player.blocks,
            turnovers=player.turnovers,
            fouls=player.fouls,
            plus_minus=player.plus_minus,
            minutes=player.minutes,
            field_goals_made=player.field_goals_made,
            field_goals_attempted=player.field_goals_attempted,
            three_pointers_made=player.three_pointers_made,
            free_throws_made=player.free_throws_made,
            free_throws_attempted=player.free_throws_attempted,
        )

        analysis = LivePlayerAnalysisSchema(
            player_id=player.player_id,
            name=player.name,
            jersey_num=player.jersey_num,
            team=team_tricode,
            minutes=player.minutes,
            fouls=player.fouls,
            is_starter=player.is_starter,
            on_court=player.on_court,
            current=LiveCurrentStatsSchema(
                points=player.points,
                rebounds=player.rebounds,
                assists=player.assists,
                field_goals_made=player.field_goals_made,
                field_goals_attempted=player.field_goals_attempted,
                three_pointers_made=player.three_pointers_made,
                three_pointers_attempted=player.three_pointers_attempted,
                free_throws_made=player.free_throws_made,
                free_throws_attempted=player.free_throws_attempted,
            ),
            season_average=LiveSeasonAverageSchema(**season_avgs),
            expected_until_now=expected,
            difference=diff,
            shooting_impact=shooting_impact,
            status=status,
            score=score,
            performance_rating=rating,
            performance_label=label,
            low_confidence=low_conf,
        )
        return analysis, None

    def _analyze_boxscore(
        self, boxscore: LiveBoxscoreSchema, season: str
    ) -> tuple[list[LivePlayerAnalysisSchema], list[LiveAnalysisErrorSchema]]:
        """
        Analisa todos os jogadores dos dois times em paralelo.

        ThreadPoolExecutor dispara todas as buscas de médias simultaneamente,
        então o tempo total é ~6 s (1 timeout) e não 20×6 s = 120 s.
        """
        tasks = [
            (player, team.tricode)
            for team in (boxscore.home_team, boxscore.away_team)
            for player in team.players
        ]

        analyzed: list[LivePlayerAnalysisSchema] = []
        errors:   list[LiveAnalysisErrorSchema]  = []

        with ThreadPoolExecutor(max_workers=min(len(tasks), 16)) as pool:
            future_map = {
                pool.submit(self._analyze_player, player, tricode, season): player
                for player, tricode in tasks
            }
            for future in as_completed(future_map):
                player = future_map[future]
                try:
                    result, reason = future.result()
                except Exception as exc:
                    logger.error("Erro inesperado analisando jogador %d: %s", player.player_id, exc)
                    result, reason = None, f"unexpected_error: {exc}"

                if result is not None:
                    analyzed.append(result)
                else:
                    errors.append(
                        LiveAnalysisErrorSchema(
                            player_id=player.player_id,
                            name=player.name,
                            reason=reason or "unknown_error",
                        )
                    )

        return analyzed, errors

    # ------------------------------------------------------------------ #
    # Public methods                                                       #
    # ------------------------------------------------------------------ #

    def get_game_preview(self, game_id: str, season: str) -> GamePreviewSchema:
        """
        Briefing pré-jogo (mai/2026): médias da temporada + recents +
        linhas dos books pra prováveis titulares e bench top.

        Reaproveita:
          - `lineups` (live_game.get_lineup) pra descobrir titulares
          - `_get_season_averages` (cache 24h) pra stats
          - `odds.get_player_lines` (cache + prefetch window) pra linhas reais
          - `matchup.get_pair` (cache 24h) pra DRtg/pace

        Front usa quando game_status == "not_started". Em jogo live/final,
        deve usar `/live-hot-ranking` em vez disso (mais informação).
        """
        lineup = self.live.get_lineup(game_id)

        # Lê informações do jogo agendado (game_time_utc) — precisa do
        # scoreboard. Reusa o cache do worker (TTL 2s).
        game_time_utc: Optional[str] = None
        minutes_to_tipoff: Optional[float] = None
        try:
            today = self.live.get_today_games()
            for g in today.games:
                if g.game_id == game_id:
                    game_time_utc = g.game_time_utc
                    break
            if game_time_utc:
                from datetime import datetime, timezone
                try:
                    ts = game_time_utc.rstrip("Z")
                    tipoff = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                    delta = (tipoff - datetime.now(timezone.utc)).total_seconds()
                    minutes_to_tipoff = round(delta / 60.0, 1)
                except (ValueError, AttributeError):
                    pass
        except Exception as exc:
            logger.info("get_game_preview: today_games fetch falhou (%s)", exc)

        # Real lines: chama OddsService. Dentro da janela de 5min antes
        # do tipoff, ele faz prefetch. Fora, retorna dict vazio.
        odds_by_player: dict[int, dict[str, "PlayerOdds"]] = {}
        if self.odds is not None:
            try:
                odds_by_player = self.odds.get_player_lines(
                    game_id=game_id,
                    period=lineup.period,
                    clock_minutes_remaining=12.0,
                    game_status=lineup.game_status,
                    home_tricode=lineup.home_team.tricode,
                    away_tricode=lineup.away_team.tricode,
                )
            except Exception as exc:
                logger.info("get_game_preview: odds fetch falhou (%s)", exc)

        real_lines_available = bool(odds_by_player)

        def _player_to_preview(
            p, team_tricode: str, is_starter: bool,
        ) -> Optional[GamePreviewPlayerSchema]:
            """Converte um LineupPlayerSchema em GamePreviewPlayerSchema."""
            if p.status != "ACTIVE":
                return None
            avgs = self._get_season_averages(p.player_id, season) or {}
            if not avgs or avgs.get("minutes", 0) <= 0:
                # Sem médias confiáveis — pula (rookie, sem amostra suficiente)
                return None

            player_odds = odds_by_player.get(p.player_id, {})
            line_pts = player_odds.get(_ODDS_MARKET_PTS)
            line_reb = player_odds.get(_ODDS_MARKET_REB)
            line_ast = player_odds.get(_ODDS_MARKET_AST)

            return GamePreviewPlayerSchema(
                player_id=p.player_id,
                name=p.name,
                jersey_num=p.jersey_num,
                position=p.position,
                team=team_tricode,
                is_starter=is_starter,
                season_points=avgs.get("points", 0.0),
                season_rebounds=avgs.get("rebounds", 0.0),
                season_assists=avgs.get("assists", 0.0),
                season_minutes=avgs.get("minutes", 0.0),
                season_three_pm=avgs.get("three_pointers_made", 0.0),
                last_5_points=avgs.get("last_5_points", 0.0),
                last_5_rebounds=avgs.get("last_5_rebounds", 0.0),
                last_5_assists=avgs.get("last_5_assists", 0.0),
                last_10_points=avgs.get("last_10_points", 0.0),
                last_10_rebounds=avgs.get("last_10_rebounds", 0.0),
                last_10_assists=avgs.get("last_10_assists", 0.0),
                line_points=line_pts.line if line_pts else None,
                line_rebounds=line_reb.line if line_reb else None,
                line_assists=line_ast.line if line_ast else None,
                book_count=line_pts.book_count if line_pts else 0,
            )

        # Constrói listas. Bench top = top 3 por season minutes (mais relevantes).
        def _build_team(
            starters_raw, bench_raw, tricode: str,
        ) -> tuple[list[GamePreviewPlayerSchema], list[GamePreviewPlayerSchema]]:
            starters = []
            for p in starters_raw:
                preview = _player_to_preview(p, tricode, is_starter=True)
                if preview is not None:
                    starters.append(preview)

            bench_previews = []
            for p in bench_raw:
                preview = _player_to_preview(p, tricode, is_starter=False)
                if preview is not None:
                    bench_previews.append(preview)
            # Ordena bench por minutos da temporada (decrescente), pega top 3
            bench_previews.sort(key=lambda x: x.season_minutes, reverse=True)
            return starters, bench_previews[:3]

        starters_home, bench_home = _build_team(
            lineup.home_team.starters, lineup.home_team.bench, lineup.home_team.tricode,
        )
        starters_away, bench_away = _build_team(
            lineup.away_team.starters, lineup.away_team.bench, lineup.away_team.tricode,
        )

        # Matchup context
        matchup: Optional[GamePreviewMatchupSchema] = None
        try:
            home_m, away_m = self.matchup.get_pair(
                lineup.home_team.tricode, lineup.away_team.tricode, season,
            )
            combined_pace = round((home_m.pace + away_m.pace) / 2, 1)
            # Pace × ~2.2 ≈ total típico NBA moderno (102 pace ≈ 224 pts)
            matchup = GamePreviewMatchupSchema(
                home_drtg=round(home_m.drtg, 1),
                away_drtg=round(away_m.drtg, 1),
                home_pace=round(home_m.pace, 1),
                away_pace=round(away_m.pace, 1),
                combined_pace=combined_pace,
                expected_total=int(round(combined_pace * 2.2)),
            )
        except Exception as exc:
            logger.info("get_game_preview: matchup falhou (%s)", exc)

        return GamePreviewSchema(
            game_id=game_id,
            game_status=lineup.game_status,
            game_time_utc=game_time_utc,
            minutes_to_tipoff=minutes_to_tipoff,
            real_lines_available=real_lines_available,
            home_team=LiveTeamSchema(
                team_id=lineup.home_team.team_id,
                name=lineup.home_team.name,
                tricode=lineup.home_team.tricode,
                score=lineup.home_team.score,
            ),
            away_team=LiveTeamSchema(
                team_id=lineup.away_team.team_id,
                name=lineup.away_team.name,
                tricode=lineup.away_team.tricode,
                score=lineup.away_team.score,
            ),
            starters_home=starters_home,
            starters_away=starters_away,
            bench_top_home=bench_home,
            bench_top_away=bench_away,
            matchup=matchup,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def get_game_analysis(self, game_id: str, season: str) -> LiveGameAnalysisSchema:
        bs = self.live.get_live_boxscore(game_id)
        analyzed, errors = self._analyze_boxscore(bs, season)

        hot = [p for p in analyzed if p.status in ("hot", "above_average")]
        cold = [p for p in analyzed if p.status in ("cold", "below_average")]
        hot.sort(key=lambda p: p.score, reverse=True)
        cold.sort(key=lambda p: p.score)

        return LiveGameAnalysisSchema(
            game_id=game_id,
            season=season,
            game_status=bs.game_status,
            period=bs.period,
            clock=bs.clock,
            analysis_type=ANALYSIS_TYPE,
            players=analyzed,
            hot_players=hot,
            cold_players=cold,
            errors=errors,
        )

    def get_player_live_comparison(
        self, player_id: int, game_id: str, season: str
    ) -> LivePlayerComparisonSchema:
        bs = self.live.get_live_boxscore(game_id)

        # Find player in either team
        player: Optional[LivePlayerStatsSchema] = None
        team_tricode = ""
        for team in (bs.home_team, bs.away_team):
            for p in team.players:
                if p.player_id == player_id:
                    player = p
                    team_tricode = team.tricode
                    break
            if player:
                break

        if player is None:
            raise ValueError(
                f"Jogador {player_id} não encontrado no boxscore do jogo {game_id}. "
                "Verifique se ele já entrou em quadra."
            )

        result, reason = self._analyze_player(player, team_tricode, season)
        if result is None:
            raise ValueError(
                f"Não foi possível analisar player {player_id}: {reason}"
            )

        return LivePlayerComparisonSchema(
            player_id=result.player_id,
            game_id=game_id,
            name=result.name,
            team=result.team,
            minutes=result.minutes,
            current=result.current,
            season_average=result.season_average,
            expected_until_now=result.expected_until_now,
            difference=result.difference,
            shooting_impact=result.shooting_impact,
            status=result.status,
            analysis_type=ANALYSIS_TYPE,
        )

    @staticmethod
    def _project_game(stat: float, minutes: float, avg_stat: float, avg_minutes: float) -> float:
        """
        Projeção BASE (blended) para um jogo típico (avg_minutes).

        Mistura o ritmo atual deste jogo com o ritmo histórico da temporada.
        Conforme o jogador acumula minutos, o peso do ritmo atual cresce
        (até 60%), mas a temporada nunca some — isso evita que um chute
        quente de 5 minutos vire previsão absurda.

        Responde: "considerando o que ele costuma fazer + como está hoje,
        quanto deve terminar?"
        """
        if avg_minutes <= 0:
            return round(avg_stat, 1)
        if minutes < 1.0:
            return round(avg_stat, 1)
        current_ppm = stat / minutes
        season_ppm  = avg_stat / avg_minutes
        alpha = min(minutes / avg_minutes, 0.60)
        return round((alpha * current_ppm + (1.0 - alpha) * season_ppm) * avg_minutes, 1)

    @staticmethod
    def _is_playoff_game(game_id: str) -> bool:
        """
        NBA game IDs seguem padrão '00<TT><Y><NNNNN>' onde TT identifica o tipo:
            01 = Preseason   02 = Regular Season
            03 = All-Star    04 = Playoffs
            05 = Play-in
        Em playoffs, blowout praticamente não rola — técnicos mantêm titulares
        mesmo com grande vantagem (medo de virada, fechamento de série, etc.).
        """
        if not game_id or len(game_id) < 4:
            return False
        return game_id[2:4] == "04"

    @staticmethod
    def _compute_game_context(
        period: int,
        clock: str,
        home_score: int,
        away_score: int,
        consider_blowout: bool = True,
    ) -> dict:
        """
        Calcula contexto do jogo usado pra ajustar a projeção.

        Retorna:
        - period (int)            — período atual (1..4 OT=5+)
        - score_diff (int)        — diferença absoluta de placar
        - minutes_elapsed (float) — minutos decorridos no jogo (clamp >=0.1)
        - blowout_severity (float in [0,1]) — quão provável é o garbage time:
            * 0.0 → jogo normal/disputado
            * 0.5 → Q4 com 10+ pts de diferença (estrela pode sair antes)
            * 0.7 → Q3+ com 20+ (técnico já considerando descansar titulares)
            * 1.0 → Q4 com 15+ (banco assumindo, estrelas saem)
        """
        try:
            if ":" in clock:
                mm, ss = clock.split(":")
                clock_minutes_remaining = int(mm) + int(ss) / 60.0
            else:
                clock_minutes_remaining = 12.0
        except (ValueError, AttributeError):
            clock_minutes_remaining = 12.0

        period_clamped = max(period, 1)
        minutes_elapsed = (period_clamped - 1) * 12 + (12 - clock_minutes_remaining)
        minutes_elapsed = max(minutes_elapsed, 0.1)
        score_diff = abs(home_score - away_score)

        # Blowout: thresholds calibrados pra padrão NBA. Q3 com 20+ já
        # sinaliza intenção de descanso; Q4 com 15+ é praticamente garantido.
        # Em jogos sem blowout (playoffs, decisão de série, etc.) o usuário
        # desativa via flag — todos os jogadores ficam sem ajuste de garbage.
        blowout_severity = 0.0
        if consider_blowout:
            if period_clamped >= 4 and score_diff >= 15:
                blowout_severity = 1.0
            elif period_clamped >= 3 and score_diff >= 20:
                blowout_severity = 0.7
            elif period_clamped >= 4 and score_diff >= 10:
                blowout_severity = 0.5

        # Pace: ritmo do jogo vs média NBA (~220 pts totais).
        # Shootout (240+) = ritmo continua quente; jogo lento (200-) = cai.
        # Em Q1 cedo o sample é ruim demais — peso menor pra evitar overreact.
        # Clamp em [0.92, 1.08]: ajuste sutil, não mexe muito na projeção.
        total_pts = home_score + away_score
        if minutes_elapsed >= 6.0:  # precisa pelo menos meio quarto pra dar significado
            projected_total = (total_pts / minutes_elapsed) * 48.0
            raw_factor = projected_total / 220.0
            # Confiança cresce com tempo de jogo: peso vai de 0.5 (6 min) a 1.0 (24+ min)
            pace_confidence = min((minutes_elapsed - 6.0) / 18.0 + 0.5, 1.0)
            # Ajuste suavizado pela confiança
            pace_factor = 1.0 + (raw_factor - 1.0) * pace_confidence
            pace_factor = max(0.92, min(pace_factor, 1.08))
        else:
            pace_factor = 1.0

        # Minutos restantes reais do jogo (considera OT: cada prorrogação = 5 min).
        total_game_minutes = 48.0 if period_clamped <= 4 else 48.0 + (period_clamped - 4) * 5.0
        game_minutes_remaining = max(total_game_minutes - minutes_elapsed, 0.0)

        return {
            "period": period_clamped,
            "score_diff": score_diff,
            "minutes_elapsed": minutes_elapsed,
            "blowout_severity": blowout_severity,
            "pace_factor": pace_factor,
            "game_minutes_remaining": game_minutes_remaining,
            # Necessário pro RotationProvider mapear posição no histogram.
            "clock_minutes_remaining": clock_minutes_remaining,
        }

    # ⚠️ _project_to_end MIGRADO pra ProjectionEngine.project (Fase 1).
    # Mantido aqui só pra documentar a migração. A função original tinha
    # ~210 linhas; toda a lógica vive em src/services/projection/projection_engine.py
    # sem alteração de comportamento. Callers usam self.projection_engine.project(...).


    def get_hot_ranking(
        self,
        game_id: str,
        season: str,
        limit: int,
        consider_blowout: Optional[bool] = None,
    ) -> HotRankingSchema:
        """
        consider_blowout:
          - None (padrão): auto-detecta. Playoffs = False, resto = True.
          - True/False: override explícito do usuário (UI manda quando o
            usuário liga/desliga o toggle de blowout).
        """
        bs = self.live.get_live_boxscore(game_id)
        analyzed, _ = self._analyze_boxscore(bs, season)

        ranking = sorted(analyzed, key=lambda p: p.score, reverse=True)[:limit]

        # Data ISO de hoje (UTC) — usada pra computar rest_days. Não temos
        # game_time_utc no boxscore; UTC vs ET difere ≤ 5h, irrelevante pra
        # diff de DIAS entre jogos consecutivos.
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Auto-detecção: jogos de playoff ignoram blowout por padrão.
        if consider_blowout is None:
            consider_blowout = not self._is_playoff_game(game_id)

        is_final = bs.game_status == "final"
        is_playoff = self._is_playoff_game(game_id)

        # Contexto do jogo é o mesmo pra todos os jogadores deste game.
        ctx = self._compute_game_context(
            bs.period, bs.clock,
            bs.home_team.score, bs.away_team.score,
            consider_blowout=consider_blowout,
        )

        # Matchup context — defesa + pace de cada time. Cacheado 24h.
        # Falha silenciosa: se fetch global da liga falhar, contextos saem
        # neutros (drtg_factor=1.0, pace_factor=1.0) e nada muda.
        home_matchup, away_matchup = self.matchup.get_pair(
            bs.home_team.tricode, bs.away_team.tricode, season,
        )
        # Pace combinado (média dos dois times) é o mesmo pra todo player
        # do jogo, independente do time.
        combined_pace_factor = (home_matchup.pace_factor + away_matchup.pace_factor) / 2

        # Risco de blowout (porcentagem qualitativa) — exposto no payload.
        # LineContext é a entrada do LineEngine; demais funções continuam
        # como utils puros até as próximas fases as moverem.
        from src.utils.stats import (
            LineContext,
            calculate_blowout_risk,
            calculate_edge_decision,
            calculate_player_blowout_impact,
            dampen_decision_for_low_sample,
        )
        bo_pct, bo_level, bo_reason = calculate_blowout_risk(
            period=bs.period,
            clock=bs.clock,
            home_score=bs.home_team.score,
            away_score=bs.away_team.score,
            game_status=bs.game_status,
            is_playoff=is_playoff,
        )
        blowout_payload = BlowoutRiskSchema(
            percentage=bo_pct, level=bo_level, reason=bo_reason
        )

        def _impact(player) -> Optional[PlayerBlowoutImpactSchema]:
            """Calcula impacto do blowout pra ESTE jogador (None = sem flag)."""
            d = calculate_player_blowout_impact(
                player_minutes=player.minutes,
                is_starter=player.is_starter,
                game_blowout_pct=bo_pct,
                game_blowout_level=bo_level,
            )
            return PlayerBlowoutImpactSchema(**d) if d else None

        def _proj(
            stat: int, minutes: float,
            avg_stat: float, avg_minutes: float,
            fouls: int,
            is_starter: bool,
            last_10_avg: Optional[float] = None,
            last_5_avg: Optional[float] = None,
            heat_score: float = 0.0,
            expected_minutes_remaining: Optional[float] = None,
            clutch_close_game_boost: float = 0.0,
            rotation_blowout_cut: float = 0.0,
            period_production_rate: Optional[float] = None,
            rest_days: Optional[int] = None,
            is_unexpected_rest: bool = False,
            variance_factor: float = 1.0,
            shrinkage_min_threshold: float = 8.0,
        ):
            """Wrapper que aplica fouls + contexto do jogo + recentes + heat + rotation."""
            return PaceProjectionSchema(
                **self.projection_engine.project(
                    stat, minutes, avg_stat, avg_minutes,
                    fouls=fouls,
                    period=ctx["period"],
                    blowout_severity=ctx["blowout_severity"],
                    pace_factor=ctx["pace_factor"],
                    game_minutes_remaining=ctx["game_minutes_remaining"],
                    is_final=is_final,
                    last_10_avg=last_10_avg,
                    last_5_avg=last_5_avg,
                    is_starter=is_starter,
                    heat_score=heat_score,
                    expected_minutes_remaining=expected_minutes_remaining,
                    clutch_close_game_boost=clutch_close_game_boost,
                    rotation_blowout_cut=rotation_blowout_cut,
                    period_production_rate=period_production_rate,
                    rest_days=rest_days,
                    is_unexpected_rest=is_unexpected_rest,
                    variance_factor=variance_factor,
                    shrinkage_min_threshold=shrinkage_min_threshold,
                )
            )

        def _fair_line(
            *,
            season_avg: float,
            last_10_avg: float,
            last_5_avg: float,
            season_minutes: float,
            current_stat: float,
            projection: float,
            player,
            shot_signals: bool,
            stat_label: str = "",
            heat_score: float = 0.0,
            sample_conf: float = 0.5,
            projection_conf: float = 0.5,
            real_odds: Optional[PlayerOdds] = None,
            usage: float = 0.5,
            is_indeterminate: bool = False,
            variance_factor: float = 1.0,
            projection_breakdown: Optional[dict] = None,
            is_unexpected_rest: bool = False,
            period_production_rate: Optional[float] = None,
            rotation_status: Optional[str] = None,
        ) -> FairLineSchema:
            """
            Linha estimada (synthetic bookmaker) + edge da projeção.

            shot_signals=True → ativa volume/eFG (faz sentido só pra PTS).
            Pra REB/AST não tem volume direto, então passamos só os básicos.
            """
            current_fga = player.current.field_goals_attempted if shot_signals else 0
            current_fgm = player.current.field_goals_made if shot_signals else 0
            current_3pm = player.current.three_pointers_made if shot_signals else 0

            season_fga_per_min: Optional[float] = None
            season_efg: Optional[float] = None
            if shot_signals and season_minutes > 0 and player.season_average.field_goals_attempted > 0:
                season_fga_per_min = (
                    player.season_average.field_goals_attempted / season_minutes
                )
                season_efg = (
                    player.season_average.field_goals_made
                    + 0.5 * player.season_average.three_pointers_made
                ) / player.season_average.field_goals_attempted

            # Matchup do adversário deste player (DRtg do oponente).
            opp_matchup = (
                away_matchup if player.team == bs.home_team.tricode else home_matchup
            )
            line_ctx = LineContext(
                season_avg=season_avg,
                last_10_avg=last_10_avg,
                last_5_avg=last_5_avg,
                season_minutes=season_minutes,
                current_stat=current_stat,
                minutes_played=player.minutes,
                projected_end=projection,
                current_fga=current_fga,
                current_fgm=current_fgm,
                current_3pm=current_3pm,
                season_fga_per_min=season_fga_per_min,
                season_efg=season_efg,
                is_starter=player.is_starter,
                on_court=player.on_court,
                fouls=player.fouls,
                opponent_drtg_factor=opp_matchup.drtg_factor,
                pace_factor_matchup=combined_pace_factor,
                blowout_severity=ctx["blowout_severity"],
                game_minutes_remaining=ctx["game_minutes_remaining"],
            )
            result = self.line_engine.calculate(line_ctx)
            edge = round(projection - result.line, 1)
            decision = calculate_edge_decision(edge)
            # Mai/2026: rebaixa decisão se sample pequeno (Robinson/Duren
            # ~7min viravam STRONG_OVER). <6min→NEUTRAL, 6-10min→LEAN.
            decision = dampen_decision_for_low_sample(decision, player.minutes)
            betting_conf = betting_confidence_from_signals(
                edge=edge,
                heat_score=heat_score,
                sample_confidence=sample_conf,
                projection_confidence=projection_conf,
            )
            # Real line do The Odds API (None = indisponível). Lado a lado
            # com o synthetic — usuário compara.
            real_line: Optional[float] = None
            real_edge: Optional[float] = None
            real_decision: Optional[str] = None
            real_book_count = 0
            real_line_age_seconds: Optional[int] = None
            if real_odds is not None:
                real_line = real_odds.line
                real_edge = round(projection - real_odds.line, 1)
                real_decision = calculate_edge_decision(real_edge)
                real_decision = dampen_decision_for_low_sample(
                    real_decision, player.minutes
                )
                real_book_count = real_odds.book_count
                # Idade da linha em segundos — frescor pro front.
                # fetched_at = 0.0 só em testes/mocks; usa None nesse caso.
                if real_odds.fetched_at > 0.0:
                    import time as _time
                    real_line_age_seconds = max(
                        0, int(_time.time() - real_odds.fetched_at)
                    )

            # Recomendação ponderada. Quando real_line está disponível,
            # ela é a referência (mercado real); senão, cai pro synthetic.
            # `has_real_book` ativa o BOOK BYPASS no bet_recommendation:
            # se o edge real for ≥ 2.0, aceita SMALL mesmo com confidence
            # baixa (book serve como segunda opinião, reduz risco de falso
            # positivo solo).
            recommend_edge = real_edge if real_edge is not None else edge
            rec_label, rec_size = bet_recommendation(
                edge=recommend_edge,
                betting_confidence=betting_conf,
                usage=usage,
                has_real_book=real_edge is not None,
                variance_factor=variance_factor,
            )

            # Override total quando projeção é indeterminate (mai/2026):
            # amostra pequena + zero produção. Sem projeção confiável,
            # qualquer edge é falso. Força NEUTRAL/PASS pra suprimir
            # recomendação espúria. Mantém line/real_line pra UI mostrar
            # "qual era a linha" mas sem CTA de aposta.
            if is_indeterminate:
                edge = 0.0
                decision = "NEUTRAL"
                if real_line is not None:
                    real_edge = 0.0
                    real_decision = "NEUTRAL"
                rec_label = "PASS"
                rec_size = 0.0

            # LineLog (mai/2026): grava aqui em vez de dentro do LineEngine
            # — temos o contexto completo (decision, edge, real_line). Esse
            # é o registro que o BackTester replica depois pra medir hit rate.
            # Falha silenciosa via flag LOG_LINE_CALC.
            if stat_label:
                from src.services.line.line_log import log_line_calculation
                # Log DESCRITIVO (mai/2026): além da linha/projeção, captura
                # TODOS os sinais que disparam caps na projeção (rotação,
                # period rate, heat, rates) + o breakdown completo. O objetivo
                # é poder RE-RODAR variações de regra offline em cima do
                # histórico — testar vários cenários sem esperar jogos novos.
                # Nada disso altera cálculo; é puro registro.
                log_extra = {
                    "rotation_status": rotation_status,
                    "is_unexpected_rest": is_unexpected_rest,
                    "on_court": player.on_court,
                    "is_starter": player.is_starter,
                    "minutes_played": player.minutes,
                    "fouls": player.fouls,
                    "period": ctx["period"],
                    "heat_score": heat_score,
                    "current_rate": (
                        round(current_stat / player.minutes, 4)
                        if player.minutes > 0 else None
                    ),
                    "prior_rate": (
                        round(season_avg / season_minutes, 4)
                        if season_minutes > 0 else None
                    ),
                    "period_production_rate": period_production_rate,
                    # Breakdown completo da projeção: period_rate_input,
                    # final_expected, sanity_cap, in_deficit_cap,
                    # unexpected_rest_cap_* (com a projeção pré-cap embutida),
                    # variance, etc. É a fonte mais rica pra testar regras.
                    "projection_breakdown": projection_breakdown,
                }
                log_line_calculation(
                    player_id=player.player_id,
                    player_name=player.name,
                    team_tricode=player.team,
                    stat=stat_label,
                    line_context=line_ctx,
                    line_result=result,
                    projection=projection,
                    game_id=game_id,
                    decision=decision,
                    edge=edge,
                    real_line=real_line,
                    real_edge=real_edge,
                    real_book_count=real_book_count,
                    extra=log_extra,
                )

            return FairLineSchema(
                line=result.line,
                edge=edge,
                decision=decision,
                reason=result.reason,
                betting_confidence=betting_conf,
                betting_confidence_label=confidence_label(betting_conf),
                real_line=real_line,
                real_edge=real_edge,
                real_decision=real_decision,
                real_book_count=real_book_count,
                real_line_age_seconds=real_line_age_seconds,
                bet_recommendation=rec_label,
                bet_recommendation_size=rec_size,
            )

        # Lookup das médias recentes (last_5 / last_10) por player_id.
        # _get_season_averages é cacheado, então isso é praticamente free.
        # Defaults pra zero se algum jogador não tiver dados (evita travar
        # toda a resposta por 1 falha pontual de fetch da temporada).
        recent_avgs_by_id: dict[int, dict[str, float]] = {}
        for p in ranking:
            avgs = self._get_season_averages(p.player_id, season) or {}
            recent_avgs_by_id[p.player_id] = avgs

        # Fetch único de PBP pra TODO o jogo (cacheado 5s no service).
        # Map: {player_id: {period: {points, assists, rebounds, three_pt_made,
        #                            two_pt_made, minutes_played, intervals}}}
        # Em jogo "not_started" ou se PBP falhar, vem dict vazio — front
        # renderiza "—". A versão `with_court_time` rastreia subs pra derivar
        # minutos jogados + intervalos em quadra por período (mai/2026).
        q1_starters = {
            p.player_id
            for team in (bs.home_team, bs.away_team)
            for p in team.players
            if p.is_starter
        }
        # live_period + live_clock_minutes garantem que intervalos abertos
        # do quarter atual fechem no clock CORRETO (não em 0). Sem isso,
        # starter de jogo recém-começado virava 12 min jogados (bug Gobert).
        per_period_by_id = self.pbp.get_per_period_stats_with_court_time(
            game_id, q1_starters,
            live_period=ctx["period"] if bs.game_status == "in_progress" else None,
            live_clock_minutes=ctx["clock_minutes_remaining"] if bs.game_status == "in_progress" else None,
        )

        # Anomaly alerts (item 5, mai/2026): regras determinísticas
        # rodam UMA VEZ pra todos os jogadores ativos do boxscore.
        # Mapeio por player_id pra `_build_player` consumir.
        minute_of_game = int(round(
            (max(ctx["period"], 1) - 1) * 12.0
            + (12.0 - ctx["clock_minutes_remaining"])
        ))
        anomaly_inputs = [
            AnomalyPlayerStatsSchema(
                player_id=p.player_id,
                player_name=p.name,
                team_abbr=p.team,
                minutes=p.minutes,
                points=p.current.points,
                rebounds=p.current.rebounds,
                assists=p.current.assists,
                steals=0,    # não vem em LivePlayerAnalysisSchema
                blocks=0,
                three_pointers_made=p.current.three_pointers_made,
                fouls_personal=p.fouls,
                minute_of_game=max(minute_of_game, 1),
            )
            for p in ranking
        ]
        try:
            all_alerts = self.anomaly.detect(anomaly_inputs)
        except Exception as exc:
            logger.warning("AnomalyService falhou pro jogo %s: %s", game_id, exc)
            all_alerts = []
        alerts_by_player: dict[int, list[HotStatSchema]] = {}
        for alert in all_alerts:
            alerts_by_player.setdefault(alert.player_id, []).append(alert)

        # Real odds (mai/2026, The Odds API) — fetch único por jogo, com
        # cache de TTL dinâmico DENTRO do service. Quando `self.odds` é
        # None (feature flag off ou sem API key), volta dict vazio e
        # `real_line` nos schemas fica None.
        odds_by_player: dict[int, dict[str, "PlayerOdds"]] = {}
        if self.odds is not None:
            try:
                odds_by_player = self.odds.get_player_lines(
                    game_id=game_id,
                    period=ctx["period"],
                    clock_minutes_remaining=ctx["clock_minutes_remaining"],
                    game_status=bs.game_status,
                    home_tricode=bs.home_team.tricode,
                    away_tricode=bs.away_team.tricode,
                )
            except Exception as exc:
                logger.warning("OddsService falhou pro jogo %s: %s", game_id, exc)
                odds_by_player = {}

        def _build_player(p) -> HotRankingPlayerSchema:
            r = recent_avgs_by_id.get(p.player_id, {})
            l5_p  = r.get("last_5_points",    p.season_average.points)
            l5_r  = r.get("last_5_rebounds",  p.season_average.rebounds)
            l5_a  = r.get("last_5_assists",   p.season_average.assists)
            l5_tpm  = r.get("last_5_three_pm",  p.season_average.three_pointers_made)
            l10_p = r.get("last_10_points",   p.season_average.points)
            l10_r = r.get("last_10_rebounds", p.season_average.rebounds)
            l10_a = r.get("last_10_assists",  p.season_average.assists)
            l10_tpm = r.get("last_10_three_pm", p.season_average.three_pointers_made)

            # Heat score (Fase 4) — sinal composto pra boost/cut na projeção.
            # Computado 1x por player (não muda entre PTS/REB/AST).
            season_fga_per_min = (
                p.season_average.field_goals_attempted / p.season_average.minutes
                if p.season_average.minutes > 0 else None
            )
            season_efg = (
                (p.season_average.field_goals_made + 0.5 * p.season_average.three_pointers_made)
                / p.season_average.field_goals_attempted
                if p.season_average.field_goals_attempted > 0 else None
            )
            season_fta_per_min = (
                p.season_average.free_throws_attempted / p.season_average.minutes
                if p.season_average.minutes > 0 else None
            )
            # Item 1 (mai/2026): per-stat heat também precisa de AST/min e
            # REB/min médios da temporada.
            season_ast_per_min = (
                p.season_average.assists / p.season_average.minutes
                if p.season_average.minutes > 0 else None
            )
            season_reb_per_min = (
                p.season_average.rebounds / p.season_average.minutes
                if p.season_average.minutes > 0 else None
            )
            heat = self.heat.score(
                minutes_played=p.minutes,
                current_points=p.current.points,
                current_fga=p.current.field_goals_attempted,
                current_fgm=p.current.field_goals_made,
                current_3pm=p.current.three_pointers_made,
                current_fta=p.current.free_throws_attempted,
                current_ftm=p.current.free_throws_made,
                season_minutes=p.season_average.minutes,
                season_fga_per_min=season_fga_per_min,
                season_efg=season_efg,
                season_fta_per_min=season_fta_per_min,
                current_assists=p.current.assists,
                current_rebounds=p.current.rebounds,
                current_turnovers=0,  # turnovers não vêm em LiveCurrentStatsSchema; deixa 0
                season_ast_per_min=season_ast_per_min,
                season_reb_per_min=season_reb_per_min,
            )

            # Rotation: minutos esperados restantes + context explicável
            # (Fase 2 V3, mai/2026). Respeita ENABLE_NBA_ROTATION_ADJUSTMENT.
            from src.config import ENABLE_NBA_ROTATION_ADJUSTMENT
            from src.services.rotation.rotation_context import (
                build_context as build_rotation_context,
                clutch_minute_in_window,
            )
            rot_remaining: Optional[float] = None
            rot_context_dict: Optional[dict] = None
            clutch_boost = 0.0
            blowout_cut = 0.0
            rot_profile = None  # init pra escopo: usado depois no period_rate
            if ENABLE_NBA_ROTATION_ADJUSTMENT:
                rot_profile = self.rotation.get_profile(
                    player_id=p.player_id,
                    season_minutes=p.season_average.minutes,
                )
                rot_remaining = self.rotation.expected_minutes_remaining(
                    rot_profile,
                    period=ctx["period"],
                    clock_minutes_remaining=ctx["clock_minutes_remaining"],
                    minutes_already_played=p.minutes,
                    blowout_severity=ctx["blowout_severity"],
                )
                # Score difference do POV do time do player
                team_score = (
                    bs.home_team.score if p.team == bs.home_team.tricode
                    else bs.away_team.score
                )
                opp_score = (
                    bs.away_team.score if p.team == bs.home_team.tricode
                    else bs.home_team.score
                )
                score_diff = team_score - opp_score
                is_close_game = abs(score_diff) <= 8
                rot_ctx = build_rotation_context(
                    profile=rot_profile,
                    expected_remaining_minutes=rot_remaining,
                    period=ctx["period"],
                    clock_minutes_remaining=ctx["clock_minutes_remaining"],
                    is_player_on_court=p.on_court,
                    score_difference=score_diff,
                    is_close_game=is_close_game,
                )
                rot_context_dict = {
                    "available": rot_ctx.available,
                    "expected_remaining_minutes": rot_ctx.expected_remaining_minutes,
                    "current_rotation_status": rot_ctx.current_rotation_status,
                    "blowout_risk": rot_ctx.blowout_risk,
                    "closing_game_probability": rot_ctx.closing_game_probability,
                    "rotation_confidence": rot_ctx.rotation_confidence,
                    "notes": rot_ctx.notes,
                    "sample_games": rot_ctx.sample_games,
                }
                # Clutch boost (item 9, mai/2026): single source = closing
                # game probability já calculado dentro do RotationContext
                # (com todos os gates: Q4+, competitivo, perfil clutch, etc).
                # Se chegou >0 lá, é aplicável aqui.
                if (
                    is_close_game
                    and clutch_minute_in_window(
                        ctx["period"], ctx["clock_minutes_remaining"]
                    )
                    and rot_profile.clutch_usage
                    and rot_profile.clutch_usage.get("usually_closes_games")
                    and rot_ctx.closing_game_probability > 0
                ):
                    clutch_boost = rot_ctx.closing_game_probability
                # Blowout-specific cut quando jogador costuma sentar
                if rot_ctx.blowout_risk == "HIGH":
                    blowout_cut = 1.0
                elif rot_ctx.blowout_risk == "MEDIUM":
                    blowout_cut = 0.5

            # Period production rate (Fase 11): rate empírico do player
            # NESTE período pra cada stat, derivado do PBP histórico ×
            # histograma. None se RotationProvider não tem PBP fetcher
            # ou se sample insuficiente.
            from src.services.rotation.production_by_period import lookup_rate
            period_rate_pts = None
            period_rate_ast = None
            period_rate_reb = None
            if (
                ENABLE_NBA_ROTATION_ADJUSTMENT
                and rot_profile
                and rot_profile.production_by_period
                and 1 <= ctx["period"] <= 4
            ):
                period_rate_pts = lookup_rate(
                    rot_profile.production_by_period,
                    period=ctx["period"], stat="points",
                )
                period_rate_ast = lookup_rate(
                    rot_profile.production_by_period,
                    period=ctx["period"], stat="assists",
                )
                period_rate_reb = lookup_rate(
                    rot_profile.production_by_period,
                    period=ctx["period"], stat="rebounds",
                )

            # Rest days (mai/2026): dias desde o último jogo do gamelog.
            # None = não computável (sem gamelog ou sem today). Quando válido,
            # a projeção aplica B2B=-8% / 2d=+2% / 3+d=+3%.
            rest_d = self._get_rest_days(p.player_id, season, today_iso)

            # UNEXPECTED_REST flag (item 3, mai/2026): jogador no banco em
            # minuto onde o perfil histórico diz que ele DEVERIA estar em
            # quadra. Sinal forte de problema (lesão, foul out, decisão).
            # ProjectionEngine usa pra capar projeção em current × 1.05.
            is_unexpected_rest = (
                rot_context_dict is not None
                and rot_context_dict.get("current_rotation_status")
                == "UNEXPECTED_REST"
            )
            # Status completo de rotação pro log descritivo (não só o
            # boolean acima) — permite testar cenários offline depois
            # (EXPECTED_REST, UNEXPECTED_ON_COURT, etc.).
            rotation_status = (
                rot_context_dict.get("current_rotation_status")
                if rot_context_dict else None
            )

            # Sample confidence (Item 6, mai/2026) — derivada dos minutos
            # jogados ATÉ AGORA. Re-usada nas 3 chamadas de _fair_line e no
            # confidence_breakdown abaixo.
            sample_conf = sample_confidence_from_minutes(p.minutes)

            # Usage proxy (item 6, mai/2026): quão primário o jogador é no
            # ataque do time. Usado pra ponderar bet recommendation depois
            # (item 4) — primary options têm projeção mais "trustável" em
            # alto edge, role players viram noise rapidamente.
            player_usage = usage_proxy(
                season_fga=p.season_average.field_goals_attempted,
                season_minutes=p.season_average.minutes,
            )

            # Variance factors (item 2, mai/2026): std-based confidence
            # factor por stat. Usado pra modular projection_confidence —
            # cara volátil tem projection_confidence mais baixa mesmo se
            # minutos e amostra forem ok.
            variance_factors = self._get_variance_factors(p.player_id, season)

            # Item 1 (mai/2026): heat por stat — cada projeção recebe APENAS
            # o sub-score relevante. Cara com eFG estourado mas REB normal
            # NÃO leva boost em REB.
            proj_pts = _proj(p.current.points, p.minutes,
                             p.season_average.points, p.season_average.minutes, p.fouls,
                             p.is_starter,
                             last_10_avg=l10_p, last_5_avg=l5_p,
                             heat_score=heat.for_stat("points"),
                             expected_minutes_remaining=rot_remaining,
                             clutch_close_game_boost=clutch_boost,
                             rotation_blowout_cut=blowout_cut,
                             period_production_rate=period_rate_pts,
                             rest_days=rest_d,
                             is_unexpected_rest=is_unexpected_rest,
                             variance_factor=variance_factors["points"])
            proj_ast = _proj(p.current.assists, p.minutes,
                             p.season_average.assists, p.season_average.minutes, p.fouls,
                             p.is_starter,
                             last_10_avg=l10_a, last_5_avg=l5_a,
                             heat_score=heat.for_stat("assists"),
                             expected_minutes_remaining=rot_remaining,
                             clutch_close_game_boost=clutch_boost,
                             rotation_blowout_cut=blowout_cut,
                             period_production_rate=period_rate_ast,
                             rest_days=rest_d,
                             is_unexpected_rest=is_unexpected_rest,
                             variance_factor=variance_factors["assists"],
                             # AST: 4 ast em 9 min são ~6 possessões coletivas,
                             # sample muito ruidoso. Threshold maior pra
                             # shrinkage continuar protegendo contra hot-start.
                             shrinkage_min_threshold=14.0)
            proj_reb = _proj(p.current.rebounds, p.minutes,
                             p.season_average.rebounds, p.season_average.minutes, p.fouls,
                             p.is_starter,
                             last_10_avg=l10_r, last_5_avg=l5_r,
                             heat_score=heat.for_stat("rebounds"),
                             expected_minutes_remaining=rot_remaining,
                             clutch_close_game_boost=clutch_boost,
                             rotation_blowout_cut=blowout_cut,
                             period_production_rate=period_rate_reb,
                             rest_days=rest_d,
                             is_unexpected_rest=is_unexpected_rest,
                             variance_factor=variance_factors["rebounds"],
                             # REB: matchup defensivo do quarto influencia muito.
                             # Threshold intermediário (10) vs PTS (8 padrão).
                             shrinkage_min_threshold=10.0)
            # 3PM projection (item 8, mai/2026): mesmo motor, sem
            # period_production_rate (não derivamos rate de 3PM por quarter
            # ainda) e sem clutch boost (3PM é menos sensível a clutch).
            # Heat herda do scoring (mesma natureza de tiro).
            proj_three_pm = _proj(
                p.current.three_pointers_made, p.minutes,
                p.season_average.three_pointers_made, p.season_average.minutes,
                p.fouls, p.is_starter,
                last_10_avg=l10_tpm, last_5_avg=l5_tpm,
                heat_score=heat.for_stat("three_pm"),
                expected_minutes_remaining=rot_remaining,
                rotation_blowout_cut=blowout_cut,
                rest_days=rest_d,
                is_unexpected_rest=is_unexpected_rest,
                variance_factor=variance_factors["three_pm"],
            )

            # Similar games analysis (mai/2026): "quando ele teve um início
            # assim, o que aconteceu?". Só roda quando jogador está em
            # underperformance no Q1 — onde o sinal é mais valioso pra
            # validar entrada (OVER vai virar ou continua frio?).
            similar_pts: Optional["SimilarGamesResultSchema"] = None
            # Pega Q1 stat do current game via PBP live
            current_q1_pts = (
                per_period_by_id.get(p.player_id, {}).get(1, {}).get("points", 0)
            )
            # Critério: Q1 baixo (< 60% do prior rate em 12min)
            prior_q1_expected = (p.season_average.points / 4.0) if p.season_average.points > 0 else 0
            is_q1_slow = (
                current_q1_pts > 0
                and prior_q1_expected > 2.0
                and current_q1_pts < prior_q1_expected * 0.6
            )
            # Ou então: jogador ON COURT em Q2+ com déficit detectado
            slow_overall = (
                ctx["period"] >= 1
                and p.minutes >= 6.0
                and p.season_average.points >= 5.0
                and (p.current.points / max(p.minutes, 1)) <
                    (p.season_average.points / max(p.season_average.minutes, 1)) * 0.65
            )
            if (is_q1_slow or slow_overall) and ctx["period"] <= 2:
                # Q1+Q2 only — depois disso, contexto muda demais pra
                # comparação com Q1 fazer sentido
                try:
                    similar_pts = self.similar_games.analyze(
                        player_id=p.player_id,
                        season=season,
                        current_q1_stat=int(current_q1_pts),
                        stat="points",
                    )
                except Exception as exc:
                    logger.info(
                        "similar_games falhou pra player %d (%s)",
                        p.player_id, exc,
                    )

            # Cap por similar_games (mai/2026, fix Shannon/McDaniels): quando
            # o histórico mostra que cara costuma terminar BAIXO em jogos
            # similares (recovery_factor < 0.85 + sample ≥ 8), evita
            # STRONG_OVER inflado. Cap = prior_avg × recovery_factor.
            #
            # Casos reais que motivaram:
            #   Shannon Jr. 2pts/9min projetando 11.7 com 📉 -21% no histórico
            #   McDaniels 7pts/16min projetando 18.3 com 📉 -19% no histórico
            # Em ambos: o badge de similar_games CONTRADIZ a recomendação
            # STRONG_OVER — não fazia sentido visual nem estatístico.
            if (
                similar_pts is not None
                and similar_pts.sample_size >= 8
                and similar_pts.recovery_factor < 0.85
            ):
                # Prior_avg blendado (mesma fórmula do engine: 55/30/15)
                prior_avg_pts = (
                    0.55 * p.season_average.points
                    + 0.30 * l10_p
                    + 0.15 * l5_p
                )
                similar_cap = prior_avg_pts * similar_pts.recovery_factor
                if proj_pts.expected > similar_cap:
                    # Preserva confidence/etc., atualiza expected + breakdown
                    new_breakdown = dict(proj_pts.breakdown or {})
                    new_breakdown["similar_games_cap_applied"] = {
                        "raw_expected": round(proj_pts.expected, 2),
                        "cap_value": round(similar_cap, 2),
                        "recovery_factor": round(similar_pts.recovery_factor, 3),
                        "sample_size": similar_pts.sample_size,
                    }
                    new_breakdown["final_expected"] = round(similar_cap, 2)
                    capped_pct = int((similar_pts.recovery_factor - 1) * 100)
                    proj_pts = proj_pts.model_copy(update={
                        "expected": round(similar_cap, 1),
                        "low": round(min(proj_pts.low, similar_cap), 1),
                        "high": round(max(similar_cap, proj_pts.high * 0.95), 1),
                        "reason": (
                            f"Histórico em jogos similares aponta "
                            f"{capped_pct}% vs média — projeção truncada"
                        ),
                        "breakdown": new_breakdown,
                    })

            # Confidence breakdown (Item 6) — agrega sample/rotation/projection
            # numa visão única explicável. projection_conf = média das 3 stats
            # (PTS/REB/AST) pra refletir a estabilidade global da projeção;
            # rotation_conf = vem do rotation_context (0 quando não disponível).
            #
            # Item 2 (mai/2026): cada projection_confidence é MULTIPLICADA
            # pelo variance_factor da stat. Cara volátil tem confidence cai
            # mesmo com minutos suficientes — projeção extrapolada de uma
            # média instável é objetivamente menos confiável.
            proj_conf_pts = (
                projection_confidence_from_label(proj_pts.confidence)
                * variance_factors["points"]
            )
            proj_conf_reb = (
                projection_confidence_from_label(proj_reb.confidence)
                * variance_factors["rebounds"]
            )
            proj_conf_ast = (
                projection_confidence_from_label(proj_ast.confidence)
                * variance_factors["assists"]
            )
            proj_conf_three_pm = (
                projection_confidence_from_label(proj_three_pm.confidence)
                * variance_factors["three_pm"]
            )
            proj_conf_avg = (proj_conf_pts + proj_conf_reb + proj_conf_ast) / 3.0
            rot_conf = (
                float(rot_context_dict.get("rotation_confidence", 0.0))
                if rot_context_dict else 0.0
            )
            overall_conf = round(
                0.40 * sample_conf + 0.40 * proj_conf_avg + 0.20 * rot_conf, 3
            )
            confidence_breakdown = ConfidenceBreakdownSchema(
                sample=sample_conf,
                sample_label=confidence_label(sample_conf),
                rotation=rot_conf,
                rotation_label=confidence_label(rot_conf),
                projection=round(proj_conf_avg, 3),
                projection_label=confidence_label(proj_conf_avg),
                overall=overall_conf,
                overall_label=confidence_label(overall_conf),
            )

            # Fair lines extraídas em variáveis (reusadas no schema E no
            # detector de cashout, que precisa da linha por mercado).
            _fl_pts = _fair_line(
                season_avg=p.season_average.points,
                last_10_avg=l10_p, last_5_avg=l5_p,
                season_minutes=p.season_average.minutes,
                current_stat=p.current.points,
                projection=proj_pts.expected,
                player=p,
                shot_signals=True,
                stat_label="PTS",
                heat_score=heat.for_stat("points"),
                sample_conf=sample_conf,
                projection_conf=proj_conf_pts,
                real_odds=odds_by_player.get(p.player_id, {}).get(_ODDS_MARKET_PTS),
                usage=player_usage,
                is_indeterminate=proj_pts.indeterminate,
                variance_factor=variance_factors["points"],
                projection_breakdown=proj_pts.breakdown,
                is_unexpected_rest=is_unexpected_rest,
                period_production_rate=period_rate_pts,
                rotation_status=rotation_status,
            )
            _fl_reb = _fair_line(
                season_avg=p.season_average.rebounds,
                last_10_avg=l10_r, last_5_avg=l5_r,
                season_minutes=p.season_average.minutes,
                current_stat=p.current.rebounds,
                projection=proj_reb.expected,
                player=p,
                shot_signals=False,
                stat_label="REB",
                heat_score=heat.for_stat("rebounds"),
                sample_conf=sample_conf,
                projection_conf=proj_conf_reb,
                real_odds=odds_by_player.get(p.player_id, {}).get(_ODDS_MARKET_REB),
                usage=player_usage,
                is_indeterminate=proj_reb.indeterminate,
                variance_factor=variance_factors["rebounds"],
                projection_breakdown=proj_reb.breakdown,
                is_unexpected_rest=is_unexpected_rest,
                period_production_rate=period_rate_reb,
                rotation_status=rotation_status,
            )
            _fl_ast = _fair_line(
                season_avg=p.season_average.assists,
                last_10_avg=l10_a, last_5_avg=l5_a,
                season_minutes=p.season_average.minutes,
                current_stat=p.current.assists,
                projection=proj_ast.expected,
                player=p,
                shot_signals=False,
                stat_label="AST",
                heat_score=heat.for_stat("assists"),
                sample_conf=sample_conf,
                projection_conf=proj_conf_ast,
                real_odds=odds_by_player.get(p.player_id, {}).get(_ODDS_MARKET_AST),
                usage=player_usage,
                is_indeterminate=proj_ast.indeterminate,
                variance_factor=variance_factors["assists"],
                projection_breakdown=proj_ast.breakdown,
                is_unexpected_rest=is_unexpected_rest,
                period_production_rate=period_rate_ast,
                rotation_status=rotation_status,
            )

            # ── CASHOUT (mai/2026) ────────────────────────────────────────
            # Avalia os 3 mercados; emite no de linha mais colada. Usa a
            # linha REAL do book quando disponível, senão a sintética.
            _cashout: Optional[CashoutAlertSchema] = None
            if rot_context_dict is not None:
                _markets = [
                    ("PTS", float(p.current.points),
                     _fl_pts.real_line if _fl_pts.real_line is not None else _fl_pts.line),
                    ("REB", float(p.current.rebounds),
                     _fl_reb.real_line if _fl_reb.real_line is not None else _fl_reb.line),
                    ("AST", float(p.current.assists),
                     _fl_ast.real_line if _fl_ast.real_line is not None else _fl_ast.line),
                ]
                _co = detect_cashout(
                    markets=_markets,
                    on_court=p.on_court,
                    status=p.status,
                    is_starter=p.is_starter,
                    usage=player_usage,
                    foul_trouble=p.fouls >= 4,
                    rotation_available=bool(rot_context_dict.get("available")),
                    expected_remaining_minutes=float(
                        rot_context_dict.get("expected_remaining_minutes", 0.0)
                    ),
                    closing_game_probability=float(
                        rot_context_dict.get("closing_game_probability", 0.0)
                    ),
                    current_rotation_status=str(
                        rot_context_dict.get("current_rotation_status", "UNKNOWN")
                    ),
                    rotation_confidence=float(
                        rot_context_dict.get("rotation_confidence", 0.0)
                    ),
                    blowout_risk=str(rot_context_dict.get("blowout_risk", "LOW")),
                )
                if _co is not None:
                    _cashout = CashoutAlertSchema(**_co)

            return HotRankingPlayerSchema(
                player_id=p.player_id,
                name=p.name,
                jersey_num=p.jersey_num,
                team=p.team,
                minutes=p.minutes,
                current_points=p.current.points,
                current_assists=p.current.assists,
                current_rebounds=p.current.rebounds,
                expected_points=p.expected_until_now.points,
                expected_assists=p.expected_until_now.assists,
                expected_rebounds=p.expected_until_now.rebounds,
                points_diff=p.difference.points,
                assists_diff=p.difference.assists,
                rebounds_diff=p.difference.rebounds,
                projected_points=self._project_game(
                    p.current.points, p.minutes,
                    p.season_average.points, p.season_average.minutes,
                ),
                projected_assists=self._project_game(
                    p.current.assists, p.minutes,
                    p.season_average.assists, p.season_average.minutes,
                ),
                projected_rebounds=self._project_game(
                    p.current.rebounds, p.minutes,
                    p.season_average.rebounds, p.season_average.minutes,
                ),
                pace_projection_points=proj_pts,
                pace_projection_assists=proj_ast,
                pace_projection_rebounds=proj_reb,
                # Médias recentes — base do synthetic fair line.
                last_5_points=l5_p,
                last_5_rebounds=l5_r,
                last_5_assists=l5_a,
                last_10_points=l10_p,
                last_10_rebounds=l10_r,
                last_10_assists=l10_a,
                # Linha estimada + edge (projeção − linha) por mercado.
                # Pra PTS, ativamos sinais de volume/eficiência de arremesso —
                # pra REB/AST esses sinais não fazem sentido direto.
                fair_line_points=_fl_pts,
                fair_line_rebounds=_fl_reb,
                fair_line_assists=_fl_ast,
                pace_projection_three_pm=proj_three_pm,
                fair_line_three_pm=_fair_line(
                    season_avg=p.season_average.three_pointers_made,
                    last_10_avg=l10_tpm, last_5_avg=l5_tpm,
                    season_minutes=p.season_average.minutes,
                    current_stat=p.current.three_pointers_made,
                    projection=proj_three_pm.expected,
                    player=p,
                    shot_signals=False,
                    stat_label="3PM",
                    heat_score=heat.for_stat("three_pm"),
                    sample_conf=sample_conf,
                    projection_conf=proj_conf_three_pm,
                    real_odds=None,
                    usage=player_usage,
                    is_indeterminate=proj_three_pm.indeterminate,
                    variance_factor=variance_factors["three_pm"],
                    projection_breakdown=proj_three_pm.breakdown,
                    is_unexpected_rest=is_unexpected_rest,
                    period_production_rate=None,
                    rotation_status=rotation_status,
                ),
                fouls=p.fouls,
                foul_trouble=p.fouls >= 4,
                blowout_impact=_impact(p),
                # blowout_risk legacy: True se houver impact pra ESTE jogador
                # (não mais "any blowout risk no jogo"). Mantido pra
                # compatibilidade até o front migrar.
                blowout_risk=_impact(p) is not None,
                on_court=p.on_court,
                is_starter=p.is_starter,
                shooting_impact=p.shooting_impact,
                status=p.status,
                points_status=calc_per_stat_status(p.difference.points, "points"),
                assists_status=calc_per_stat_status(p.difference.assists, "assists"),
                rebounds_status=calc_per_stat_status(p.difference.rebounds, "rebounds"),
                score=p.score,
                performance_rating=p.performance_rating,
                performance_label=p.performance_label,
                heat_score=heat.score,
                heat_label=heat.label,
                usage=round(player_usage, 3),
                usage_label=usage_label(player_usage),
                rotation_context=(
                    RotationContextSchema(**rot_context_dict)
                    if rot_context_dict else None
                ),
                confidence_breakdown=confidence_breakdown,
                anomaly_alerts=alerts_by_player.get(p.player_id, []),
                cashout_alert=_cashout,
                similar_games_points=similar_pts,
                # Split por período do PBP. Ordena por period asc pro front
                # renderizar Q1→Q4→OT na ordem cronológica natural.
                periods=[
                    QuarterStatsSchema(period=period, **counters)
                    for period, counters in sorted(
                        per_period_by_id.get(p.player_id, {}).items()
                    )
                ],
            )

        return HotRankingSchema(
            game_id=game_id,
            limit=limit,
            ranking=[_build_player(p) for p in ranking],
            game_status=bs.game_status,
            period=bs.period,
            clock=bs.clock,
            home_score=bs.home_team.score,
            away_score=bs.away_team.score,
            blowout_risk=blowout_payload,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Hot rankings agregado de TODOS os jogos do dia ───────────────────
    # Usado pela tela "Todos os jogos" do Hot Picks (front). Em vez do
    # cliente abrir N requests pro endpoint por-jogo, ele faz 1 só e
    # recebe a lista consolidada — o backend paraleliza internamente
    # e cacheia o resultado por alguns segundos pra suportar polling.

    # TTL curto: 15s é o suficiente pra reduzir N x get_hot_ranking
    # entre polls sem deixar o ranking visivelmente velho (cada
    # get_hot_ranking já tem custo ≥ 1-2s).
    _ALL_TODAY_TTL = 15

    def get_all_today_hot_rankings(
        self,
        season: str,
        limit: int,
        games: list,  # list[LiveGameSchema] — caller extrai da fonte (worker/ESPN)
        consider_blowout: Optional[bool] = None,
    ) -> TodayHotRankingsSchema:
        """Calcula hot ranking pra todos os jogos vivos/finalizados do dia.

        Caller passa a lista de jogos. Desacopla o service da fonte:
        NBA usa o snapshot do worker, WNBA usa ESPN. Mesma função.

        Tolerante a falha por jogo: se um get_hot_ranking falha, o jogo cai
        em `errors` com a razão e os outros são entregues normalmente. O
        front decide se mostra um aviso discreto.

        Paralelização: ThreadPoolExecutor — get_hot_ranking é I/O-bound
        (ESPN + odds proxies) e o GIL libera durante o I/O. max_workers=8
        cobre cards da WNBA (5-8 jogos/dia tipicamente) sem explodir
        thread count.
        """
        cache_key = f"today_hot_rankings:{season}:{limit}:{consider_blowout}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Pré-jogo não tem stats → nada pra rankear.
        eligible = [
            g for g in (games or [])
            if g.game_status in ("in_progress", "final")
        ]

        if not eligible:
            return TodayHotRankingsSchema(
                season=season,
                items=[],
                errors=[],
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

        items: list[TodayHotRankingItemSchema] = []
        errors: list[TodayHotRankingErrorSchema] = []

        def _one(game) -> tuple[str, Optional[HotRankingSchema], Optional[str]]:
            try:
                ranking = self.get_hot_ranking(
                    game.game_id, season, limit,
                    consider_blowout=consider_blowout,
                )
                return game.game_id, ranking, None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_all_today_hot_rankings: jogo %s falhou: %s",
                    game.game_id, exc,
                )
                return game.game_id, None, str(exc)

        results: dict[str, tuple[Optional[HotRankingSchema], Optional[str]]] = {}
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="all-rankings") as ex:
            futures = {ex.submit(_one, g): g for g in eligible}
            for fut in as_completed(futures):
                gid, ranking, err = fut.result()
                results[gid] = (ranking, err)

        # Preserva ordem do snapshot pra não embaralhar o feed entre polls.
        for game in eligible:
            ranking, err = results.get(game.game_id, (None, "missing"))
            if ranking is not None:
                items.append(
                    TodayHotRankingItemSchema(
                        game_id=game.game_id,
                        away_tricode=game.away_team.tricode,
                        home_tricode=game.home_team.tricode,
                        away_score=game.away_team.score,
                        home_score=game.home_team.score,
                        game_status=game.game_status,
                        period=game.period,
                        clock=game.clock,
                        ranking=ranking,
                    )
                )
            elif err is not None:
                errors.append(
                    TodayHotRankingErrorSchema(game_id=game.game_id, reason=err)
                )

        result = TodayHotRankingsSchema(
            season=season,
            items=items,
            errors=errors,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._cache.set(cache_key, result, self._ALL_TODAY_TTL)
        return result
