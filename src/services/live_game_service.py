import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nba_api.live.nba.endpoints import boxscore, scoreboard
from nba_api.stats.endpoints import scoreboardv2

from src.config import USE_FIXTURES
from src.schemas.live_schemas import (
    BlowoutRiskSchema,
    LineupGameSchema,
    LineupPlayerSchema,
    LineupTeamSchema,
    LiveBoxscoreSchema,
    LiveGameSchema,
    LivePlayerStatsSchema,
    LiveTeamBoxscoreSchema,
    LiveTeamSchema,
    PlayerBlowoutImpactSchema,
    QuarterStatsSchema,
    TodayGamesSchema,
)
from src.utils.cache import SimpleCache
from src.utils.photos import player_photo_url
from src.utils.stats import (
    calculate_blowout_risk,
    calculate_player_blowout_impact,
    calculate_player_performance_rating,
    rounded,
)
from src.utils.time_utils import (
    format_game_clock,
    map_game_status,
    parse_minutes_to_float,
    today_in_et,
)

logger = logging.getLogger(__name__)

SCOREBOARD_TTL = 30           # seconds — cache do scoreboard live
SCOREBOARD_BY_DATE_TTL = 300  # seconds — ScoreboardV2 muda menos (lista
                              # do dia já é estável). 5 min é mais que
                              # suficiente pra fallback.
SCOREBOARD_V2_TIMEOUT = 8     # seconds — timeout AGRESSIVO no ScoreboardV2.
                              # Default do nba_api é 30s, mas o worker fica
                              # travado nesse tempo todo sem atualizar o
                              # snapshot → /games/live/today devolve 503
                              # → frontend mostra erro. Melhor falhar rápido
                              # e cair de volta no live (que já temos).
BOXSCORE_TTL = 5      # seconds — clock/score atualizam a cada 5s no front,
                      # então fazia sentido bater no live API com a mesma cadência.
                      # cdn.nba.com aguenta sem stress.

# Fixtures path — relativo à raiz do repo (subindo de src/services).
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _load_fixture(name: str) -> Optional[dict]:
    """Lê um JSON de tests/fixtures/. Retorna None se não existir."""
    path = _FIXTURES_DIR / name
    if not path.exists():
        logger.warning("Fixture %s não encontrada em %s", name, path)
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# Rank de "quão avançado/confiável" é o estado de um jogo. Usado pra
# mesclar live + scheduled SEM nunca rebaixar um jogo (um jogo que já
# começou/terminou jamais deve voltar pra "não iniciado").
_STATUS_RANK = {"in_progress": 3, "final": 2, "not_started": 1, "unknown": 0}


def _merge_scoreboards(
    live: "TodayGamesSchema",
    scheduled: "TodayGamesSchema",
) -> "TodayGamesSchema":
    """
    Mescla os dois scoreboards por game_id, mantendo por jogo o estado
    MAIS avançado (in_progress > final > not_started). Resolve o bug do
    jogo recém-finalizado aparecer como "não iniciado" 0-0 porque o
    ScoreboardV2 ainda listava ele como agendado.

    - `scheduled` define o slate correto da data ET (e jogos futuros).
    - `live` sobrepõe quando conhece um estado mais avançado/placar real.
    - game_time_utc: preserva o que existir (scheduled não expõe).
    """
    order: list[str] = []
    by_id: dict[str, LiveGameSchema] = {}

    def consider(g: LiveGameSchema) -> None:
        prev = by_id.get(g.game_id)
        if prev is None:
            by_id[g.game_id] = g
            order.append(g.game_id)
            return
        r_new = _STATUS_RANK.get(g.game_status, 0)
        r_old = _STATUS_RANK.get(prev.game_status, 0)
        chosen, other = (
            (g, prev) if r_new > r_old else
            (prev, g) if r_new < r_old else
            # mesmo rank → quem tem mais placar (dado real) vence
            (g, prev)
            if (g.home_team.score + g.away_team.score)
               >= (prev.home_team.score + prev.away_team.score)
            else (prev, g)
        )
        # Não perde o horário previsto: se o escolhido não tem mas o
        # outro tem, herda.
        if not chosen.game_time_utc and other.game_time_utc:
            chosen = chosen.model_copy(
                update={"game_time_utc": other.game_time_utc}
            )
        by_id[g.game_id] = chosen

    for g in scheduled.games:
        consider(g)
    for g in live.games:
        consider(g)

    games = [by_id[i] for i in order]
    all_final = bool(games) and all(
        x.game_status == "final" for x in games
    )
    return TodayGamesSchema(
        date=scheduled.date or live.date,
        games=games,
        source="scheduled",
        all_final=all_final,
    )


