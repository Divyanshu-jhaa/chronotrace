"""Typed agent response — evidence-first, deterministic.

``AgentResponse`` is the sole return type of :meth:`chronotrace.agent.agent.Agent.ask`.
All repository claims must be traceable to ``tool_calls_made`` / ``evidence_references``;
when evidence is insufficient the answer states that explicitly and ``limitations``
carries the disclosure.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentResponse(BaseModel):
    """Evidence-grounded answer for one repository-scoped conversation turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str = Field(description="Final natural-language answer; cites only tool output")
    status: str = Field(
        default="ok",
        description="Outcome: ok | max_iterations_exceeded | insufficient_evidence | error",
    )
    tool_calls_made: list[Any] = Field(
        default_factory=list,
        description="Tool invocations made during the loop, in provider format",
    )
    evidence_references: list[Any] = Field(
        default_factory=list,
        description="Structured evidence extracts backing the answer (paths, hashes, dates, scores)",
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="Disclosures when evidence is insufficient or limits were reached",
    )
    repository_id: str = Field(description="Repository the conversation is bound to")

    def model_dump_with_citations(self) -> dict[str, Any]:
        return self.model_dump()


__all__ = ["AgentResponse"]
