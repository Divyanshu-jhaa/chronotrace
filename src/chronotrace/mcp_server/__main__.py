"""MCP server entry point for `python -m chronotrace.mcp_server`."""

from __future__ import annotations

from chronotrace.mcp_server.server import _cli as _server_cli


def main(argv: list[str] | None = None) -> None:
    _server_cli(argv)


if __name__ == "__main__":
    main()
