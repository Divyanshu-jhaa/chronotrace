"""MCP server package — ChronoTrace validated gateway.

Consumes validated ChronTraceConfig via explicit injection, never re-parses YAML.
"""

from __future__ import annotations

from .server import MCP_HTTP_PATH, MCP_SSE_PATH, create_mcp_server

__all__ = ["MCP_HTTP_PATH", "MCP_SSE_PATH", "create_mcp_server"]
