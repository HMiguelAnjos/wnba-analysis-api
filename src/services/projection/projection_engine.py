"""
ProjectionEngine — projeção fim-de-jogo de um stat (PTS/REB/AST).

Versão Refatorada (mai/2026): redesign focado em **reagir corretamente
a jogadores em bom jogo**. A versão anterior tinha múltiplos cortes
(UNEXPECTED_REST cap, period_rate fixo 40%, weight cap 0.85, heat
threshold 0.6, sanity cap 1.8×) que se acumulavam e travavam projeções
de caras claramente HOT. Casos reais: LeVert 17pts/16min → proj=17;
Harris 8reb/18min → proj=9.8 com linha 11.8.

Mudanças desta versão:
  1. UNEXPECTED_REST só capa quando heat ≤ -0.3 (frio confirma).
  2. Floor agressivo de minutos quando `minutes > avg_minutes`
     (bom jogo = coach mantém).
  3. expected_minutes_remaining nunca abaixo do naive_remaining quando
     o jogador está em "extended deployment".
  4. Weight cap do blend Bayesiano sobe com heat + minutos (0.85 → 0.95).
  5. Heat boost começa em 0.3 com rampa até +30% (antes era 0.6 / +15%).
  6. period_production_rate pesa menos quando hot_ratio é alto.
  7. Sanity cap dinâmico com heat (1.8× → até 2.6×).
  8. Campo `breakdown` no retorno com todos os números intermediários
     pra debug/calibração.

Decisões de design preservadas (NÃO mexer sem comparar contra Bet365):
  - Prior blend 55/30/15 (season / L10 / L5)
  - Hot-start shrinkage até 8 min (defesa anti-noise)
  - Indeterminate suppression em 0 stat + <8min + prior alto
  - Hard cap físico em target_minutes ≤ minutes + game_minutes_remaining
  - Cold-weight boost simétrico (já existia, mantido)
"""

from __future__ import annotations

from typing import Optional

from src.utils.stats import blowout_role_factor, foul_trouble_multiplier, rest_factor


