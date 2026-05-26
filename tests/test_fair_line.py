"""
Testes para calculate_fair_line e calculate_edge_decision.

A linha estimada é o coração do synthetic bookmaker. Garantir que:
- Os pesos refletem corretamente forma recente vs temporada
- Arredondamento bate com o formato típico de mercado (.5)
- Linhas mínimas / edges extremos não geram lixo
- Decisões de edge mapeiam direto pros 5 estados de aposta
- Cálculo rico (calculate_estimated_sportsbook_line) integra volume,
  eficiência, blowout, fouls e role
"""
from src.utils.stats import (
    LineContext,
    calculate_edge_decision,
    calculate_estimated_sportsbook_line,
    calculate_fair_line,
    dampen_decision_for_low_sample,
)


# ─── calculate_fair_line ────────────────────────────────────────────────────

def test_consistent_player_line_matches_average():
    """Player que faz exatamente a média em todos os splits = linha ≈ avg."""
    line = calculate_fair_line(season_avg=20.0, last_10_avg=20.0, last_5_avg=20.0)
    # blend = 20.0 (sem vig), arredonda → 20.0
    assert line == 20.0


def test_hot_streak_pushes_line_up():
    """Forma recente acima da média deve subir a linha."""
    cold_line = calculate_fair_line(season_avg=15.0, last_10_avg=15.0, last_5_avg=15.0)
    hot_line  = calculate_fair_line(season_avg=15.0, last_10_avg=18.0, last_5_avg=22.0)
    assert hot_line > cold_line
    # blend = 0.55*15 + 0.30*18 + 0.15*22 = 8.25 + 5.4 + 3.3 = 16.95 → 17.0
    assert hot_line == 17.0


def test_cold_streak_pulls_line_down():
    """Forma ruim recente puxa a linha pra baixo (mas mais devagar — temp ancora)."""
    line = calculate_fair_line(season_avg=18.0, last_10_avg=14.0, last_5_avg=10.0)
    # blend = 0.55*18 + 0.30*14 + 0.15*10 = 9.9 + 4.2 + 1.5 = 15.6 → 15.5
    assert line == 15.5


def test_season_anchors_more_than_recent_form():
    """
    Regressão calibração: temporada deve dominar pesos de recente.
    Antes (30/40/30) este caso voltava 17.5 — agora ancora mais perto da temp.
    """
    # Estrela em mini-slump: season alta, recentes baixos.
    line = calculate_fair_line(season_avg=22.0, last_10_avg=18.0, last_5_avg=14.0)
    # blend = 0.55*22 + 0.30*18 + 0.15*14 = 12.1 + 5.4 + 2.1 = 19.6 → 19.5
    assert line == 19.5


def test_low_volume_player_has_minimum_line():
    """Reserva de fim de banco com 0.2 ppg não vai ter linha de 0 ou negativa."""
    line = calculate_fair_line(season_avg=0.2, last_10_avg=0.1, last_5_avg=0.0)
    assert line >= 0.5
    assert line == 0.5


def test_rounding_to_half():
    """Sempre arredonda pro .5 mais próximo, formato padrão de bookmaker."""
    cases = [
        # (season, last_10, last_5) → expected_line
        # blend = 0.55*s + 0.30*l10 + 0.15*l5 ; arredonda pro .5 mais próximo
        ((4.0, 4.0, 4.0),    4.0),   # blend=4.0 → 4.0
        ((10.0, 11.0, 12.0), 10.5),  # blend=10.6 → 10.5
        ((25.0, 28.0, 30.0), 26.5),  # blend=26.65 → 26.5
    ]
    for (s, l10, l5), expected in cases:
        line = calculate_fair_line(s, l10, l5)
        assert line == expected, f"({s},{l10},{l5}) → esperado {expected}, veio {line}"


# ─── Pisos live (current + projection) ─────────────────────────────────────


