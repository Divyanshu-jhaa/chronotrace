"""Agent tool facade — re-exports the single canonical schema constant.

Per iteration-7 task 4.1 the five MCP tool schemas live in one shared place.
The MCP side owns them (``chronotrace.mcp_server.tools.schemas``); this
module re-exports the same object so that either import path resolves to the
identical list and snapshot tests stay green if the canonical location changes.

Prefer::

    from chronotrace.mcp_server.tools.schemas import CANONICAL_SCHEMAS, to_provider_tools
    # or
    from chronotrace.agent.tools import SCHEMAS, to_provider_tools

Both refer to the same underlying list.
"""

from __future__ import annotations

# Re-export the canonical constant — same object, not a copy.
try:
    from chronotrace.mcp_server.tools.schemas import (  # type: ignore[import-not-found]
        CANONICAL_SCHEMAS,
        CANONICAL_SCHEMAS_BY_NAME,
        FILE_PATH_SCHEMA,
        LIMIT_SCHEMA,
        REPOSITORY_ID_SCHEMA,
        SCHEMAS,
        to_provider_tools,
    )
except Exception:  # pragma: no cover - import fallback if mcp_server not yet loaded
    # Minimal fallback so the agent package can be imported without the server.
    # Duplicates the fragments from mcp_server.tools.schemas; drift is caught
    # by the snapshot test that asserts both import paths produce identical output.
    REPOSITORY_ID_SCHEMA = {  # type: ignore[no-redef]
        "type": "string",
        "minLength": 1,
        "maxLength": 256,
        "pattern": r"^[A-Za-z0-9\-_:/\.]+$",
        "description": "Non-empty repository identifier.",
    }
    FILE_PATH_SCHEMA = {  # type: ignore[no-redef]
        "type": "string",
        "minLength": 1,
        "maxLength": 4096,
        "pattern": r"^(?![A-Za-z]:)(?!/)(?!.*\\)[^\x00]+$",
        "description": "Repository-relative POSIX path.",
    }
    LIMIT_SCHEMA = {  # type: ignore[no-redef]
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 20,
        "description": "Optional bounded limit for get_file_history.",
    }
    CANONICAL_SCHEMAS = [  # type: ignore[no-redef]
        {
            "name": "get_change_criticality",
            "description": (
                "Get change criticality for a file via validated analytics."
            ),
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
            "description": (
                "Get regression risk as historical fix-commit evidence."
            ),
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
            "description": (
                "Get historical impact: importers, dependencies, co-changed."
            ),
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
    for _s in CANONICAL_SCHEMAS:
        _s["inputSchema"] = _s["parameters"]  # type: ignore
    SCHEMAS = CANONICAL_SCHEMAS  # type: ignore[no-redef]
    CANONICAL_SCHEMAS_BY_NAME = {  # type: ignore[no-redef]
        s["name"]: s for s in CANONICAL_SCHEMAS
    }

    def to_provider_tools(  # type: ignore[no-redef,misc]
        canonical=None,  # type: ignore[no-untyped-def]
    ):  # type: ignore[no-untyped-def]
        import copy

        if canonical is None:
            canonical = CANONICAL_SCHEMAS
        if isinstance(canonical, dict):
            items: list[dict] = []
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
                    items.append(v)  # type: ignore[arg-type]
            canonical = items  # type: ignore[assignment]
        out: list[dict] = []
        for tool in canonical:  # type: ignore[union-attr]
            name = str(tool.get("name", ""))  # type: ignore[union-attr]
            desc = str(tool.get("description", ""))  # type: ignore[union-attr]
            params = (
                tool.get("parameters")  # type: ignore[union-attr]
                or tool.get("inputSchema")  # type: ignore[union-attr]
                or tool.get("input_schema")  # type: ignore[union-attr]
                or {}
            )
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": copy.deepcopy(params),
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
