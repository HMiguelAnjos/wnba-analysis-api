"""Testes para calculate_player_performance_rating."""
from src.utils.stats import calculate_player_performance_rating


def test_did_not_play_returns_na():
    """Jogador que não entrou em quadra recebe 0/N/A com low_confidence."""
    rating, label, low_conf = calculate_player_performance_rating(minutes=0)
    assert rating == 0.0
    assert label == "N/A"
    assert low_conf is True


def test_average_player_around_5():
    """Linha estatística mediana → nota próxima de 5–6 (Regular)."""
    rating, label, _ = calculate_player_performance_rating(
        points=10, rebounds=4, assists=3,
        steals=1, blocks=0, turnovers=2, fouls=2,
        plus_minus=0, minutes=25,
        field_goals_made=4, field_goals_attempted=10,
        three_pointers_made=1, free_throws_made=1, free_throws_attempted=2,
    )
    assert 5.0 <= rating <= 7.5, f"Esperado entre 5–7.5, veio {rating}"
    assert label in ("Regular", "Bom")


def test_star_performance_around_85():
    """Stat line de candidato a Player of the Game vira Excelente."""
    rating, label, low_conf = calculate_player_performance_rating(
        points=32, rebounds=10, assists=8,
        steals=2, blocks=1, turnovers=3, fouls=2,
        plus_minus=15, minutes=36,
        field_goals_made=12, field_goals_attempted=20,
        three_pointers_made=4, free_throws_made=4, free_throws_attempted=4,
    )
    assert rating >= 8.5, f"Esperado >=8.5, veio {rating}"
    assert label == "Excelente"
    assert low_conf is False


def test_bad_performance_below_5():
    """Muitos turnovers + arremessos errados + plus/minus negativo → Ruim."""
    rating, label, _ = calculate_player_performance_rating(
        points=4, rebounds=1, assists=0,
        steals=0, blocks=0, turnovers=5, fouls=4,
        plus_minus=-15, minutes=22,
        field_goals_made=2, field_goals_attempted=11,  # 18% — péssimo
        three_pointers_made=0, free_throws_made=0, free_throws_attempted=0,
    )
    assert rating < 5.0, f"Esperado <5, veio {rating}"
    assert label == "Ruim"


def test_short_minutes_caps_rating():
    """Cara que jogou 4 min com 8 pts não ganha nota 9."""
    rating, label, low_conf = calculate_player_performance_rating(
        points=8, rebounds=2, assists=1,
        steals=1, blocks=0, turnovers=0, fouls=0,
        plus_minus=8, minutes=4,
        field_goals_made=3, field_goals_attempted=4,
        three_pointers_made=2, free_throws_made=0, free_throws_attempted=0,
    )
    assert low_conf is True
    assert rating <= 7.0, f"Esperado <=7 com low confidence, veio {rating}"


def test_efficient_shooter_bonus():
    """Mesmas stats, eFG% 60% > eFG% 40% deve dar nota maior."""
    high_efg, _, _ = calculate_player_performance_rating(
        points=18, minutes=25,
        field_goals_made=8, field_goals_attempted=12,  # 67%
        three_pointers_made=2,
    )
    low_efg, _, _ = calculate_player_performance_rating(
        points=18, minutes=25,
        field_goals_made=7, field_goals_attempted=20,  # 35%
        three_pointers_made=2,
    )
    assert high_efg > low_efg


def test_clamps_to_zero_ten():
    """Nunca deve passar de 10 nem ficar negativo."""
    high_rating, _, _ = calculate_player_performance_rating(
        points=70, rebounds=20, assists=15,
        steals=8, blocks=8, turnovers=0,
        plus_minus=50, minutes=48,
        field_goals_made=25, field_goals_attempted=30, three_pointers_made=10,
    )
    assert high_rating <= 10.0

    low_rating, _, _ = calculate_player_performance_rating(
        points=0, rebounds=0, assists=0,
        turnovers=15, fouls=10, plus_minus=-40, minutes=20,
        field_goals_made=0, field_goals_attempted=15,
    )
    assert 0.0 <= low_rating <= 10.0


def test_label_thresholds():
    """Labels seguem os limiares documentados."""
    # Excelente: >= 8.5
    rating, label, _ = calculate_player_performance_rating(
        points=40, rebounds=12, assists=10, steals=3, blocks=2,
        plus_minus=20, minutes=38,
        field_goals_made=15, field_goals_attempted=22, three_pointers_made=5,
    )
    assert rating >= 8.5
    assert label == "Excelente"
