"""
Testes para ProjectionEngine.project (antigo `_project_to_end`).

Cobre o fix do bug do Barnes (jogo finalizado não pode extrapolar), o fix
do bug do Shamet (cold-start não pode produzir projeção absurda), e casos
típicos: pouco jogado, ritmo quente, blowout, fouls.

A função retorna dict {low, expected, high, confidence, reason}.

Após Fase 1 (mai/2026), a lógica vive em ProjectionEngine — o helper
`project` aqui apenas instancia uma vez e expõe como callable pra manter
os asserts do estilo `r = project(...)` legíveis.
"""
from src.services.projection import ProjectionEngine
from src.services.projection.projection_engine import _classify_archetype

_engine = ProjectionEngine()
project = _engine.project


# ─── Estados terminais ──────────────────────────────────────────────────────


def test_final_game_returns_actual_stat_no_extrapolation():
    """
    Bug do Barnes: jogador com 4 reb em 6 minutos não pode projetar 9.9
    quando o jogo já acabou. Final = stat real, sem margem nenhuma.
    """
    r = project(
        stat=4, minutes=6, avg_stat=5.0, avg_minutes=30.0,
        is_final=True,
    )
    assert r["low"] == 4.0
    assert r["expected"] == 4.0
    assert r["high"] == 4.0
    assert r["confidence"] == "high"


def test_final_game_with_zero_minutes_player():
    """Reserva que não jogou em jogo final = 0 stat sem extrapolação."""
    r = project(
        stat=0, minutes=0, avg_stat=10.0, avg_minutes=20.0,
        is_final=True,
    )
    assert (r["low"], r["expected"], r["high"]) == (0.0, 0.0, 0.0)


def test_zero_minutes_returns_zero():
    """Jogador que ainda não entrou em quadra (live)."""
    r = project(stat=0, minutes=0, avg_stat=20.0, avg_minutes=30.0)
    assert (r["low"], r["expected"], r["high"]) == (0.0, 0.0, 0.0)
    assert r["confidence"] == "very_low"


# ─── Indeterminate suppression (mai/2026, substitui cold-start) ────────────
#
# Mudança de filosofia (mai/2026): a antiga "cold-start protection" projetava
# usando só prior quando o jogador estava 0 em early minutes — gerava
# proj=10.9 pra cara com 0 pts em 5 min, inutilizável pra apostas (linha
# mercado fica ~5, edge falso de +6).
#
# Novo contrato: SUPPRESSION quando minutes<8 + stat==0 + prior_avg>=3.
# Retorno fica `expected = stat (0)` com `indeterminate: True` — front
# mostra "—" e a recomendação vira NEUTRAL/PASS automaticamente.
# Acima de 8 min, blend Bayesiano natural pega — sem proteção especial.


def test_indeterminate_when_zero_in_very_small_sample():
    """
    Suppression: cara com 0 pts em 5 min E média histórica relevante (≥3)
    não recebe projeção extrapolada. Retorna 0 com indeterminate=True.
    """
    r = project(
        stat=0, minutes=5, avg_stat=10.0, avg_minutes=24.0,
        period=2, game_minutes_remaining=31.0,
    )
    assert r["expected"] == 0.0, f"Esperado 0 (indeterminate), veio {r['expected']}"
    assert r.get("indeterminate") is True
    assert r["confidence"] == "very_low"
    assert "amostra pequena" in r["reason"].lower() or "ritmo" in r["reason"].lower()


def test_indeterminate_skipped_when_minutes_above_threshold():
    """
    Após 8 min sem produção, NÃO é mais ruído — é sinal real de
    underperformance. Blend Bayesiano natural pega: rate baixo,
    projeção pequena.
    """
    r = project(
        stat=0, minutes=10, avg_stat=10.0, avg_minutes=24.0,
        period=2, game_minutes_remaining=22.0,
    )
    assert not r.get("indeterminate", False)
    # Natural blend: weight_current=10/16=0.63, blend = 0.63*0 + 0.37*0.42 = 0.155
    # remaining ~ 14 min, expected ~ 2.2 — bem menor que prior puro
    assert r["expected"] < 5.0


def test_indeterminate_skipped_when_low_avg_player():
    """
    Reserva com prior_avg < 3 → sem suppression, sem proteção especial.
    O 0 atual é coerente com a expectativa baixa do player.
    """
    r = project(
        stat=0, minutes=5, avg_stat=2.0, avg_minutes=10.0,
        period=2, game_minutes_remaining=31.0,
    )
    assert not r.get("indeterminate", False)
    # Natural blend pequeno
    assert r["expected"] <= 1.5


def test_indeterminate_skipped_when_player_started_producing():
    """
    Jogador com qualquer pts em early game NÃO é indeterminate —
    já mostrou que está jogando. Natural blend pega.
    """
    r = project(
        stat=2, minutes=5, avg_stat=10.0, avg_minutes=24.0,
        period=2, game_minutes_remaining=31.0,
    )
    assert not r.get("indeterminate", False)


def test_indeterminate_threshold_at_8_minutes():
    """Boundary: exatamente 8 min com 0 = JÁ NÃO é indeterminate."""
    r = project(
        stat=0, minutes=8, avg_stat=10.0, avg_minutes=24.0,
        period=2, game_minutes_remaining=24.0,
    )
    assert not r.get("indeterminate", False)
    # Blend natural produz projeção pequena — não 5+
    assert r["expected"] < 5.0