class ProjectionEngine:
    """
    Estima o stat final de um jogador. NÃO se ancora na linha de mercado —
    é independente; o edge contra a linha vem depois no caller.
    """

    def project(
        self,
        stat: float,
        minutes: float,
        avg_stat: float,
        avg_minutes: float,
        fouls: int = 0,
        period: int = 1,
        blowout_severity: float = 0.0,
        pace_factor: float = 1.0,
        game_minutes_remaining: float = 0.0,
        is_final: bool = False,
        last_10_avg: Optional[float] = None,
        last_5_avg: Optional[float] = None,
        is_starter: bool = True,
        heat_score: float = 0.0,
        expected_minutes_remaining: Optional[float] = None,
        clutch_close_game_boost: float = 0.0,
        rotation_blowout_cut: float = 0.0,
        period_production_rate: Optional[float] = None,
        rest_days: Optional[int] = None,
        is_unexpected_rest: bool = False,
        variance_factor: float = 1.0,
        # ↑ Volatilidade do jogador (mai/2026). 0..1: 1=consistente,
        # 0=imprevisível. Usado pra expandir margem (low/high) — cara
        # volátil tem range maior. Default 1.0 = sem expansão (compat).
        shrinkage_min_threshold: float = 8.0,
        # ↑ Mai/2026 v2: threshold de minutos abaixo do qual hot-start
        # shrinkage ainda se aplica. Default 8 (PTS — calibrado). Caller
        # pode subir pra stats mais voláteis: AST=14 (4 ast em 9 min são
        # ~6 possessões coletivas, sample muito ruidoso), REB=10
        # (depende muito de matchup defensivo no momento).
    ) -> dict:
        """
        Returns dict: {low, expected, high, confidence, reason, breakdown?}.
        `breakdown` é um dict com números intermediários — opcional, pra debug.
        """
        # Acumula intermediários pro breakdown (visibilidade total da decisão).
        bd: dict = {}

        # ── Estados terminais ──────────────────────────────────────────────
        if is_final:
            base = float(stat)
            return {
                "low": base, "expected": base, "high": base,
                "confidence": "high",
                "reason": "Jogo encerrado — valor final consolidado",
                "breakdown": {"terminal": "is_final"},
            }

        if minutes <= 0:
            return {
                "low": 0.0, "expected": 0.0, "high": 0.0,
                "confidence": "very_low",
                "reason": "Jogador ainda não entrou em quadra",
                "breakdown": {"terminal": "no_minutes"},
            }

        # ── Prior: blend de season + recentes ─────────────────────────────
        if last_10_avg is not None and last_5_avg is not None and avg_stat > 0:
            prior_avg = 0.55 * avg_stat + 0.30 * last_10_avg + 0.15 * last_5_avg
        else:
            prior_avg = avg_stat

        base_avg_minutes = avg_minutes if avg_minutes > 0 else 32.0
        prior_rate = prior_avg / base_avg_minutes if base_avg_minutes > 0 else 0.0
        current_rate = stat / minutes
        hot_ratio = max(current_rate / prior_rate, 1.0) if prior_rate > 0 else 1.0

        bd["prior_avg"] = round(prior_avg, 2)
        bd["prior_rate"] = round(prior_rate, 3)
        bd["current_rate"] = round(current_rate, 3)
        bd["hot_ratio"] = round(hot_ratio, 2)
        bd["heat_score"] = round(heat_score, 3)

        # "Bom jogo": já passou da minutagem média histórica. Coach está
        # estendendo o jogador — sinal forte de que vai continuar.
        is_in_good_game = minutes >= base_avg_minutes
        # Heat ajuda a definir o estado pra muitas decisões abaixo.
        is_hot_signal = heat_score >= 0.30
        is_strong_hot = heat_score >= 0.60
        bd["is_in_good_game"] = is_in_good_game
        bd["is_hot_signal"] = is_hot_signal

        # ── Target minutes (base) ─────────────────────────────────────────
        # Naive estimate: assume jogador continua na fração on-court
        # histórica durante o tempo restante. Usado como FLOOR garantido
        # em situações de "bom jogo" — evita o nbarotations puxar
        # `rotation_remaining` pra perto de 0 quando o cara já passou
        # do padrão dele mas está rendendo (UNEXPECTED_ON_COURT).
        on_court_fraction = base_avg_minutes / 48.0 if base_avg_minutes > 0 else 0.7
        naive_remaining_by_game = max(0.0, game_minutes_remaining) * on_court_fraction

        # Mudança 3 (mai/2026): floor do expected_minutes_remaining quando
        # rotation provider devolve valor anormalmente baixo MAS o jogador
        # está em "bom jogo" (já excedeu minutagem ou heat alto).
        # Pega o naive_remaining como piso — o coach não vai tirar quem
        # está performando "porque o histograma disse pra tirar".
        effective_rot_remaining = expected_minutes_remaining
        if (
            expected_minutes_remaining is not None
            and (is_in_good_game or is_hot_signal)
            and naive_remaining_by_game > expected_minutes_remaining
        ):
            effective_rot_remaining = naive_remaining_by_game
            bd["rot_remaining_floored"] = {
                "original": round(expected_minutes_remaining, 2),
                "naive_floor": round(naive_remaining_by_game, 2),
                "reason": "good_game_or_hot",
            }

        # P3 (mai/2026, v2): VALIDAÇÃO do rotation provider — cap superior.
        # Quando rotation diz "vai jogar muito mais que o esperado pelo
        # histograma" (rotation_remaining > 1.5× naive_remaining), o
        # rotation pode estar otimista demais (lesão escondida, lineup
        # mudou, foul trouble, etc.).
        # Descontamos 25%, mas mantemos o teto generoso (1.3× naive) pra
        # não derrubar casos legítimos onde o cara JÁ estava em bom jogo.
        # Filosofia: rotation é sugestão informada, não verdade absoluta.
        if (
            effective_rot_remaining is not None
            and effective_rot_remaining > 0
            and naive_remaining_by_game > 0
            and effective_rot_remaining > 1.5 * naive_remaining_by_game
        ):
            capped = naive_remaining_by_game * 1.3
            bd["rot_remaining_capped"] = {
                "original": round(effective_rot_remaining, 2),
                "naive_estimate": round(naive_remaining_by_game, 2),
                "capped_to": round(capped, 2),
                "reason": "rotation_too_optimistic_vs_naive",
            }
            effective_rot_remaining = capped

        if effective_rot_remaining is not None and effective_rot_remaining > 0:
            target_minutes = max(base_avg_minutes, minutes + effective_rot_remaining)
        else:
            target_minutes = base_avg_minutes
            if game_minutes_remaining > 0:
                target_minutes = max(
                    target_minutes, minutes + naive_remaining_by_game,
                )

        bd["target_minutes_base"] = round(target_minutes, 2)

        # ── Redutores de contexto ─────────────────────────────────────────
        if blowout_severity > 0:
            target_minutes *= (1.0 - blowout_role_factor(is_starter) * blowout_severity)

        foul_mult = foul_trouble_multiplier(fouls, game_minutes_remaining)
        foul_rate_factor = 1.0
        if foul_mult < 1.0:
            target_minutes *= 0.30 + 0.70 * foul_mult
            foul_rate_factor = 0.70 + 0.30 * foul_mult

        # Mai/2026 v3: "early foul trouble" — 3 fouls com muito jogo
        # restante (>20 min = mais de 1 quarto pela frente) significa
        # coach quase sempre senta o jogador pra preservar.
        # foul_trouble_multiplier clássico só capa 4-5 fouls. Fix SCN_008:
        # forward com 3 fouls/9min em Q2 projetava 19.9 (prior 14.2).
        # Realista: 13-16 (senta até intervalo). Cut leve de 10%.
        if fouls == 3 and game_minutes_remaining > 20 and foul_mult >= 1.0:
            target_minutes *= 0.90
            bd["early_foul_trouble_applied"] = {
                "fouls": fouls,
                "game_min_rem": round(game_minutes_remaining, 1),
                "target_cut_pct": 10,
            }

        bd["target_minutes_after_context"] = round(target_minutes, 2)

        # ── Live floor — Mudança 2 (mai/2026) ─────────────────────────────
        # Filosofia: jogou mais que a média = bom jogo = coach mantém.
        # Gradientes do floor:
        #
        #   Base (3 min): jogo em andamento com tempo restante OK.
        #
        #   Bom jogo (30% game_rem): minutes ≥ avg_minutes.
        #     Coach está estendendo o jogador.
        #
        #   Bom jogo + hot signal (40% game_rem): minutes ≥ avg_minutes
        #     E heat ≥ 0.3. Convergência: minutagem estendida + sinal de
        #     produção.
        #
        #   Strong hot ride (50% game_rem): minutes ≥ avg_minutes E heat ≥ 0.6.
        #     Career night em curso — coach pode jogar até o fim.
        LIVE_FLOOR_MIN_GAME_REMAINING = 10.0
        LIVE_FLOOR_MINUTES_DEFAULT = 3.0
        FLOOR_FRAC_GOOD_GAME = 0.30
        FLOOR_FRAC_GOOD_HOT = 0.40
        FLOOR_FRAC_STRONG_HOT = 0.50

        if (
            game_minutes_remaining >= LIVE_FLOOR_MIN_GAME_REMAINING
            and minutes > 0
            and blowout_severity < 0.5
            and fouls < 5
        ):
            floor_remaining = LIVE_FLOOR_MINUTES_DEFAULT
            if is_in_good_game and is_strong_hot:
                floor_remaining = max(
                    floor_remaining,
                    game_minutes_remaining * FLOOR_FRAC_STRONG_HOT,
                )
            elif is_in_good_game and is_hot_signal:
                floor_remaining = max(
                    floor_remaining,
                    game_minutes_remaining * FLOOR_FRAC_GOOD_HOT,
                )
            elif is_in_good_game:
                floor_remaining = max(
                    floor_remaining,
                    game_minutes_remaining * FLOOR_FRAC_GOOD_GAME,
                )
            new_target = max(target_minutes, minutes + floor_remaining)
            if new_target > target_minutes:
                bd["live_floor_applied"] = {
                    "floor_remaining": round(floor_remaining, 2),
                    "target_was": round(target_minutes, 2),
                    "target_became": round(new_target, 2),
                }
                target_minutes = new_target

        # ── Hard cap físico: tempo do jogo ────────────────────────────────
        if game_minutes_remaining > 0:
            target_minutes = min(
                target_minutes, minutes + game_minutes_remaining,
            )

        bd["target_minutes_final"] = round(target_minutes, 2)

        # ── Saída antecipada ──────────────────────────────────────────────
        if minutes >= target_minutes:
            base = float(stat)
            margin = base * 0.05
            bd["terminal"] = "minutes_exceeded_target"
            return {
                "low": round(base - margin, 1),
                "expected": round(base, 1),
                "high": round(base + margin, 1),
                "confidence": "high",
                "reason": "Já excedeu minutagem média esperada — projeção fechada",
                "breakdown": bd,
            }

        # ── Indeterminate (suppression) ──────────────────────────────────
        INDETERMINATE_MAX_MINUTES = 8.0
        if (
            minutes < INDETERMINATE_MAX_MINUTES
            and stat == 0
            and prior_avg >= 3
        ):
            base = float(stat)
            bd["terminal"] = "indeterminate_suppression"
            return {
                "low": base, "expected": base, "high": base,
                "confidence": "very_low",
                "reason": (
                    f"Amostra pequena ({minutes:.1f} min sem produção) — "
                    "aguardando jogador entrar em ritmo"
                ),
                "indeterminate": True,
                "breakdown": bd,
            }

        # ── Bayesian-like blend ───────────────────────────────────────────
        # Mudança 4: cap do weight escala com heat + minutos. Cara
        # claramente hot + sample relevante confia mais no ao vivo.
        PRIOR_STRENGTH_MIN = 6.0
        if is_strong_hot and minutes >= 20:
            weight_cap = 0.95
        elif is_hot_signal and minutes >= 15:
            weight_cap = 0.90
        else:
            weight_cap = 0.85
        weight_current = min(minutes / (minutes + PRIOR_STRENGTH_MIN), weight_cap)
        bd["weight_cap"] = weight_cap
        bd["weight_current_initial"] = round(weight_current, 3)

        # Boost de weight em underperformance.
        # Calibração mai/2026: reduzidos pra eliminar UNDER over-confident.
        # Cara cold ainda projeta baixo (peso 0.55-0.65 no zero atual já é
        # forte), mas preserva mais o prior — evita projetar 2 pts pra
        # titular de 16 ppg só porque ele fez 0 em 15 min.
        is_in_deficit = False
        if minutes >= 5 and prior_rate > 0:
            deficit_ratio = current_rate / prior_rate
            heat_confirms_cold = heat_score <= -0.2
            if deficit_ratio < 0.5:
                if heat_confirms_cold:
                    weight_current = max(weight_current, 0.65)  # multi-signal cold
                else:
                    weight_current = max(weight_current, 0.55)
                is_in_deficit = True
            elif deficit_ratio < 0.7:
                if heat_confirms_cold:
                    weight_current = max(weight_current, 0.55)
                else:
                    weight_current = max(weight_current, 0.45)
                is_in_deficit = True

        bd["weight_current_final"] = round(weight_current, 3)
        bd["is_in_deficit"] = is_in_deficit

        blended_rate = weight_current * current_rate + (1 - weight_current) * prior_rate
        bd["blended_rate_initial"] = round(blended_rate, 3)

        # ── Period rate blend — Mudança 6 ────────────────────────────────
        # Peso proporcional ao "tamanho do desvio HOT". Cara em ritmo
        # normal pega 40%; cara claramente acima do prior, o histórico
        # do quarter desce — ele NÃO está seguindo o padrão dele de Q,
        # então o histórico desse Q é ruído.
        used_period_rate = False
        if period_production_rate is not None and period_production_rate > 0:
            if is_in_deficit:
                # Em deficit já reduzido pra 20% (preservado).
                period_weight_base = 0.20
            else:
                period_weight_base = 0.40
            # Divide pelo hot_ratio: 1× → 40%; 2× → 20%; 3× → 13%.
            period_weight = period_weight_base / hot_ratio
            # Floor mínimo de 0.05 pra não zerar contribuição em casos extremos.
            period_weight = max(period_weight, 0.05)
            bd["period_weight"] = round(period_weight, 3)
            bd["period_rate_input"] = round(period_production_rate, 3)
            blended_rate = (1 - period_weight) * blended_rate + period_weight * period_production_rate
            used_period_rate = True
            bd["blended_rate_with_period"] = round(blended_rate, 3)

        # ── Soft floor anti-overcorrection (mai/2026, v2) ─────────────────
        # Mesmo cara claramente frio não termina em 0% do ritmo histórico —
        # regressão pra média é real. Sem essa proteção, model projetava 2pts
        # pra titular de 16ppg que fez 0 em 15 min, gerando UNDERs com edge
        # absurdo (-9) que não são realistas.
        #
        # Floor de 60% do prior_rate (reduzido de 70% pós-baseline backtester
        # v1 em mai/2026). 70% disparava em 30% dos casos, sinal de
        # over-application. 60% mantém proteção contra absurdos pra baixo
        # sem pendurar projeção em cima.
        #
        # Hot side: não é afetado (floor < blended_rate quando current alto).
        SOFT_FLOOR_PCT = 0.60
        soft_floor_value = prior_rate * SOFT_FLOOR_PCT
        if prior_rate > 0 and blended_rate < soft_floor_value:
            bd["soft_floor_applied"] = {
                "before": round(blended_rate, 3),
                "floor": round(soft_floor_value, 3),
            }
            blended_rate = soft_floor_value

        # ── Hot-start shrinkage (preservado, anti-noise) ─────────────────
        # Mai/2026 v2: threshold parametrizado por stat (default 8 pra PTS).
        # AST/REB precisam de threshold maior porque são mais ruidosos em
        # amostra pequena.
        #
        # Mai/2026 v3: curva CÔNCAVA (^0.6). Linear puxava só 37.5% pro
        # prior em 5 min, deixando 62.5% no ritmo absurdo (case SCN_002:
        # bench 11pts/5min, prior 6.2, projetava 21.8 vs realista ~14-17).
        # Côncava puxa 55% em 5 min, 29% em 7 min — defesa muito mais
        # forte contra ruído sem afetar quem passou do threshold
        # (acima de 8 min, ambas curvas dão zero).
        used_hot_shrinkage = False
        shrinkage_threshold = max(shrinkage_min_threshold, 1.0)
        if (
            prior_avg > 0
            and minutes < shrinkage_threshold
            and prior_rate > 0
            and current_rate > prior_rate * 1.4
        ):
            linear_progress = (shrinkage_threshold - minutes) / shrinkage_threshold
            base_shrinkage = linear_progress ** 0.6  # côncava: mais peso em low minutes
            # Mai/2026 v3 (SCN_002): amplificador por hot_ratio.
            # Ratios absurdos (>4×) são quase 100% variância pura — a
            # shrinkage clássica deixa ~20% peso no current mesmo com
            # ratio 5.8×, projetando bench fazendo 11pts/5min como 21+.
            # Amplifier: ratio 1.4-2.0 → 1.0×, 2-4 → até 1.5×, 4+ → até 2×
            # (capped em 1.0 total).
            if hot_ratio > 2.0:
                ratio_amplifier = 1.0 + min((hot_ratio - 2.0) / 4.0, 1.0)
            else:
                ratio_amplifier = 1.0
            shrinkage = min(base_shrinkage * ratio_amplifier, 1.0)
            blended_rate = blended_rate * (1.0 - shrinkage) + prior_rate * shrinkage
            used_hot_shrinkage = True
            bd["hot_start_shrinkage_applied"] = round(shrinkage, 3)
            bd["shrinkage_threshold"] = shrinkage_threshold
            bd["hot_ratio_amplifier"] = round(ratio_amplifier, 2)

        # Ajustes multiplicativos finais no rate
        blended_rate *= foul_rate_factor
        blended_rate *= pace_factor

        used_rest_factor = False
        if rest_days is not None:
            rf = rest_factor(rest_days)
            if rf != 1.0:
                blended_rate *= rf
                used_rest_factor = True
                bd["rest_factor"] = round(rf, 3)

        bd["blended_rate_final"] = round(blended_rate, 3)

        remaining = max(target_minutes - minutes, 0.0)
        bd["remaining_minutes"] = round(remaining, 2)

        # ── P1 (mai/2026): Player archetype + rate decay ─────────────────
        # Cara que normalmente joga 12 min eficientes tem rate alto. Mas
        # rate burst NÃO se sustenta em minutos estendidos (24+) por
        # fadiga, contexto diferente, lineup não otimizado.
        #
        # Solução: classificar archetype baseado em avg_minutes e aplicar
        # decay nos minutos EXCEDIDOS (acima de avg_minutes).
        # Minutos "normais" (até avg_minutes) usam rate completo.
        archetype, decay_factor = _classify_archetype(base_avg_minutes)
        bd["archetype"] = archetype
        bd["archetype_decay_factor"] = decay_factor

        # Calcula quanto dos minutos restantes são "excesso" vs "normais"
        projected_total_min = minutes + remaining
        normal_end = max(minutes, min(projected_total_min, base_avg_minutes))
        normal_remaining = max(0.0, normal_end - minutes)
        excess_remaining = max(0.0, projected_total_min - normal_end)

        normal_production = blended_rate * normal_remaining
        excess_production = blended_rate * decay_factor * excess_remaining

        if excess_remaining > 0:
            bd["rate_decay_applied"] = {
                "normal_minutes": round(normal_remaining, 2),
                "excess_minutes": round(excess_remaining, 2),
                "decay_factor": decay_factor,
                "savings_pts": round(blended_rate * (1 - decay_factor) * excess_remaining, 2),
            }

        expected = stat + normal_production + excess_production
        bd["raw_expected"] = round(expected, 2)

        # ── Heat boost/cut — Mudança 5 ────────────────────────────────────
        # Rampa começa em 0.30, vai até 0.85; boost final +5% a +30%.
        # Cold simétrico: -0.30 → -0.85, -5% a -30%.
        #
        # Anti double-counting (mai/2026): se is_in_deficit já disparou,
        # SKIP heat cut. Os dois sinais (deficit + heat negativo) são
        # correlacionados — aplicar ambos amplificava UNDERs irrealisticamente.
        # Boost (heat positivo) continua aplicando normalmente.
        #
        # Mai/2026 v2 (fix Schröder 4ast/9min → proj 11.1): heat boost
        # com **confidence-floor de minutos**. Antes: heat 1.00 em 9 min
        # dava +30% num rate que JÁ incorpora "hot" via current_rate
        # (double-counting). Agora: o efeito do heat escala com minutos
        # jogados — em 9 min, full boost vira ~60% do valor (rampa
        # min(minutes/15, 1.0)). A 15+ min: efeito completo como antes.
        used_heat = False
        if not used_hot_shrinkage:
            future_value = max(expected - float(stat), 0.0)
            # Confidence ramp: 0 em 0 min, 1.0 em 15+ min (linear).
            # Aplicado simetricamente em cold cut também — sinal de heat
            # com pouca amostra é ruidoso pros dois lados.
            minutes_confidence = min(minutes / 15.0, 1.0)
            if heat_score >= 0.30 and future_value > 0:
                # Mai/2026 v3: 30% → 15% (halving). v4: 15% → 10%.
                # Baseline_v10 em Q2 mid mostrou heat_boost Δ +5.96 em
                # LARGE (signed_fired +4.24 vs off -1.71). Mesmo cortado
                # pela metade, em early game o boost amplifica em cima
                # de menos informação e infla projeção. Q3 end LARGE bias
                # +0.84; previsão pós-fix: ~+0.54.
                # 0.30 → 2%, 0.85+ → 10%. Linear.
                ramp_pos = min((heat_score - 0.30) / 0.55, 1.0)
                heat_boost_full = 1.02 + (0.10 - 0.02) * ramp_pos
                # Escala o EFEITO (delta acima de 1.0) por minutes_confidence
                heat_boost = 1.0 + (heat_boost_full - 1.0) * minutes_confidence
                expected = float(stat) + future_value * heat_boost
                used_heat = True
                bd["heat_multiplier"] = round(heat_boost, 3)
                bd["heat_multiplier_full"] = round(heat_boost_full, 3)
                bd["minutes_confidence"] = round(minutes_confidence, 3)
            elif heat_score <= -0.30 and future_value > 0 and not is_in_deficit:
                ramp_neg = min((-0.30 - heat_score) / 0.55, 1.0)
                heat_cut_full = 0.95 - (0.30 - 0.05) * ramp_neg
                # Escala o EFEITO (delta abaixo de 1.0) por minutes_confidence
                heat_cut = 1.0 - (1.0 - heat_cut_full) * minutes_confidence
                expected = float(stat) + future_value * heat_cut
                used_heat = True
                bd["heat_multiplier"] = round(heat_cut, 3)
                bd["heat_multiplier_full"] = round(heat_cut_full, 3)
                bd["minutes_confidence"] = round(minutes_confidence, 3)
            elif heat_score <= -0.30 and is_in_deficit:
                # Heat cut suprimido por deficit boost já aplicado
                bd["heat_cut_skipped_reason"] = "in_deficit"

        # ── Clutch / blowout cut (preservados) ────────────────────────────
        used_clutch = False
        used_rotation_blowout = False
        if clutch_close_game_boost > 0 and (expected - float(stat)) > 0:
            future_value = max(expected - float(stat), 0.0)
            boost_pct = 1.0 + 0.12 * min(clutch_close_game_boost, 1.0)
            expected = float(stat) + future_value * boost_pct
            used_clutch = True
            bd["clutch_multiplier"] = round(boost_pct, 3)
        if rotation_blowout_cut > 0 and (expected - float(stat)) > 0:
            future_value = max(expected - float(stat), 0.0)
            cut_pct = 1.0 - 0.25 * min(rotation_blowout_cut, 1.0)
            expected = float(stat) + future_value * cut_pct
            used_rotation_blowout = True
            bd["rotation_blowout_multiplier"] = round(cut_pct, 3)

        bd["expected_before_caps"] = round(expected, 2)

        # ── Sanity cap dinâmico — P5 (mai/2026, v3) ──────────────────────
        # Detecta "hot" via heat_score E/OU hot_ratio (current vs prior).
        #
        # Baseline_v5 mostrava sanity_cap atrapalhando +1.56 MAE com signed
        # -1.87 (cap sub-estimava em quase 2 pts). Causa: em produção, heat
        # detecta hot e relaxa cap. Mas hot_ratio sozinho também é um
        # sinal forte — cara fazendo 1.8× o rate histórico É hot mesmo sem
        # eFG/volume oficiais altos.
        #
        # Fix: detecção unificada — cap relaxa quando heat OU hot_ratio
        # indica hot. Resolve career nights sub-estimadas no backtester
        # (que não passa heat) E mantém comportamento em produção.
        is_strong_hot_combined = is_strong_hot or hot_ratio >= 1.8
        is_hot_signal_combined = is_hot_signal or hot_ratio >= 1.4

        if prior_avg > 0:
            if is_strong_hot_combined:
                sanity_cap = max(prior_avg * 3.0, prior_avg + 15.0)
            elif is_hot_signal_combined:
                sanity_cap = max(prior_avg * 2.4, prior_avg + 10.0)
            else:
                sanity_cap = max(prior_avg * 2.0, prior_avg + 7.0)
            sanity_cap = max(sanity_cap, float(stat))
            bd["sanity_cap"] = round(sanity_cap, 2)
            bd["sanity_cap_hot_signal"] = (
                "strong" if is_strong_hot_combined
                else "moderate" if is_hot_signal_combined
                else "default"
            )
            if expected > sanity_cap:
                bd["sanity_cap_applied"] = True
            expected = min(expected, sanity_cap)

        # ── Cap "deficit ceiling" — Mudança mai/2026 (fix Shannon Jr.) ────
        # Cara claramente em deficit (current_rate < 70% prior_rate +
        # heat negativo confirma) NÃO termina acima da média histórica.
        # Esse cap é a regra física: cara cold com inflação de projeção
        # via soft floor + rotação otimista (típico de role player com
        # prior_rate "burst" — alto pq joga poucos minutos eficientes)
        # estava produzindo STRONG_OVER falso.
        #
        # Diferente do sanity_cap (que permite 1.8x-2.6x prior pra
        # ABSORVER cases legítimos hot), esse cap é o TETO do COLD —
        # não tem upside quando todos os sinais dizem "abaixo da média".
        #
        # Casos motivadores:
        #   Shannon Jr. 2pts/9min, prior 8.8: projetava 11.7 (acima!)
        #   → vira 8.8 com esse cap. Soft floor existe pra anti-UNDER,
        #     não pra inflar OVER em cold.
        if is_in_deficit and prior_avg > 0 and expected > prior_avg:
            bd["in_deficit_cap_applied"] = {
                "raw_expected": round(expected, 2),
                "cap_value": round(prior_avg, 2),
                "reason": "cara cold não termina acima da média histórica",
            }
            expected = prior_avg

        if used_hot_shrinkage:
            expected = max(expected, float(stat))

        # ── Confidence ────────────────────────────────────────────────────
        if minutes >= 24:
            confidence = "high"
        elif minutes >= 14:
            confidence = "medium"
        elif minutes >= 6:
            confidence = "low"
        else:
            confidence = "very_low"

        # ── Reason ────────────────────────────────────────────────────────
        if used_hot_shrinkage:
            reason = "Ritmo inicial muito acima da média — projeção regrediu pra base histórica"
        elif used_heat and heat_score >= 0.30:
            reason = f"Jogador quente (heat {heat_score:+.2f}) — projeção foi acelerada"
        elif used_heat and heat_score <= -0.30:
            reason = f"Jogador frio (heat {heat_score:+.2f}) — projeção foi reduzida"
        elif is_in_deficit and heat_score <= -0.30:
            reason = "Jogador em ritmo abaixo do histórico — projeção reduzida"
        elif used_rotation_blowout:
            reason = "Risco de poupança em blowout — projeção cortada"
        elif used_clutch:
            reason = "Jogo apertado + jogador clutch — projeção acelerada"
        elif used_rest_factor and rest_days == 0:
            reason = "Back-to-back — projeção reduzida por fadiga"
        elif used_period_rate:
            reason = f"Rate do Q{period} ajustado por padrão histórico do jogador"
        elif blowout_severity > 0.5:
            role_label = "titular" if is_starter else "reserva"
            reason = f"Blowout — minutos restantes reduzidos ({role_label})"
        elif fouls >= 5:
            reason = f"Foul out iminente ({fouls}F com {game_minutes_remaining:.0f}min restantes) — minutos cortados"
        elif fouls >= 4:
            reason = f"Foul trouble ({fouls}F com {game_minutes_remaining:.0f}min restantes) reduz tempo de quadra"
        elif minutes < 6:
            reason = "Amostra pequena — projeção ancorada em média histórica"
        elif prior_rate > 0 and current_rate >= prior_rate * 1.2:
            reason = "Ritmo acima da média — projeção blend favorece o jogo atual"
        elif prior_rate > 0 and current_rate <= prior_rate * 0.7:
            reason = "Ritmo abaixo da média — projeção reflete underperformance"
        else:
            reason = "Ritmo dentro do esperado pela média histórica"

        # ── UNEXPECTED_REST cap (mai/2026, v2 — fix caso McDaniels) ────────
        # Refatoração em 2 partes pra evitar falso-positivos onde o cap
        # massacrava projeções de caras em rotação normal:
        #
        # A) Gate MAIS RIGOROSO (3 condições TODAS necessárias):
        #    1. is_unexpected_rest=True (flag externa do rotation_context)
        #    2. heat ≤ -0.30 (cara frio claro)
        #    3. current_rate < 60% prior_rate (produção claramente abaixo)
        #
        #    Antes só 1+2 era suficiente, mas heat -0.31 + flag fraca
        #    disparavam o cap pra cara em jogo normal (caso McDaniels).
        #    A condição 3 garante "produção realmente baixa" — não só
        #    "talvez frio".
        #
        # B) Cap MENOS BRUTAL quando dispara:
        #    Antes: current * 1.15 (cara projeta basicamente só +15%
        #    do atual — assume que mal volta a quadra)
        #    Agora: max(current * 1.15, expected_before * 0.5)
        #    Pega o maior dos dois:
        #      - Cara realmente parado: 1.15× ainda vence → cap agressivo
        #      - Cara projetando muito: 50% do projetado vence → corta
        #        pela metade mas não destrói a projeção
        if is_unexpected_rest:
            bd["is_unexpected_rest"] = True
            # Gate A: 3 condições TODAS necessárias
            rate_ratio = (current_rate / prior_rate) if prior_rate > 0 else 1.0
            severe_cold = heat_score <= -0.30 and rate_ratio < 0.60
            if severe_cold:
                # Cap B: o MAIOR entre current*1.15 e expected_before*0.5
                aggressive_cap = float(stat) * 1.15
                softer_cap = expected * 0.5
                capped = max(aggressive_cap, softer_cap)
                if capped < expected:
                    bd["unexpected_rest_cap_applied"] = True
                    bd["unexpected_rest_cap_value"] = round(capped, 2)
                    bd["unexpected_rest_cap_floors"] = {
                        "aggressive_current_x_115": round(aggressive_cap, 2),
                        "softer_expected_x_50": round(softer_cap, 2),
                        "rate_ratio": round(rate_ratio, 3),
                    }
                    expected = capped
                    reason = (
                        "Descanso fora do padrão + jogador frio + ritmo "
                        "muito abaixo — projeção truncada"
                    )
                    if confidence in ("high", "medium"):
                        confidence = "low"
            else:
                # Diagnóstico claro: qual condição não bateu
                missing = []
                if heat_score > -0.30:
                    missing.append(f"heat_score {heat_score:+.2f} acima de -0.30")
                if rate_ratio >= 0.60:
                    missing.append(f"rate_ratio {rate_ratio:.2f} acima de 0.60")
                bd["unexpected_rest_cap_skipped"] = "; ".join(missing) or "ok"

        # ── Margem de incerteza ───────────────────────────────────────────
        progress = minutes / max(target_minutes, 1.0)
        uncertainty = max(0.15 * (1.0 - progress), 0.05)
        if fouls >= 4 or blowout_severity > 0.4:
            uncertainty = min(uncertainty + 0.05, 0.20)
        if is_unexpected_rest:
            uncertainty = min(uncertainty + 0.05, 0.25)

        # Variance penalty: cara volátil tem margem maior (low/high range)
        # variance_factor ∈ [0..1]: 1=consistente (sem expansão),
        #                          <0.5=expande 50%, <0.3=expande 100%
        if variance_factor < 0.30:
            uncertainty *= 2.0
            bd["margin_expanded_for_variance"] = "high"
        elif variance_factor < 0.50:
            uncertainty *= 1.5
            bd["margin_expanded_for_variance"] = "moderate"
        bd["variance_factor"] = round(variance_factor, 3)

        # Cap final na uncertainty pra evitar margens absurdas
        uncertainty = min(uncertainty, 0.40)

        margin = expected * uncertainty

        low = max(float(stat), expected - margin)
        high = expected + margin

        bd["final_expected"] = round(expected, 2)

        return {
            "low": round(low, 1),
            "expected": round(expected, 1),
            "high": round(high, 1),
            "confidence": confidence,
            "reason": reason,
            "breakdown": bd,
        }