def test_line_never_below_current_stat():
    """
    Caso Oubre: blend baixo (slump), mas current já passou. Linha tem
    que pular pra cima do current — não faz sentido oferecer over já
    garantido.
    """
    # Sem pisos: blend = 0.55*17 + 0.30*12 + 0.15*8 = 14.15 → 14.0
    pre_game = calculate_fair_line(season_avg=17, last_10_avg=12, last_5_avg=8)
    assert pre_game == 14.0

    # Com current=19: linha sobe pra 19.5 (current + 0.5 buffer no-push)
    live = calculate_fair_line(
        season_avg=17, last_10_avg=12, last_5_avg=8,
        current_stat=19,
    )
    assert live >= 19.5


def test_line_pace_tracking_pulls_to_projection_when_on_track():
    """
    Caso Oubre on-pace: current=19, projection=21.6. Pace fraction = 0.88
    (>= 0.60). Após recalibração mai/2026 (PEAK 0.95 → 1.05), o anchor
    target vira proj × ~1.02 = 21.97 → arredonda 22.0.

    Bet365 abre essas situações entre 95-105% da projeção dependendo
    do caso. Aceitamos faixa larga aqui — o invariante é só que o
    anchor está puxando pra próximo da projeção, não pra baixo dela.
    """
    line = calculate_fair_line(
        season_avg=17, last_10_avg=12, last_5_avg=8,
        current_stat=19,
        projected_end=21.6,
    )
    assert 21.0 <= line <= 22.5, f"esperado 21.0-22.5, veio {line}"


def test_line_partial_pace_pull_for_cold_player():
    """
    Caso Paul George: current=11, projection=30.3. Pace fraction = 0.363.
    Com gain=1.5 no anchor weight, a projeção domina mais cedo:
      anchor_weight = min(1, 0.363 × 1.5) = 0.545
    Linha resultante puxa pra ~25.5 (matches Bet365 conhecido).
    """
    line = calculate_fair_line(
        season_avg=22, last_10_avg=18, last_5_avg=14,
        current_stat=11,
        projected_end=30.3,
    )
    # blend ≈ 19.6, target = 30.3 × 0.874 = 26.48
    # line = 0.455 × 19.6 + 0.545 × 26.48 ≈ 23.4 → 23.5
    assert 23.0 <= line <= 26.0, f"esperado 23-26, veio {line}"


def test_line_pre_game_no_floors():
    """
    Sem current/projection (chamada pré-jogo), comportamento volta ao
    blend puro.
    """
    line = calculate_fair_line(season_avg=20, last_10_avg=20, last_5_avg=20)
    assert line == 20.0


# ─── calculate_edge_decision ────────────────────────────────────────────────

def test_strong_over_at_three_or_more():
    assert calculate_edge_decision(3.0) == "STRONG_OVER"
    assert calculate_edge_decision(3.5) == "STRONG_OVER"
    assert calculate_edge_decision(10.0) == "STRONG_OVER"


def test_lean_over_starts_at_1_5():
    """
    Thresholds atualizados (mai/2026 — Proposta B):
    LEAN_OVER agora começa em +1.5 (era +1.0). Filtra OVERs marginais.
    """
    assert calculate_edge_decision(1.5) == "LEAN_OVER"
    assert calculate_edge_decision(2.0) == "LEAN_OVER"
    assert calculate_edge_decision(2.9) == "LEAN_OVER"
    # Edge entre 1.0 e 1.5 agora é NEUTRAL
    assert calculate_edge_decision(1.0) == "NEUTRAL"
    assert calculate_edge_decision(1.4) == "NEUTRAL"


def test_neutral_zone():
    assert calculate_edge_decision(0.0) == "NEUTRAL"
    assert calculate_edge_decision(0.5) == "NEUTRAL"
    assert calculate_edge_decision(-1.9) == "NEUTRAL"
    assert calculate_edge_decision(1.4) == "NEUTRAL"
    # Edge LeBron-like (-1.0): agora NEUTRAL (era LEAN_UNDER)
    assert calculate_edge_decision(-1.0) == "NEUTRAL"