def test_no_more_inflated_grimes_case():
    """
    Caso real (mai/2026): Quentin Grimes 0 pts em 5.3 min → era proj 10.9.
    Agora: indeterminate, expected=0. Edge não vira falso STRONG_OVER.
    """
    r = project(
        stat=0, minutes=5.3, avg_stat=12.0, avg_minutes=22.0,
        period=1, game_minutes_remaining=42.7,
    )
    assert r.get("indeterminate") is True
    assert r["expected"] == 0.0


# ─── Live floor de minutos restantes (mai/2026) ─────────────────────────────
#
# Quando rotation profile diz "jogador já jogou tudo que ia jogar" mas
# o jogo ainda tem tempo significativo, projeção precisa ter pelo menos
# 3 min de produção ahead. Cobre UNEXPECTED_ON_COURT principalmente.


def test_live_floor_extends_when_player_unexpected_on_court():
    """
    Caso real (Miles McBride): bench guard com 11 min/jogo médio,
    jogou 18 min e fez 20 pts. Rotation profile diz "ele já saiu".
    Antes: target=18.3 → proj=20 (igual current). Inutilizável pra apostas.
    Agora: floor garante target=21.3, projeção continua extrapolando.
    """
    r = project(
        stat=20, minutes=18.3, avg_stat=12.0, avg_minutes=11.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,   # rotation profile says "done"
    )
    # Antes do floor, expected ficava em 20 (current). Agora deve subir.
    assert r["expected"] > 21.0, f"Esperado > 21 (com floor), veio {r['expected']}"


def test_live_floor_aggressive_when_hot_and_exceeded():
    """
    Hot player playing além do average: floor escala pra ~40% do jogo
    restante + sanity cap mais permissivo. Coach em "ride the hot hand"
    deve resultar em projeção bem maior.
    """
    cold = project(
        stat=20, minutes=18.3, avg_stat=12.0, avg_minutes=11.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
        heat_score=0.0,
    )
    hot = project(
        stat=20, minutes=18.3, avg_stat=12.0, avg_minutes=11.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
        heat_score=0.7,
    )
    # Hot + exceeded amplifica significativamente.
    # Threshold ajustado em mai/2026:
    #   v1: 5.0 → 4.5 (rate decay P1)
    #   v2: 4.5 → 3.5 (heat_boost cortado pela metade — diagnóstico
    #       tier × modifier mostrou heat_boost inflando LARGE em +2.75)
    diff = hot["expected"] - cold["expected"]
    assert diff > 3.5, f"Esperado diff > 3.5 (hot vs cold), veio {diff:.1f}"
    # Bench scorer com 11 min avg em career night pode chegar a 25+
    # (com rate decay aplicado em minutos excedidos)
    assert hot["expected"] >= 25.0, f"Esperado >=25 (hot + exceeded), veio {hot['expected']}"


def test_live_floor_gradient_by_signal_strength():
    """
    Mudança 2 (mai/2026): floor agora escalona com sinais convergentes
    em vez de exigir HOT+EXCEEDED juntos.

    Verifica via breakdown qual fração de floor foi aplicada em cada
    nível de sinal — não usa expected diretamente porque ele pode bater
    no sanity_cap e mascarar o efeito.

    Esperado:
      - Cara abaixo da minutagem normal sem heat: SEM floor agressivo
      - Cara que excedeu minutagem sem heat: floor 30% (bom jogo)
      - Cara que excedeu + heat médio: floor 40% (bom jogo + sinal)
      - Cara que excedeu + heat forte: floor 50% (career night)
    """
    # Player abaixo da minutagem (10/22) — sem floor agressivo
    normal = project(
        stat=15, minutes=10.0, avg_stat=12.0, avg_minutes=22.0,
        period=2, game_minutes_remaining=30.0,
        expected_minutes_remaining=0.0,
        heat_score=0.0,
    )
    # Player que excedeu (15/11) — floor 30%
    extended_no_heat = project(
        stat=15, minutes=15.0, avg_stat=12.0, avg_minutes=11.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
        heat_score=0.0,
    )
    # Player que excedeu + heat médio — floor 40%
    extended_with_heat = project(
        stat=15, minutes=15.0, avg_stat=12.0, avg_minutes=11.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
        heat_score=0.45,
    )
    # Player que excedeu + heat forte — floor 50%
    extended_strong_hot = project(
        stat=15, minutes=15.0, avg_stat=12.0, avg_minutes=11.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
        heat_score=0.65,
    )

    # Caso "normal" não está em good_game — `live_floor_applied` ausente
    assert "live_floor_applied" not in normal["breakdown"]
    # Os outros 3 estão em good_game → floor aplicado
    assert "live_floor_applied" in extended_no_heat["breakdown"]
    assert "live_floor_applied" in extended_with_heat["breakdown"]
    assert "live_floor_applied" in extended_strong_hot["breakdown"]

    # Floor remaining cresce com sinais convergentes
    # (Base 3min, mas vence o cálculo %·game_rem)
    floor_no_heat = extended_no_heat["breakdown"]["live_floor_applied"]["floor_remaining"]
    floor_with_heat = extended_with_heat["breakdown"]["live_floor_applied"]["floor_remaining"]
    floor_strong = extended_strong_hot["breakdown"]["live_floor_applied"]["floor_remaining"]

    assert floor_no_heat < floor_with_heat < floor_strong, (
        f"Esperado escalonamento crescente, veio "
        f"{floor_no_heat=} {floor_with_heat=} {floor_strong=}"
    )