# ─── P1: Player archetype classification (mai/2026) ─────────────────────────
# Filosofia: rate de pts/min representa eficiência em minutos típicos do
# jogador. Cara que joga 12 min eficientes tem rate alto, mas esse rate
# NÃO se sustenta em minutos estendidos (fadiga, lineup diferente,
# contexto não-otimizado). Quanto MENOR a minutagem média, mais o rate
# decai em minutos excedidos.


def _classify_archetype(avg_minutes: float) -> tuple[str, float]:
    """
    Classifica o jogador em um archetype baseado em minutagem média e
    retorna o fator de decay aplicado a minutos EXCEDIDOS.

    Returns:
        (archetype_name, decay_factor) onde decay_factor ∈ [0.55, 0.95].

    Calibração inicial (refinar com dados após baseline_v5):
      STAR (≥32 min):       0.95  ↓5%  — rate consistente, decay mínimo
      STARTER (24-32):      0.85  ↓15% — decay moderado
      SIXTH_MAN (18-24):    0.75  ↓25% — decay significativo
      ROLE_PLAYER (12-18):  0.65  ↓35% — rate burst forte
      SPOT_MINUTES (<12):   0.55  ↓45% — rate burst extremo
    """
    if avg_minutes >= 32:
        return ("STAR", 0.95)
    if avg_minutes >= 24:
        return ("STARTER", 0.85)
    if avg_minutes >= 18:
        return ("SIXTH_MAN", 0.75)
    if avg_minutes >= 12:
        return ("ROLE_PLAYER", 0.65)
    return ("SPOT_MINUTES", 0.55)
