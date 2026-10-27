"""Schemas de tradução de textos do backend (out/2026) — ver utils/i18n_text.py."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, model_validator

from src.utils.i18n_text import text_code


class TextCodeSchema(BaseModel):
    """Código de tradução de um texto do backend (o front traduz)."""
    code: str
    params: dict[str, str] = {}


class CodedTextModel(BaseModel):
    """Base de schemas com texto pro usuário. `I18N_FIELDS` mapeia campo de
    texto → campo `*_i18n`; se o texto chega como CodedText (ou lista deles),
    o `*_i18n` é preenchido sozinho — sem mexer em quem constrói o schema."""

    I18N_FIELDS: ClassVar[dict[str, str]] = {}

    @model_validator(mode="before")
    @classmethod
    def _fill_i18n(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for text_field, i18n_field in cls.I18N_FIELDS.items():
            if data.get(i18n_field) is not None or text_field not in data:
                continue
            value = data[text_field]
            if isinstance(value, (list, tuple)):
                codes = [text_code(v) for v in value]
                if any(codes):
                    data = {**data, i18n_field: codes}
            else:
                code = text_code(value)
                if code:
                    data = {**data, i18n_field: code}
        return data