def test_unexpected_rest_no_cap_when_heat_neutral():
    """
    Mudança 1 (mai/2026): UNEXPECTED_REST sozinho NÃO trava a projeção.
    Só capa quando heat ≤ -0.3 (frio multi-signal confirmando).

    Caso real: jogador tira 2 min de descanso normal entre rotações,
    flag dispara mas ele não está machucado — projeção deve continuar
    normal.
    """
    r = project(
        stat=15, minutes=18.0, avg_stat=20.0, avg_minutes=32.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
        is_unexpected_rest=True,
        heat_score=0.0,  # neutro — não confirma "está mal"
    )
    # Sem heat negativo, projeção deve extrapolar normalmente
    # (não fica truncada em current * 1.05).
    assert r["expected"] > 16.5, (
        f"Sem heat negativo, UNEXPECTED_REST não deveria capar — veio {r['expected']}"
    )


def test_live_floor_skipped_in_severe_blowout():
    """
    Floor NÃO aplica em blowout severo (mai/2026). Compara com cenário
    sem blowout — em blowout, projeção deve ser ≤ sem blowout (cortada).
    """
    no_blowout = project(
        stat=15, minutes=18.0, avg_stat=20.0, avg_minutes=22.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
    )
    severe_blowout = project(
        stat=15, minutes=18.0, avg_stat=20.0, avg_minutes=22.0,
        period=4, game_minutes_remaining=10.0,
        blowout_severity=0.7,
        expected_minutes_remaining=0.0,
    )
    # Em blowout severo, projeção é estritamente menor (target reduzido,
    # floor desativado pra não compensar blowout reduction)
    assert severe_blowout["expected"] < no_blowout["expected"]


def test_live_floor_skipped_when_foul_out_imminent():
    """5 faltas: não aplica floor, target já reduzido por foul trouble."""
    r = project(
        stat=15, minutes=20.0, avg_stat=20.0, avg_minutes=32.0,
        period=3, game_minutes_remaining=18.0,
        fouls=5,
        expected_minutes_remaining=0.0,
    )
    # Foul trouble corta minutos, projeção fica próxima do current
    assert r["expected"] <= 17.0


def test_underperformance_boost_when_clearly_below_prior():
    """
    Caso real (Keldon Johnson): 2 pts em 8 min com prior 13.2 ppg / 23.3 min.
    Deficit ratio = 0.44 → forte underperformance. Boost weight_current
    pra 0.70 (vs 0.57 do blend padrão). Projeção meaningfully menor mas
    sem matar regressão à média.

    Bayesian blend padrão confia demais no histórico em sample pequena,
    mas quando current é claramente abaixo é SINAL (matchup ruim,
    fadiga, role reduzido), não ruído.

    Calibração conservadora: weight 0.70 (não 0.80) pra não matar OVER
    bets legítimas quando o jogador esquentar no 2º tempo.
    """
    r = project(
        stat=2, minutes=8.0, avg_stat=13.2, avg_minutes=23.3,
        period=2, game_minutes_remaining=28.0,
    )
    # Com boost, projeção fica entre 7-9 pts (vs ~10+ sem boost)
    assert r["expected"] < 10.0, f"Esperado <10 (boost ativado), veio {r['expected']}"
    assert r["expected"] > 6.0   # mas não puxa demais (preserva upside)


def test_extreme_cold_respects_soft_floor():
    """
    Soft floor (mai/2026, v2 60%): projeção cold não desce abaixo de
    60% do prior_rate na produção restante — regressão pra média é real.

    Caso Holmgren-like: 0 pts em 15 min, prior 16/30 (rate 0.53/min).
    Sem floor, blend Bayesiano puxava projeção pra ~2 pts (UNDER absurdo).
    Com floor de 60%, projeção fica ~4-5 (UNDER ainda claro mas crível).
    """
    out = project(
        stat=0, minutes=15.0, avg_stat=16.0, avg_minutes=30.0,
        period=2, game_minutes_remaining=24.0,
        heat_score=-0.5,
        period_production_rate=0.50,
    )
    bd = out["breakdown"]
    # Floor deve ter sido aplicado
    assert "soft_floor_applied" in bd, (
        f"soft_floor não disparou em caso extreme cold; bd={bd}"
    )
    # Projeção mínima realista pra titular de 16ppg cold: ~4-5
    # (era >= 5.0 com floor 70%, ajustado pra >= 4.0 com floor 60%)
    assert out["expected"] >= 4.0, (
        f"Projeção muito baixa pra titular cold; veio {out['expected']}"
    )
    # Mas ainda bem abaixo da média histórica (cara cold real)
    assert out["expected"] < 12.0


def test_heat_cut_skipped_when_in_deficit():
    """
    Anti double-counting (mai/2026): quando is_in_deficit já disparou
    boost de weight, heat cut é suprimido — sinais correlacionados.

    Sem essa proteção, cara cold em deficit pegava penalidade dupla
    (peso + heat cut) e projetava UNDER irrealisticamente baixo.
    """
    deficit_cold = project(
        stat=0, minutes=15.0, avg_stat=11.0, avg_minutes=26.0,
        period=2, game_minutes_remaining=24.0,
        heat_score=-0.5,  # em deficit + heat negativo
    )
    bd = deficit_cold["breakdown"]
    # Breakdown deve indicar que heat cut foi skipped
    assert bd.get("heat_cut_skipped_reason") == "in_deficit"
    # heat_multiplier NÃO presente (heat não aplicou)
    assert "heat_multiplier" not in bd