def _parse_player(p: dict) -> Optional[LivePlayerStatsSchema]:
    """Parser para análise live (filtra quem não jogou)."""
    stats = p.get("statistics", {})
    minutes = parse_minutes_to_float(stats.get("minutes", "PT00M00.00S"))
    if minutes <= 0:
        return None
    return LivePlayerStatsSchema(
        player_id=int(p.get("personId", 0)),
        name=p.get("name", ""),
        position=p.get("position", ""),
        jersey_num=str(p.get("jerseyNum", "")),
        is_starter=str(p.get("starter", "0")) == "1",
        minutes=rounded(minutes),
        points=int(stats.get("points", 0)),
        rebounds=int(stats.get("reboundsTotal", 0)),
        assists=int(stats.get("assists", 0)),
        steals=int(stats.get("steals", 0)),
        blocks=int(stats.get("blocks", 0)),
        turnovers=int(stats.get("turnovers", 0)),
        field_goals_made=int(stats.get("fieldGoalsMade", 0)),
        field_goals_attempted=int(stats.get("fieldGoalsAttempted", 0)),
        three_pointers_made=int(stats.get("threePointersMade", 0)),
        three_pointers_attempted=int(stats.get("threePointersAttempted", 0)),
        free_throws_made=int(stats.get("freeThrowsMade", 0)),
        free_throws_attempted=int(stats.get("freeThrowsAttempted", 0)),
        plus_minus=int(stats.get("plusMinusPoints", 0)),
        fouls=int(stats.get("foulsPersonal", 0)),
        # 'oncourt' vem como string "1"/"0" (às vezes "0" mesmo entre jogadas).
        # Default True quando o campo está ausente — evita marcar metade do
        # boxscore como "no banco" se a NBA mudar o nome do campo.
        on_court=str(p.get("oncourt", "1")) == "1",
    )


def _parse_lineup_player(
    p: dict,
    game_blowout_pct: int = 0,
    game_blowout_level: str = "low",
    player_periods: Optional[dict[int, dict]] = None,
) -> LineupPlayerSchema:
    """
    Parser para a aba Lineups.

    Diferenças em relação a `_parse_player`:
    - NÃO filtra por minutes <= 0 (precisa mostrar reservas que não entraram)
    - Inclui campos oficiais: starter, played, status, notPlayingReason, jerseyNum
    - Calcula performance_rating + photo_url
    - Calcula blowout_impact por jogador (None se não fizer sentido pra ele)
    - Mai/2026: opcional split por quarter via player_periods (vindo do PBP)

    Args:
        p: dict cru do boxscore
        game_blowout_pct: 0–100 do jogo (já calculado em get_lineup)
        game_blowout_level: 'low'|'medium'|'high'|'final'
        player_periods: dict {period: {points, assists, ..., minutes_played,
                        intervals}} pré-computado pelo LivePbpService. None
                        ou ausente → lista vazia (front mostra "sem dados").
    """
    stats = p.get("statistics", {})
    minutes = parse_minutes_to_float(stats.get("minutes", "PT00M00.00S"))
    person_id = int(p.get("personId", 0))

    points    = int(stats.get("points", 0))
    rebounds  = int(stats.get("reboundsTotal", 0))
    assists   = int(stats.get("assists", 0))
    steals    = int(stats.get("steals", 0))
    blocks    = int(stats.get("blocks", 0))
    turnovers = int(stats.get("turnovers", 0))
    fouls     = int(stats.get("foulsPersonal", 0))
    plus_minus = int(stats.get("plusMinusPoints", 0))
    fgm = int(stats.get("fieldGoalsMade", 0))
    fga = int(stats.get("fieldGoalsAttempted", 0))
    tpm = int(stats.get("threePointersMade", 0))
    tpa = int(stats.get("threePointersAttempted", 0))
    ftm = int(stats.get("freeThrowsMade", 0))
    fta = int(stats.get("freeThrowsAttempted", 0))

    rating, label, low_conf = calculate_player_performance_rating(
        points=points,
        rebounds=rebounds,
        assists=assists,
        steals=steals,
        blocks=blocks,
        turnovers=turnovers,
        fouls=fouls,
        plus_minus=plus_minus,
        minutes=minutes,
        field_goals_made=fgm,
        field_goals_attempted=fga,
        three_pointers_made=tpm,
        free_throws_made=ftm,
        free_throws_attempted=fta,
    )

    is_starter_flag = str(p.get("starter", "0")) == "1"
    impact_dict = calculate_player_blowout_impact(
        player_minutes=minutes,
        is_starter=is_starter_flag,
        game_blowout_pct=game_blowout_pct,
        game_blowout_level=game_blowout_level,
    )
    blowout_impact_payload = (
        PlayerBlowoutImpactSchema(**impact_dict) if impact_dict else None
    )

    # Constrói lista de QuarterStats ordenada por period asc.
    # player_periods pode vir como None (sem PBP) ou dict vazio. Ambos
    # caem pra [] pra o front renderizar a seção sem erros.
    periods_list: list[QuarterStatsSchema] = []
    if player_periods:
        for period, counters in sorted(player_periods.items()):
            periods_list.append(QuarterStatsSchema(period=period, **counters))

    return LineupPlayerSchema(
        player_id=person_id,
        name=p.get("name", ""),
        jersey_num=str(p.get("jerseyNum", "")),
        position=p.get("position", "") or "",
        is_starter=is_starter_flag,
        is_on_court=str(p.get("oncourt", "0")) == "1",
        played=str(p.get("played", "0")) == "1",
        status=str(p.get("status", "ACTIVE")),
        not_playing_reason=p.get("notPlayingReason"),
        photo_url=player_photo_url(person_id),
        minutes=rounded(minutes),
        points=points,
        rebounds=rebounds,
        assists=assists,
        steals=steals,
        blocks=blocks,
        turnovers=turnovers,
        fouls=fouls,
        field_goals_made=fgm,
        field_goals_attempted=fga,
        three_pointers_made=tpm,
        three_pointers_attempted=tpa,
        free_throws_made=ftm,
        free_throws_attempted=fta,
        plus_minus=plus_minus,
        performance_rating=rating,
        performance_label=label,
        low_confidence=low_conf,
        blowout_impact=blowout_impact_payload,
        periods=periods_list,
    )


