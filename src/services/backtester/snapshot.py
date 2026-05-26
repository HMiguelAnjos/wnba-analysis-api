"""
Reconstrutor de snapshot — pega PBP completo de um jogo e calcula
o estado de TODOS os jogadores num ponto específico do jogo.

Por que: pra rodar o motor de projeção em jogos passados, preciso
saber "como estavam os jogadores no Q3 com 6:00 restantes". Esse
módulo deriva isso do PBP.

Entradas:
  - actions: lista de eventos PBP da NBA Live API
  - boxscore: stats finais + starters (pra fallback de minutos via subs)
  - snapshot_period: 1-4 (regulação) ou 5+ (OT)
  - snapshot_clock_min: minutos restantes no clock (12.0 = início do período)

Saída:
  Dict {player_id: PlayerSnapshot} com stats até aquele momento.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PlayerSnapshot:
    """Estado de um jogador num momento específico do jogo."""
    player_id: int
    name: str
    team_tricode: str
    is_starter: bool
    # Stats acumulados até o momento do snapshot
    points: int = 0
    rebounds: int = 0
    assists: int = 0
    three_pointers_made: int = 0
    field_goals_attempted: int = 0
    field_goals_made: int = 0
    free_throws_attempted: int = 0
    free_throws_made: int = 0
    fouls: int = 0
    minutes_played: float = 0.0


@dataclass
class GameSnapshot:
    """Estado completo do jogo num ponto no tempo."""
    period: int
    clock_minutes_remaining: float
    players: dict[int, PlayerSnapshot] = field(default_factory=dict)
    home_tricode: str = ""
    away_tricode: str = ""
    home_score: int = 0
    away_score: int = 0


def _parse_clock_to_min(clock_str: str) -> float:
    """'PT08M30.00S' → 8.5 minutos."""
    if not clock_str:
        return 0.0
    m = re.match(r"PT(\d+)M([\d.]+)S", str(clock_str))
    if not m:
        return 0.0
    return int(m.group(1)) + float(m.group(2)) / 60.0


def _is_after_snapshot(action: dict, snap_period: int, snap_clock: float) -> bool:
    """
    True se a ação aconteceu DEPOIS do momento do snapshot.
    Snapshot = "estado até esse momento, exclusivo".

    Action acontece no clock X do period Y. Clock decresce de 12 → 0.
    Snapshot está em (snap_period, snap_clock).
    """
    try:
        ap = int(action.get("period", 0))
    except (TypeError, ValueError):
        return True
    if ap > snap_period:
        return True  # período posterior → depois
    if ap < snap_period:
        return False  # período anterior → antes
    # Mesmo período: action é "depois" se action.clock < snap_clock
    a_clock = _parse_clock_to_min(action.get("clock", ""))
    return a_clock < snap_clock


def reconstruct_snapshot(
    actions: list[dict],
    boxscore: dict,
    snapshot_period: int,
    snapshot_clock_min: float,
) -> GameSnapshot:
    """
    Walk pelo PBP, acumulando stats por player até o snapshot.

    Args:
        actions: lista do PBP (cdn.nba.com format)
        boxscore: box score completo (pra pegar starters + nome dos jogadores)
        snapshot_period: 1-4 (ou 5+ OT)
        snapshot_clock_min: minutos restantes no relógio (12.0 = início)
    """
    home = boxscore.get("homeTeam", {})
    away = boxscore.get("awayTeam", {})

    # Inicializa snapshot com TODOS os jogadores que aparecem no boxscore
    # (mesmo que ainda não tenham jogado — preserva is_starter).
    snap = GameSnapshot(
        period=snapshot_period,
        clock_minutes_remaining=snapshot_clock_min,
        home_tricode=home.get("teamTricode", ""),
        away_tricode=away.get("teamTricode", ""),
    )

    def _add_team_players(team_data: dict, tricode: str) -> None:
        for p in team_data.get("players", []):
            pid = int(p.get("personId", 0))
            if pid <= 0:
                continue
            snap.players[pid] = PlayerSnapshot(
                player_id=pid,
                name=p.get("name", ""),
                team_tricode=tricode,
                is_starter=str(p.get("starter", "0")) == "1",
            )

    _add_team_players(home, snap.home_tricode)
    _add_team_players(away, snap.away_tricode)

    # Walk pelas ações, acumulando stats até o snapshot
    for action in actions:
        if _is_after_snapshot(action, snapshot_period, snapshot_clock_min):
            continue

        atype = str(action.get("actionType", "")).lower()
        pid_raw = action.get("personId")
        try:
            pid = int(pid_raw) if pid_raw is not None else 0
        except (TypeError, ValueError):
            pid = 0

        if pid > 0 and pid in snap.players:
            ps = snap.players[pid]

            # Shots
            if atype in ("2pt", "3pt"):
                ps.field_goals_attempted += 1
                if str(action.get("shotResult", "")).lower() == "made":
                    ps.field_goals_made += 1
                    if atype == "3pt":
                        ps.points += 3
                        ps.three_pointers_made += 1
                    else:
                        ps.points += 2
            elif atype == "freethrow":
                ps.free_throws_attempted += 1
                if str(action.get("shotResult", "")).lower() == "made":
                    ps.free_throws_made += 1
                    ps.points += 1
            elif atype == "rebound":
                ps.rebounds += 1
            elif atype == "foul":
                # Apenas faltas pessoais que contam pra foul-out (não tech/flag)
                sub = str(action.get("subType", "")).lower()
                if "technical" not in sub and "flagrant" not in sub:
                    ps.fouls += 1

        # Assist (vai pro assistPersonId, não pro shooter)
        ast_raw = action.get("assistPersonId")
        if (
            ast_raw is not None
            and atype in ("2pt", "3pt")
            and str(action.get("shotResult", "")).lower() == "made"
        ):
            try:
                aid = int(ast_raw)
            except (TypeError, ValueError):
                aid = 0
            if aid > 0 and aid in snap.players:
                snap.players[aid].assists += 1

        # Score do jogo (escoreboardo)
        score_home = action.get("scoreHome")
        score_away = action.get("scoreAway")
        if score_home is not None:
            try:
                snap.home_score = int(score_home)
            except (TypeError, ValueError):
                pass
        if score_away is not None:
            try:
                snap.away_score = int(score_away)
            except (TypeError, ValueError):
                pass

    # Calcula minutes_played usando subs do PBP
    _populate_minutes_played(snap, actions, snapshot_period, snapshot_clock_min)
    return snap


def _populate_minutes_played(
    snap: GameSnapshot,
    actions: list[dict],
    snap_period: int,
    snap_clock: float,
) -> None:
    """
    State machine de substituições: rastreia tempo em quadra por player
    até o snapshot. Reusa lógica similar ao aggregate_per_period_with_court_time.
    """
    QUARTER_LEN = 12.0
    OT_LEN = 5.0

    def period_len(p: int) -> float:
        return OT_LEN if p > 4 else QUARTER_LEN

    # Starters começam em quadra no clock 12.0 do Q1
    starters = {pid for pid, ps in snap.players.items() if ps.is_starter}
    on_court = set(starters)

    # intervals[pid][period] = list of [in_clock, out_clock or None]
    intervals: dict[int, dict[int, list[list]]] = defaultdict(lambda: defaultdict(list))
    for pid in starters:
        intervals[pid][1].append([QUARTER_LEN, None])

    current_period = 1

    # Sort actions cronologicamente
    def chrono(a: dict) -> tuple[int, float]:
        try:
            return (int(a.get("period", 0)), -_parse_clock_to_min(a.get("clock", "")))
        except (TypeError, ValueError):
            return (0, 0)

    for action in sorted(actions, key=chrono):
        if _is_after_snapshot(action, snap_period, snap_clock):
            break  # já passou o snapshot

        try:
            ap = int(action.get("period", 0))
        except (TypeError, ValueError):
            continue
        if ap < 1:
            continue

        # Transição de período
        while current_period < ap:
            for pid in list(on_court):
                opens = intervals[pid][current_period]
                if opens and opens[-1][1] is None:
                    opens[-1][1] = 0.0
            current_period += 1
            new_len = period_len(current_period)
            for pid in on_court:
                intervals[pid][current_period].append([new_len, None])

        atype = str(action.get("actionType", "")).lower()
        if atype != "substitution":
            continue

        sub_type = str(action.get("subType", "")).lower()
        try:
            pid = int(action.get("personId", 0))
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue

        clock = _parse_clock_to_min(action.get("clock", ""))
        if sub_type == "out":
            opens = intervals[pid][ap]
            if opens and opens[-1][1] is None:
                opens[-1][1] = clock
            on_court.discard(pid)
        elif sub_type == "in":
            intervals[pid][ap].append([clock, None])
            on_court.add(pid)

    # Catch-up até o snap_period (se snapshot é em período sem ações)
    while current_period < snap_period:
        for pid in list(on_court):
            opens = intervals[pid][current_period]
            if opens and opens[-1][1] is None:
                opens[-1][1] = 0.0
        current_period += 1
        new_len = period_len(current_period)
        for pid in on_court:
            intervals[pid][current_period].append([new_len, None])

    # Fecha intervalos abertos no snap_clock (mesmo period) ou em 0 (anterior)
    close_at = snap_clock if current_period == snap_period else 0.0
    for pid in list(on_court):
        opens = intervals[pid][current_period]
        if opens and opens[-1][1] is None:
            opens[-1][1] = close_at

    # Soma duração por player
    for pid, periods in intervals.items():
        if pid not in snap.players:
            continue
        total = 0.0
        for ivs in periods.values():
            for iv in ivs:
                out_clock = iv[1] if iv[1] is not None else 0.0
                total += max(0.0, iv[0] - out_clock)
        snap.players[pid].minutes_played = round(total, 2)


def extract_final_stats(boxscore: dict) -> dict[int, dict[str, int]]:
    """
    Pega stats FINAIS do box score pra usar como ground truth.
    Returns: {player_id: {points, rebounds, assists, three_pm, minutes}}
    """
    result: dict[int, dict[str, int]] = {}
    for team_key in ("homeTeam", "awayTeam"):
        team = boxscore.get(team_key, {})
        for p in team.get("players", []):
            try:
                pid = int(p.get("personId", 0))
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            stats = p.get("statistics", {})
            result[pid] = {
                "points": int(stats.get("points", 0)),
                "rebounds": int(stats.get("reboundsTotal", 0)),
                "assists": int(stats.get("assists", 0)),
                "three_pm": int(stats.get("threePointersMade", 0)),
                "minutes": _parse_minutes_iso(stats.get("minutes", "")),
                "name": p.get("name", ""),
            }
    return result


def _parse_minutes_iso(min_str: str) -> float:
    """'PT24M30.00S' → 24.5."""
    return _parse_clock_to_min(min_str)
