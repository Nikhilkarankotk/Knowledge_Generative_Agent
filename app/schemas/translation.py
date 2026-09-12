"""Equivalent of ``TranslationResult.java``."""


from pydantic import BaseModel, ConfigDict


class TranslationResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    lang: str | None = None
    langName: str | None = None
    translatedText: str | None = None
