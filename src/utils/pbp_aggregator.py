"""
Aggregator de Play-By-Play (PBP) → stats por jogador POR PERÍODO.

Usado pra mostrar o split por quarto no card live (Q1 / Q2 / Q3 / Q4 / OT).
A NBA Live API entrega PBP como uma lista plana de eventos:

    {
      "actionNumber": ...,
      "actionType": "2pt" | "3pt" | "freethrow" | "rebound" |
                    "substitution" | ...,
      "subType":    "Jump Shot" | "Driving Layup" | "in" | "out" | ...,
      "shotResult": "Made" | "Missed",         (em ações de arremesso)
      "isFieldGoal": 0 | 1,
      "personId":   <quem fez a ação>,
      "period":     1, 2, 3, 4, 5+ (OT),
      "clock":      "PT08M30.00S"  (8 min 30 seg restantes do período),
      "assistPersonId": <quem assistiu, quando aplicável>,
      ...
    }

Aqui agregamos por (player_id, period):
  - points       = pts de FG made + pts de FT made
  - assists      = count de eventos onde `assistPersonId == player_id` e tiro feito
  - rebounds     = count de actionType == 'rebound'
  - three_pt_made = count de '3pt' com shotResult == 'Made'
  - two_pt_made   = count de '2pt' com shotResult == 'Made'
  - minutes_played = minutos efetivos jogados nesse período (derivado de subs)
  - intervals    = list[[clock_in, clock_out]] dos intervalos em quadra
                   (clock decresce de 12→0, então clock_in > clock_out)

Defensivo contra payloads incompletos: campos faltantes viram 0.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

# Duração padrão do período em minutos (NBA: 12 min regulação, 5 min OT).
QUARTER_LENGTH_MIN = 12.0
OT_LENGTH_MIN = 5.0


def _period_length(period: int) -> float:
    return OT_LENGTH_MIN if period > 4 else QUARTER_LENGTH_MIN


def _parse_clock_to_minutes(clock_str: str) -> float:
    """
    Converte 'PT08M30.00S' → 8.5 minutos restantes do período.
    Defensivo: clock vazio/inválido vira 0 (assume fim de período).
    """
    if not clock_str:
        return 0.0
    match = re.match(r"PT(\d+)M([\d.]+)S", str(clock_str))
    if not match:
        return 0.0
    try:
        minutes = int(match.group(1))
        seconds = float(match.group(2))
        return minutes + seconds / 60.0
    except (ValueError, IndexError):
        return 0.0


def _is_made(action: dict) -> bool:
    """True se a ação é um shot que entrou."""
    return str(action.get("shotResult", "")).lower() == "made"


def _shot_points(action: dict) -> int:
    """
    Pontos do shot (0 se errou).
    NBA Live API às vezes traz `pointsTotal` cumulativo; preferimos derivar
    de actionType pra evitar interpretação errada.
    """
    if not _is_made(action):
        return 0
    atype = str(action.get("actionType", "")).lower()
    if atype == "3pt":
        return 3
    if atype == "2pt":
        return 2
    if atype == "freethrow":
        return 1
    return 0


def aggregate_per_period(
    actions: Iterable[dict],
) -> dict[int, dict[int, dict[str, int]]]:
    """
    Agrupa eventos PBP em stats por (player_id → period → counters).

    Returns:
        {
            <player_id>: {
                <period>: {
                    "points": int, "assists": int, "rebounds": int,
                    "three_pt_made": int, "two_pt_made": int,
                },
                ...
            },
            ...
        }
    Períodos sem ação simplesmente não aparecem (caller decide se renderiza
    "—" / "0" pra eles).
    """
    # nested defaultdict: player_id → period → counter dict
    out: dict[int, dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {
            "points": 0, "assists": 0, "rebounds": 0,
            "three_pt_made": 0, "two_pt_made": 0,
        })
    )

    for a in actions:
        try:
            period = int(a.get("period", 0))
            if period <= 0:
                continue
        except (TypeError, ValueError):
            continue

        atype = str(a.get("actionType", "")).lower()

        # 1) Shot principal: pts pro shooter, conta 3PT/2PT made
        person_id = a.get("personId")
        if person_id and atype in ("2pt", "3pt", "freethrow"):
            try:
                pid = int(person_id)
            except (TypeError, ValueError):
                pid = 0
            if pid > 0:
                bucket = out[pid][period]
                bucket["points"] += _shot_points(a)
                if _is_made(a):
                    if atype == "3pt":
                        bucket["three_pt_made"] += 1
                    elif atype == "2pt":
                        bucket["two_pt_made"] += 1

        # 2) Rebote
        if atype == "rebound" and person_id:
            try:
                pid = int(person_id)
                if pid > 0:
                    out[pid][period]["rebounds"] += 1
            except (TypeError, ValueError):
                pass

        # 3) Assist: conta no jogador que assistiu (não no shooter)
        ast_id = a.get("assistPersonId")
        if ast_id and _is_made(a) and atype in ("2pt", "3pt"):
            try:
                aid = int(ast_id)
                if aid > 0:
                    out[aid][period]["assists"] += 1
            except (TypeError, ValueError):
                pass

    return out


def aggregate_per_period_with_court_time(
    actions: Iterable[dict],
    q1_starters: set[int],
    live_period: int | None = None,
    live_clock_minutes: float | None = None,
) -> dict[int, dict[int, dict]]:
    """
    Mesma agregação que `aggregate_per_period`, mas TAMBÉM rastreia
    minutos jogados + intervalos em quadra por período via state machine
    de substituições.

    Args:
        actions: lista de eventos PBP da NBA Live API
        q1_starters: set de player_ids que começaram o Q1 (do boxscore)
        live_period: período ATUAL do jogo ao vivo (1-4 ou 5+ pra OT).
                     None = jogo finalizado/sem info, fecha tudo em 0.
        live_clock_minutes: minutos restantes no relógio do período atual.
                            Usado pra fechar intervalos abertos no clock
                            correto (sem isso, fecharia em 0 e dava 12 min
                            jogados pro starter mesmo com jogo começando —
                            bug real do Gobert: 0.1 min jogados mas
                            sistema mostrava 12 min no Q1).

    Returns:
        {
            <player_id>: {
                <period>: {
                    "points": int, "assists": int, "rebounds": int,
                    "three_pt_made": int, "two_pt_made": int,
                    "minutes_played": float,    # minutos efetivos no período
                    "intervals": list[list[float]],  # [[clock_in, clock_out], ...]
                                                     # clock_in > clock_out (decrescente)
                },
                ...
            },
            ...
        }

    Notas:
      - Titulares do Q1 começam em quadra no clock_in = 12.0
      - Carrega estado de on_court pra periods seguintes (quem terminou
        Q1 em quadra começa Q2 em quadra)
      - Intervalos de períodos PASSADOS fecham em clock = 0
      - Intervalos do período ATUAL fecham em current_clock_minutes
        (sem isso, sobrestima minutos em jogo ao vivo)
    """
    actions_list = list(actions)

    # Pré-cálculo: stats counters via função existente (reuso)
    base = aggregate_per_period(actions_list)

    # Sort actions cronologicamente: (period asc, clock desc).
    # Clock conta DECRESCENTE (12→0), então maior clock = mais cedo.
    def _chrono_key(a: dict) -> tuple[int, float]:
        try:
            period = int(a.get("period", 0))
        except (TypeError, ValueError):
            period = 0
        clock = _parse_clock_to_minutes(a.get("clock", ""))
        return (period, -clock)

    actions_sorted = sorted(actions_list, key=_chrono_key)

    # Estado: on_court é o set de player_ids atualmente em quadra.
    on_court: set[int] = set(q1_starters)

    # intervals[pid][period] = list de [clock_in, clock_out]
    # clock_out=None significa "ainda aberto" (não saiu ainda)
    intervals: dict[int, dict[int, list[list]]] = defaultdict(lambda: defaultdict(list))

    # Abre intervalo inicial dos titulares do Q1 (entram no clock 12.0)
    for pid in q1_starters:
        intervals[pid][1].append([QUARTER_LENGTH_MIN, None])

    current_period = 1

    for action in actions_sorted:
        try:
            action_period = int(action.get("period", 0))
        except (TypeError, ValueError):
            continue
        if action_period < 1:
            continue

        # Transição de período: fecha opens em 0.0 + reabre no novo período
        while current_period < action_period:
            # Fecha qualquer interval aberto no current_period em clock=0
            for pid in list(on_court):
                opens = intervals[pid][current_period]
                if opens and opens[-1][1] is None:
                    opens[-1][1] = 0.0
            current_period += 1
            # Reabre intervalos no novo período pros que estão on_court
            new_len = _period_length(current_period)
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

        clock = _parse_clock_to_minutes(action.get("clock", ""))

        if sub_type == "out":
            # Fecha interval atual
            opens = intervals[pid][action_period]
            if opens and opens[-1][1] is None:
                opens[-1][1] = clock
            on_court.discard(pid)
        elif sub_type == "in":
            # Abre novo interval
            intervals[pid][action_period].append([clock, None])
            on_court.add(pid)

    # Catch-up de transições: se live_period > current_period (jogo
    # avançou de quarter mas não temos actions ainda no novo quarter),
    # faz as transições agora pra abrir intervalos no live_period.
    if live_period is not None and live_period > current_period:
        while current_period < live_period:
            # Fecha opens no current_period em 0.0
            for pid in list(on_court):
                opens = intervals[pid][current_period]
                if opens and opens[-1][1] is None:
                    opens[-1][1] = 0.0
            current_period += 1
            new_len = _period_length(current_period)
            for pid in on_court:
                intervals[pid][current_period].append([new_len, None])

    # Fim das ações: fecha intervalos abertos no período em que paramos.
    #
    # Se temos info do live (live_period + live_clock):
    #   - Período em que paramos == live_period: fecha em live_clock
    #     (jogo está rolando, cara está em quadra até esse momento)
    #
    # Se NÃO temos info do live (jogo finalizado ou caller antigo):
    #   - Fecha em 0.0 (período supostamente acabou)
    close_at = 0.0
    if (
        live_period is not None
        and live_clock_minutes is not None
        and live_period == current_period
    ):
        close_at = max(0.0, min(live_clock_minutes, _period_length(current_period)))

    for pid in list(on_court):
        opens = intervals[pid][current_period]
        if opens and opens[-1][1] is None:
            opens[-1][1] = close_at

    # Calcula minutos jogados por (pid, period) somando durações dos intervalos
    minutes_played: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for pid, periods in intervals.items():
        for period, ivs in periods.items():
            total = 0.0
            for iv in ivs:
                in_clock = iv[0]
                out_clock = iv[1] if iv[1] is not None else 0.0
                # Duração = clock_in - clock_out (clock decresce)
                duration = max(0.0, in_clock - out_clock)
                total += duration
            minutes_played[pid][period] = round(total, 2)

    # Merge: cada (pid, period) ganha minutes_played + intervals + stats counters
    result: dict[int, dict[int, dict]] = defaultdict(dict)

    # Set de todos os (pid, period) que aparecem em base OU em intervals
    all_pids: set[int] = set(base.keys()) | set(intervals.keys())
    for pid in all_pids:
        # Períodos relevantes pra esse jogador
        base_periods = set(base.get(pid, {}).keys())
        int_periods = set(intervals.get(pid, {}).keys())
        all_periods = base_periods | int_periods
        for period in all_periods:
            stats = dict(base.get(pid, {}).get(period, {
                "points": 0, "assists": 0, "rebounds": 0,
                "three_pt_made": 0, "two_pt_made": 0,
            }))
            stats["minutes_played"] = minutes_played[pid].get(period, 0.0)
            # Filtra intervalos válidos (out fechado, mesmo que 0)
            valid_intervals = [
                [iv[0], iv[1] if iv[1] is not None else 0.0]
                for iv in intervals[pid].get(period, [])
            ]
            stats["intervals"] = valid_intervals
            result[pid][period] = stats

    return dict(result)
