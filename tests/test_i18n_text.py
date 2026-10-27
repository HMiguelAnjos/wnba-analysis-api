"""
Textos do backend com código de tradução (out/2026).

Cobertura:
  • CodedText: é o mesmo str em PT, carrega code/params, sobrevive a cópia.
  • Schemas preenchem `*_i18n` sozinhos (texto e lista de notas).
  • Geradores: risco de blowout, impacto no jogador, motivo da projeção,
    cashout, notas de rotação.

(Fork WNBA: sem auth/pagamento, então os testes de HTTP error e email do
NBA ficaram de fora — não há rotas correspondentes aqui.)
"""
from __future__ import annotations

import copy
import json
import pickle

from src.schemas.live_schemas import (
    BlowoutRiskSchema,
    PaceProjectionSchema,
    RotationContextSchema,
)
from src.services.cashout import detect_cashout
from src.services.projection import ProjectionEngine
from src.services.rotation import RotationProfile
from src.services.rotation.rotation_context import build_context
from src.utils.i18n_text import CodedText, coded, text_code
from src.utils.stats import calculate_blowout_risk, calculate_player_blowout_impact


# ─── CodedText ───────────────────────────────────────────────────────────


def test_coded_is_the_portuguese_string_with_code():
    r = coded("blowout.gap", "Diferença de {diff} pts no {period}", diff=12, period="Q3")
    assert r == "Diferença de 12 pts no Q3"
    assert isinstance(r, str)
    assert text_code(r) == {"code": "blowout.gap", "params": {"diff": "12", "period": "Q3"}}


def test_plain_string_has_no_code():
    assert text_code("texto livre") is None
    assert text_code(None) is None


def test_coded_survives_copy_pickle_and_json():
    r = coded("projection.hot", "Jogador quente (heat {heat})", heat="+0.45")
    for clone in (copy.deepcopy(r), pickle.loads(pickle.dumps(r))):
        assert isinstance(clone, CodedText)
        assert (clone, clone.code, clone.params) == (r, r.code, r.params)
    assert json.loads(json.dumps({"r": r})) == {"r": "Jogador quente (heat +0.45)"}


def test_concatenation_drops_the_code():
    assert text_code(coded("x.y", "abc") + " def") is None


# ─── Schemas ─────────────────────────────────────────────────────────────


def test_schema_fills_reason_i18n():
    s = BlowoutRiskSchema(percentage=40, level="medium", reason=coded("blowout.gap", "Gap {d}", d=9))
    assert s.reason == "Gap 9"
    assert s.reason_i18n.code == "blowout.gap" and s.reason_i18n.params == {"d": "9"}
    assert BlowoutRiskSchema(percentage=0, level="low", reason="sem código").reason_i18n is None


def test_schema_fills_notes_i18n_aligned_with_notes():
    s = RotationContextSchema(
        available=True, expected_remaining_minutes=10.0, current_rotation_status="EXPECTED_REST",
        closing_game_probability=0.0, rotation_confidence=0.5, blowout_risk="LOW",
        notes=[coded("rotation.likely_done", "Encerrou"), "livre"],
    )
    assert [n.code if n else None for n in s.notes_i18n] == ["rotation.likely_done", None]


def test_schema_roundtrip_keeps_i18n():
    s = PaceProjectionSchema(low=1, expected=2, high=3, reason=coded("projection.on_pace", "No ritmo"))
    again = PaceProjectionSchema.model_validate(json.loads(s.model_dump_json()))
    assert again.reason_i18n.code == "projection.on_pace"


# ─── Geradores ───────────────────────────────────────────────────────────


def test_blowout_risk_reasons_are_coded():
    _, _, live = calculate_blowout_risk(3, "05:00", 90, 70, "in_progress", False)
    assert text_code(live)["code"].startswith("blowout.")
    assert text_code(live)["params"]["diff"] == "20"
    _, _, final = calculate_blowout_risk(4, "", 120, 105, "final", False)
    assert text_code(final) == {"code": "blowout.final_clear", "params": {"margin": "15"}}
    assert final == "Vitória clara — margem de 15 pontos"


def test_player_blowout_impact_reason_is_coded():
    d = calculate_player_blowout_impact(
        player_minutes=30.0, is_starter=True, game_blowout_pct=80, game_blowout_level="high",
    )
    assert text_code(d["reason"])["code"].startswith("impact.")


def test_projection_reason_is_coded():
    out = ProjectionEngine().project(20, 14.0, 15.0, 32.0, period=2, game_minutes_remaining=26.0,
                                     heat_score=0.6)
    code = text_code(out["reason"])
    assert code is not None and code["code"].startswith("projection.")
    assert text_code(ProjectionEngine().project(0, 0.0, 15.0, 32.0)["reason"])["code"] == "projection.not_played"


def test_cashout_reason_is_coded_and_unchanged():
    r = detect_cashout(
        markets=[("PTS", 15.0, 16.5), ("REB", 4.0, 9.5), ("AST", 3.0, 6.5)],
        on_court=False, status="ACTIVE", is_starter=True, usage=0.55, foul_trouble=False,
        rotation_available=True, expected_remaining_minutes=6.0, closing_game_probability=0.62,
        current_rotation_status="EXPECTED_REST", rotation_confidence=0.70, blowout_risk="LOW",
    )
    assert r["reason"] == (
        "Jogador titular no banco agora — tem 15 pontos, linha 16.5 (faltam 1.5). NBA Rotation "
        "indica retorno provável (~6 min restantes, 62% de fechar jogo). Atenção pra cashout: a "
        "linha tende a subir quando ele voltar."
    )
    assert text_code(r["reason"]) == {
        "code": "cashout.starter_closing",
        "params": {"current": "15", "market": "PTS", "line": "16.5", "gap": "1.5",
                   "minutes": "6", "closing": "62"},
    }


def test_rotation_notes_are_coded():
    profile = RotationProfile(
        player_id=1, total_minutes=48.0, minute_probabilities=[1.0] * 48, sample_games=10,
        is_fallback=False, clutch_usage={}, blowout_risk={},
    )
    ctx = build_context(
        profile=profile, expected_remaining_minutes=20.0, period=1, clock_minutes_remaining=6,
        is_player_on_court=False, score_difference=0, is_close_game=True, minutes_played=0.0,
    )
    assert ctx.notes and all(text_code(n) for n in ctx.notes)
    assert text_code(ctx.notes[0])["code"] == "rotation.less_than_expected"
