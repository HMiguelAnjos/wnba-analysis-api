"""
Helpers para gerar URLs de foto dos jogadores.

A NBA expõe headshots em CDN público com padrão estável:
    https://cdn.nba.com/headshots/nba/latest/{size}/{personId}.png

Confirmado em testes: o CDN responde 200 mesmo para personIds inválidos
ou desconhecidos — nesse caso devolve uma silhueta padrão. Por isso não
precisamos validar prévio: o front só precisa lidar com erro de carga
(`<img onError>`) caso o CDN fique offline.

Tamanhos usados:
- 260x190 → thumbnails de cards
- 1040x760 → modal/foto grande
"""
from typing import Literal

from src.league import HEADSHOT_BASE_URL

PhotoSize = Literal["260x190", "1040x760"]

# Base do CDN de headshot por liga (nba/wnba) — vem do config de liga.
_BASE_URL = HEADSHOT_BASE_URL


def player_photo_url(person_id: int, size: PhotoSize = "260x190") -> str:
    """
    Constrói URL da foto do jogador na NBA CDN.

    Args:
        person_id: ID do jogador (campo `personId` no boxscore live).
        size: Tamanho do headshot. "260x190" para cards, "1040x760" para modais.

    Returns:
        URL absoluta. Sempre retorna string válida (mesmo para IDs estranhos
        o CDN devolve um placeholder padrão).
    """
    if person_id <= 0:
        # Defensivo: pra IDs zerados/inválidos, retorna URL inexistente — front
        # vai cair no fallback de iniciais via onError.
        return f"{_BASE_URL}/{size}/0.png"
    return f"{_BASE_URL}/{size}/{person_id}.png"
