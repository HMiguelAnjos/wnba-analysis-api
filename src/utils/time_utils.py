import re
from datetime import datetime, timedelta, timezone

GAME_STATUS_MAP = {1: "not_started", 2: "in_progress", 3: "final"}


def today_in_et() -> str:
    """
    Data de "hoje" no fuso de Nova York (Eastern Time), no formato YYYY-MM-DD.

    A NBA usa ET como timezone canônico — o "game day" oficial. Usado pra
    decidir se o que a NBA Live API devolveu (que muda só ao meio-dia ET)
    ainda representa o dia de jogos atual, ou se a gente precisa puxar
    o scoreboard de outra data via ScoreboardV2.

    Implementação sem zoneinfo (que falha em alguns Windows sem tzdata):
    aplica offset fixo de -5h (UTC-5) e ajusta DST manualmente entre
    2º domingo de março e 1º domingo de novembro (regras dos EUA).
    Margem suficiente pra decisão de "que dia é hoje na NBA".
    """
    now_utc = datetime.now(timezone.utc)
    # Estimativa rápida: ET = UTC-5 (EST) ou UTC-4 (EDT).
    # DST nos EUA: 2º domingo de março a 1º domingo de novembro.
    year = now_utc.year
    # 2º domingo de março
    march_start = datetime(year, 3, 8, tzinfo=timezone.utc)
    march_start += timedelta(days=(6 - march_start.weekday()) % 7)  # próximo domingo
    # 1º domingo de novembro
    nov_start = datetime(year, 11, 1, tzinfo=timezone.utc)
    nov_start += timedelta(days=(6 - nov_start.weekday()) % 7)
    is_dst = march_start <= now_utc < nov_start
    offset_hours = -4 if is_dst else -5
    et_now = now_utc + timedelta(hours=offset_hours)
    return et_now.strftime("%Y-%m-%d")


def parse_minutes_to_float(value: str) -> float:
    """Convert NBA minutes string to float.

    Handles:
      - ISO 8601 duration: 'PT24M30.00S' -> 24.5
      - Clock format:      '24:30'        -> 24.5
      - Plain number:      '24'           -> 24.0
    """
    s = str(value).strip()

    # ISO 8601 duration (live endpoints)
    match = re.match(r"PT(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", s)
    if match and (match.group(1) or match.group(2)):
        minutes = int(match.group(1) or 0)
        seconds = float(match.group(2) or 0)
        return minutes + seconds / 60

    # MM:SS
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) + int(parts[1]) / 60
        except (ValueError, IndexError):
            return 0.0

    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def format_game_clock(game_clock: str) -> str:
    """Convert 'PT08M41.00S' to '08:41'. Returns '' on empty/unknown input."""
    s = str(game_clock).strip()
    if not s:
        return ""
    match = re.match(r"PT(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", s)
    if match:
        mins = int(match.group(1) or 0)
        secs = int(float(match.group(2) or 0))
        return f"{mins:02d}:{secs:02d}"
    return s


def map_game_status(status_code: int) -> str:
    return GAME_STATUS_MAP.get(int(status_code), "unknown")
