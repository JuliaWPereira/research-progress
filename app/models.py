"""Contratos de entrada da API.

Os limites nao sao decorativos: `depth` e `top_n` altos multiplicam requisicoes ao
OpenAlex e nos no canvas. Validar aqui evita que um typo na interface dispare um
crawl de dezenas de milhares de artigos.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MarkStatus = Literal["unread", "interested", "read", "discarded"]


class ResolveRequest(BaseModel):
    input: str = Field(min_length=1)

    @field_validator("input")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("input vazio")
        return v.strip()


class CrawlRequest(BaseModel):
    seed_id: str = Field(min_length=1)
    depth: int = Field(default=2, ge=1, le=5)
    top_n: int = Field(default=20, ge=1, le=200)
    min_year: int | None = Field(default=None, ge=1500, le=2100)
    max_nodes: int = Field(default=500, ge=1, le=5000)


class ExpandRequest(BaseModel):
    paper_id: str = Field(min_length=1)
    top_n: int = Field(default=20, ge=1, le=200)
    min_year: int | None = Field(default=None, ge=1500, le=2100)


class MarkRequest(BaseModel):
    status: MarkStatus
    note: str = Field(default="", max_length=4000)


class ActiveSearchRequest(BaseModel):
    id: str | None = None