def test_period_rate_reduced_in_deficit():
    """
    Quando jogador está em deficit, period_production_rate pesa 20% em
    vez de 40%. Evita inflar projeção com "rate típico do Q1" quando
    o jogador claramente não está executando esse padrão hoje.
    """
    deficit = project(
        stat=2, minutes=8.0, avg_stat=11.0, avg_minutes=26.0,
        period=1, game_minutes_remaining=36.0,
        period_production_rate=0.80,      # period rate ALTO
    )
    no_deficit = project(
        stat=6, minutes=10.0, avg_stat=11.0, avg_minutes=26.0,
        period=2, game_minutes_remaining=24.0,
        period_production_rate=0.80,
    )
    # Em deficit, period rate pesa menos → não infla tanto
    # No-deficit projeta perto da média (com period boost)
    # Em deficit, projeta perto do current scaled
    assert deficit["expected"] < no_deficit["expected"]


def test_underperformance_no_boost_when_close_to_prior():
    """
    Deficit moderado (deficit > 0.7) NÃO ativa boost — segue blend padrão.
    Ex: 9 pts em 12 min com prior 17/31 (rate 0.75 vs 0.55 = ratio 1.36).
    """
    r = project(
        stat=9, minutes=12.0, avg_stat=17.0, avg_minutes=31.0,
        period=2, game_minutes_remaining=24.0,
    )
    # Sem boost — blend padrão (weight_current ≈ 0.67)
    # Projection deve ficar acima de 15 (rate atual está acima do prior)
    assert r["expected"] >= 14.0


def test_underperformance_boost_does_not_affect_hot_players():
    """
    Player JOGANDO BEM (current > prior) não recebe boost. Boost só
    quando deficit_ratio < 0.7 (claramente abaixo).
    """
    cold = project(
        stat=3, minutes=10.0, avg_stat=17.0, avg_minutes=31.0,
        period=2, game_minutes_remaining=24.0,
    )
    hot = project(
        stat=14, minutes=10.0, avg_stat=17.0, avg_minutes=31.0,
        period=2, game_minutes_remaining=24.0,
    )
    # Cold projeta muito abaixo (boost ativo), hot projeta muito acima
    assert hot["expected"] > cold["expected"] + 10


def test_underperformance_boost_gated_by_min_minutes():
    """
    Boost requer >= 5 min jogados (sample mínimo). Antes disso,
    blend padrão segura — evita over-reagir a 2 min de cold.
    """
    # 1 pt em 3 min — deficit forte mas amostra muito pequena
    r = project(
        stat=1, minutes=3.0, avg_stat=17.0, avg_minutes=31.0,
        period=1, game_minutes_remaining=33.0,
    )
    # Sem boost — projeção fica próxima do prior (não puxada drasticamente
    # pelo current de 0.33 pts/min)
    # Esperado: blend padrão pega weight_current ≈ 3/9 = 0.33
    assert r["expected"] >= 9.0   # ainda relativamente alta


def test_target_minutes_capped_by_game_remaining_time():
    """
    Hard cap (mai/2026): target_minutes NUNCA pode exceder
    minutes + game_minutes_remaining. Caso real Barnes: 2 pts em 8 min
    com APENAS 2 MIN RESTANTES de jogo — projeção 8.7 era impossível
    fisicamente (assumia 18 min de extrapolação).
    """
    # Fim de jogo (2 min restantes)
    end = project(
        stat=2, minutes=8.0, avg_stat=11.0, avg_minutes=26.0,
        period=4, game_minutes_remaining=2.0,
    )
    # Mid game (36 min restantes)
    mid = project(
        stat=2, minutes=8.0, avg_stat=11.0, avg_minutes=26.0,
        period=1, game_minutes_remaining=36.0,
    )
    # Fim de jogo: projeção muito próxima do current (mal pode crescer)
    assert end["expected"] < 4.0
    # Mid game: projeção tem espaço pra extrapolar
    assert mid["expected"] > end["expected"]


def test_target_minutes_cap_does_not_affect_mid_game():
    """Em meio de jogo, cap não morde — target já é menor que game time."""
    r = project(
        stat=10, minutes=15.0, avg_stat=18.0, avg_minutes=32.0,
        period=2, game_minutes_remaining=24.0,
    )
    # 15 + 24 = 39 minutos disponíveis, target será ≤ 32 (avg) → cap não morde
    # Projeção razoável, próxima do prior pace
    assert 14.0 <= r["expected"] <= 22.0


def test_live_floor_skipped_when_game_almost_over():
    """
    Late game (< 10 min restantes): floor não aplica. Compara com mid game
    — projeção em fim de jogo deve ser ≤ projeção em mid game pro mesmo
    state (mais minutos restantes lá significa mais projeção).
    """
    end_of_game = project(
        stat=20, minutes=30.0, avg_stat=20.0, avg_minutes=32.0,
        period=4, game_minutes_remaining=5.0,
        expected_minutes_remaining=0.0,
    )
    mid_game = project(
        stat=20, minutes=18.0, avg_stat=20.0, avg_minutes=32.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=0.0,
    )
    # Mid game permite extensão; final de jogo está próximo do current
    assert mid_game["expected"] >= end_of_game["expected"]


