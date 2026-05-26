"""Testes para player_photo_url."""
from src.utils.photos import player_photo_url


def test_default_size_is_thumbnail():
    url = player_photo_url(1627759)
    assert url == "https://cdn.nba.com/headshots/nba/latest/260x190/1627759.png"


def test_large_size():
    url = player_photo_url(1627759, "1040x760")
    assert url == "https://cdn.nba.com/headshots/nba/latest/1040x760/1627759.png"


def test_zero_or_negative_id_falls_through_to_zero():
    """IDs inválidos retornam URL conhecida pra ativar fallback no front."""
    assert "0.png" in player_photo_url(0)
    assert "0.png" in player_photo_url(-1)


def test_real_player_id_pattern():
    """Sanidade: IDs altos (jogadores recentes) também produzem URL válida."""
    url = player_photo_url(1641705)  # Wemby
    assert url.startswith("https://cdn.nba.com/headshots/nba/latest/")
    assert url.endswith("/1641705.png")