def test_lean_under_starts_at_minus_2():
    """
    LEAN_UNDER agora exige edge ≤ -2.0 (era -1.0). Reflete variance
    assimétrica — UNDER precisa de edge maior pra compensar risco.
    """
    assert calculate_edge_decision(-2.0) == "LEAN_UNDER"
    assert calculate_edge_decision(-2.5) == "LEAN_UNDER"
    assert calculate_edge_decision(-3.9) == "LEAN_UNDER"
    # Edge -1 a -2 agora é NEUTRAL
    assert calculate_edge_decision(-1.5) == "NEUTRAL"
    assert calculate_edge_decision(-1.9) == "NEUTRAL"


def test_strong_under_starts_at_minus_4():
    """STRONG_UNDER agora exige edge ≤ -4.0 (era -3.0)."""
    assert calculate_edge_decision(-4.0) == "STRONG_UNDER"
    assert calculate_edge_decision(-5.0) == "STRONG_UNDER"
    # Edge -3 agora é LEAN_UNDER (era STRONG_UNDER)
    assert calculate_edge_decision(-3.0) == "LEAN_UNDER"
    assert calculate_edge_decision(-3.5) == "LEAN_UNDER"


# ─── dampen_decision_for_low_sample (mai/2026 — fix Robinson/Duren) ─────────

def test_dampen_full_sample_unchanged():
    """≥10 min jogados → decisão passa intacta."""
    assert dampen_decision_for_low_sample("STRONG_OVER", 10.0) == "STRONG_OVER"
    assert dampen_decision_for_low_sample("STRONG_OVER", 24.0) == "STRONG_OVER"
    assert dampen_decision_for_low_sample("LEAN_UNDER", 15.0) == "LEAN_UNDER"


def test_dampen_mid_sample_strong_to_lean():
    """
    6–10 min (caso Robinson 6.9 / Duren 7.6): STRONG vira LEAN.
    Mantém a direção (over/under) mas tira o "forte".
    """
    assert dampen_decision_for_low_sample("STRONG_OVER", 6.9) == "LEAN_OVER"
    assert dampen_decision_for_low_sample("STRONG_OVER", 7.6) == "LEAN_OVER"
    assert dampen_decision_for_low_sample("STRONG_UNDER", 8.0) == "LEAN_UNDER"
    # LEAN não muda nessa faixa (já é fraco)
    assert dampen_decision_for_low_sample("LEAN_OVER", 7.0) == "LEAN_OVER"
    assert dampen_decision_for_low_sample("NEUTRAL", 7.0) == "NEUTRAL"


def test_dampen_tiny_sample_everything_to_neutral():
    """<6 min: amostra minúscula → qualquer LEAN/STRONG vira NEUTRAL."""
    assert dampen_decision_for_low_sample("STRONG_OVER", 4.0) == "NEUTRAL"
    assert dampen_decision_for_low_sample("LEAN_OVER", 5.9) == "NEUTRAL"
    assert dampen_decision_for_low_sample("STRONG_UNDER", 3.0) == "NEUTRAL"
    assert dampen_decision_for_low_sample("LEAN_UNDER", 2.0) == "NEUTRAL"
    assert dampen_decision_for_low_sample("NEUTRAL", 4.0) == "NEUTRAL"


def test_dampen_boundary_exactly_6_and_10():
    """Fronteiras: 6.0 entra na faixa LEAN, 10.0 já é full."""
    # 6.0 → faixa 6-10 (STRONG vira LEAN, não NEUTRAL)
    assert dampen_decision_for_low_sample("STRONG_OVER", 6.0) == "LEAN_OVER"
    # 5.99 → faixa <6 (NEUTRAL)
    assert dampen_decision_for_low_sample("STRONG_OVER", 5.99) == "NEUTRAL"
    # 10.0 → full sample (intacto)
    assert dampen_decision_for_low_sample("STRONG_OVER", 10.0) == "STRONG_OVER"