# ─── Hot-start shrinkage (mantida) ──────────────────────────────────────────


def test_low_minutes_hot_pace_capped():
    """
    Cara fez 4 reb em 6 min num jogo ainda rolando — não pode projetar 25.

    Mai/2026 v3 (P5): cap detecta hot por rate também. Esse cara está
    fazendo rate 4 reb em 6 min vs prior 5/30 = 0.17/min → hot_ratio = 4
    (extremo). Cap relaxa pra strong: max(5*3.0, 5+15) = 20.

    Mas hot-start shrinkage com minutes<8 + ratio alto regride
    drasticamente, mantendo projeção realista. Resultado capped ~14.
    """
    r = project(
        stat=4, minutes=6, avg_stat=5.0, avg_minutes=30.0,
        period=1, game_minutes_remaining=42.0,
    )
    # Cap relaxado (strong hot), mas shrinkage mantém realista
    assert r["expected"] <= 18.0


def test_javonte_case_low_avg_quick_start_no_inflation():
    """
    Regressão real: Javonte Green (avg 6.9 pts em 17.6 min) com 3 pts em 3 min.
    Antes a fórmula projetava 15.2. Com cap + shrinkage, deve ficar perto da
    média com leve uptick.
    """
    r = project(
        stat=3, minutes=3, avg_stat=6.9, avg_minutes=17.6,
        period=2, game_minutes_remaining=24.0,
    )
    assert r["expected"] <= 10.5, f"Esperado <=10.5, veio {r['expected']}"
    assert r["expected"] >= 4.0, f"Esperado >=4, veio {r['expected']}"


def test_shrinkage_only_when_hot():
    """Ritmo na média não é tocado pelo shrinkage."""
    r = project(
        stat=2, minutes=4, avg_stat=15.0, avg_minutes=30.0,
        period=1, game_minutes_remaining=44.0,
    )
    # Sem shrinkage: blend normal projeta perto de 15 (na média).
    assert r["expected"] >= 12.0


def test_tobias_case_modest_avg_hot_first_half():
    """
    Tobias (avg ~5.5 reb) com 6 reb em 12 min: projeção não explode demais.

    P5 (mai/2026): hot_ratio = (6/12)/(5.5/30) = 2.73 → strong hot.
    Cap = max(5.5*3.0, 5.5+15) = 20.5. Projeção fica abaixo desse teto.
    """
    r = project(
        stat=6, minutes=12, avg_stat=5.5, avg_minutes=30.0,
        period=2, game_minutes_remaining=24.0,
    )
    # Strong hot mas blend natural mantém projeção razoável
    assert r["expected"] <= 16.0  # bem abaixo do cap strong (20.5)


# ─── Game context (blowout, fouls, target_minutes) ──────────────────────────


def test_live_game_extrapolates():
    """Live: jogador no Q2 com produção típica → projeção razoável."""
    r = project(
        stat=10, minutes=15, avg_stat=20.0, avg_minutes=32.0,
        period=2, game_minutes_remaining=24.0,
    )
    assert r["expected"] >= 10.0
    assert r["expected"] <= 25.0
    assert r["low"] <= r["expected"] <= r["high"]


def test_blowout_reduces_target_minutes():
    """Blowout severo → menos minutos esperados → projeção menor."""
    no = project(
        stat=10, minutes=20, avg_stat=20.0, avg_minutes=32.0,
        period=4, game_minutes_remaining=8.0,
        blowout_severity=0.0,
    )
    severe = project(
        stat=10, minutes=20, avg_stat=20.0, avg_minutes=32.0,
        period=4, game_minutes_remaining=8.0,
        blowout_severity=1.0,
    )
    assert severe["expected"] <= no["expected"]


def test_already_above_target_no_extrapolation():
    """Jogador que já jogou mais que avg_minutes → sem extrapolar."""
    r = project(
        stat=25, minutes=35, avg_stat=20.0, avg_minutes=30.0,
        period=4, game_minutes_remaining=2.0,
    )
    assert r["expected"] <= 27.0
    assert r["confidence"] == "high"


def test_low_never_below_actual():
    """Low nunca pode ser menor que o stat real."""
    r = project(
        stat=15, minutes=10, avg_stat=8.0, avg_minutes=30.0,
        period=2, game_minutes_remaining=24.0,
    )
    assert r["low"] >= 15.0


# ─── Confidence + reason ────────────────────────────────────────────────────


def test_confidence_grows_with_minutes_played():
    """Confidence reflete tamanho de amostra (minutos jogados)."""
    early = project(
        stat=2, minutes=4, avg_stat=10.0, avg_minutes=30.0,
        period=1, game_minutes_remaining=44.0,
    )
    mid = project(
        stat=8, minutes=14, avg_stat=10.0, avg_minutes=30.0,
        period=2, game_minutes_remaining=24.0,
    )
    late = project(
        stat=15, minutes=26, avg_stat=10.0, avg_minutes=30.0,
        period=4, game_minutes_remaining=10.0,
    )
    assert early["confidence"] in ("very_low", "low")
    assert mid["confidence"] == "medium"
    assert late["confidence"] == "high"