def _parse_lineup_team(
    team: dict,
    game_blowout_pct: int = 0,
    game_blowout_level: str = "low",
    per_player_periods: Optional[dict[int, dict[int, dict]]] = None,
) -> LineupTeamSchema:
    """Separa jogadores em starters, bench e inactive.

    per_player_periods: {player_id: {period: counters}} pré-computado pelo
    LivePbpService. None → nenhum split por quarter (lista vazia).
    """
    raw_players = team.get("players", [])
    parsed = [
        _parse_lineup_player(
            p, game_blowout_pct, game_blowout_level,
            player_periods=(per_player_periods or {}).get(
                int(p.get("personId", 0))
            ),
        )
        for p in raw_players
    ]

    starters: list[LineupPlayerSchema] = []
    bench:    list[LineupPlayerSchema] = []
    inactive: list[LineupPlayerSchema] = []

    for p in parsed:
        if p.status != "ACTIVE":
            inactive.append(p)
        elif p.is_starter:
            starters.append(p)
        else:
            bench.append(p)

    # Ordenação: starters mantêm ordem da NBA (1..5);
    # bench ordena por minutos jogados (quem mais joga primeiro);
    # inactive por nome.
    bench.sort(key=lambda x: (-x.minutes, x.name))
    inactive.sort(key=lambda x: x.name)

    if len(starters) != 5:
        logger.warning(
            "Time %s tem %d titulares (esperado 5) — boxscore pode estar atrasado",
            team.get("teamTricode", "?"), len(starters),
        )

    city = team.get("teamCity", "")
    name = team.get("teamName", "")
    return LineupTeamSchema(
        team_id=int(team.get("teamId", 0)),
        name=f"{city} {name}".strip(),
        tricode=team.get("teamTricode", ""),
        score=int(team.get("score", 0)),
        starters=starters,
        bench=bench,
        inactive=inactive,
    )


def _parse_team_boxscore(team: dict) -> LiveTeamBoxscoreSchema:
    players = [
        parsed
        for p in team.get("players", [])
        if (parsed := _parse_player(p)) is not None
    ]
    city = team.get("teamCity", "")
    name = team.get("teamName", "")
    return LiveTeamBoxscoreSchema(
        team_id=int(team.get("teamId", 0)),
        name=f"{city} {name}".strip(),
        tricode=team.get("teamTricode", ""),
        score=int(team.get("score", 0)),
        players=players,
    )


