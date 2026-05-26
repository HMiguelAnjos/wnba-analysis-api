"""
Testes para o parsing de lineups.

Usa fixtures sintéticas que imitam o JSON real da NBA Live API
(confirmado via inspeção de boxscores reais).
"""
from src.services.live_game_service import _parse_lineup_player, _parse_lineup_team


def _make_player(
    person_id=1627759,
    name="Jaylen Brown",
    position="SF",
    starter="0",
    oncourt="0",
    played="1",
    status="ACTIVE",
    not_playing_reason=None,
    jersey_num="7",
    minutes="PT24M30.00S",
    points=18,
    rebounds=5,
    assists=3,
    steals=1,
    blocks=0,
    turnovers=2,
    fouls=2,
    fgm=7,
    fga=14,
    tpm=2,
    tpa=5,
    ftm=2,
    fta=2,
    plus_minus=8,
):
    return {
        "personId": person_id,
        "name": name,
        "position": position,
        "starter": starter,
        "oncourt": oncourt,
        "played": played,
        "status": status,
        "notPlayingReason": not_playing_reason,
        "jerseyNum": jersey_num,
        "statistics": {
            "minutes": minutes,
            "points": points,
            "reboundsTotal": rebounds,
            "assists": assists,
            "steals": steals,
            "blocks": blocks,
            "turnovers": turnovers,
            "foulsPersonal": fouls,
            "fieldGoalsMade": fgm,
            "fieldGoalsAttempted": fga,
            "threePointersMade": tpm,
            "threePointersAttempted": tpa,
            "freeThrowsMade": ftm,
            "freeThrowsAttempted": fta,
            "plusMinusPoints": plus_minus,
        },
    }


def test_parse_player_basic_fields():
    p = _parse_lineup_player(_make_player())
    assert p.player_id == 1627759
    assert p.name == "Jaylen Brown"
    assert p.position == "SF"
    assert p.jersey_num == "7"
    assert p.points == 18
    assert p.steals == 1
    assert p.photo_url.endswith("/1627759.png")


def test_parse_player_starter_flag():
    starter = _parse_lineup_player(_make_player(starter="1"))
    bench = _parse_lineup_player(_make_player(starter="0"))
    assert starter.is_starter is True
    assert bench.is_starter is False


def test_parse_player_on_court_flag():
    in_game = _parse_lineup_player(_make_player(oncourt="1"))
    on_bench = _parse_lineup_player(_make_player(oncourt="0"))
    assert in_game.is_on_court is True
    assert on_bench.is_on_court is False


def test_parse_player_inactive():
    p = _parse_lineup_player(_make_player(
        status="INACTIVE",
        not_playing_reason="INACTIVE_INJURY",
        played="0",
        minutes="PT00M00.00S",
        points=0, rebounds=0, assists=0,
    ))
    assert p.status == "INACTIVE"
    assert p.not_playing_reason == "INACTIVE_INJURY"
    assert p.played is False
    # Não jogou → N/A na nota
    assert p.performance_rating == 0.0
    assert p.performance_label == "N/A"


def test_parse_team_separates_starters_bench_inactive():
    team = {
        "teamId": 1610612738,
        "teamCity": "Boston",
        "teamName": "Celtics",
        "teamTricode": "BOS",
        "score": 95,
        "players": [
            _make_player(person_id=1, name="A", starter="1", oncourt="1"),
            _make_player(person_id=2, name="B", starter="1", oncourt="1"),
            _make_player(person_id=3, name="C", starter="1", oncourt="0"),
            _make_player(person_id=4, name="D", starter="1", oncourt="0"),
            _make_player(person_id=5, name="E", starter="1", oncourt="0"),
            _make_player(person_id=6, name="F", starter="0", minutes="PT12M00.00S"),
            _make_player(person_id=7, name="G", starter="0", minutes="PT00M00.00S",
                         points=0, rebounds=0, assists=0, played="0"),
            _make_player(person_id=8, name="H", status="INACTIVE",
                         not_playing_reason="INACTIVE_INJURY",
                         played="0", minutes="PT00M00.00S"),
        ],
    }
    parsed = _parse_lineup_team(team)
    assert parsed.tricode == "BOS"
    assert parsed.score == 95
    assert len(parsed.starters) == 5
    assert len(parsed.bench) == 2     # F (jogou) + G (não entrou ainda)
    assert len(parsed.inactive) == 1
    # Bench ordenado por minutos jogados desc
    assert parsed.bench[0].name == "F"
    assert parsed.bench[1].name == "G"


def test_parse_player_handles_zero_minutes_gracefully():
    """Reserva que ainda não entrou aparece no lineup com nota N/A."""
    p = _parse_lineup_player(_make_player(
        starter="0", oncourt="0", played="0",
        minutes="PT00M00.00S",
        points=0, rebounds=0, assists=0,
        steals=0, blocks=0, turnovers=0,
        fgm=0, fga=0, tpm=0, tpa=0, ftm=0, fta=0,
    ))
    assert p.minutes == 0.0
    assert p.played is False
    assert p.performance_label == "N/A"
    assert p.low_confidence is True


def test_parse_player_missing_optional_fields():
    """Falta de notPlayingReason ou position não quebra o parse."""
    raw = {
        "personId": 999,
        "name": "X",
        "starter": "0",
        "oncourt": "0",
        "played": "1",
        "status": "ACTIVE",
        "jerseyNum": "0",
        "statistics": {"minutes": "PT15M00.00S", "points": 5},
    }
    p = _parse_lineup_player(raw)
    assert p.position == ""
    assert p.not_playing_reason is None
    assert p.points == 5
