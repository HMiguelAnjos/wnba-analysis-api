"""
Testes do detector CASHOUT (mai/2026).

Cobre o caso positivo do spec + todos os exclusores (item 5):
reserva profundo, baixa chance de volta, blowout forte, foul trouble,
lesão/expulsão, pouco tempo, linha distante, baixa rotação.
"""
from src.services.cashout import detect_cashout


def _base_kwargs(**over):
    """Cenário POSITIVO base (exemplo do spec) — override por teste."""
    kw = dict(
        markets=[("PTS", 15.0, 16.5), ("REB", 4.0, 9.5), ("AST", 3.0, 6.5)],
        on_court=False,
        status="ACTIVE",
        is_starter=True,
        usage=0.55,
        foul_trouble=False,
        rotation_available=True,
        expected_remaining_minutes=6.0,
        closing_game_probability=0.62,
        current_rotation_status="EXPECTED_REST",
        rotation_confidence=0.70,
        blowout_risk="LOW",
    )
    kw.update(over)
    return kw


# ─── Caso positivo ──────────────────────────────────────────────────────────


def test_positive_case_emits_cashout():
    r = detect_cashout(**_base_kwargs())
    assert r is not None
    assert r["market"] == "PTS"           # linha mais colada (gap 1.5)
    assert r["label"] == "Cashout"
    assert r["icon"] == "money"
    assert r["confidence"] == "HIGH"      # closing 0.62 + rot 0.7 + starter
    assert r["current_stat"] == 15.0
    assert r["line"] == 16.5
    assert r["difference_to_line"] == 1.5
    assert r["is_on_court"] is False
    assert r["rotation_status"] == "LIKELY_TO_RETURN"
    assert "cashout" in r["reason"].lower()


def test_picks_tightest_line_market():
    """Vários mercados na janela → escolhe o de menor gap."""
    r = detect_cashout(**_base_kwargs(
        markets=[
            ("PTS", 15.0, 18.0),   # gap 3.0
            ("REB", 8.0, 9.0),     # gap 1.0  ← mais colada
            ("AST", 5.0, 7.5),     # gap 2.5
        ],
    ))
    assert r is not None
    assert r["market"] == "REB"
    assert r["difference_to_line"] == 1.0


def test_confidence_medium_when_signals_softer():
    """closing/rot abaixo do limiar HIGH → MEDIUM (mas ainda emite)."""
    r = detect_cashout(**_base_kwargs(
        closing_game_probability=0.50,
        rotation_confidence=0.50,
    ))
    assert r is not None
    assert r["confidence"] == "MEDIUM"


# ─── Exclusores (item 5 do spec) ────────────────────────────────────────────


def test_excluded_when_on_court():
    assert detect_cashout(**_base_kwargs(on_court=True)) is None


def test_excluded_when_inactive_injured_or_ejected():
    assert detect_cashout(**_base_kwargs(status="INACTIVE")) is None


def test_excluded_when_no_real_rotation_profile():
    assert detect_cashout(**_base_kwargs(rotation_available=False)) is None


def test_excluded_when_foul_trouble():
    assert detect_cashout(**_base_kwargs(foul_trouble=True)) is None


def test_excluded_when_strong_blowout():
    assert detect_cashout(**_base_kwargs(blowout_risk="HIGH")) is None


def test_excluded_when_low_rotation_confidence():
    assert detect_cashout(**_base_kwargs(rotation_confidence=0.30)) is None


def test_excluded_when_too_little_time_left():
    assert detect_cashout(**_base_kwargs(expected_remaining_minutes=2.0)) is None


def test_excluded_when_deep_bench():
    """Não titular + baixo usage + sem minutos esperados altos → fora."""
    assert detect_cashout(**_base_kwargs(
        is_starter=False, usage=0.20, expected_remaining_minutes=4.5,
    )) is None


def test_excluded_when_low_return_chance():
    """closing baixo E status != EXPECTED_REST → não volta provável."""
    assert detect_cashout(**_base_kwargs(
        closing_game_probability=0.20,
        current_rotation_status="UNEXPECTED_REST",
    )) is None


def test_excluded_when_line_too_far():
    """15 pts, linha 22 (gap 7) → fora da janela [0.5, 3.5]."""
    assert detect_cashout(**_base_kwargs(
        markets=[("PTS", 15.0, 22.0), ("REB", 4.0, 20.0), ("AST", 3.0, 15.0)],
    )) is None


def test_excluded_when_already_above_line():
    """Já passou a linha (16 pts, linha 14.5) → gap negativo, não cashout."""
    assert detect_cashout(**_base_kwargs(
        markets=[("PTS", 16.0, 14.5), ("REB", 9.0, 8.5), ("AST", 7.0, 6.5)],
    )) is None


# ─── Casos de borda ─────────────────────────────────────────────────────────


def test_relevance_via_high_expected_minutes_only():
    """Não titular, usage médio, mas expected_remaining alto = relevante."""
    r = detect_cashout(**_base_kwargs(
        is_starter=False, usage=0.30, expected_remaining_minutes=8.0,
    ))
    assert r is not None
    assert r["confidence"] == "MEDIUM"   # sem is_starter não chega a HIGH


def test_return_via_expected_rest_status_only():
    """closing baixo mas EXPECTED_REST (volta pelo padrão) → emite."""
    r = detect_cashout(**_base_kwargs(
        closing_game_probability=0.10,
        current_rotation_status="EXPECTED_REST",
    ))
    assert r is not None


def test_line_gap_boundaries():
    """Gap exatamente 0.5 e 3.5 entram; 0.4 e 3.6 ficam fora."""
    assert detect_cashout(**_base_kwargs(
        markets=[("PTS", 15.0, 15.5)],   # gap 0.5
    )) is not None
    assert detect_cashout(**_base_kwargs(
        markets=[("PTS", 15.0, 18.5)],   # gap 3.5
    )) is not None
    assert detect_cashout(**_base_kwargs(
        markets=[("PTS", 15.0, 15.4)],   # gap 0.4
    )) is None
    assert detect_cashout(**_base_kwargs(
        markets=[("PTS", 15.0, 18.6)],   # gap 3.6
    )) is None
