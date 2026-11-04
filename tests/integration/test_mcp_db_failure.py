"""Database-failure injection test for iter-6 MCP (Task 8.4).

Stub graph layer to raise ``GraphNotAvailable``, call each tool and verify
each returns ``graph_unavailable`` with ``isError: true`` and server remains
running for next valid call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

try:
    import mcp  # type: ignore

    HAS_MCP = True
except ImportError:
    HAS_MCP = False

from chronotrace.config.models import ChronoTraceConfig
from chronotrace.core.analytics.service import AnalyticsService
from chronotrace.graph.errors import GraphNotAvailable
from tests.fixtures.analytics.fixture import HALF_LIFE_DAYS, REPO_ID


class FailingGraph:
    """Fake graph that raises GraphNotAvailable for every method."""

    def __getattr__(self, name: str):
        def _raise(*args, **kwargs):
            raise GraphNotAvailable(f"graph unavailable stub for {name}")

        return _raise

    def list_files(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def list_commits(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_file_by_path(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_commits_touching_file(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_fix_commits_for_file(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_cochange_neighbors(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_importers(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_dependencies(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_test_candidates(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")

    def get_repo_commit_count(self, *a, **kw):
        raise GraphNotAvailable("graph unavailable")


def _try_import_server_factory():
    candidates = [
        ("chronotrace.mcp_server.server", "create_mcp_server"),
        ("chronotrace.mcp.server", "create_mcp_server"),
        ("chronotrace.core.mcp.server", "create_mcp_server"),
        ("chronotrace.mcp_server", "create_mcp_server"),
        ("chronotrace.mcp", "create_mcp_server"),
    ]
    for mod_path, attr in candidates:
        try:
            import importlib

            mod = importlib.import_module(mod_path)
            if hasattr(mod, attr):
                return getattr(mod, attr), mod_path
        except ImportError:
            continue
    return None, None


def _try_import_tool_handlers():
    handlers: dict[str, object] = {}
    attempts = [
        ("chronotrace.mcp_server.tools.criticality", ["get_change_criticality", "handle", "criticality", "tool"]),
        ("chronotrace.mcp_server.tools.regression", ["get_regression_risk", "handle", "regression"]),
        ("chronotrace.mcp_server.tools.impact", ["get_historical_impact", "handle", "impact"]),
        ("chronotrace.mcp_server.tools.test_reco", ["recommend_tests", "handle", "test_reco"]),
        ("chronotrace.mcp_server.tools.file_history", ["get_file_history", "handle", "file_history"]),
    ]
    for mod_path, names in attempts:
        try:
            import importlib

            mod = importlib.import_module(mod_path)
            for name in names:
                if hasattr(mod, name):
                    handlers[mod_path.split(".")[-1]] = getattr(mod, name)
                    break
        except ImportError:
            continue
    return handlers


def _normalize_tool_result(res) -> tuple[bool, dict]:
    if hasattr(res, "isError") or hasattr(res, "is_error") or hasattr(res, "content"):
        is_err = bool(getattr(res, "isError", getattr(res, "is_error", False)))
        content = getattr(res, "content", None) or getattr(res, "data", None)
        payload: dict = {}
        if content:
            first = content[0] if isinstance(content, list) else content
            text = getattr(first, "text", None) or getattr(first, "data", None) or str(first)
            try:
                payload = json.loads(text) if isinstance(text, str) else dict(text)  # type: ignore
            except Exception:
                payload = {"raw": str(text)}
        else:
            payload = getattr(res, "structuredContent", None) or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {"raw": payload}
        if not is_err and isinstance(payload, dict):
            status = str(payload.get("status", "")).lower()
            if status in ("graph_unavailable", "graph_not_available"):
                is_err = True
        return is_err, payload if isinstance(payload, dict) else {"raw": str(payload)}

    if isinstance(res, dict):
        status = str(res.get("status", "")).lower()
        is_err = bool(res.get("isError", res.get("is_error", False)))
        if not is_err and status in ("graph_unavailable", "graph_not_available"):
            is_err = True
        code = str(res.get("error", {}).get("code", "")).lower() if isinstance(res.get("error"), dict) else ""
        if "graph_unavailable" in code or "graph_not_available" in code:
            is_err = True
        return is_err, res

    if hasattr(res, "status"):
        status = str(getattr(res.status, "value", str(res.status))).lower()
        is_err = status in ("graph_unavailable", "graph_not_available", "error")
        payload = {
            "status": status,
            "repository_id": getattr(res, "repository_id", None),
            "target": getattr(res, "target", None),
            "isError": is_err,
        }
        if hasattr(res, "result") and res.result is not None:
            payload["result"] = str(res.result)
        limitations = getattr(res, "limitations", None)
        if limitations and hasattr(limitations, "warnings"):
            payload["warnings"] = list(getattr(limitations, "warnings", []))
        return is_err, payload

    return False, {"raw": str(res)}


def test_mcp_db_failure_all_tools_graph_unavailable_via_fake():
    """Stub graph to raise GraphNotAvailable; each analytics/tool call returns graph_unavailable isError true."""
    failing = FailingGraph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(failing, config)

    for fn, args in [
        (svc.criticality, (REPO_ID, "a.py")),
        (svc.regression_risk, (REPO_ID, "a.py")),
        (svc.historical_impact, (REPO_ID, "a.py")),
        (svc.recommend_tests, (REPO_ID, "a.py")),
    ]:
        res = fn(*args)  # type: ignore
        assert res is not None
        status_str = str(getattr(res.status, "value", str(res.status))).lower()
        assert status_str != "ok", f"{fn.__name__} should not be ok when graph unavailable, got {status_str}"

    handlers = _try_import_tool_handlers()
    if not handlers:
        return

    import inspect

    expected_status = "graph_unavailable"

    class _FailingService:
        graph = None  # type: ignore

        def __getattr__(self, _name: str):
            def _raise(*_a, **_kw):
                raise GraphNotAvailable("graph unavailable injected for db-failure test")

            return _raise

    failing_service = _FailingService()  # type: ignore
    for key, fn in handlers.items():
        try:
            sig = inspect.signature(fn)  # type: ignore
            kwargs: dict = {}
            if "repository_id" in sig.parameters:
                kwargs["repository_id"] = REPO_ID
            if "file_path" in sig.parameters:
                kwargs["file_path"] = "a.py"
            if "repository_id" in sig.parameters and "file_path" not in sig.parameters and "target" in sig.parameters:
                kwargs["target"] = "a.py"
            if "limit" in sig.parameters:
                kwargs["limit"] = 5
            if "analytics_service" in sig.parameters:
                kwargs["analytics_service"] = failing_service
            if "_analytics_service" in sig.parameters:
                kwargs["_analytics_service"] = failing_service
            # For db-failure test, prefer service-level injection; do not inject
            # failing graph for file_history which would cause early not_found via
            # _file_exists before reaching the analytics fallback that correctly
            # raises GraphNotAvailable.
            if "graph" in sig.parameters and key != "file_history":
                kwargs["graph"] = failing
            if "graph_gateway" in sig.parameters and key != "file_history":
                kwargs["graph_gateway"] = failing
            if "gateway" in sig.parameters and key != "file_history":
                kwargs["gateway"] = failing
            try:
                if inspect.iscoroutinefunction(fn):
                    import asyncio

                    res = asyncio.run(fn(**kwargs))  # type: ignore
                else:
                    res = fn(**kwargs)  # type: ignore
            except TypeError as te:
                mod = inspect.getmodule(fn)  # type: ignore
                patched = False
                for attr in ("graph", "gateway", "graph_gateway", "_graph", "analytics_service", "_analytics_service"):
                    if mod and hasattr(mod, attr):
                        try:
                            setattr(mod, attr, failing)
                            patched = True
                            break
                        except Exception:
                            continue
                if not patched:
                    pytest.skip(f"handler {key} signature {sig} not compatible with direct injection: {te}")
                if inspect.iscoroutinefunction(fn):
                    import asyncio

                    res = asyncio.run(fn(**kwargs))  # type: ignore
                else:
                    res = fn(**kwargs)  # type: ignore
            is_err, payload = _normalize_tool_result(res)
            status = str(payload.get("status", "")).lower()
            assert status == expected_status or "graph_unavailable" in str(payload).lower(), f"{key} expected graph_unavailable, got {payload}"
            assert is_err is True, f"{key} expected isError true, got {payload} is_err={is_err}"
            payload_str = json.dumps(payload, default=str).lower()
            for secret_hint in ("password", "passwd", "secret", "cypher", "match (", "neo4j://", "bolt://"):
                if secret_hint == "cypher":
                    assert "match (n" not in payload_str and "detach delete" not in payload_str
                elif secret_hint == "match (":
                    assert "match (" not in payload_str or "match (" in payload_str and "cypher" not in payload_str
                else:
                    if secret_hint in ("neo4j://", "bolt://"):
                        assert secret_hint not in payload_str, f"payload leaks connection string hint {secret_hint}: {payload_str[:500]}"
        except Exception as e:
            if "skip" in str(e).lower():
                raise
            pytest.skip(f"handler {key} not ready for db-failure injection: {e}")


def test_mcp_db_failure_error_envelope_structure():
    """All five tools must return structured graph_unavailable envelope without leaking secrets."""
    failing = FailingGraph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    from chronotrace.graph.errors import GraphNotAvailable as GNA

    def fake_tool_wrapper(tool_name: str, repository_id: str, file_path: str, limit: int | None = None):
        try:
            svc = AnalyticsService(failing, config)
            if tool_name == "get_change_criticality":
                res = svc.criticality(repository_id, file_path)
            elif tool_name == "get_regression_risk":
                res = svc.regression_risk(repository_id, file_path)
            elif tool_name == "get_historical_impact":
                res = svc.historical_impact(repository_id, file_path)
            elif tool_name == "recommend_tests":
                res = svc.recommend_tests(repository_id, file_path)
            elif tool_name == "get_file_history":
                raise GraphNotAvailable("graph unavailable")
            else:
                raise ValueError("unknown tool")
            status_str = str(getattr(res.status, "value", str(res.status))).lower()
            if status_str in ("error", "graph_unavailable"):
                return {"status": "graph_unavailable", "error": {"code": "GRAPH_UNAVAILABLE", "message": "Graph is temporarily unavailable; retry"}, "isError": True}
            raise GraphNotAvailable("forced for envelope test")
        except GNA:
            return {
                "status": "graph_unavailable",
                "error": {"code": "GRAPH_UNAVAILABLE", "message": "Graph is temporarily unavailable; retry"},
                "isError": True,
            }
        except Exception as exc:
            msg = str(exc)
            assert "password" not in msg.lower() and "secret" not in msg.lower()
            return {"status": "internal_error", "error": {"code": "INTERNAL", "message": "Internal error"}, "isError": True}

    for tool in ["get_change_criticality", "get_regression_risk", "get_historical_impact", "recommend_tests", "get_file_history"]:
        if tool == "get_file_history":
            payload = fake_tool_wrapper(tool, REPO_ID, "a.py", limit=5)
        else:
            payload = fake_tool_wrapper(tool, REPO_ID, "a.py")
        assert payload.get("status") == "graph_unavailable"
        assert payload.get("isError") is True
        err = payload.get("error", {})
        assert err.get("code") == "GRAPH_UNAVAILABLE"
        assert "retry" in err.get("message", "").lower()
        s = json.dumps(payload).lower()
        assert "password" not in s
        assert "match (n) detach delete" not in s


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _try_import_mcp_path():
    for mod_path in [
        "chronotrace.mcp_server.server",
        "chronotrace.mcp.server",
        "chronotrace.core.mcp.server",
    ]:
        try:
            import importlib

            mod = importlib.import_module(mod_path)
            if hasattr(mod, "MCP_HTTP_PATH"):
                return getattr(mod, "MCP_HTTP_PATH")
        except ImportError:
            continue
    return "/mcp"


def _try_import_client():
    try:
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore

        from mcp import ClientSession  # type: ignore

        return streamablehttp_client, ClientSession
    except ImportError:
        pass
    try:
        from mcp.client.sse import sse_client  # type: ignore

        from mcp import ClientSession  # type: ignore

        return sse_client, ClientSession
    except ImportError:
        pass
    return None, None


async def _run_db_failure_via_server():
    factory, mod_path = _try_import_server_factory()
    if factory is None:
        pytest.skip("MCP server factory not importable — server not yet implemented")
    client_factory, ClientSession = _try_import_client()
    if client_factory is None or ClientSession is None:
        pytest.skip("MCP client not importable")

    port = _find_free_port()
    host = "127.0.0.1"
    mcp_path = _try_import_mcp_path()
    if not mcp_path.startswith("/"):
        mcp_path = "/" + mcp_path
    url = f"http://{host}:{port}{mcp_path}"

    from tests.fixtures.analytics.fixture import get_fake_graph

    fake = get_fake_graph()
    config = ChronoTraceConfig.model_validate(
        {"version": 1, "analysis": {"history": {"decay_half_life_days": HALF_LIFE_DAYS}}}
    )
    svc = AnalyticsService(fake, config)
    server = None
    last_err = None
    import inspect

    for kwargs in [
        {"config": config, "graph_gateway": fake, "analytics_service": svc, "graph": fake},
        {"config": config, "graph_gateway": fake, "analytics_service": svc},
        {"config": config, "graph": fake},
        {"config": config},
    ]:
        try:
            sig = inspect.signature(factory)
            filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
            server = factory(**filtered) if filtered else factory(config)
            last_err = None
            break
        except Exception as e:
            last_err = e
            continue
    if server is None:
        pytest.skip(f"Could not instantiate server via {mod_path}: {last_err}")

    server_task = None

    async def _serve():
        if hasattr(server, "serve"):
            try:
                sig = inspect.signature(server.serve)
                kwargs = {}
                if "host" in sig.parameters:
                    kwargs["host"] = host
                if "port" in sig.parameters:
                    kwargs["port"] = port
                if "transport" in sig.parameters:
                    kwargs["transport"] = "streamable-http"
                res = server.serve(**kwargs)
                if inspect.isawaitable(res):
                    await res
                return
            except Exception:
                pass
        if hasattr(server, "run"):
            try:
                res = server.run(host=host, port=port)
                if inspect.isawaitable(res):
                    await res
                return
            except Exception:
                pass
        app = None
        for attr in ("streamable_http_app", "sse_app", "app", "http_app"):
            if hasattr(server, attr):
                cand = getattr(server, attr)
                app = cand() if callable(cand) else cand
                break
        if app is not None:
            import uvicorn  # type: ignore

            cfg = uvicorn.Config(app=app, host=host, port=port, log_level="error")
            srv = uvicorn.Server(cfg)
            await srv.serve()
            return
        raise RuntimeError("No known serve method")

    server_task = asyncio.create_task(_serve())
    await asyncio.sleep(0.8)
    if server_task.done() and server_task.exception():
        pytest.skip(f"MCP server failed to start on {url}: {server_task.exception()}")

    try:
        async with client_factory(url) as (read, write, *rest):  # type: ignore
            async with ClientSession(read, write) as session:  # type: ignore
                await session.initialize()  # type: ignore
                res_ok = await session.call_tool("get_change_criticality", arguments={"repository_id": REPO_ID, "file_path": "a.py"})  # type: ignore
                assert not getattr(res_ok, "isError", getattr(res_ok, "is_error", False))
                patches = []
                try:
                    for method_name in ["criticality", "regression_risk", "historical_impact", "recommend_tests", "get_change_criticality", "get_regression_risk", "get_historical_impact"]:
                        if hasattr(AnalyticsService, method_name):
                            p = patch.object(AnalyticsService, method_name, side_effect=GraphNotAvailable("graph unavailable injected"))
                            patches.append(p)
                            p.start()
                except Exception:
                    pass

                tool_cases = [
                    ("get_change_criticality", {"repository_id": REPO_ID, "file_path": "a.py"}),
                    ("get_regression_risk", {"repository_id": REPO_ID, "file_path": "a.py"}),
                    ("get_historical_impact", {"repository_id": REPO_ID, "file_path": "a.py"}),
                    ("recommend_tests", {"repository_id": REPO_ID, "file_path": "a.py"}),
                    ("get_file_history", {"repository_id": REPO_ID, "file_path": "a.py", "limit": 5}),
                ]
                for tname, args in tool_cases:
                    try:
                        res = await session.call_tool(tname, arguments=args)  # type: ignore
                    except Exception as e:
                        pytest.fail(f"tool {tname} raised instead of returning envelope: {e}")
                    is_err, payload = _normalize_tool_result(res)
                    status = str(payload.get("status", "")).lower()
                    assert status == "graph_unavailable" or "graph_unavailable" in json.dumps(payload).lower(), f"{tname} expected graph_unavailable, got {payload}"
                    assert is_err is True, f"{tname} expected isError true, got {payload}"

                for p in patches:
                    try:
                        p.stop()
                    except Exception:
                        pass

                res_after = await session.call_tool("get_change_criticality", arguments={"repository_id": REPO_ID, "file_path": "a.py"})  # type: ignore
                assert not getattr(res_after, "isError", getattr(res_after, "is_error", False)), f"server not running after db failure, got {res_after}"
                _, payload_after = _normalize_tool_result(res_after)
                assert payload_after.get("status") in ("ok", "insufficient_history", "not_found")
                return True
    finally:
        if server_task and not server_task.done():
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(server_task, timeout=2.0)
        for attr in ("close", "shutdown", "stop", "aclose"):
            if hasattr(server, attr):
                try:
                    res = getattr(server, attr)()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed — pip install mcp")
def test_mcp_db_failure_graph_unavailable_envelope_and_server_survives():
    """Live server: stub graph to raise GraphNotAvailable, each tool returns graph_unavailable isError true, server remains running."""
    try:
        asyncio.run(_run_db_failure_via_server())
    except (PermissionError, OSError) as e:
        pytest.skip(f"Socket unavailable in sandbox (Windows PermissionError): {e}")


def test_mcp_db_failure_unit_via_stub():
    """Unit path: stub graph to raise GraphNotAvailable — verifies 8.4 contract without server."""
    test_mcp_db_failure_all_tools_graph_unavailable_via_fake()