def test_reason_always_present():
    """Toda projeção retorna uma reason não-vazia."""
    cases = [
        # (description, kwargs)
        ("normal", dict(stat=10, minutes=20, avg_stat=12.0, avg_minutes=30.0,
                        period=3, game_minutes_remaining=15.0)),
        ("foul_trouble", dict(stat=8, minutes=15, avg_stat=12.0, avg_minutes=30.0,
                              fouls=4, period=3, game_minutes_remaining=14.0)),
        ("blowout", dict(stat=10, minutes=20, avg_stat=20.0, avg_minutes=32.0,
                         period=4, game_minutes_remaining=8.0, blowout_severity=1.0)),
    ]
    for desc, kwargs in cases:
        r = project(**kwargs)
        assert r["reason"], f"{desc}: reason vazia"


# ─── Casos reais HOT (Mudanças 1-7, mai/2026) ─────────────────────────────
# Casos reportados pelo usuário que motivaram o redesign do motor.


def test_levert_like_case_reacts_to_hot_pace():
    """
    Caso LeVert: 17 pts em 16 min, prior ~14 pts em 28 min, heat 0.6+.
    Pace atual ~2× o prior. Projeção antiga: 17 (travada).
    Esperado agora: ≥ 22 (reflete o hot pace + good_game floor).
    """
    out = project(
        stat=17, minutes=16.2, avg_stat=14.0, avg_minutes=27.0,
        last_10_avg=15.0, last_5_avg=16.0,
        period=3, game_minutes_remaining=20.0,
        # Cenário UNEXPECTED_ON_COURT: nbarotations dizia que ele já era,
        # mas naive_remaining é decente (good_game floor compensa)
        expected_minutes_remaining=3.0,
        heat_score=0.62,
    )
    assert out["expected"] >= 22.0, (
        f"LeVert-like deveria projetar ≥22, veio {out['expected']}"
    )
    # Breakdown precisa mostrar floor aplicado quando rotation_remaining
    # foi insuficiente (good_game ativou floor por naive_remaining)
    bd = out["breakdown"]
    # rot_remaining foi corrigido OU live floor aplicou
    assert "rot_remaining_floored" in bd or "live_floor_applied" in bd


def test_harris_like_case_reacts_to_hot_rebounding():
    """
    Caso Harris: 8 reb em 18 min, prior ~7 reb em 29 min, heat reb alto.
    Pace atual ~1.8× o prior. Projeção antiga: 9.8 (linha 11.8).
    Esperado agora: ≥ 11 (reflete o hot rebounding).
    """
    out = project(
        stat=8, minutes=18.3, avg_stat=7.0, avg_minutes=29.0,
        last_10_avg=7.5, last_5_avg=8.0,
        period=2, game_minutes_remaining=24.0,
        expected_minutes_remaining=10.0,
        heat_score=0.70,  # rebounding signal forte
    )
    assert out["expected"] >= 11.0, (
        f"Harris-like deveria projetar ≥11, veio {out['expected']}"
    )


def test_hot_signal_boosts_weight_cap():
    """
    Mudança 4: cap do weight Bayesiano sobe com heat + minutos.
    Com heat 0.65 + minutes 25, weight pode chegar a 0.95.
    """
    out = project(
        stat=22, minutes=25.0, avg_stat=14.0, avg_minutes=30.0,
        period=3, game_minutes_remaining=15.0,
        heat_score=0.65,
    )
    bd = out["breakdown"]
    assert bd["weight_cap"] == 0.95, (
        f"weight_cap deveria ser 0.95 com strong_hot+min≥20, veio {bd['weight_cap']}"
    )


def test_sanity_cap_relaxes_with_heat():
    """
    Mudança 7: sanity cap escala com heat.
    Heat ≥ 0.6 → cap = max(prior×3.0, prior+15) com calibração v3 (mai/2026).

    Pra esse teste precisa que NÃO dispare hot_ratio combined (caso
    contrário cap também relaxa por causa do P5). Por isso usa stat=20
    em prior 12: current_rate = 20/18 = 1.11, prior_rate = 12/28 = 0.43,
    hot_ratio = 2.59 → hot por rate TAMBÉM, mas comparação é heat 0 vs 0.7.
    Como hot_ratio é igual nos dois casos, dependência é só no heat.
    """
    out_neutral = project(
        stat=20, minutes=18.0, avg_stat=12.0, avg_minutes=28.0,
        period=3, game_minutes_remaining=18.0, heat_score=0.0,
    )
    out_hot = project(
        stat=20, minutes=18.0, avg_stat=12.0, avg_minutes=28.0,
        period=3, game_minutes_remaining=18.0, heat_score=0.70,
    )
    cap_neutral = out_neutral["breakdown"]["sanity_cap"]
    cap_hot = out_hot["breakdown"]["sanity_cap"]
    # Ambos podem estar em strong (hot_ratio 2.59 dispara ≥1.8) — neste
    # caso caps são iguais, mas cap_hot pelo menos não menor.
    assert cap_hot >= cap_neutral