# ─── Cenários reais combinados ──────────────────────────────────────────────

def test_marcus_smart_realistic_scenario():
    """
    Marcus Smart AST. Season 4.0, last_10 4.5, last_5 5.2.
    blend = 0.55*4 + 0.30*4.5 + 0.15*5.2 = 2.2 + 1.35 + 0.78 = 4.33 → 4.5.
    Projeção fim 7 → edge +2.5 → LEAN_OVER (era STRONG_OVER no threshold ±2).
    """
    line = calculate_fair_line(season_avg=4.0, last_10_avg=4.5, last_5_avg=5.2)
    assert line == 4.5
    edge = round(7.0 - line, 1)  # 2.5
    assert calculate_edge_decision(edge) == "LEAN_OVER"


def test_role_player_no_edge():
    """
    Player com forma estável e projeção bate na linha → sem edge → NEUTRAL.
    Ex: cara fazendo 8 ppg constante, projeção 7.8.
    """
    line = calculate_fair_line(season_avg=8.0, last_10_avg=8.2, last_5_avg=7.9)
    # blend = 0.55*8 + 0.30*8.2 + 0.15*7.9 = 4.4 + 2.46 + 1.185 = 8.045 → 8.0
    assert line == 8.0
    edge = round(7.8 - line, 1)  # -0.2
    assert calculate_edge_decision(edge) == "NEUTRAL"


def test_underperforming_player_under_signal():
    """
    Player projetado a finalizar bem abaixo da linha → STRONG_UNDER.
    Threshold STRONG_UNDER agora é -4.0 (mai/2026), então precisamos
    de gap maior que o teste original (-3.0 caía em LEAN_UNDER).
    """
    line = calculate_fair_line(season_avg=20.0, last_10_avg=22.0, last_5_avg=24.0)
    assert line == 21.0
    # Jogador caminha pra 17 — edge -4 (foul trouble + blowout severo)
    edge = round(17.0 - line, 1)  # -4.0
    assert calculate_edge_decision(edge) == "STRONG_UNDER"


# ─── Cálculo rico (calculate_estimated_sportsbook_line) ─────────────────────


def test_rich_line_pre_game_equals_blend():
    """Sem live data, deve dar mesma linha que blend histórico simples."""
    ctx = LineContext(season_avg=20, last_10_avg=20, last_5_avg=20)
    r = calculate_estimated_sportsbook_line(ctx)
    assert r.line == 20.0
    assert r.components["prior"] == 20.0


def test_rich_line_floor_obrigatorio_acima_de_current():
    """REGRA INVIOLÁVEL: linha não pode ficar abaixo do atual."""
    # Cenário: jogador com 12 pts, blend histórico daria linha 10.5
    ctx = LineContext(
        season_avg=10, last_10_avg=10, last_5_avg=10,
        current_stat=12, minutes_played=20, season_minutes=20,
        projected_end=14,
    )
    r = calculate_estimated_sportsbook_line(ctx)
    assert r.line >= 12.5, f"linha {r.line} < piso obrigatório 12.5"


def test_rich_line_volume_signal_pushes_up():
    """
    Volume FGA acima do habitual → linha sobe (com cap).
    Mesmo jogador com volume normal vs +50% volume.
    """
    base_kwargs = dict(
        season_avg=15, last_10_avg=15, last_5_avg=15,
        season_minutes=22, current_stat=8, minutes_played=12,
        season_fga_per_min=0.5,  # 11 FGA/22min
        season_efg=0.5, projected_end=15,
    )
    normal_volume = calculate_estimated_sportsbook_line(LineContext(
        **base_kwargs, current_fga=6, current_fgm=4, current_3pm=0,
    ))
    high_volume = calculate_estimated_sportsbook_line(LineContext(
        **base_kwargs, current_fga=10, current_fgm=4, current_3pm=0,
    ))
    assert high_volume.line >= normal_volume.line
    assert "volume_adj" in high_volume.components


