"""
Textos com código de tradução (out/2026).

Texto que vai pronto pro usuário (risco de blowout, motivo da projeção, notas
de rotação, cashout…) continua saindo em PT — contrato inalterado —, mas
carrega um CÓDIGO + PARÂMETROS pra o front traduzir (EN/ES). O schema expõe
isso num campo `*_i18n` ao lado do texto (ver schemas/i18n_schemas.py).

    reason = coded("blowout.live_gap", "Diferença de {diff} pts no {period}",
                   diff=12, period="Q3")
    reason == "Diferença de 12 pts no Q3"     # é um str normal
    text_code(reason) == {"code": "blowout.live_gap", "params": {"diff": "12", "period": "Q3"}}

Parâmetros viram texto já formatado (o front só interpola). Não usar palavra
em PT como parâmetro — variação de texto vira outro código.

Concatenar ou reformatar um CodedText devolve `str` comum: o código some e o
front mostra o PT (degrada sem quebrar). O código não depende de idioma, então
o cache continua único pra todos os usuários.
"""

from __future__ import annotations

from typing import Any, Optional


class CodedText(str):
    """`str` (texto em PT já formatado) que também guarda `code` e `params`."""

    code: str
    params: dict[str, str]

    def __new__(cls, value: str, code: str = "", params: Optional[dict[str, str]] = None):
        obj = super().__new__(cls, value)
        obj.code = code
        obj.params = dict(params or {})
        return obj


def coded(code: str, template: str, **params: Any) -> CodedText:
    """Formata `template` com `params` e anexa o código de tradução."""
    as_text = {k: str(v) for k, v in params.items()}
    return CodedText(template.format(**as_text), code, as_text)


def text_code(value: Any) -> Optional[dict]:
    """{"code", "params"} de um CodedText; None pra texto sem código."""
    if isinstance(value, CodedText) and value.code:
        return {"code": value.code, "params": dict(value.params)}
    return None
