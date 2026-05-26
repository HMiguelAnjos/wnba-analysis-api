import re
from typing import Any

# EVENTMSGTYPE codes from nba_api play-by-play
EVENT_TYPE_MAP = {
    1: "Field Goal Made",
    2: "Field Goal Missed",
    3: "Free Throw",
    4: "Rebound",
    5: "Turnover",
    6: "Foul",
    7: "Violation",
    8: "Substitution",
    9: "Timeout",
    10: "Jump Ball",
    12: "Start Period",
    13: "End Period",
}

FIELD_GOAL_MADE = 1
FREE_THROW = 3


def safe_str(value: Any) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return str(value).strip()


def is_three_pointer(description: str) -> bool:
    return "3PT" in description.upper()


def is_free_throw_made(description: str) -> bool:
    desc = description.upper()
    return "MISS" not in desc and "FREE THROW" in desc


def points_from_event(event_type: int, description: str) -> int:
    if event_type == FIELD_GOAL_MADE:
        return 3 if is_three_pointer(description) else 2
    if event_type == FREE_THROW and is_free_throw_made(description):
        return 1
    return 0


def normalize_player_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


# ─── Team mapping ─────────────────────────────────────────────────────────
# `LeagueDashTeamStats` da NBA API expõe apenas TEAM_NAME (nome completo,
# ex: "Los Angeles Lakers") em alguns endpoints — não TEAM_ABBREVIATION.
# Mapeamento manual cobre os 30 times. Manutenção: só muda quando uma
# franquia troca de nome (raro).

_TEAM_NAME_TO_TRICODE = {
    "Atlanta Hawks": "ATL",
    "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU",
    "Indiana Pacers": "IND",
    "LA Clippers": "LAC",
    "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}


def team_name_to_tricode(team_name: str) -> str:
    """Mapeia nome completo NBA → tricode 3 letras. Vazio se não reconhece."""
    if not team_name:
        return ""
    return _TEAM_NAME_TO_TRICODE.get(team_name.strip(), "")