def test_rich_line_low_volume_pulls_down():
    """Pouco arremesso → linha mais conservadora."""
    ctx = LineContext(
        season_avg=15, last_10_avg=15, last_5_avg=15,
        season_minutes=22, current_stat=2, minutes_played=15,
        current_fga=2, current_fgm=1, current_3pm=0,
        season_fga_per_min=0.5,
        season_efg=0.5, projected_end=8,
    )
    r = calculate_estimated_sportsbook_line(ctx)
    # Volume bem abaixo do habitual → adj negativo
    assert r.components.get("volume_adj", 0) < 0


def test_rich_line_blowout_reduces():
    """Blowout severo puxa a linha pra baixo."""
    no = calculate_estimated_sportsbook_line(LineContext(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        current_stat=10, minutes_played=24, season_minutes=32,
        projected_end=20, blowout_severity=0.0,
    ))
    severe = calculate_estimated_sportsbook_line(LineContext(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        current_stat=10, minutes_played=24, season_minutes=32,
        projected_end=20, blowout_severity=1.0,
    ))
    assert severe.line <= no.line
    assert severe.components.get("blowout_adj", 0) < 0


def test_rich_line_foul_trouble_reduces():
    """4+ faltas = penalidade na linha."""
    clean = calculate_estimated_sportsbook_line(LineContext(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        current_stat=12, minutes_played=24, season_minutes=32,
        projected_end=20, fouls=2,
    ))
    fouled = calculate_estimated_sportsbook_line(LineContext(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        current_stat=12, minutes_played=24, season_minutes=32,
        projected_end=20, fouls=5,
    ))
    assert fouled.line <= clean.line
    assert "foul_trouble_adj" in fouled.components


def test_rich_line_ceiling_prevents_overshoot():
    """Linha nunca pode ficar muito acima da projeção sem motivo."""
    ctx = LineContext(
        season_avg=30, last_10_avg=35, last_5_avg=40,  # histórico inflado
        current_stat=2, minutes_played=8, season_minutes=22,
        projected_end=8,  # projeção muito menor que histórico
    )
    r = calculate_estimated_sportsbook_line(ctx)
    # Teto: line <= projection + 3
    assert r.line <= 11.5


def test_rich_line_oubre_case_pace_tracking():
    """
    Caso real Oubre: current=19, projection=21.6, atual ≥ 60% da proj.
    Após recalibração de mai/2026 (PEAK 0.95 → 1.05), a linha é puxada
    pelo anchor direto pra ~projeção × 1.02 (sem precisar do piso).

    Bet365 abriu 20.5 nesse caso específico, mas em casos análogos vimos
    24.5 (Oubre Q3) — a curva de Bet365 varia entre 95-105% da projeção.
    Aceitamos a faixa: o invariante é que estamos perto da projeção,
    não 5pts abaixo.
    """
    ctx = LineContext(
        season_avg=17, last_10_avg=14, last_5_avg=10,
        current_stat=19, minutes_played=30, season_minutes=22,
        projected_end=21.6,
    )
    r = calculate_estimated_sportsbook_line(ctx)
    assert 20.0 <= r.line <= 22.5, f"esperado 20.0-22.5, veio {r.line}"
    # Linha foi influenciada pelo anchor (reason menciona "anchored")
    # OU bateu num floor — qualquer um valida que o tracking está ativo.
    assert (
        r.floor_applied in ("pace", "current")
        or "anchor" in r.reason.lower()
    ), f"reason inesperada: {r.reason}, floor: {r.floor_applied}"


def test_rich_line_components_structure():
    """LineResult sempre traz components nomeados pra debug/UI."""
    ctx = LineContext(
        season_avg=15, last_10_avg=15, last_5_avg=15,
        current_stat=8, minutes_played=20, season_minutes=30,
        projected_end=15,
    )
    r = calculate_estimated_sportsbook_line(ctx)
    # Componentes obrigatórios
    for key in ("prior", "w_season", "w_last_10", "w_last_5", "base_line"):
        assert key in r.components, f"missing component: {key}"
    # Reason sempre presente
    assert r.reason