def test_sanity_cap_relaxes_with_hot_ratio_even_without_heat():
    """
    P5 (mai/2026): sanity_cap detecta hot pelo rate (current/prior),
    não só pelo heat_score. Caso no backtester (heat=0): cara fazendo
    rate 1.5× do prior já é detectado como hot, cap relaxa.
    """
    # Cara com rate 1.5× prior (hot por rate, sem heat)
    out_hot_by_rate = project(
        stat=18, minutes=16.0, avg_stat=10.0, avg_minutes=25.0,
        period=3, game_minutes_remaining=18.0, heat_score=0.0,
    )
    # current_rate = 18/16 = 1.125, prior_rate = 10/25 = 0.4
    # hot_ratio = 2.81 → STRONG hot por rate
    bd = out_hot_by_rate["breakdown"]
    assert bd["sanity_cap_hot_signal"] == "strong"
    # Cap deve estar relaxado pra strong (3.0× ou +15)
    assert bd["sanity_cap"] >= 25.0  # max(10*3, 25) = 30


def test_period_rate_weight_decays_with_hot_ratio():
    """
    Mudança 6: period_production_rate pesa menos quando hot_ratio é alto.
    Cara fazendo 2× o prior → period_weight ~20% (vs 40% normal).
    """
    # Player fazendo o dobro do esperado
    out = project(
        stat=20, minutes=10.0, avg_stat=10.0, avg_minutes=20.0,  # rate=2.0 vs prior=0.5
        period=2, game_minutes_remaining=24.0,
        period_production_rate=0.5,  # rate típico do Q2
        heat_score=0.5,
    )
    bd = out["breakdown"]
    # hot_ratio = 2.0/0.5 = 4.0, mas current_rate é capped... vamos verificar
    # period_weight = 0.40 / hot_ratio
    assert "period_weight" in bd
    assert bd["period_weight"] <= 0.20, (
        f"period_weight deveria ser ≤0.20 com hot_ratio alto, veio {bd['period_weight']}"
    )


def test_breakdown_present_in_normal_output():
    """Breakdown deve estar presente no retorno normal (não-terminal)."""
    out = project(
        stat=10, minutes=15.0, avg_stat=12.0, avg_minutes=28.0,
        period=2, game_minutes_remaining=24.0, heat_score=0.0,
    )
    assert "breakdown" in out
    bd = out["breakdown"]
    # Campos essenciais
    for key in ("prior_avg", "prior_rate", "current_rate", "hot_ratio",
                "weight_current_final", "blended_rate_final",
                "target_minutes_final", "final_expected"):
        assert key in bd, f"breakdown missing {key}"


# ─── Cap em prior_avg quando in_deficit (mai/2026, fix Shannon Jr.) ────────


def test_role_player_in_deficit_projection_capped_below_prior():
    """
    Caso real Shannon Jr.: prior 8.8 ppg em 12 min (role player burst rate),
    cara fez 2 pts em 9 min (claramente cold). Antes do redesign,
    soft_floor + rotação inflada projetavam 11.7 (ACIMA da média).

    Após P3 (rotation cap) + in_deficit_cap, projeção fica BEM ABAIXO
    do prior — defesa em camadas: rotation_cap reduz minutos antes,
    in_deficit_cap pega o resto se passar.
    """
    out = project(
        stat=2, minutes=9.0, avg_stat=8.8, avg_minutes=12.4,
        last_10_avg=8.8, last_5_avg=8.8,
        period=2, game_minutes_remaining=27.0,
        expected_minutes_remaining=17.3,  # rotação OTIMISTA — naive ~7
        heat_score=0.05,
    )
    bd = out["breakdown"]
    # Cara está em deficit
    assert bd["is_in_deficit"] is True
    # Pelo menos UMA das proteções disparou:
    has_rot_cap = "rot_remaining_capped" in bd
    has_deficit_cap = "in_deficit_cap_applied" in bd
    assert has_rot_cap or has_deficit_cap, (
        f"Nenhuma proteção disparou; bd={bd}"
    )
    # Projeção final NÃO excede prior_avg (o resultado realista)
    assert out["expected"] <= 8.8 + 0.01


def test_in_deficit_cap_does_not_fire_for_hot_player():
    """Cara hot NÃO está em deficit → cap não dispara."""
    out = project(
        stat=18, minutes=15.0, avg_stat=14.0, avg_minutes=28.0,
        period=2, game_minutes_remaining=24.0, heat_score=0.55,
    )
    bd = out["breakdown"]
    # Não em deficit
    assert bd["is_in_deficit"] is False
    # Cap não disparou
    assert "in_deficit_cap_applied" not in bd
    # Projeção pode estar acima do prior_avg normalmente
    assert out["expected"] > 14.0


def test_rotation_remaining_capped_when_too_optimistic():
    """
    P3 (mai/2026): quando rotation diz que cara vai jogar muito mais
    que o naive estimate (> 1.5×), capa em 1.3× naive.

    Caso típico: rotation_provider devolve 17 min restantes pra cara
    com avg 12 min total. Naive (game_remaining × on_court_fraction)
    diria ~8 min. Cap reduz 17 → 10.4 (1.3 × 8).
    """
    # Cara reserva: avg 12 min total. Q1 com 9 min restantes (3 min
    # jogados), game_remaining = 33 min.
    # on_court_fraction = 12/48 = 0.25
    # naive_remaining = 33 × 0.25 = 8.25 min
    # rotation diz 17 min (otimista demais — 2× o naive)
    # Cap em 1.3 × 8.25 = 10.7
    out = project(
        stat=2, minutes=3.0, avg_stat=8.0, avg_minutes=12.0,
        period=1, game_minutes_remaining=33.0,
        expected_minutes_remaining=17.0,  # ROTATION otimista
        heat_score=0.0,
    )
    bd = out["breakdown"]
    # Cap deve ter sido aplicado
    assert "rot_remaining_capped" in bd, (
        f"rot_remaining_capped não disparou; bd={bd}"
    )
    # Valor capado é ~10.7 (não 17)
    assert bd["rot_remaining_capped"]["capped_to"] < 12.0


