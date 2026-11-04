"""Canonical MCP tool schemas — single shared source of truth.

This module is the canonical origin for the five MCP tool definitions
(``get_change_criticality``, ``get_regression_risk``, ``get_historical_impact``,
``recommend_tests``, ``get_file_history``).  Both the MCP server and the
evidence-first agent import from here so that MCP validation and the
model's ``tools`` payload cannot drift.

JSON Schema constraints mirror :mod:`chronotrace.mcp_server.tools.validation`:
- ``repository_id``: non-empty string, 1..256, stable-ID alphabet, whitespace trimmed.
- ``file_path``: repository-relative POSIX path, 1..4096, no absolute, no
  traversal ``..``, no backslash, no NUL.
- ``limit`` (``get_file_history`` only): integer 1..100, default 20.

Translation to the provider's function-tool format is a pure function
:data:`to_provider_tools` that snapshot tests cover.

Canonical import paths (both resolve to the same object):
- ``chronotrace.mcp_server.tools.schemas.CANONICAL_SCHEMAS``
- ``chronotrace.mcp_server.tools.schemas.SCHEMAS``
- ``chronotrace.agent.tools.SCHEMAS`` / ``chronotrace.agent.tools.CANONICAL_SCHEMAS``
"""

from __future__ import annotations

import copy
from typing import Any

# ---------------------------------------------------------------------------
# Shared JSON Schema fragments
# ---------------------------------------------------------------------------

REPOSITORY_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 256,
    "pattern": r"^[A-Za-z0-9\-_:/\.]+$",
    "description": (
        "Non-empty repository identifier; stable-ID alphabet (alphanumerics, "
        "'-', '_', ':', '/', '.'); 1..256 characters; whitespace is trimmed "
        "before validation."
    ),
}

FILE_PATH_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
    # Reject Windows drive, leading slash, backslash, and NUL byte at the
    # JSON-Schema level.  Fine-grained traversal/segment checks are enforced
    # by validate_file_path before any graph access.
    "pattern": r"^(?![A-Za-z]:)(?!/)(?!.*\\)[^\x00]+$",
    "description": (
        "Repository-relative POSIX path (e.g., 'src/foo/bar.py'); must not be "
        "absolute, must not contain parent traversal '..' segments, backslash, "
        "or NUL byte; normalized via posixpath.normpath."
    ),
}

LIMIT_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 100,
    "default": 20,
    "description": "Optional bounded limit for get_file_history (1..100, default 20).",
}

# ---------------------------------------------------------------------------
# Canonical schemas — single shared constant
# ---------------------------------------------------------------------------

CANONICAL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_change_criticality",
        "description": "Get change criticality for a file via validated analytics.",
        "parameters": {
            "type": "object",
            "properties": {
                "repository_id": REPOSITORY_ID_SCHEMA,
                "file_path": FILE_PATH_SCHEMA,
            },
            "required": ["repository_id", "file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_regression_risk",
        "description": "Get regression risk as historical fix-commit evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "repository_id": REPOSITORY_ID_SCHEMA,
                "file_path": FILE_PATH_SCHEMA,
            },
            "required": ["repository_id", "file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_historical_impact",
        "description": "Get historical impact: importers, dependencies, co-changed.",
        "parameters": {
            "type": "object",
            "properties": {
                "repository_id": REPOSITORY_ID_SCHEMA,
                "file_path": FILE_PATH_SCHEMA,
            },
            "required": ["repository_id", "file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recommend_tests",
        "description": "Recommend candidate test files.",
        "parameters": {
            "type": "object",
            "properties": {
                "repository_id": REPOSITORY_ID_SCHEMA,
                "file_path": FILE_PATH_SCHEMA,
            },
            "required": ["repository_id", "file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_file_history",
        "description": "Get bounded commit history for a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "repository_id": REPOSITORY_ID_SCHEMA,
                "file_path": FILE_PATH_SCHEMA,
                "limit": LIMIT_SCHEMA,
            },
            "required": ["repository_id", "file_path"],
            "additionalProperties": False,
        },
    },
]

# Back-compat aliases — same object, not a copy, so drift is impossible.
SCHEMAS: list[dict[str, Any]] = CANONICAL_SCHEMAS
CANONICAL_SCHEMAS_BY_NAME: dict[str, dict[str, Any]] = {s["name"]: s for s in CANONICAL_SCHEMAS}

# Ensure each entry also exposes ``inputSchema`` for MCP SDK callers that
# expect that key (points to same object as ``parameters``).
for _s in CANONICAL_SCHEMAS:
    _s["inputSchema"] = _s["parameters"]  # type: ignore[typeddict-item]


# ---------------------------------------------------------------------------
# Pure provider translation
# ---------------------------------------------------------------------------

def to_provider_tools(
    canonical: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Translate canonical schemas to provider function-tool format.

    Pure function — no I/O, no mutation of the input.

    Each canonical entry ``{name, description, parameters|inputSchema}`` becomes::

        {type: "function", function: {name, description, parameters}}

    Args:
        canonical: Canonical schemas list, or dict mapping name->schema, or
            ``None`` to use the shared :data:`CANONICAL_SCHEMAS`.

    Returns:
        New list of provider-shaped tool definitions, one per canonical tool
        in iteration order.
    """
    if canonical is None:
        canonical = CANONICAL_SCHEMAS

    # Normalize dict form {name: schema} -> list form.
    if isinstance(canonical, dict):
        # dict mapping name->schema; preserve insertion order
        items: list[dict[str, Any]] = []
        for k, v in canonical.items():
            if isinstance(v, dict) and "name" not in v:
                items.append(
                    {
                        "name": k,
                        "description": v.get("description", ""),
                        "parameters": v,
                    }
                )
            else:
                if isinstance(v, dict):
                    items.append(v)
                else:
                    items.append(
                        {"name": k, "description": "", "parameters": v}
                    )
        canonical_list = items
    else:
        canonical_list = canonical

    out: list[dict[str, Any]] = []
    for tool in canonical_list:
        name = str(tool.get("name", ""))
        description = str(tool.get("description", ""))
        # Accept either ``parameters`` or ``inputSchema``/``input_schema`` as source.
        params: Any = tool.get("parameters")
        if params is None:
            params = tool.get("inputSchema")
        if params is None:
            params = tool.get("input_schema")
        if params is None:
            params = {}
        # Defensive deep copy so caller cannot mutate the shared constant via
        # the returned provider tools.
        params_copy = copy.deepcopy(params)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params_copy,
                },
            }
        )
    return out


__all__ = [
    "CANONICAL_SCHEMAS",
    "CANONICAL_SCHEMAS_BY_NAME",
    "FILE_PATH_SCHEMA",
    "LIMIT_SCHEMA",
    "REPOSITORY_ID_SCHEMA",
    "SCHEMAS",
    "to_provider_tools",
]