def test_rich_line_bench_off_court_discount():
    """Reserva no banco: pequeno desconto."""
    starter_active = calculate_estimated_sportsbook_line(LineContext(
        season_avg=10, last_10_avg=10, last_5_avg=10,
        current_stat=4, minutes_played=10, season_minutes=20,
        projected_end=10, is_starter=True, on_court=True,
    ))
    bench_off = calculate_estimated_sportsbook_line(LineContext(
        season_avg=10, last_10_avg=10, last_5_avg=10,
        current_stat=4, minutes_played=10, season_minutes=20,
        projected_end=10, is_starter=False, on_court=False,
    ))
    assert bench_off.line <= starter_active.line


# ─── Fase 3: foul trouble contextual + blowout por papel ──────────────────


def test_foul_trouble_multiplier_by_remaining_time():
    """Quanto mais tempo de jogo resta, maior a proteção do treinador."""
    from src.utils.stats import foul_trouble_multiplier

    # ≤3 faltas: sem ajuste
    assert foul_trouble_multiplier(0, 24) == 1.00
    assert foul_trouble_multiplier(3, 24) == 1.00

    # 4 faltas: penaliza mais quando há tempo
    assert foul_trouble_multiplier(4, 24) == 0.75   # Q3 ou antes (>18min)
    assert foul_trouble_multiplier(4, 12) == 0.85   # Q4 início (8-18min)
    assert foul_trouble_multiplier(4, 5)  == 0.92   # Q4 final (≤8min)

    # 5 faltas: penaliza forte com tempo, modera no crunch
    assert foul_trouble_multiplier(5, 12) == 0.50
    assert foul_trouble_multiplier(5, 4)  == 0.70

    # 6+ = fouled out
    assert foul_trouble_multiplier(6, 12) == 0.20


def test_rich_line_foul_4f_q3_kat_case():
    """
    Caso KAT (NYK Q3, mai/2026): 4F com 24min de jogo restante.
    Proj 14.6 mas Bet365 11.5 — foul trouble agressivo.

    Após Fase 3, o multiplicador 0.75 é aplicado ao valor restante
    (line - current). Linha resultante deve ficar bem abaixo de 14.6.
    """
    ctx = LineContext(
        season_avg=24, last_10_avg=24, last_5_avg=24,
        season_minutes=33,
        current_stat=8, minutes_played=15.8,
        season_fga_per_min=0.55, season_efg=0.55,
        current_fga=8, current_fgm=3, current_3pm=1,
        projected_end=14.6,
        fouls=4, game_minutes_remaining=24,
        is_starter=True, on_court=False,  # foul trouble = banco
    )
    r = calculate_estimated_sportsbook_line(ctx)
    # Multiplicador 0.75 reduz valor restante (line-current) em 25%.
    # Linha deve cair pelo menos 1.5pt do que seria sem foul.
    ctx_clean = LineContext(
        season_avg=24, last_10_avg=24, last_5_avg=24,
        season_minutes=33,
        current_stat=8, minutes_played=15.8,
        season_fga_per_min=0.55, season_efg=0.55,
        current_fga=8, current_fgm=3, current_3pm=1,
        projected_end=14.6,
        fouls=2, game_minutes_remaining=24,
        is_starter=True, on_court=True,
    )
    r_clean = calculate_estimated_sportsbook_line(ctx_clean)
    assert r.line < r_clean.line - 0.5, (
        f"linha com 4F={r.line} deveria ficar bem abaixo de clean={r_clean.line}"
    )
    assert r.components.get("foul_multiplier") == 0.75


