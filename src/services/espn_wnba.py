"""
EspnWnbaSource — fonte de dados WNBA via API pública da ESPN.

Motivo de existir: o `stats.nba.com`/`stats.wnba.com` é bloqueado em IPs
sem proxy (cloud E vários IPs residenciais). A API da ESPN responde 200
direto, então é a fonte ideal para TESTAR A WNBA 100% LOCAL, sem proxy.

Cobre o suficiente pra alimentar a camada de stats/histórico (busca de
jogadoras + gamelog → análise de temporada + stats por jogo). Retorna os
MESMOS schemas que o NbaService (PlayerSchema, GameLogSchema), então o
resto do sistema não percebe a diferença.

Endpoints usados (todos públicos, sem chave):
  - /teams                      → lista de times (ids)
  - /teams/{id}/roster          → atletas (id, nome, posição)
  - /athletes/{id}/gamelog      → jogo a jogo (MIN/PTS/REB/AST/FG/3PT/FT)

NÃO cobre (fica no nba_api/proxy ou pendente): play-by-play por período,
hot-board/team-board (league dash), feed ao vivo.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import requests

from src.config import STATS_PROXY
from src.schemas.nba_schemas import GameLogSchema, PlayerSchema
from src.utils.cache import PersistentCache, SimpleCache
from src.utils.converters import normalize_player_name

logger = logging.getLogger(__name__)

_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
_WEB = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba"
_TIMEOUT = 15

# A ESPN devolve 404 pro User-Agent default do python-requests. Com um UA
# de browser, responde normal (200) — mesma fonte que o curl alcança.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Proxy — reutiliza STATS_PROXY (mesmo residencial usado pra stats.nba.com).
# Motivo: a ESPN (Akamai) bloqueia IPs de cloud/data-center → 403 Access
# Denied em cima de tudo (scoreboard, boxscore, roster, gamelog). Local
# funciona; Railway não. Com proxy residencial (ScraperAPI/Webshare) passa.
# Sem STATS_PROXY setado, mantém o comportamento antigo (chamada direta).
_PROXIES: Optional[dict] = (
    {"http": STATS_PROXY, "https": STATS_PROXY} if STATS_PROXY else None
)
# ScraperAPI termina TLS no proxy → precisa skip verify. Outros proxies
# (Webshare, residencial CONNECT-tunnel) mantêm verify=True normal.
_VERIFY_SSL: bool = not (STATS_PROXY and "scraperapi" in STATS_PROXY.lower())


# ── Circuit breaker do ScraperAPI ────────────────────────────────────────────
# Quando o ScraperAPI responde 403 "You have exhausted the API Credits",
# cada request seguinte gasta 3-5s esperando por outro 403 previsível — puro
# desperdício de CPU/latência. O breaker abre um "fusível" por N segundos:
# durante esse tempo, `_get()` levanta imediatamente (sem hit no proxy),
# forçando os consumidores a caírem no cache stale (ver `_last_good`).
# Reset automático quando o TTL passa; primeira request depois testa se
# a quota voltou.
_QUOTA_OPEN_UNTIL: float = 0.0   # epoch seconds; 0 = fechado (chamadas passam)
_QUOTA_COOLDOWN = 15 * 60        # 15 min de "silêncio" após detectar exhausted
_QUOTA_MARKERS = ("exhausted", "api credits available", "upgrade your subscription")


class _QuotaExhaustedError(RuntimeError):
    """
    Levantada por `_get()` quando o breaker está aberto ou quando o response
    do ScraperAPI indica quota zerada. Consumidores devem tratar como uma
    falha esperada (não um bug) e servir cache stale ou vazio gracefully.
    """


def _quota_open() -> bool:
    return _QUOTA_OPEN_UNTIL > time.time()


def _mark_quota_exhausted(reason: str) -> None:
    global _QUOTA_OPEN_UNTIL
    _QUOTA_OPEN_UNTIL = time.time() + _QUOTA_COOLDOWN
    logger.warning(
        "ScraperAPI quota exhausted — circuit breaker aberto por %ds (%s)",
        _QUOTA_COOLDOWN, reason,
    )


def _looks_exhausted(body_preview: str) -> bool:
    low = (body_preview or "").lower()
    return any(m in low for m in _QUOTA_MARKERS)


def _get(url: str, params: Optional[dict] = None, timeout: int = _TIMEOUT):
    """
    requests.get wrapper com proxy + headers ESPN padrão + circuit breaker.
    Usar SEMPRE ao invés de requests.get direto neste módulo — assim
    o proxy fica em UM lugar só, o log de "sem proxy" fica visível no
    startup, e o breaker do ScraperAPI pega tudo.

    Levanta `_QuotaExhaustedError` quando o breaker está aberto OU quando
    o response indica quota zerada. Consumidores fazem fallback pro
    `_last_good_*` do cache.
    """
    if _quota_open():
        raise _QuotaExhaustedError("circuit breaker aberto (aguardando cooldown)")

    r = requests.get(
        url,
        params=params,
        headers=_HEADERS,
        timeout=timeout,
        proxies=_PROXIES,
        verify=_VERIFY_SSL,
    )
    # Detecta resposta do PRÓPRIO ScraperAPI (não do destino) dizendo que
    # a quota acabou. Nesses casos o body é HTML/text, não JSON válido.
    if r.status_code in (401, 403) and _looks_exhausted(r.text[:500]):
        _mark_quota_exhausted(f"HTTP {r.status_code} em {url[:80]}")
        raise _QuotaExhaustedError(f"quota do proxy zerada ({r.status_code})")
    return r


if _PROXIES:
    logger.info(
        "EspnWnbaSource: roteando via STATS_PROXY (verify_ssl=%s)",
        _VERIFY_SSL,
    )
else:
    logger.warning(
        "EspnWnbaSource: SEM proxy — ESPN bloqueia IPs de cloud/DC. "
        "Se rodando em prod (Railway/etc), setar STATS_PROXY.",
    )


ROSTER_TTL = 24 * 3600      # lista de jogadoras muda raramente
GAMELOG_TTL = 2 * 3600      # 2h — temporada corrente atualiza pós-jogo, 2h é OK
# Caches curtos in-memory pra endpoints "quentes" — SEM ELES, cada request
# do front vira 1 request ESPN via ScraperAPI (queima credit rápido). Com
# esses TTLs, N users pollando ficam servidos do mesmo hit ESPN.
SCOREBOARD_TTL = 30         # 30s — placar/status muda em segundos, 30s é OK
BOXSCORE_TTL = 5            # 5s — clock/placar precisam ser fresh no live
TEAM_BOARD_TTL = 6 * 3600   # 6h — médias de time mudam lentamente + endpoint
                            #      faz 14 hits ESPN (1 por time), muito caro


def _year_from_season(season: str) -> Optional[int]:
    """Extrai o ano de uma season ('2025' ou '2024-25' → 2025 / 2024)."""
    m = re.match(r"(\d{4})", str(season or ""))
    return int(m.group(1)) if m else None


def _split_made_att(value: str) -> tuple[int, int]:
    """'7-15' → (7, 15). Robusto a lixo ('--', '', None) → (0, 0)."""
    try:
        made, att = str(value).split("-")
        return int(made), int(att)
    except (ValueError, AttributeError):
        return 0, 0


def _to_int(value) -> int:
    try:
        return int(round(float(value)))
    except (ValueError, TypeError):
        return 0


def _to_float(value) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _parse_pm(value) -> int:
    """'+16' / '-3' / '0' → int."""
    try:
        return int(str(value).replace("+", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


class EspnWnbaSource:
    """Fonte WNBA via ESPN. API compatível com o subset usado do NbaService."""

    def __init__(self, cache_dir: Optional[str] = None) -> None:
        cdir = cache_dir or os.getenv("CACHE_DIR", "/tmp")
        self._roster_cache = PersistentCache(
            path=os.path.join(cdir, "espn_wnba_roster_cache.json")
        )
        self._gamelog_cache = PersistentCache(
            path=os.path.join(cdir, "espn_wnba_gamelog_cache.json")
        )
        # Caches in-memory pros endpoints "quentes" chamados pelo front.
        # Não persistir: TTL curto e ScraperAPI cobra por chamada nova.
        self._live_cache = SimpleCache()
        # Último response BOM por chave — sem TTL, sobrevive ao expirar do
        # cache normal e ao circuit breaker do ScraperAPI. Servido como
        # fallback quando a chamada nova falha (quota exausta, timeout, 403).
        # Trade-off consciente: prefiro dado ligeiramente antigo a UI vazia.
        self._last_good: dict[str, object] = {}

    # ── Times / rosters ──────────────────────────────────────────────────────
    def _teams(self) -> list[dict]:
        """Lista de times: [{id, abbr, name}]. Cacheado 24h."""
        cached = self._roster_cache.get("teams")
        if cached is not None:
            return cached
        out: list[dict] = []
        try:
            r = _get(f"{_SITE}/teams")
            r.raise_for_status()
            for t in r.json()["sports"][0]["leagues"][0]["teams"]:
                tm = t["team"]
                out.append({
                    "id": str(tm["id"]),
                    "abbr": tm.get("abbreviation", ""),
                    "name": tm.get("displayName", ""),
                })
        except Exception as exc:
            logger.warning("ESPN /teams falhou: %s", exc)
        if out:
            self._roster_cache.set("teams", out, ROSTER_TTL)
        return out

    def _all_players(self) -> list[dict]:
        """
        Índice completo de jogadoras (todos os rosters). Cacheado 24h.
        Cada item: {id, full_name, first_name, last_name, is_active, team}.
        """
        cached = self._roster_cache.get("all_players")
        if cached is not None:
            return cached

        out: list[dict] = []
        for tm in self._teams():
            tid = tm["id"]
            try:
                r = _get(f"{_SITE}/teams/{tid}/roster")
                r.raise_for_status()
                for a in r.json().get("athletes", []):
                    pid = a.get("id")
                    full = (a.get("fullName") or "").strip()
                    if not pid or not full:
                        continue
                    out.append({
                        "id": int(pid),
                        "full_name": full,
                        "first_name": a.get("firstName") or full.split(" ")[0],
                        "last_name": a.get("lastName") or " ".join(full.split(" ")[1:]),
                        "is_active": True,
                        "team": tm["abbr"],
                    })
            except Exception as exc:
                logger.info("ESPN roster team %s falhou: %s", tid, exc)
                continue

        if out:
            self._roster_cache.set("all_players", out, ROSTER_TTL)
        return out

    def _player_index(self) -> dict[int, dict]:
        """{player_id: {full_name, team}} pra lookup rápido."""
        return {p["id"]: p for p in self._all_players()}

    def search_players(self, name: str) -> list[PlayerSchema]:
        query = normalize_player_name(name)
        matches = [
            p for p in self._all_players()
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

    # ── Scoreboard (jogos do dia) ────────────────────────────────────────────
    def get_today_games(self):
        """
        Jogos do dia via ESPN scoreboard → LiveGamesCachedResponseSchema.
        Cacheado in-memory por SCOREBOARD_TTL (30s) pra não queimar
        credit do ScraperAPI a cada request do front.
        """
        cached = self._live_cache.get("scoreboard")
        if cached is not None:
            return cached

        from datetime import datetime, timezone
        from src.schemas.live_schemas import (
            LiveGameSchema,
            LiveGamesCachedResponseSchema,
            LiveTeamSchema,
        )

        # pre → not_started · in → in_progress · post → final
        # (vocabulário canônico do sistema — bate com GAME_STATUS_MAP e com
        # o front, que só reconhece "in_progress" como ao vivo.)
        _STATE = {"pre": "not_started", "in": "in_progress", "post": "final"}

        games: list = []
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fetch_failed = False
        try:
            r = _get(f"{_SITE}/scoreboard")
            r.raise_for_status()
            data = r.json()
        except _QuotaExhaustedError as exc:
            # ScraperAPI sem credit — sinal esperado, não erro real. Cai
            # direto no fallback do last_good abaixo sem log verbose.
            logger.info("ESPN scoreboard: %s — servindo last_good", exc)
            data = {"events": []}
            fetch_failed = True
        except Exception as exc:
            logger.warning("ESPN scoreboard falhou: %s", exc)
            data = {"events": []}
            fetch_failed = True

        for ev in data.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            status = ev.get("status", {})
            stype = status.get("type", {})
            state = _STATE.get(stype.get("state"), "not_started")

            home = away = None
            for c in comp.get("competitors", []):
                team = c.get("team", {})
                t = LiveTeamSchema(
                    team_id=int(team.get("id", 0) or 0),
                    name=team.get("displayName", "") or team.get("name", ""),
                    tricode=team.get("abbreviation", ""),
                    score=int(c.get("score", 0) or 0),
                )
                if c.get("homeAway") == "home":
                    home = t
                else:
                    away = t
            if home is None or away is None:
                continue

            games.append(LiveGameSchema(
                game_id=str(ev.get("id", "")),
                game_status=state,
                period=int(status.get("period", 0) or 0),
                clock=str(status.get("displayClock", "") or ""),
                game_time_utc=ev.get("date"),
                home_team=home,
                away_team=away,
                blowout_risk=None,
            ))

        # Fallback pro último bom quando (a) falhamos e (b) temos cache
        # do MESMO dia. Impede que o front mostre "sem jogos hoje" quando
        # ScraperAPI dá 403 no meio do dia mesmo tendo pego a rodada antes.
        last_good = self._last_good.get("scoreboard")
        if fetch_failed and not games and last_good is not None:
            if getattr(last_good, "date", None) == date_str:
                logger.info("ESPN scoreboard: servindo last_good do dia %s", date_str)
                return last_good  # type: ignore[return-value]

        all_final = bool(games) and all(g.game_status == "final" for g in games)
        now_iso = datetime.now(timezone.utc).isoformat()
        result = LiveGamesCachedResponseSchema(
            date=date_str,
            games=games,
            updated_at=now_iso,
            age_ms=0,
            scoreboard_source="live",
            all_final=all_final,
        )
        self._live_cache.set("scoreboard", result, SCOREBOARD_TTL)
        # Só marca como "último bom" se REALMENTE achou jogos — resposta
        # vazia (offseason, madrugada, falha silenciosa) não deve substituir
        # um snapshot bom anterior.
        if games:
            self._last_good["scoreboard"] = result
        return result

    # ── Team board (pontos pró/contra por time) ──────────────────────────────
    def get_team_board(self, season: str):
        """
        Pontos marcados/sofridos por time da liga, via ESPN core stats
        (avgPoints / avgPointsAllowed). 1 chamada por time (~14) — cada
        chamada custa 1 credit no ScraperAPI, então CACHE 30 min in-memory
        pra não queimar 14 credits a cada request do dashboard.
        Só cacheia o resultado bom (available=True); falhas retornam
        sem cachear pra permitir retry rápido.
        """
        cache_key = f"team_board:{season}"
        cached = self._live_cache.get(cache_key)
        if cached is not None:
            return cached

        from datetime import datetime, timezone
        from src.schemas.nba_schemas import TeamBoardSchema, TeamBoardTeam

        year = _year_from_season(season) or datetime.now().year
        now = datetime.now(timezone.utc).isoformat()
        _CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba"

        # id -> {abbr, name}
        info: dict[str, dict] = {}
        try:
            r = _get(f"{_SITE}/teams")
            r.raise_for_status()
            for t in r.json()["sports"][0]["leagues"][0]["teams"]:
                tm = t["team"]
                info[str(tm["id"])] = {
                    "abbr": tm.get("abbreviation", ""),
                    "name": tm.get("displayName", ""),
                }
        except _QuotaExhaustedError as exc:
            logger.info("ESPN team board: %s — servindo last_good", exc)
            last_good = self._last_good.get(cache_key)
            if last_good is not None:
                return last_good  # type: ignore[return-value]
            return TeamBoardSchema(season=str(year), updated_at=now, available=False, teams=[])
        except Exception as exc:
            logger.warning("ESPN team board /teams falhou: %s", exc)
            last_good = self._last_good.get(cache_key)
            if last_good is not None:
                return last_good  # type: ignore[return-value]
            return TeamBoardSchema(season=str(year), updated_at=now, available=False, teams=[])

        teams: list = []
        for tid, meta in info.items():
            try:
                rs = _get(
                    f"{_CORE}/seasons/{year}/types/2/teams/{tid}/statistics",
                )
                if rs.status_code != 200:
                    continue
                flat: dict = {}
                for c in rs.json().get("splits", {}).get("categories", []):
                    for s in c.get("stats", []):
                        flat[s.get("name")] = s.get("value")
                pf = float(flat.get("avgPoints") or 0.0)
                pa = float(flat.get("avgPointsAllowed") or 0.0)
                gp = int(flat.get("gamesPlayed") or 0)
                if pf <= 0:
                    continue
                teams.append(TeamBoardTeam(
                    team_id=int(tid),
                    tricode=meta["abbr"],
                    name=meta["name"],
                    games=gp,
                    points=round(pf, 1),
                    opp_points=round(pa, 1),
                    plus_minus=round(pf - pa, 1),
                ))
            except Exception:
                continue

        if not teams:
            # Sem times: se temos last_good, servir. Só sobe False como
            # último recurso pra evitar dashboard vazio quando quota volta
            # antes do próximo ciclo.
            last_good = self._last_good.get(cache_key)
            if last_good is not None:
                return last_good  # type: ignore[return-value]
            return TeamBoardSchema(season=str(year), updated_at=now, available=False, teams=[])
        teams.sort(key=lambda t: t.points, reverse=True)
        result = TeamBoardSchema(season=str(year), updated_at=now, available=True, teams=teams)
        self._live_cache.set(cache_key, result, TEAM_BOARD_TTL)
        self._last_good[cache_key] = result
        return result

    # ── Box score ao vivo ────────────────────────────────────────────────────
    def get_live_boxscore(self, game_id: str):
        """
        Box score ao vivo via ESPN summary → LiveBoxscoreSchema. Stats por
        jogadora (MIN/PTS/REB/AST/FG/3PT/FT/faltas).
        Cache in-memory por BOXSCORE_TTL (5s) — placar/clock precisam ser
        fresh, mas 5s é o suficiente pra vários pollings do front baterem
        no mesmo hit ESPN. Poupança grande no ScraperAPI.
        """
        cache_key = f"boxscore:{game_id}"
        cached = self._live_cache.get(cache_key)
        if cached is not None:
            return cached

        from src.schemas.live_schemas import (
            LiveBoxscoreSchema,
            LivePlayerStatsSchema,
            LiveTeamBoxscoreSchema,
        )
        _STATE = {"pre": "not_started", "in": "in_progress", "post": "final"}

        try:
            r = _get(f"{_SITE}/summary", params={"event": game_id})
            r.raise_for_status()
            d = r.json()
        except _QuotaExhaustedError as exc:
            # Fallback pro último boxscore bom desse jogo. Melhor do que
            # 502 propagando pro front (que dispara mais retries).
            last_good = self._last_good.get(cache_key)
            if last_good is not None:
                logger.info(
                    "ESPN summary %s: quota exausta — servindo last_good", game_id,
                )
                return last_good  # type: ignore[return-value]
            raise RuntimeError(f"ESPN summary {game_id}: {exc}") from exc
        except Exception as exc:
            last_good = self._last_good.get(cache_key)
            if last_good is not None:
                logger.warning(
                    "ESPN summary %s falhou (%s) — servindo last_good", game_id, exc,
                )
                return last_good  # type: ignore[return-value]
            raise RuntimeError(f"ESPN summary {game_id} falhou: {exc}") from exc

        bx = d.get("boxscore", {})
        hdr = (d.get("header", {}).get("competitions") or [{}])[0]
        hstatus = hdr.get("status", {})
        state = _STATE.get((hstatus.get("type") or {}).get("state"), "in_progress")
        period = int(hstatus.get("period") or 0)
        clock = str(hstatus.get("displayClock") or "")

        scores: dict[int, int] = {}
        home_id = away_id = None
        for c in hdr.get("competitors", []):
            tid = int((c.get("team") or {}).get("id", 0) or 0)
            scores[tid] = int(c.get("score", 0) or 0)
            if c.get("homeAway") == "home":
                home_id = tid
            else:
                away_id = tid

        teams: dict[int, "LiveTeamBoxscoreSchema"] = {}
        order: list[int] = []
        for tb in bx.get("players", []):
            team = tb.get("team", {})
            tid = int(team.get("id", 0) or 0)
            order.append(tid)
            stats0 = (tb.get("statistics") or [{}])[0]
            labels = stats0.get("labels", [])

            def _idx(lbl: str):
                return labels.index(lbl) if lbl in labels else None

            ix = {k: _idx(k) for k in (
                "MIN", "PTS", "FG", "3PT", "FT", "REB", "AST",
                "TO", "STL", "BLK", "PF", "+/-",
            )}

            players: list = []
            for a in stats0.get("athletes", []):
                if a.get("didNotPlay"):
                    continue
                s = a.get("stats", [])
                if not s:
                    continue
                ath = a.get("athlete", {})

                def gv(k: str) -> str:
                    i = ix.get(k)
                    return s[i] if i is not None and i < len(s) else "0"

                fgm, fga = _split_made_att(gv("FG"))
                tpm, tpa = _split_made_att(gv("3PT"))
                ftm, fta = _split_made_att(gv("FT"))
                mins = _to_float(gv("MIN"))
                players.append(LivePlayerStatsSchema(
                    player_id=int(ath.get("id", 0) or 0),
                    name=ath.get("displayName", ""),
                    position=(ath.get("position") or {}).get("abbreviation", "") or "",
                    jersey_num=str(ath.get("jersey", "") or ""),
                    is_starter=bool(a.get("starter")),
                    minutes=mins,
                    points=_to_int(gv("PTS")),
                    rebounds=_to_int(gv("REB")),
                    assists=_to_int(gv("AST")),
                    steals=_to_int(gv("STL")),
                    blocks=_to_int(gv("BLK")),
                    turnovers=_to_int(gv("TO")),
                    field_goals_made=fgm,
                    field_goals_attempted=fga,
                    three_pointers_made=tpm,
                    three_pointers_attempted=tpa,
                    free_throws_made=ftm,
                    free_throws_attempted=fta,
                    plus_minus=_parse_pm(gv("+/-")),
                    fouls=_to_int(gv("PF")),
                    on_court=(mins > 0 and state == "in_progress"),
                ))
            teams[tid] = LiveTeamBoxscoreSchema(
                team_id=tid,
                name=team.get("displayName", "") or team.get("name", ""),
                tricode=team.get("abbreviation", ""),
                score=scores.get(tid, 0),
                players=players,
            )

        # Resolve home/away; se os ids não baterem, usa a ordem do boxscore.
        home = teams.get(home_id)
        away = teams.get(away_id)
        if home is None or away is None:
            uniq = [teams[t] for t in dict.fromkeys(order) if t in teams]
            if len(uniq) >= 2:
                away, home = uniq[0], uniq[1]

        if home is None or away is None:
            raise RuntimeError(f"Box score ESPN incompleto para {game_id}")

        result = LiveBoxscoreSchema(
            game_id=game_id,
            game_status=state,
            period=period,
            clock=clock,
            home_team=home,
            away_team=away,
        )
        self._live_cache.set(cache_key, result, BOXSCORE_TTL)
        self._last_good[cache_key] = result
        return result

    # ── Gamelog ──────────────────────────────────────────────────────────────
    def get_player_gamelog(self, player_id: int, season: str) -> list[GameLogSchema]:
        year = _year_from_season(season)
        cache_key = f"espn_gamelog:{player_id}:{year}"
        cached = self._gamelog_cache.get(cache_key)
        if cached is not None:
            return [GameLogSchema(**g) for g in cached]

        url = f"{_WEB}/athletes/{player_id}/gamelog"

        def _fetch(season_param: Optional[int]) -> Optional[dict]:
            params = {"season": season_param} if season_param else {}
            try:
                r = _get(url, params=params)
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                logger.info(
                    "ESPN gamelog %s (season=%s) falhou: %s",
                    player_id, season_param, exc,
                )
                return None

        def _has_regular(d: Optional[dict]) -> bool:
            if not d:
                return False
            return any(
                "regular" in (s.get("displayName", "")).lower()
                and any(c.get("events") for c in s.get("categories", []))
                for s in d.get("seasonTypes", [])
            )

        # Tenta a temporada pedida; se vier vazia (a ESPN nem sempre serve
        # histórico por ano), cai pra temporada CORRENTE (sem param) — assim
        # o teste local sempre tem dado real.
        data = _fetch(year) if year else None
        if not _has_regular(data):
            data = _fetch(None)
        if not data:
            return []

        names = data.get("names", [])
        events_meta = data.get("events", {})

        def idx(name: str) -> Optional[int]:
            return names.index(name) if name in names else None

        i_min = idx("minutes")
        i_pts = idx("points")
        i_reb = idx("totalRebounds")
        i_ast = idx("assists")
        i_fg = idx("fieldGoalsMade-fieldGoalsAttempted")
        i_3p = idx("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
        i_ft = idx("freeThrowsMade-freeThrowsAttempted")

        def stat(arr: list, i: Optional[int]) -> str:
            return arr[i] if i is not None and i < len(arr) else "0"

        results: list[GameLogSchema] = []
        for stype in data.get("seasonTypes", []):
            # Só temporada regular (ignora preseason/playoffs pra alinhar com
            # a média de temporada). "Regular Season" no displayName.
            if "regular" not in (stype.get("displayName", "")).lower():
                continue
            for cat in stype.get("categories", []):
                for ev in cat.get("events", []):
                    arr = ev.get("stats", [])
                    if not arr:
                        continue
                    eid = str(ev.get("eventId", ""))
                    meta = events_meta.get(eid, {})
                    fgm, fga = _split_made_att(stat(arr, i_fg))
                    tpm, tpa = _split_made_att(stat(arr, i_3p))
                    ftm, fta = _split_made_att(stat(arr, i_ft))
                    # matchup: "IND @ GS" / "IND vs GS"
                    own = (meta.get("team") or {}).get("abbreviation", "")
                    opp = (meta.get("opponent") or {}).get("abbreviation", "")
                    at_vs = meta.get("atVs", "vs")
                    matchup = f"{own} {at_vs} {opp}".strip()
                    game_date = str(meta.get("gameDate", ""))[:10]  # YYYY-MM-DD
                    try:
                        results.append(GameLogSchema(
                            game_id=eid,
                            game_date=game_date,
                            matchup=matchup,
                            minutes=str(stat(arr, i_min)),
                            points=_to_int(stat(arr, i_pts)),
                            rebounds=_to_int(stat(arr, i_reb)),
                            assists=_to_int(stat(arr, i_ast)),
                            field_goals_made=fgm,
                            field_goals_attempted=fga,
                            three_pointers_made=tpm,
                            three_pointers_attempted=tpa,
                            free_throws_made=ftm,
                            free_throws_attempted=fta,
                        ))
                    except Exception:
                        continue

        if results:
            self._gamelog_cache.set(
                cache_key, [g.model_dump() for g in results], GAMELOG_TTL
            )
        return results
