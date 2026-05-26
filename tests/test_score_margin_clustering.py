"""
Testes pra item 9 (mai/2026): clutch/blowout via margem real do placar.

Substitui o self-clustering pelo Q4 do jogador (frágil em casos de saída
por foul) por classificação baseada na margem final do jogo (PBP).
"""

from src.services.rotation.nbarotations_parser import GameRotationEntry
from src.services.rotation.rotation_derivation import (
    BLOWOUT_MARGIN,
    CLOSE_GAME_MARGIN,
    derive_clutch_and_blowout,
)


def _entry(gamecode: str, q4_avg: float, q1q3_avg: float = 0.5) -> GameRotationEntry:
    """Histograma sintético: q1q3 = primeiros 36 mins, q4 = últimos 12."""
    return GameRotationEntry(
        gamedate="2026-04-01",
        gamecode=gamecode,
        opponent="OPP",
        histogram=[q1q3_avg] * 36 + [q4_avg] * 12,
    )


def test_real_margin_overrides_self_clustering_for_close():
    """
    Cenário: jogador saiu por foul no Q4 (q4_avg=0.20) MAS o jogo foi
    decidido por 4 pts. Self-clustering classificaria como blowout (errado);
    margem real diz close → clutch metrics populadas.
    """
    games = [
        _entry("g1", q4_avg=0.20),
        _entry("g2", q4_avg=0.20),
        _entry("g3", q4_avg=0.20),
        _entry("g4", q4_avg=0.20),
        _entry("g5", q4_avg=0.20),
    ]
    margins = {g.gamecode: 4 for g in games}  # ≤ CLOSE_GAME_MARGIN

    clutch, blowout = derive_clutch_and_blowout(games, margins=margins)

    # Q4 baixo → close_game_minutes_probability NÃO é alto (jogador saiu),
    # mas o cluster "close" inclui esses jogos. fourth_quarter_usage_rate
    # reflete isso (média do Q4 nos jogos classificados como close).
    assert clutch.fourth_quarter_usage_rate > 0
    # Não tem "blowout" pra computar typical_minutes_lost
    assert blowout.typical_minutes_lost_in_blowout == 0.0


def test_real_margin_falls_back_when_too_few_provided():
    """
    Quando margins cobre < MIN_GAMES_WITH_MARGIN, cai no self-clustering
    pra TODOS os jogos (não mistura). Garante consistência.
    """
    games = [
        _entry("g1", q4_avg=0.80),  # self → close
        _entry("g2", q4_avg=0.80),
        _entry("g3", q4_avg=0.80),
        _entry("g4", q4_avg=0.20, q1q3_avg=0.50),  # self → blowout
        _entry("g5", q4_avg=0.80),
    ]
    # Só 1 game com margem real (< MIN_GAMES_WITH_MARGIN=5)
    margins = {"g1": 4}

    clutch, _ = derive_clutch_and_blowout(games, margins=margins)
    # Self-clustering pegou 4 jogos como close (q4_avg=0.8) e 1 como blowout.
    # close_game_minutes_probability deve refletir esse Q4 alto.
    assert clutch.fourth_quarter_usage_rate >= 0.7


def test_real_margin_blowout_reduces_minutes_lost():
    """Margem real ≥ BLOWOUT_MARGIN → cluster blowout, minutos perdidos calculáveis."""
    # 5 jogos blowout (q4_avg=0.2, q1q3=0.5) + 5 jogos normais (q4_avg=0.5)
    blowout_games = [_entry(f"b{i}", q4_avg=0.20, q1q3_avg=0.50) for i in range(5)]
    normal_games = [_entry(f"n{i}", q4_avg=0.50, q1q3_avg=0.50) for i in range(5)]
    games = blowout_games + normal_games
    margins = {g.gamecode: 25 for g in blowout_games}     # > BLOWOUT_MARGIN
    margins.update({g.gamecode: 10 for g in normal_games})  # entre os dois

    _, blowout = derive_clutch_and_blowout(games, margins=margins)
    # Blowout games tiveram Q4 baixo → fourth_quarter_return_probability baixo
    assert blowout.fourth_quarter_return_probability_when_blowout < 0.30
    # Total de minutos do blowout < normal (Q4 baixo) → minutes_lost > 0
    assert blowout.typical_minutes_lost_in_blowout > 0


def test_no_margins_uses_self_clustering_unchanged():
    """Sem margins → comportamento legado (self-clustering)."""
    games = [_entry(f"g{i}", q4_avg=0.80) for i in range(5)]

    clutch, _ = derive_clutch_and_blowout(games, margins=None)
    assert clutch.fourth_quarter_usage_rate >= 0.7


def test_margin_thresholds_bracket_close_and_blowout():
    """Garante que os thresholds estão coerentes (close ≤ blowout)."""
    assert CLOSE_GAME_MARGIN < BLOWOUT_MARGIN