def test_rich_line_foul_4f_late_q4_minor_impact():
    """
    Mesmo 4F mas com 5min restantes (crunch time): penalidade mínima
    porque coach deixa jogar.
    """
    ctx_crunch = LineContext(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        season_minutes=32,
        current_stat=18, minutes_played=30,
        projected_end=21,
        fouls=4, game_minutes_remaining=5,
        is_starter=True, on_court=True,
    )
    ctx_early = LineContext(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        season_minutes=32,
        current_stat=8, minutes_played=12,
        projected_end=21,
        fouls=4, game_minutes_remaining=24,
        is_starter=True, on_court=True,
    )
    r_crunch = calculate_estimated_sportsbook_line(ctx_crunch)
    r_early = calculate_estimated_sportsbook_line(ctx_early)
    # Crunch deveria ter penalty muito menor (mult 0.92 vs 0.75)
    assert r_crunch.components["foul_multiplier"] == 0.92
    assert r_early.components["foul_multiplier"] == 0.75


def test_rich_line_blowout_starter_vs_bench():
    """Titular perde mais minutos em blowout que reserva."""
    common = dict(
        season_avg=15, last_10_avg=15, last_5_avg=15,
        current_stat=8, minutes_played=20, season_minutes=30,
        projected_end=15, blowout_severity=0.8,
    )
    starter = calculate_estimated_sportsbook_line(LineContext(
        **common, is_starter=True, on_court=True,
    ))
    bench = calculate_estimated_sportsbook_line(LineContext(
        **common, is_starter=False, on_court=True,
    ))
    # Titular deveria ter penalty MAIOR (factor 0.30 vs 0.10)
    starter_adj = starter.components.get("blowout_adj", 0)
    bench_adj = bench.components.get("blowout_adj", 0)
    assert starter_adj < bench_adj, (
        f"titular ({starter_adj}) deveria ter penalty maior que reserva ({bench_adj})"
    )


# ─── Fase 5: matchup context ──────────────────────────────────────────────


def test_rich_line_matchup_weak_defense_pushes_up():
    """Adversário com defesa fraca (DRtg alto → factor > 1.0) sobe a linha."""
    base = dict(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        current_stat=10, minutes_played=20, season_minutes=32,
        projected_end=22,
    )
    neutral = calculate_estimated_sportsbook_line(LineContext(**base))
    weak_def = calculate_estimated_sportsbook_line(LineContext(
        **base,
        opponent_drtg_factor=1.10,  # +10% DRtg = defesa pior
    ))
    assert weak_def.line >= neutral.line, (
        f"weak_def ({weak_def.line}) deveria >= neutral ({neutral.line})"
    )
    assert weak_def.components.get("matchup_combined_factor", 1.0) > 1.0


def test_rich_line_matchup_strong_defense_pulls_down():
    """Adversário com defesa boa (factor < 1.0) reduz a linha."""
    base = dict(
        season_avg=20, last_10_avg=20, last_5_avg=20,
        current_stat=10, minutes_played=20, season_minutes=32,
        projected_end=22,
    )
    neutral = calculate_estimated_sportsbook_line(LineContext(**base))
    strong_def = calculate_estimated_sportsbook_line(LineContext(
        **base,
        opponent_drtg_factor=0.90,  # -10% DRtg = defesa boa
    ))
    assert strong_def.line <= neutral.line


def test_matchup_provider_neutral_fallback():
    """MatchupProvider devolve neutro quando team_tricode é vazio."""
    from src.services.matchup import MatchupProvider
    p = MatchupProvider()
    ctx = p.get("")
    assert ctx.is_neutral
    assert ctx.drtg_factor == 1.0
    assert ctx.pace_factor == 1.0


def test_matchup_context_factor_clamps():
    """drtg_factor clamped em ±15%, pace ±10%."""
    from src.services.matchup import MatchupContext
    extreme_high = MatchupContext(
        team_tricode="TEST", drtg=200, pace=200, ortg=100,
    )
    assert extreme_high.drtg_factor == 1.15  # clamp
    assert extreme_high.pace_factor == 1.10  # clamp
    extreme_low = MatchupContext(
        team_tricode="TEST", drtg=50, pace=50, ortg=100,
    )
    assert extreme_low.drtg_factor == 0.85
    assert extreme_low.pace_factor == 0.90
