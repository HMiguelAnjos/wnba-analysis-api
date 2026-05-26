"""
Testes do OddsService (mai/2026 — The Odds API integration).

Mocka o HTTP client + matcher pra evitar rede e dependência do roster
estático real do nba_api. Cobertura:
  - TTL dinâmico por estado de jogo (pre-game/normal/crunch/final)
  - Agregação: média de N books pra (player, market)
  - Cache hit/miss respeita TTL
  - Resolve event_id via mapping de tricodes
  - Falha silenciosa: payload None / events vazio / matcher miss
  - Player não no roster vira ignored (sem crash)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.services.odds.odds_service import (
    ALL_MARKETS,
    GameOdds,
    MARKET_AST,
    MARKET_PTS,
    MARKET_REB,
    OddsService,
    PlayerOdds,
    compute_odds_ttl,
)
from src.services.odds.player_matcher import normalize_name


# ─── compute_odds_ttl ──────────────────────────────────────────────────────


def test_ttl_not_started_is_long_sentinel():
    # Pré-jogo NÃO chama API — TTL é grande sentinel; get_player_lines pula
    val = compute_odds_ttl(period=0, clock_minutes_remaining=0.0,
                            game_status="not_started")
    assert val >= 3600   # >> 1h, não vai expirar durante pré-jogo


def test_ttl_q1_is_120s():
    # Q1 movei lento — 120s
    assert compute_odds_ttl(period=1, clock_minutes_remaining=10.0,
                             game_status="in_progress") == 120


def test_ttl_q2_normal_is_90s():
    assert compute_odds_ttl(period=2, clock_minutes_remaining=6.0,
                             game_status="in_progress") == 90


def test_ttl_q3_first_7min_is_45s():
    # Q3 com clock>5 (ainda no início do quarter) → 45s (mai/2026:
    # cadência mais agressiva, linhas começam a importar pra decisão).
    assert compute_odds_ttl(period=3, clock_minutes_remaining=8.0,
                             game_status="in_progress") == 45


def test_ttl_q3_last_5min_is_30s():
    # Crunch começa quando clock ≤ 5 no Q3
    assert compute_odds_ttl(period=3, clock_minutes_remaining=5.0,
                             game_status="in_progress") == 30
    assert compute_odds_ttl(period=3, clock_minutes_remaining=2.0,
                             game_status="in_progress") == 30


def test_ttl_q4_is_30s():
    # Q4 inteiro = crunch
    assert compute_odds_ttl(period=4, clock_minutes_remaining=11.0,
                             game_status="in_progress") == 30


def test_ttl_overtime_is_30s():
    assert compute_odds_ttl(period=5, clock_minutes_remaining=4.0,
                             game_status="in_progress") == 30


def test_ttl_final_is_long():
    assert compute_odds_ttl(period=4, clock_minutes_remaining=0.0,
                             game_status="final") == 24 * 3600


# ─── normalize_name ────────────────────────────────────────────────────────


def test_normalize_strips_diacritics():
    assert normalize_name("Luka Dončić") == "luka doncic"


def test_normalize_strips_punctuation_and_collapses_whitespace():
    assert normalize_name("Tim Hardaway Jr.") == "tim hardaway jr"
    assert normalize_name("J.J.  Redick") == "jj redick"


def test_normalize_handles_empty():
    assert normalize_name("") == ""


# ─── OddsService ───────────────────────────────────────────────────────────


class _FakeClient:
    """Cliente mockado: retorna fixtures sem HTTP."""

    def __init__(
        self,
        events: Optional[list[dict]] = None,
        odds_payload: Optional[dict] = None,
    ) -> None:
        self.events = events if events is not None else []
        self.odds_payload = odds_payload
        self.events_calls = 0
        self.odds_calls = 0

    def list_events(self, sport: str = "basketball_nba") -> list[dict]:
        self.events_calls += 1
        return list(self.events)

    def event_odds(self, *, event_id, markets, sport="basketball_nba",
                   bookmakers=None) -> Optional[dict]:
        self.odds_calls += 1
        return self.odds_payload


class _FakeMatcher:
    """Matcher fixo — usa um dict pré-definido de nome → player_id."""

    def __init__(self, mapping: dict[str, int]) -> None:
        self.mapping = {normalize_name(k): v for k, v in mapping.items()}

    def find(self, name: str) -> Optional[int]:
        return self.mapping.get(normalize_name(name))


def _payload_with(*, books: list[tuple[str, dict]]) -> dict:
    """
    Helper pra montar payload do The Odds API.
    `books` = list[(book_title, {market_key: [(player_name, point), ...]})]
    """
    bookmakers = []
    for title, markets_dict in books:
        markets = []
        for m_key, players in markets_dict.items():
            outcomes = []
            for player_name, point in players:
                outcomes.append({
                    "name": "Over", "description": player_name,
                    "price": 1.91, "point": point,
                })
                outcomes.append({
                    "name": "Under", "description": player_name,
                    "price": 1.91, "point": point,
                })
            markets.append({"key": m_key, "outcomes": outcomes})
        bookmakers.append({"key": title.lower(), "title": title, "markets": markets})
    return {"id": "evt1", "bookmakers": bookmakers}


def _events_index_payload() -> list[dict]:
    return [
        {"id": "evt1", "home_team": "Boston Celtics",
         "away_team": "Los Angeles Lakers", "commence_time": "2026-05-09T23:00:00Z"},
    ]


def test_aggregate_averages_lines_across_books():
    """3 books com lines diferentes → média arredondada."""
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 24.5)]}),
            ("FanDuel",    {MARKET_PTS: [("LeBron James", 25.0)]}),
            ("BetMGM",     {MARKET_PTS: [("LeBron James", 24.0)]}),
        ]),
    )
    matcher = _FakeMatcher({"LeBron James": 2544})
    svc = OddsService(client=client, matcher=matcher)

    out = svc.get_player_lines(
        game_id="g1", period=2, clock_minutes_remaining=8.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert 2544 in out
    pts = out[2544][MARKET_PTS]
    # média de 24.5, 25.0, 24.0 = 24.5
    assert pts.line == 24.5
    assert pts.book_count == 3
    assert set(pts.books) == {"DraftKings", "FanDuel", "BetMGM"}


def test_aggregate_handles_multiple_markets_per_player():
    """Mesmo book pode trazer PTS, REB e AST do mesmo jogador."""
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload=_payload_with(books=[
            ("DraftKings", {
                MARKET_PTS: [("LeBron James", 25.0)],
                MARKET_REB: [("LeBron James", 7.5)],
                MARKET_AST: [("LeBron James", 6.5)],
            }),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    player_lines = out[2544]
    assert player_lines[MARKET_PTS].line == 25.0
    assert player_lines[MARKET_REB].line == 7.5
    assert player_lines[MARKET_AST].line == 6.5


def test_aggregate_skips_players_not_in_roster():
    """Jogadores que o matcher não conhece são silenciosamente ignorados."""
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [
                ("LeBron James", 25.0),
                ("Some Random Guy Not Real", 10.0),
            ]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    # Só LeBron passa
    assert list(out.keys()) == [2544]


def test_returns_empty_when_event_id_unmappable():
    """Tricodes que não batem com nenhum event → dict vazio."""
    client = _FakeClient(events=_events_index_payload(), odds_payload={})
    svc = OddsService(client=client, matcher=_FakeMatcher({}))

    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="MIA", away_tricode="ATL",   # não está nos events
    )
    assert out == {}
    # Não chamou event_odds (já parou no resolve)
    assert client.odds_calls == 0


def test_returns_empty_when_payload_is_none():
    """Falha de fetch (resp None) → dict vazio sem crash."""
    client = _FakeClient(events=_events_index_payload(), odds_payload=None)
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert out == {}


def test_cache_hit_within_ttl_avoids_second_fetch():
    """Duas chamadas seguidas dentro do TTL → 1 fetch só."""
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 25.0)]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    args = dict(
        game_id="g1", period=2, clock_minutes_remaining=8.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    svc.get_player_lines(**args)
    svc.get_player_lines(**args)
    assert client.odds_calls == 1   # cache hit na 2ª


def test_constructor_requires_api_key_when_client_not_injected():
    with pytest.raises(ValueError):
        OddsService()


def test_zero_books_returns_empty_player_dict():
    """Payload com bookmakers=[] → nenhum player no resultado."""
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload={"id": "evt1", "bookmakers": []},
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert out == {}


def test_unknown_market_key_is_ignored():
    """Books que retornam mercados não-NBA (ex: futebol) são pulados."""
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload={
            "id": "evt1",
            "bookmakers": [{
                "key": "dk", "title": "DraftKings",
                "markets": [{
                    "key": "h2h",   # não está em ALL_MARKETS
                    "outcomes": [{
                        "name": "Over", "description": "LeBron James", "point": 99,
                    }],
                }],
            }],
        },
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))
    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert out == {}


def test_empty_events_list_means_no_mapping_built():
    """Sem events do The Odds API → não cria índice → retorno vazio."""
    client = _FakeClient(events=[], odds_payload=None)
    svc = OddsService(client=client, matcher=_FakeMatcher({}))
    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert out == {}
    assert client.odds_calls == 0


def test_not_started_outside_prefetch_window_skips_odds_fetch():
    """
    Pré-jogo > 5 min do tipoff: NÃO chama o endpoint caro de odds.
    O endpoint barato (events list, 1 crédito) pode ser chamado uma vez
    pra resolver event_id — isso é normal e cacheado 6h.
    """
    # Tipoff daqui a 30 min — fora da janela de prefetch
    far_future = (
        datetime.now(timezone.utc) + timedelta(minutes=30)
    ).isoformat().replace("+00:00", "Z")
    client = _FakeClient(
        events=[{
            "id": "evt1", "home_team": "Boston Celtics",
            "away_team": "Los Angeles Lakers",
            "commence_time": far_future,
        }],
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 25.0)]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=0, clock_minutes_remaining=0.0,
        game_status="not_started",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert out == {}
    # O endpoint CARO (odds, 30 créditos) NÃO foi chamado
    assert client.odds_calls == 0


def test_not_started_within_prefetch_window_does_one_fetch():
    """Pré-jogo ≤ 5 min do tipoff: faz UMA fetch antecipada."""
    near_future = (
        datetime.now(timezone.utc) + timedelta(minutes=3)
    ).isoformat().replace("+00:00", "Z")
    client = _FakeClient(
        events=[{
            "id": "evt1", "home_team": "Boston Celtics",
            "away_team": "Los Angeles Lakers",
            "commence_time": near_future,
        }],
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 25.0)]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=0, clock_minutes_remaining=0.0,
        game_status="not_started",
        home_tricode="BOS", away_tricode="LAL",
    )
    # Linha foi pré-carregada
    assert 2544 in out
    assert out[2544][MARKET_PTS].line == 25.0
    assert client.odds_calls == 1


def test_not_started_after_tipoff_skips():
    """Tipoff já passou (game atrasado, status ainda not_started): skip."""
    past = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat().replace("+00:00", "Z")
    client = _FakeClient(
        events=[{
            "id": "evt1", "home_team": "Boston Celtics",
            "away_team": "Los Angeles Lakers",
            "commence_time": past,
        }],
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 25.0)]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=0, clock_minutes_remaining=0.0,
        game_status="not_started",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert out == {}
    assert client.odds_calls == 0


def test_prefetch_cache_hit_reused_after_status_flip():
    """
    Cenário real: 1× pré-jogo dentro dos 5min finais, depois Q1 começa.
    A 1ª chamada em in_progress reusa o cache (TTL=5min do prefetch
    cobre os primeiros minutos do Q1).
    """
    near_future = (
        datetime.now(timezone.utc) + timedelta(minutes=3)
    ).isoformat().replace("+00:00", "Z")
    client = _FakeClient(
        events=[{
            "id": "evt1", "home_team": "Boston Celtics",
            "away_team": "Los Angeles Lakers",
            "commence_time": near_future,
        }],
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 25.0)]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    # 1ª: pré-jogo, 3min do tipoff → fetch
    svc.get_player_lines(
        game_id="g1", period=0, clock_minutes_remaining=0.0,
        game_status="not_started",
        home_tricode="BOS", away_tricode="LAL",
    )
    # 2ª: jogo começou, Q1 → reusa cache pré-aquecido
    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=11.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert 2544 in out
    # Total de fetches: 1 (não duplicou)
    assert client.odds_calls == 1


def test_final_game_without_cache_skips_api():
    """
    Jogo encerrado sem cache prévio (ex: backend reiniciou após o jogo):
    NÃO chama API. Linha pós-jogo não tem valor pra +EV.
    """
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 25.0)]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    out = svc.get_player_lines(
        game_id="g1", period=4, clock_minutes_remaining=0.0,
        game_status="final",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert out == {}
    # Nem o events list nem o odds endpoint foram chamados — economia de créditos
    assert client.events_calls == 0
    assert client.odds_calls == 0


def test_final_game_with_cache_returns_cached_without_api():
    """
    Jogo finalizou enquanto o cache estava quente (in_progress → final).
    Devolve o cache existente, NÃO refetcha. Comum quando usuário olha
    o card logo depois do apito final.
    """
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload=_payload_with(books=[
            ("DraftKings", {MARKET_PTS: [("LeBron James", 25.0)]}),
        ]),
    )
    svc = OddsService(client=client, matcher=_FakeMatcher({"LeBron James": 2544}))

    # 1ª: jogo in_progress, popula o cache
    svc.get_player_lines(
        game_id="g1", period=4, clock_minutes_remaining=2.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    initial_calls = client.odds_calls
    assert initial_calls == 1

    # 2ª: mesmo game_id agora final → devolve do cache, NÃO chama API
    out = svc.get_player_lines(
        game_id="g1", period=4, clock_minutes_remaining=0.0,
        game_status="final",
        home_tricode="BOS", away_tricode="LAL",
    )
    assert 2544 in out
    assert out[2544][MARKET_PTS].line == 25.0
    # Nenhuma fetch nova — economia
    assert client.odds_calls == initial_calls


def test_outcome_without_point_is_skipped():
    """Outcome sem `point` (raro, mas possível) não quebra parser."""
    client = _FakeClient(
        events=_events_index_payload(),
        odds_payload={
            "id": "evt1",
            "bookmakers": [{
                "key": "dk", "title": "DraftKings",
                "markets": [{
                    "key": MARKET_PTS,
                    "outcomes": [
                        {"name": "Over", "description": "LeBron James"},   # sem point
                        {"name": "Over", "description": "Anthony Davis", "point": 22.5},
                    ],
                }],
            }],
        },
    )
    svc = OddsService(
        client=client,
        matcher=_FakeMatcher({"LeBron James": 2544, "Anthony Davis": 203076}),
    )
    out = svc.get_player_lines(
        game_id="g1", period=1, clock_minutes_remaining=12.0,
        game_status="in_progress",
        home_tricode="BOS", away_tricode="LAL",
    )
    # LeBron pulado por falta de point; AD passa
    assert 2544 not in out
    assert out[203076][MARKET_PTS].line == 22.5