class LiveGameService:
    def __init__(self) -> None:
        self._cache = SimpleCache()
        # Fork WNBA: box score ao vivo vem da ESPN (cdn.nba.com é NBA-only).
        from src.league import DATA_SOURCE
        self._espn = None
        if DATA_SOURCE == "espn":
            from src.services.espn_wnba import EspnWnbaSource
            self._espn = EspnWnbaSource()

    def fetch_scoreboard(self) -> TodayGamesSchema:
        """
        Fetch live scoreboard directly from the NBA API.
        Called by the background worker — no caching here.

        Quando USE_FIXTURES=1, lê de tests/fixtures/scoreboard_today.json
        em vez de bater na NBA. Útil pra dev offline / dia sem jogos.
        """
        if USE_FIXTURES:
            logger.info("USE_FIXTURES: lendo scoreboard de fixture")
            fixture = _load_fixture("scoreboard_today.json")
            if fixture is not None:
                data = fixture["scoreboard"]
            else:
                raise RuntimeError("Fixture scoreboard_today.json não encontrada")
        else:
            logger.info("Fetching live scoreboard from NBA API.")
            try:
                board = scoreboard.ScoreBoard()
                data = board.get_dict()["scoreboard"]
            except Exception as exc:
                raise RuntimeError(f"Erro ao buscar scoreboard: {exc}") from exc

        games: list[LiveGameSchema] = []
        for g in data.get("games", []):
            home = g.get("homeTeam", {})
            away = g.get("awayTeam", {})
            game_id = g.get("gameId", "")
            game_status = map_game_status(g.get("gameStatus", 0))
            period = int(g.get("period", 0))
            clock = format_game_clock(g.get("gameClock", ""))
            home_score = int(home.get("score", 0))
            away_score = int(away.get("score", 0))

            # Calcula risco de blowout pra cada game tile do scoreboard.
            # Permite o front mostrar badge no card sem precisar buscar
            # o ranking do jogo. Detecta playoff via game_id ('0042...').
            # Pra jogos não iniciados, deixa None — sem placar não há
            # como calcular, e badge não faz sentido.
            blowout_risk = None
            if game_status != "not_started":
                is_playoff = len(game_id) >= 4 and game_id[2:4] == "04"
                pct, level, reason = calculate_blowout_risk(
                    period=period,
                    clock=clock,
                    home_score=home_score,
                    away_score=away_score,
                    game_status=game_status,
                    is_playoff=is_playoff,
                )
                blowout_risk = BlowoutRiskSchema(
                    percentage=pct, level=level, reason=reason,
                )

            games.append(
                LiveGameSchema(
                    game_id=game_id,
                    game_status=game_status,
                    period=period,
                    clock=clock,
                    # gameTimeUTC vem como "2026-05-04T23:00:00Z" no scoreboard
                    # da NBA Live API. Mantemos como string crua — o front
                    # formata pro timezone do usuário com Intl.DateTimeFormat.
                    game_time_utc=g.get("gameTimeUTC") or None,
                    home_team=LiveTeamSchema(
                        team_id=int(home.get("teamId", 0)),
                        name=f"{home.get('teamCity', '')} {home.get('teamName', '')}".strip(),
                        tricode=home.get("teamTricode", ""),
                        score=home_score,
                    ),
                    away_team=LiveTeamSchema(
                        team_id=int(away.get("teamId", 0)),
                        name=f"{away.get('teamCity', '')} {away.get('teamName', '')}".strip(),
                        tricode=away.get("teamTricode", ""),
                        score=away_score,
                    ),
                    blowout_risk=blowout_risk,
                )
            )

        all_final = bool(games) and all(g.game_status == "final" for g in games)
        result = TodayGamesSchema(
            date=data.get("gameDate", ""),
            games=games,
            source="live",
            all_final=all_final,
        )
        logger.info("Scoreboard fetched: %d games.", len(games))
        return result

    # ─────────────────────────────────────────────────────────────────
    # ScoreboardV2 (stats.nba.com) — usado pra consultar uma data
    # específica quando o live ainda mostra "ontem".
    # ─────────────────────────────────────────────────────────────────
    def fetch_scoreboard_for_date(self, date_iso: str) -> TodayGamesSchema:
        """
        Busca o scoreboard de uma data específica (YYYY-MM-DD) usando o
        endpoint `scoreboardv2` do stats.nba.com.

        Por que existe: a NBA Live API (cdn.nba.com) não aceita data e só
        rola o "game day" por volta do meio-dia ET. Em fusos a oeste (BRT
        está 3h à frente de ET) o usuário abre o app de madrugada/manhã
        e ainda vê os jogos de ontem mesmo quando os de hoje já estão
        agendados. Esse método permite forçar a data correta.

        Diferente do live, aqui NÃO temos `gameTimeUTC` (o stats endpoint
        não expõe). game_time_utc fica None — front cai no fallback
        "Aguardando início" sem mostrar o horário previsto.

        Cache: 5 min, chaveado por data.
        """
        if USE_FIXTURES:
            # Em modo fixture não faz sentido bater no stats — devolve
            # o scoreboard padrão pra manter os testes determinísticos.
            return self.fetch_scoreboard()

        logger.info("Fetching ScoreboardV2 for date %s", date_iso)
        try:
            # Timeout agressivo (8s) — se stats.nba.com estiver lento,
            # preferimos falhar e ficar com o live do que travar o worker.
            sb = scoreboardv2.ScoreboardV2(
                game_date=date_iso,
                timeout=SCOREBOARD_V2_TIMEOUT,
            )
            payload = sb.get_dict()
        except Exception as exc:
            # Não estoura — fallback acaba caindo no live de novo lá em cima.
            raise RuntimeError(f"Erro ao buscar scoreboardv2 ({date_iso}): {exc}") from exc

        result_sets = {rs["name"]: rs for rs in payload.get("resultSets", [])}
        game_header = result_sets.get("GameHeader", {})
        line_score  = result_sets.get("LineScore", {})

        gh_headers = game_header.get("headers", [])
        gh_rows    = game_header.get("rowSet", [])
        ls_headers = line_score.get("headers", [])
        ls_rows    = line_score.get("rowSet", [])

        def _to_dict(headers: list[str], row: list) -> dict:
            return dict(zip(headers, row, strict=False))

        # Index LineScore por (game_id, team_id) pra montar home/away.
        line_by_key: dict[tuple[str, int], dict] = {}
        for raw in ls_rows:
            ls = _to_dict(ls_headers, raw)
            key = (str(ls.get("GAME_ID", "")), int(ls.get("TEAM_ID", 0)))
            line_by_key[key] = ls

        games: list[LiveGameSchema] = []
        for raw in gh_rows:
            gh = _to_dict(gh_headers, raw)
            game_id = str(gh.get("GAME_ID", ""))
            status = map_game_status(int(gh.get("GAME_STATUS_ID", 0)))
            period = int(gh.get("LIVE_PERIOD", 0) or 0)
            # LIVE_PC_TIME já vem como "MM:SS" ou "" — não precisa parse ISO.
            clock = str(gh.get("LIVE_PC_TIME", "") or "").strip()

            home_id = int(gh.get("HOME_TEAM_ID", 0) or 0)
            away_id = int(gh.get("VISITOR_TEAM_ID", 0) or 0)
            home_ls = line_by_key.get((game_id, home_id), {})
            away_ls = line_by_key.get((game_id, away_id), {})

            home_score = int(home_ls.get("PTS") or 0)
            away_score = int(away_ls.get("PTS") or 0)

            blowout_risk = None
            if status != "not_started":
                is_playoff = len(game_id) >= 4 and game_id[2:4] == "04"
                pct, level, reason = calculate_blowout_risk(
                    period=period,
                    clock=clock,
                    home_score=home_score,
                    away_score=away_score,
                    game_status=status,
                    is_playoff=is_playoff,
                )
                blowout_risk = BlowoutRiskSchema(
                    percentage=pct, level=level, reason=reason,
                )

            def _team(ls: dict, team_id: int) -> LiveTeamSchema:
                city = ls.get("TEAM_CITY_NAME", "") or ""
                name = ls.get("TEAM_NAME", "") or ""
                return LiveTeamSchema(
                    team_id=team_id,
                    name=f"{city} {name}".strip(),
                    tricode=ls.get("TEAM_ABBREVIATION", "") or "",
                    score=int(ls.get("PTS") or 0),
                )

            games.append(
                LiveGameSchema(
                    game_id=game_id,
                    game_status=status,
                    period=period,
                    clock=clock,
                    # ScoreboardV2 não expõe gameTimeUTC — front mostra
                    # só o status sem horário previsto.
                    game_time_utc=None,
                    home_team=_team(home_ls, home_id),
                    away_team=_team(away_ls, away_id),
                    blowout_risk=blowout_risk,
                )
            )

        all_final = bool(games) and all(g.game_status == "final" for g in games)
        return TodayGamesSchema(
            date=date_iso,
            games=games,
            source="scheduled",
            all_final=all_final,
        )

    def fetch_scoreboard_smart(self) -> TodayGamesSchema:
        """
        fetch_scoreboard com fallback inteligente — sem cache interno.

        Usado pelo background worker (que tem o próprio cache). Lógica:
          1. Bate na NBA Live API.
          2. Se `date == today_ET` e tem jogo não-final → retorna live.
          3. Senão tenta ScoreboardV2 com data ET de hoje. Se devolver
             jogos não-final → substitui.
          4. Se ScoreboardV2 falhar/vazio → mantém o live (degradação suave).
          5. Se BOTH live + ScoreboardV2 falharem → re-raise pra worker
             logar (preservar snapshot anterior se houver).

        Cache do ScoreboardV2 (5min) é interno aqui — evita martelar
        stats.nba.com a cada poll do worker (a cada 2s). Se já tem cache,
        nem chega a fazer a request.
        """
        today_et = today_in_et()
        by_date_key = f"scoreboard_by_date:{today_et}"

        # Tentativa do live (rápido, ~1s). Se ele falhar, ainda podemos
        # tentar o ScoreboardV2 — não retornamos erro imediatamente.
        live_result: TodayGamesSchema | None = None
        live_err: Exception | None = None
        try:
            live_result = self.fetch_scoreboard()
        except RuntimeError as exc:
            live_err = exc
            logger.warning("Live scoreboard falhou: %s — vou tentar ScoreboardV2", exc)

        # Caminho feliz: live já é o dia correto e tem jogo rolando.
        # Skip ScoreboardV2 totalmente (não acessa stats.nba.com).
        if live_result and live_result.date == today_et and not live_result.all_final:
            return live_result

        # Cache do fallback (TTL longo — a lista de jogos de uma data
        # passada é estável; futura ainda muda pouco ao longo do dia).
        scheduled: TodayGamesSchema | None = self._cache.get(by_date_key)
        if not scheduled:
            try:
                scheduled = self.fetch_scoreboard_for_date(today_et)
                self._cache.set(by_date_key, scheduled, SCOREBOARD_BY_DATE_TTL)
            except RuntimeError as exc:
                logger.warning("Fallback ScoreboardV2 falhou: %s", exc)
                scheduled = None

        # Se o scheduled veio TODO finalizado e já temos live, não há o
        # que "resgatar" (nenhum jogo começou/em curso pra sobrepor) —
        # mantém o live (preserva date/source 'live' e o badge "Todos
        # finalizados"). Evita virar "Próximos jogos" enganoso.
        if (
            scheduled
            and scheduled.games
            and scheduled.all_final
            and live_result is not None
            and live_result.games
        ):
            return live_result

        # MESCLA (não substitui): scheduled define o slate da data ET;
        # live sobrepõe estados mais avançados. Assim um jogo que acabou
        # nunca volta pra "não iniciado" 0-0.
        if scheduled and scheduled.games:
            if live_result is not None:
                logger.info(
                    "Mesclando live (%s) + scheduled (%s, %d jogos)",
                    live_result.date, scheduled.date, len(scheduled.games),
                )
                return _merge_scoreboards(live_result, scheduled)
            return scheduled

        if live_result is not None:
            return live_result

        # Ambos falharam — propaga o erro do live (mais crítico). Worker
        # preserva o último snapshot se houver, ou loga e tenta de novo
        # no próximo tick (2s).
        raise live_err or RuntimeError("Live + ScoreboardV2 indisponíveis")

    def get_today_games(self) -> TodayGamesSchema:
        """
        Wrapper com cache local (30s) em volta do fetch_scoreboard_smart.

        Endpoint principal (`/games/live/today`) NÃO usa este método —
        lê direto do `live_cache` populado pelo worker. Este wrapper é
        usado por rotinas de análise/serviço interno que chamam o
        scoreboard fora do fluxo do worker.
        """
        cached = self._cache.get("scoreboard")
        if cached:
            logger.debug("Scoreboard served from cache")
            return cached
        result = self.fetch_scoreboard_smart()
        self._cache.set("scoreboard", result, SCOREBOARD_TTL)
        return result

    def get_live_boxscore(self, game_id: str) -> LiveBoxscoreSchema:
        cache_key = f"boxscore:{game_id}"
        cached = self._cache.get(cache_key)
        if cached:
            logger.debug("Boxscore %s served from cache", game_id)
            return cached

        # Fork WNBA: box score da ESPN (mesmo schema), cacheado igual.
        if self._espn is not None:
            result = self._espn.get_live_boxscore(game_id)
            self._cache.set(cache_key, result, BOXSCORE_TTL)
            return result

        logger.info("Fetching live boxscore for game %s", game_id)
        try:
            bs = boxscore.BoxScore(game_id=game_id)
            game_data = bs.get_dict()["game"]
        except Exception as exc:
            raise RuntimeError(f"Erro ao buscar boxscore para {game_id}: {exc}") from exc

        result = LiveBoxscoreSchema(
            game_id=game_id,
            game_status=map_game_status(game_data.get("gameStatus", 0)),
            period=int(game_data.get("period", 0)),
            clock=format_game_clock(game_data.get("gameClock", "")),
            home_team=_parse_team_boxscore(game_data.get("homeTeam", {})),
            away_team=_parse_team_boxscore(game_data.get("awayTeam", {})),
        )
        self._cache.set(cache_key, result, BOXSCORE_TTL)
        return result

    def _fetch_raw_game_data(self, game_id: str) -> dict:
        """
        Cache compartilhado do JSON cru do boxscore (TTL curto).
        get_live_boxscore e get_lineup consomem o mesmo dado, evitando
        2 requests à NBA Live API a cada poll.

        Em modo USE_FIXTURES: tenta `boxscore_<game_id>.json`, depois cai
        no `boxscore_blowout_final.json` como fallback. Permite testar
        com jogo de blowout decidido (Celtics x Mavs G5) quando não há
        jogo ao vivo.
        """
        cache_key = f"raw_boxscore:{game_id}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        if USE_FIXTURES:
            fixture = (
                _load_fixture(f"boxscore_{game_id}.json")
                or _load_fixture("boxscore_blowout_final.json")
            )
            if fixture is None:
                raise RuntimeError("Nenhuma fixture de boxscore encontrada")
            game_data = fixture["game"]
        else:
            try:
                bs = boxscore.BoxScore(game_id=game_id)
                game_data = bs.get_dict()["game"]
            except Exception as exc:
                raise RuntimeError(f"Erro ao buscar boxscore para {game_id}: {exc}") from exc

        self._cache.set(cache_key, game_data, BOXSCORE_TTL)
        return game_data

    def get_lineup(
        self,
        game_id: str,
        pbp_service: Optional[object] = None,
    ) -> LineupGameSchema:
        """
        Lineups completas (titulares + reservas + inativos) com foto e
        nota de desempenho. Tudo direto da NBA Live API — sem inferência.

        Mai/2026: se `pbp_service` for passado (com .get_per_period_stats_*),
        adiciona split por quarter (mesmo formato do Hot Picks live).
        Falhas no PBP são silenciosas — campo `periods` vira lista vazia.
        """
        cache_key = f"lineup:{game_id}"
        cached = self._cache.get(cache_key)
        if cached:
            logger.debug("Lineup %s served from cache", game_id)
            return cached

        logger.info("Building lineup for game %s", game_id)
        game_data = self._fetch_raw_game_data(game_id)

        game_status = map_game_status(game_data.get("gameStatus", 0))
        period = int(game_data.get("period", 0))
        clock = format_game_clock(game_data.get("gameClock", ""))

        # Calcula blowout do JOGO primeiro (precisa só dos placares).
        # Os parsers de team/player recebem esse contexto pra decidir
        # individualmente quem deveria ganhar a flag de "Risco de descanso".
        home_score = int(game_data.get("homeTeam", {}).get("score", 0))
        away_score = int(game_data.get("awayTeam", {}).get("score", 0))
        is_playoff = len(game_id) >= 4 and game_id[2:4] == "04"
        bo_pct, bo_level, bo_reason = calculate_blowout_risk(
            period=period,
            clock=clock,
            home_score=home_score,
            away_score=away_score,
            game_status=game_status,
            is_playoff=is_playoff,
        )

        # Busca PBP per-period (silencioso em falha). Coleta q1_starters
        # de ambos os times pra alimentar o state machine de minutos jogados.
        per_player_periods: dict[int, dict[int, dict]] = {}
        if pbp_service is not None and game_status != "not_started":
            try:
                q1_starters: set[int] = set()
                for team_key in ("homeTeam", "awayTeam"):
                    for p in game_data.get(team_key, {}).get("players", []):
                        if str(p.get("starter", "0")) == "1":
                            q1_starters.add(int(p.get("personId", 0)))
                clock_min = None
                if clock and clock != "":
                    try:
                        mm, ss = clock.split(":")
                        clock_min = float(mm) + float(ss) / 60.0
                    except (ValueError, AttributeError):
                        clock_min = None
                per_player_periods = pbp_service.get_per_period_stats_with_court_time(
                    game_id,
                    q1_starters,
                    live_period=period if game_status == "in_progress" else None,
                    live_clock_minutes=clock_min if game_status == "in_progress" else None,
                )
            except Exception as exc:
                logger.warning("PBP per-period falhou pra lineup %s: %s", game_id, exc)
                per_player_periods = {}

        home = _parse_lineup_team(
            game_data.get("homeTeam", {}), bo_pct, bo_level,
            per_player_periods=per_player_periods,
        )
        away = _parse_lineup_team(
            game_data.get("awayTeam", {}), bo_pct, bo_level,
            per_player_periods=per_player_periods,
        )

        result = LineupGameSchema(
            game_id=game_id,
            game_status=game_status,
            period=period,
            clock=clock,
            home_team=home,
            away_team=away,
            blowout_risk=BlowoutRiskSchema(
                percentage=bo_pct, level=bo_level, reason=bo_reason
            ),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._cache.set(cache_key, result, BOXSCORE_TTL)
        return result

    def get_game_rotations(
        self,
        game_id: str,
        rotation_provider: object,
    ) -> "GameRotationsSchema":
        """
        Padrão histórico de rotação de cada jogador do jogo (nbarotations).

        Heatmap dos 48 minutos (prob. de estar em quadra) + janelas de
        descanso típicas + flag "quase voltando" pro jogador que está no
        banco mas costuma voltar nos próximos minutos.

        rotation_provider: instância com .get_profile(player_id,
        season_minutes). Cacheado 7d no provider — custo amortizado.
        """
        from src.schemas.live_schemas import (
            GameRotationsSchema,
            PlayerRotationSchema,
        )
        from src.services.rotation.rotation_context import build_context

        cache_key = f"rotations:{game_id}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        lineup = self.get_lineup(game_id)
        period = lineup.period
        clock = lineup.clock
        is_live = lineup.game_status == "in_progress"

        clock_min = 12.0
        if clock and ":" in clock:
            try:
                mm, ss = clock.split(":")
                clock_min = float(mm) + float(ss) / 60.0
            except (ValueError, AttributeError):
                clock_min = 12.0

        home_score = lineup.home_team.score
        away_score = lineup.away_team.score

        def _build_player(p: LineupPlayerSchema, team_is_home: bool):
            # season_minutes só importa pro fallback; perfil real ignora.
            # Lineup não carrega season avg — usa minutos do jogo como
            # proxy razoável (ou 24 default neutro).
            season_min_proxy = p.minutes if p.minutes > 0 else 24.0
            try:
                profile = rotation_provider.get_profile(
                    player_id=p.player_id,
                    season_minutes=season_min_proxy,
                )
            except Exception as exc:
                logger.warning(
                    "Rotation profile falhou pra %s: %s", p.player_id, exc
                )
                profile = None

            probs = list(getattr(profile, "minute_probabilities", []) or [])
            has_profile = bool(
                profile is not None
                and not getattr(profile, "is_fallback", True)
                and probs
            )

            score_diff = (
                (home_score - away_score) if team_is_home
                else (away_score - home_score)
            )
            status = "UNKNOWN"
            exp_remaining = 0.0
            notes: list[str] = []
            if profile is not None and is_live:
                try:
                    exp_remaining = rotation_provider.expected_minutes_remaining(
                        profile,
                        period=period,
                        clock_minutes_remaining=clock_min,
                        minutes_already_played=p.minutes,
                        blowout_severity=0.0,
                    )
                    ctx = build_context(
                        profile=profile,
                        expected_remaining_minutes=exp_remaining,
                        period=period,
                        clock_minutes_remaining=clock_min,
                        is_player_on_court=p.is_on_court,
                        score_difference=score_diff,
                        is_close_game=abs(score_diff) <= 8,
                        minutes_played=p.minutes,
                    )
                    status = ctx.current_rotation_status
                    exp_remaining = ctx.expected_remaining_minutes
                    notes = ctx.notes
                except Exception as exc:
                    logger.warning(
                        "build_context falhou pra %s: %s", p.player_id, exc
                    )

            rest_windows = _derive_rest_windows(probs)
            about, return_in = _about_to_return(
                probs, period, clock_min, p.is_on_court, is_live,
            )

            return PlayerRotationSchema(
                player_id=p.player_id,
                name=p.name,
                jersey_num=p.jersey_num,
                position=p.position,
                photo_url=p.photo_url,
                is_starter=p.is_starter,
                is_on_court=p.is_on_court,
                minutes_played=p.minutes,
                has_profile=has_profile,
                sample_games=int(getattr(profile, "sample_games", 0) or 0),
                avg_minutes=round(
                    float(getattr(profile, "total_minutes", 0.0) or 0.0), 1
                ),
                minute_probabilities=[round(x, 3) for x in probs],
                rest_windows=rest_windows,
                current_rotation_status=status,
                expected_remaining_minutes=round(exp_remaining, 1),
                about_to_return=about,
                return_in_minutes=return_in,
                notes=notes,
            )

        def _team_players(team: LineupTeamSchema, is_home: bool):
            # Starters + bench (quem joga ou pode jogar). Inativos fora —
            # não têm padrão de rotação relevante hoje.
            out = []
            for p in [*team.starters, *team.bench]:
                out.append(_build_player(p, is_home))
            # Ordena: em quadra primeiro, depois titulares, depois resto
            out.sort(
                key=lambda r: (
                    not r.is_on_court,
                    not r.is_starter,
                    -r.avg_minutes,
                )
            )
            return out

        result = GameRotationsSchema(
            game_id=game_id,
            game_status=lineup.game_status,
            period=period,
            clock=clock,
            home_team_tricode=lineup.home_team.tricode,
            away_team_tricode=lineup.away_team.tricode,
            home_players=_team_players(lineup.home_team, True),
            away_players=_team_players(lineup.away_team, False),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        # TTL curto — perfis são cacheados 7d no provider, mas o estado
        # ao vivo (status/about_to_return) muda. 15s é suficiente.
        self._cache.set(cache_key, result, 15)
        return result


# ─── Helpers de rotação (mai/2026) ──────────────────────────────────────────


def _derive_rest_windows(
    probs: list[float],
    rest_threshold: float = 0.45,
    min_len: int = 2,
) -> list[list[int]]:
    """
    Extrai janelas de descanso típicas do histograma: sequências
    contíguas de minutos com prob ≤ rest_threshold. Ignora janelas
    curtas (< min_len) — ruído de amostra. Retorna [[start, end], ...].
    """
    if not probs:
        return []
    windows: list[list[int]] = []
    start: Optional[int] = None
    for i, prob in enumerate(probs):
        if prob <= rest_threshold:
            if start is None:
                start = i
        else:
            if start is not None:
                if i - start >= min_len:
                    windows.append([start, i - 1])
                start = None
    if start is not None and len(probs) - start >= min_len:
        windows.append([start, len(probs) - 1])
    return windows


def _about_to_return(
    probs: list[float],
    period: int,
    clock_minutes_remaining: float,
    is_on_court: bool,
    is_live: bool,
    play_threshold: float = 0.55,
    window_minutes: int = 3,
) -> tuple[bool, Optional[float]]:
    """
    "Quase voltando": jogador NO BANCO agora, mas o histograma diz que
    costuma estar em quadra dentro dos próximos `window_minutes`.

    Retorna (about_to_return, minutos_até_retorno). minutos None quando
    não se aplica (em quadra / jogo parado / sem dado / sem retorno perto).
    """
    if not is_live or is_on_court or not probs or period > 4:
        return (False, None)
    elapsed = max(0.0, 12.0 - clock_minutes_remaining)
    cur_idx = int(round((period - 1) * 12 + elapsed))
    if cur_idx < 0 or cur_idx >= len(probs):
        return (False, None)
    for i in range(cur_idx + 1, min(cur_idx + 1 + window_minutes, len(probs))):
        if probs[i] >= play_threshold:
            return (True, float(i - cur_idx))
    return (False, None)
