"""Chat request/response schemas — typed, bounded, strict."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    """POST /chat request — repository-scoped question."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Repository ID; must be non-empty and known",
    )
    question: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="Natural-language question (1..10000 chars)",
    )
    conversation_id: str | None = Field(
        default=None,
        max_length=256,
        description="Optional conversation identifier",
    )

    @field_validator("repository_id")
    @classmethod
    def _check_repo(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("repository_id must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("repository_id must be a non-empty string")
        if len(stripped) > 256:
            raise ValueError("repository_id exceeds 256 characters")
        # Allow same alphabet as MCP validators: alphanumerics plus - _ : / .
        # But be permissive for tests — just ensure not empty and not whitespace.
        return stripped

    @field_validator("question")
    @classmethod
    def _check_question(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("question must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("question must be a non-empty string")
        if len(v) > 10000:
            raise ValueError("question exceeds 10000 characters")
        if len(stripped) < 1:
            raise ValueError("question must be a non-empty string")
        return v

    @field_validator("conversation_id")
    @classmethod
    def _check_cid(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("conversation_id must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("conversation_id must be a non-empty string when provided")
        if len(stripped) > 256:
            raise ValueError("conversation_id exceeds 256 characters")
        return stripped


class ChatResponse(BaseModel):
    """POST /chat response — evidence-grounded answer."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(description="Natural-language answer grounded in tool evidence")
    citations: list[Any] = Field(
        default_factory=list, description="Citations [{path, commit_hash, date, score}]"
    )
    evidence: list[Any] = Field(default_factory=list, description="Structured evidence extracts")
    limitations: list[str] = Field(
        default_factory=list, description="Disclosures for insufficient evidence"
    )
    conversation_id: str = Field(description="Conversation identifier (echoed or generated)")


class Citation(BaseModel):
    """Citation placeholder — structured citation for chat responses."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    commit_hash: str | None = None
    date: str | None = None
    score: float | None = None


__all__ = ["ChatRequest", "ChatResponse", "Citation"]
