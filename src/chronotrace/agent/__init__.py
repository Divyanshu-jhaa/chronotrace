"""Evidence-first agent package.

This package owns all LLM interaction (Meta Model API Muse Spark) and is
additive over the deterministic analytics/MCP path. Submodules:

- ``model_client``: isolated provider HTTP client
- ``exceptions``: typed domain errors
- ``agent``: high-level facade
- ``response``: typed AgentResponse
"""

from __future__ import annotations

from .agent import Agent
from .exceptions import (
    AgentError,
    AuthenticationError,
    MaxIterationsExceeded,
    MCPError,
    ModelTimeoutError,
    NoFinalResponseError,
    ProviderError,
    RateLimitError,
    ToolValidationError,
)
from .response import AgentResponse

__all__ = [
    "Agent",
    "AgentResponse",
    "AgentError",
    "AuthenticationError",
    "MCPError",
    "MaxIterationsExceeded",
    "ModelTimeoutError",
    "NoFinalResponseError",
    "ProviderError",
    "RateLimitError",
    "ToolValidationError",
]
