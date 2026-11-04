"""MCP client over Streamable HTTP — wraps Python mcp SDK.

Uses the verified transport route shared with the Iteration 6 MCP server
via :data:`chronotrace.mcp_server.server.MCP_HTTP_PATH`, not a hard-coded URL.

Lifecycle: ``connect`` → ``list_tools``/``call_tool`` → ``close`` (idempotent, drains).
Error mapping:
* ``validation_error`` / ``not_found`` / ``insufficient_history`` → returned as
  structured :class:`ToolResult` content (no exception).
* transport failure / connection refused / ``graph_unavailable`` → :class:`MCPError`.

Concurrency: ``_in_flight`` counter + ``_in_flight_lock``; ``close`` waits
up to 5 s for in-flight calls before tearing down the transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from chronotrace.mcp_server.server import MCP_HTTP_PATH

from .exceptions import MCPError


@dataclass
class ToolDefinition:
    """Normalized view of an MCP tool for the agent."""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    raw: Any | None = None


@dataclass
class ToolResult:
    """Result of ``call_tool`` — always structured content, never a transport exception.

    ``validation_error``/``not_found``/``insufficient_history`` are returned here
    with ``is_error=True`` so the tool loop can surface them to the model.
    ``graph_unavailable`` is mapped to :class:`MCPError` instead.
    """

    content: Any
    text: str | None = None
    is_error: bool = False
    structured_content: Any | None = None
    raw: Any | None = None


class MCPClient:
    """Streamable HTTP MCP client.

    Constructor takes ``host``/``port``/``path`` from validated config
    and the shared route constant :data:`MCP_HTTP_PATH`.

    Parameters
    ----------
    host:
        MCP server host (e.g. ``"127.0.0.1"``).
    port:
        MCP server port.
    path:
        HTTP path for the Streamable HTTP endpoint. Defaults to
        :data:`chronotrace.mcp_server.server.MCP_HTTP_PATH` (``/mcp``).
    config:
        Optional :class:`chronotrace.config.models.ChronoTraceConfig` or
        :class:`chronotrace.config.models.McpConfig`. When supplied,
        ``host``/``port`` are taken from it unless explicitly passed.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        path: str | None = None,
        *,
        config: Any | None = None,
    ) -> None:
        # Resolve from validated config if given
        if config is not None:
            # config may be ChronoTraceConfig (has .mcp) or McpConfig itself
            mcp_cfg = getattr(config, "mcp", None)
            if mcp_cfg is None and hasattr(config, "host") and hasattr(config, "port"):
                mcp_cfg = config
            if mcp_cfg is not None:
                if host is None:
                    try:
                        host = mcp_cfg.host  # type: ignore[attr-defined]
                    except Exception:
                        pass
                if port is None:
                    try:
                        port = mcp_cfg.port  # type: ignore[attr-defined]
                    except Exception:
                        pass

        if host is None:
            host = "127.0.0.1"
        if port is None:
            port = 8001
        if path is None:
            path = MCP_HTTP_PATH
        if not path.startswith("/"):
            path = "/" + path
        self.host: str = host
        self.port: int = int(port)
        self.path: str = path
        self.url: str = f"http://{host}:{port}{path}"

        self._session: Any | None = None
        self._exit_stack: contextlib.AsyncExitStack | None = None
        self._client_streams: Any | None = None
        self._connected: bool = False
        self._closed: bool = False
        self._in_flight: int = 0
        self._in_flight_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Establish Streamable HTTP transport and initialize MCP session."""
        if self._closed:
            raise MCPError("MCPClient is closed")
        if self._connected and self._session is not None:
            return
        try:
            from mcp import ClientSession  # type: ignore
            from mcp.client.streamable_http import streamable_http_client  # type: ignore
        except ImportError as exc:
            raise MCPError(f"mcp SDK not installed: {exc}") from exc

        # Defensive: close stale stack before re-connecting
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass
            except BaseException:
                pass
            self._exit_stack = None
            self._session = None
            self._connected = False

        try:
            self._exit_stack = contextlib.AsyncExitStack()
            await self._exit_stack.__aenter__()
            # enter transport — yields (read, write)
            self._client_streams = await self._exit_stack.enter_async_context(
                streamable_http_client(self.url)
            )
            if isinstance(self._client_streams, tuple) and len(self._client_streams) >= 2:
                read = self._client_streams[0]
                write = self._client_streams[1]
            else:
                # fallback for unexpected shape
                try:
                    read = self._client_streams[0]  # type: ignore[index]
                    write = self._client_streams[1]  # type: ignore[index]
                except Exception as exc2:
                    raise MCPError(
                        f"Unexpected transport streams from "
                        f"{self.url}: {self._client_streams!r}"
                    ) from exc2
            self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            # Bound initialize to avoid hanging on unreachable hosts; map timeout/cancel to MCPError
            try:
                await asyncio.wait_for(self._session.initialize(), timeout=5.0)  # type: ignore[union-attr,misc]
            except asyncio.TimeoutError as te:
                raise MCPError(f"MCP transport timeout connecting to {self.url}: {te}") from te
            self._connected = True
        except MCPError:
            raise
        except Exception as exc:
            # cleanup partially opened stack
            if self._exit_stack is not None:
                try:
                    await self._exit_stack.aclose()
                except Exception:
                    pass
                except BaseException:
                    pass
                self._exit_stack = None
            self._session = None
            self._client_streams = None
            self._connected = False
            raise MCPError(f"MCP transport failure connecting to {self.url}: {exc}") from exc
        except BaseException as exc:  # noqa: BLE001 — asyncio.CancelledError is BaseException in Python 3.12
            if isinstance(exc, asyncio.CancelledError):
                if self._exit_stack is not None:
                    try:
                        await self._exit_stack.aclose()
                    except Exception:
                        pass
                    except BaseException:
                        pass
                    self._exit_stack = None
                self._session = None
                self._client_streams = None
                self._connected = False
                raise MCPError(f"MCP transport failure connecting to {self.url}: {exc}") from exc
            raise

    async def list_tools(self) -> list[ToolDefinition]:
        """List tools exposed by the MCP server."""
        if self._closed:
            raise MCPError("MCPClient is closed")
        if not self._connected or self._session is None:
            await self.connect()
        # in-flight accounting
        async with self._in_flight_lock:
            self._in_flight += 1
        try:
            try:
                result = await self._session.list_tools()  # type: ignore[union-attr,misc]
            except MCPError:
                raise
            except Exception as exc:
                raise MCPError(f"MCP transport failure listing tools at {self.url}: {exc}") from exc
            except BaseException as exc:  # noqa: BLE001 — asyncio.CancelledError is BaseException in Python 3.12
                if isinstance(exc, asyncio.CancelledError):
                    raise MCPError(f"MCP transport failure listing tools at {self.url}: {exc}") from exc
                raise
            tools = getattr(result, "tools", result)
            if tools is None:
                tools = []
            definitions: list[ToolDefinition] = []
            for t in tools:
                if isinstance(t, dict):
                    name = t.get("name") or ""
                    desc = t.get("description")
                    schema = (
                        t.get("inputSchema")
                        or t.get("input_schema")
                        or t.get("parameters")
                        or {}
                    )
                else:
                    name = getattr(t, "name", "") or ""
                    desc = getattr(t, "description", None)
                    schema = getattr(t, "inputSchema", None)
                    if schema is None:
                        schema = getattr(t, "input_schema", None)
                    if schema is None and hasattr(t, "parameters"):
                        schema = t.parameters
                    if schema is None:
                        schema = {}
                definitions.append(
                    ToolDefinition(
                        name=str(name),
                        description=desc,
                        input_schema=dict(schema) if isinstance(schema, dict) else {},
                        raw=t,
                    )
                )
            return definitions
        finally:
            async with self._in_flight_lock:
                self._in_flight = max(0, self._in_flight - 1)

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Call a tool by name.

        ``validation_error``/``not_found``/``insufficient_history`` are returned
        as :class:`ToolResult` (``is_error=True``). ``graph_unavailable`` and
        transport failures raise :class:`MCPError`.
        """
        if self._closed:
            raise MCPError("MCPClient is closed")
        if not self._connected or self._session is None:
            await self.connect()
        async with self._in_flight_lock:
            self._in_flight += 1
        try:
            try:
                raw = await self._session.call_tool(name, arguments=args or {})  # type: ignore[union-attr,misc]
            except MCPError:
                raise
            except Exception as exc:
                raise MCPError(
                    f"MCP transport failure calling {name} at {self.url}: {exc}"
                ) from exc
            except BaseException as exc:  # noqa: BLE001 — asyncio.CancelledError is BaseException in Python 3.12
                if isinstance(exc, asyncio.CancelledError):
                    raise MCPError(
                        f"MCP transport failure calling {name} at {self.url}: {exc}"
                    ) from exc
                raise

            # raw is CallToolResult with .content (list[TextContent]),
            text: str | None = None
            payload: Any = None
            structured_content: Any | None = getattr(raw, "structured_content", None)
            if structured_content is None:
                structured_content = getattr(raw, "structuredContent", None)
            # Prefer structured_content if present, else decode text content
            content_blocks = getattr(raw, "content", None)
            if content_blocks is None:
                content_blocks = getattr(raw, "data", None)
            if isinstance(content_blocks, list) and len(content_blocks) > 0:
                first = content_blocks[0]
                t = getattr(first, "text", None)
                if t is None:
                    t = getattr(first, "data", None)
                if t is None:
                    # fallback to str(first)
                    try:
                        t = str(first)
                    except Exception:
                        t = None
                if isinstance(t, str):
                    text = t
                    # try to parse JSON payload
                    try:
                        payload = json.loads(t)
                    except Exception:
                        payload = {"raw": t, "text": t}
                elif t is not None:
                    payload = t
                    text = json.dumps(t) if isinstance(t, dict) else str(t)
            if payload is None and structured_content is not None:
                payload = structured_content
                try:
                    if isinstance(structured_content, dict):
                        text = json.dumps(structured_content)
                    else:
                        text = str(structured_content)
                except Exception:
                    text = str(structured_content)
            if payload is None:
                # empty result — treat as ok with empty dict
                payload = {}
                text = text or ""

            is_error = bool(
                getattr(raw, "is_error", False)
                or getattr(raw, "isError", False)
                or getattr(raw, "is_error", False)
            )
            # payload-level status check
            status: str | None = None
            if isinstance(payload, dict):
                status = payload.get("status")
                # some envelopes nest inside structured_content
                if status is None and isinstance(structured_content, dict):
                    status = structured_content.get("status")
            if status == "graph_unavailable":
                # transport/graph failure -> MCPError (deterministic)
                msg = ""
                if isinstance(payload, dict):
                    err = payload.get("error") or {}
                    if isinstance(err, dict):
                        msg = err.get("message") or str(err)
                    else:
                        msg = str(payload)
                raise MCPError(f"Graph unavailable for tool {name}: {msg or payload}")
            # validation_error/not_found/insufficient_history -> return as ToolResult content
            return ToolResult(
                content=payload,
                text=text,
                is_error=is_error,
                structured_content=structured_content,
                raw=raw,
            )
        finally:
            async with self._in_flight_lock:
                self._in_flight = max(0, self._in_flight - 1)

    async def close(self) -> None:
        """Close the transport, draining in-flight calls. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # drain in-flight calls up to 5 s
        start = time.monotonic()
        while True:
            async with self._in_flight_lock:
                remaining = self._in_flight
            if remaining == 0:
                break
            if time.monotonic() - start >= 5.0:
                break
            await asyncio.sleep(0.05)
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass
            self._exit_stack = None
        self._session = None
        self._client_streams = None
        self._connected = False

    # async context manager support
    async def __aenter__(self) -> MCPClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"MCPClient(url={self.url!r}, connected={self._connected}, closed={self._closed})"


__all__ = ["MCPClient", "MCPError", "MCP_HTTP_PATH", "ToolDefinition", "ToolResult"]
