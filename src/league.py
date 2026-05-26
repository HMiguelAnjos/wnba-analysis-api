"""
Configuração de LIGA (mai/2026) — fork WNBA.

Centraliza tudo que difere entre NBA e WNBA pra que o MESMO código sirva
as duas ligas. Neste repositório o default já é WNBA; basta setar
LEAGUE_ID=00 pra voltar ao comportamento NBA.

O que é específico de liga e mora aqui:
  - LeagueID do nba_api ("00" NBA, "10" WNBA, "20" G-League)
  - Modelo de jogo (WNBA = 4x10 = 40 min; NBA = 4x12 = 48 min)
  - Formato de temporada (WNBA "YYYY" mai–out; NBA "YYYY-YY" out–jun)
  - Sport key do The Odds API
  - CDN de headshot

Nada aqui altera CÁLCULO/projeção — só parametriza constantes da liga.
"""
from __future__ import annotations

import os
from datetime import datetime

# "00" = NBA · "10" = WNBA · "20" = G-League
LEAGUE_ID: str = os.getenv("LEAGUE_ID", "10")
IS_WNBA: bool = LEAGUE_ID == "10"
LEAGUE_NAME: str = "WNBA" if IS_WNBA else "NBA"

# ── Modelo de jogo ───────────────────────────────────────────────────────────
# WNBA: 4 quartos de 10 min = 40. NBA: 4 de 12 = 48. OT = 5 nas duas.
QUARTER_MINUTES: int = 10 if IS_WNBA else 12
REGULATION_QUARTERS: int = 4
REGULATION_MINUTES: int = QUARTER_MINUTES * REGULATION_QUARTERS  # 40 / 48
OVERTIME_MINUTES: int = 5

# ── Fonte de dados ───────────────────────────────────────────────────────────
# "espn"    = API pública da ESPN — responde do IP local SEM proxy (ideal
#             pra testar WNBA local). Cobre busca + gamelog (→ análise).
# "nba_api" = stats.nba.com via nba_api (precisa de STATS_PROXY).
# Default: espn no fork WNBA (pra rodar local sem proxy).
DATA_SOURCE: str = os.getenv("WNBA_DATA_SOURCE", "espn" if IS_WNBA else "nba_api")

# ── The Odds API ─────────────────────────────────────────────────────────────
ODDS_SPORT_KEY: str = "basketball_wnba" if IS_WNBA else "basketball_nba"

# ── Headshots (CDN) ──────────────────────────────────────────────────────────
_HEADSHOT_LEAGUE: str = "wnba" if IS_WNBA else "nba"
HEADSHOT_BASE_URL: str = f"https://cdn.nba.com/headshots/{_HEADSHOT_LEAGUE}/latest"


def current_season() -> str:
    """
    Temporada atual no formato da liga.

    NBA: 'YYYY-YY' (temporada cruza o ano — Out a Jun).
    WNBA: 'YYYY' (ano único — Mai a Out).
    """
    now = datetime.now()
    if IS_WNBA:
        return str(now.year)
    if now.month >= 10:
        return f"{now.year}-{str(now.year + 1)[-2:]}"
    return f"{now.year - 1}-{str(now.year)[-2:]}"


# Permite fixar a temporada pra teste (ex.: DEFAULT_SEASON=2025 — última
# completa). Sem override, usa a atual no formato da liga.
DEFAULT_SEASON: str = os.getenv("DEFAULT_SEASON") or current_season()