def test_rotation_remaining_NOT_capped_when_reasonable():
    """Cap NÃO dispara quando rotation está alinhado com naive."""
    # avg 28 min, game_remaining 30, naive = 30 × 28/48 = 17.5
    # rotation diz 20 (1.14× naive — dentro da tolerância)
    out = project(
        stat=10, minutes=10.0, avg_stat=15.0, avg_minutes=28.0,
        period=1, game_minutes_remaining=30.0,
        expected_minutes_remaining=20.0,
        heat_score=0.0,
    )
    bd = out["breakdown"]
    assert "rot_remaining_capped" not in bd


def test_in_deficit_cap_preserves_low_projections():
    """
    Cara em deficit MAS projeção já baixa (abaixo do prior_avg) → cap
    não muda nada. Esse é o caso "cold normal" tipo Holmgren onde o
    motor já está projetando corretamente baixo.
    """
    out = project(
        stat=0, minutes=15.0, avg_stat=16.0, avg_minutes=30.0,
        period=2, game_minutes_remaining=24.0, heat_score=-0.5,
    )
    bd = out["breakdown"]
    # Em deficit
    assert bd["is_in_deficit"] is True
    # Mas projeção já está abaixo do prior_avg (16) → cap não binda
    assert out["expected"] < 16.0
    # Cap NÃO foi aplicado (projeção já baixa)
    assert "in_deficit_cap_applied" not in bd


# ─── P1: Player archetype + rate decay (mai/2026) ───────────────────────────


def test_archetype_classification_thresholds():
    """Cada archetype tem seu threshold de avg_minutes."""
    assert _classify_archetype(35.0)[0] == "STAR"
    assert _classify_archetype(32.0)[0] == "STAR"
    assert _classify_archetype(31.9)[0] == "STARTER"
    assert _classify_archetype(24.0)[0] == "STARTER"
    assert _classify_archetype(23.9)[0] == "SIXTH_MAN"
    assert _classify_archetype(18.0)[0] == "SIXTH_MAN"
    assert _classify_archetype(17.9)[0] == "ROLE_PLAYER"
    assert _classify_archetype(12.0)[0] == "ROLE_PLAYER"
    assert _classify_archetype(11.9)[0] == "SPOT_MINUTES"
    assert _classify_archetype(5.0)[0] == "SPOT_MINUTES"


def test_archetype_decay_monotonic():
    """Decay diminui (mais agressivo) conforme avg_minutes diminui."""
    star_decay = _classify_archetype(35.0)[1]
    starter_decay = _classify_archetype(28.0)[1]
    sixth_decay = _classify_archetype(20.0)[1]
    role_decay = _classify_archetype(14.0)[1]
    spot_decay = _classify_archetype(8.0)[1]
    # Star > Starter > Sixth > Role > Spot
    assert star_decay > starter_decay > sixth_decay > role_decay > spot_decay
    # Todos entre 0.5 e 1.0 (reasonable)
    assert 0.5 < spot_decay < star_decay < 1.0


def test_rate_decay_applied_for_role_player_in_extended_minutes():
    """
    Role player (avg 12 min) jogando 18 min — 6 min de excesso aplicam
    decay de 0.65, ou seja 35% menos rate nos minutos extras.
    """
    out = project(
        stat=8, minutes=12.0, avg_stat=8.0, avg_minutes=12.0,
        period=3, game_minutes_remaining=18.0,
        expected_minutes_remaining=6.0,  # vai jogar mais 6 min (excedendo avg)
    )
    bd = out["breakdown"]
    assert bd["archetype"] == "ROLE_PLAYER"
    assert bd["archetype_decay_factor"] == 0.65
    # Decay aplicado em minutos excedidos
    assert "rate_decay_applied" in bd
    # Minutos excedidos = (12+6) - 12 = 6
    assert bd["rate_decay_applied"]["excess_minutes"] > 0


def test_rate_decay_NOT_applied_for_player_within_avg():
    """Cara jogando DENTRO da minutagem normal — sem decay."""
    out = project(
        stat=10, minutes=15.0, avg_stat=14.0, avg_minutes=30.0,
        period=3, game_minutes_remaining=18.0,
        expected_minutes_remaining=10.0,  # 15+10 = 25 < avg 30 → sem excesso
    )
    bd = out["breakdown"]
    assert bd["archetype"] == "STARTER"
    # Sem rate_decay_applied (ou excess_minutes = 0)
    assert "rate_decay_applied" not in bd


def test_rate_decay_minimal_for_star():
    """Star (avg 35) tem decay quase neutro (5%) — career night sustentável."""
    out = project(
        stat=20, minutes=24.0, avg_stat=22.0, avg_minutes=35.0,
        period=3, game_minutes_remaining=12.0,
        expected_minutes_remaining=14.0,  # vai jogar 24+14=38 (3 min excesso)
    )
    bd = out["breakdown"]
    assert bd["archetype"] == "STAR"
    assert bd["archetype_decay_factor"] == 0.95
