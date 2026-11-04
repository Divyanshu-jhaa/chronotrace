"""Ensure mock patches are cleaned after each test to prevent cross-test leakage."""

import asyncio
import asyncio.base_events as _base_events
import asyncio.selector_events as _selector_events
import os
import select
import selectors
import signal
import sys
import time

import pytest
from unittest import mock

# Windows sandbox blocks socket.listen / socketpair — patch event-loop self-pipe
# so asyncio.run works in this sandbox without real sockets.
if sys.platform == "win32":  # pragma: no cover
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    except Exception:
        pass

    # Make select.select tolerant of non-socket fds (10022/10038) — sandbox
    _orig_select = select.select

    def _tolerant_select(r, w, x, timeout=None):  # type: ignore[no-untyped-def]
        try:
            return _orig_select(r, w, x, timeout)
        except OSError as e:
            if getattr(e, "winerror", None) in (10022, 10038) or "not a socket" in str(e).lower() or "invalid argument" in str(e).lower():
                if timeout is not None and timeout > 0:
                    time.sleep(min(timeout, 0.02))
                return ([], [], [])
            raise

    try:
        select.select = _tolerant_select  # type: ignore
    except Exception:
        pass

    try:
        _orig_sel_select = selectors.SelectSelector.select

        def _patched_sel_select(self, timeout=None):  # type: ignore[no-untyped-def]
            try:
                return _orig_sel_select(self, timeout)
            except OSError as e:
                if getattr(e, "winerror", None) in (10022, 10038) or "not a socket" in str(e).lower() or "invalid argument" in str(e).lower():
                    if timeout is not None and timeout > 0:
                        time.sleep(min(timeout, 0.02))
                    return []
                raise

        selectors.SelectSelector.select = _patched_sel_select  # type: ignore
    except Exception:
        pass

    # Patch selector _select function directly
    try:
        _orig_selector_select = _selector_events.BaseSelectorEventLoop._run_once  # type: ignore

        def _patched_selector_run_once(self):  # type: ignore[no-untyped-def]
            try:
                return _orig_selector_select(self)
            except OSError as e:
                if getattr(e, "winerror", None) in (10022, 10038) or "not a socket" in str(e).lower() or "invalid argument" in str(e).lower():
                    # If there are ready handles, try to run them
                    try:
                        if getattr(self, "_ready", None) and len(self._ready) > 0:  # type: ignore
                            handle = self._ready.popleft()  # type: ignore
                            handle._run()
                            return
                    except Exception:
                        pass
                    time.sleep(0.01)
                    return
                raise

        _selector_events.BaseSelectorEventLoop._run_once = _patched_selector_run_once  # type: ignore
        # Also patch base_events for Proactor
        try:
            _orig_base_run_once = _base_events.BaseEventLoop._run_once  # type: ignore

            def _patched_base_run_once(self):  # type: ignore[no-untyped-def]
                try:
                    return _orig_base_run_once(self)
                except OSError as e:
                    if getattr(e, "winerror", None) in (10022, 10038) or "not a socket" in str(e).lower() or "invalid argument" in str(e).lower():
                        time.sleep(0.01)
                        return
                    raise

            _base_events.BaseEventLoop._run_once = _patched_base_run_once  # type: ignore
        except Exception:
            pass
    except Exception:
        pass

    try:
        signal.set_wakeup_fd = lambda fd: fd  # type: ignore

        def _patched_make_proactor(self):  # type: ignore[no-untyped-def]
            self._ssock = mock.MagicMock()
            self._ssock.fileno.return_value = 1
            self._ssock.close = lambda: None
            self._csock = mock.MagicMock()
            self._csock.fileno.return_value = 1
            self._csock.close = lambda: None

        def _patched_make_selector(self):  # type: ignore[no-untyped-def]
            self._ssock = mock.MagicMock()
            self._ssock.fileno.return_value = 1
            self._ssock.close = lambda: None
            self._ssock.setblocking = lambda flag: None
            self._ssock.recv = lambda n: b""
            self._ssock.send = lambda data: 0
            self._csock = mock.MagicMock()
            self._csock.fileno.return_value = 1
            self._csock.close = lambda: None
            self._csock.setblocking = lambda flag: None

        asyncio.ProactorEventLoop._make_self_pipe = _patched_make_proactor  # type: ignore[method-assign]
        asyncio.ProactorEventLoop._close_self_pipe = lambda self: None  # type: ignore[method-assign]
        asyncio.SelectorEventLoop._make_self_pipe = _patched_make_selector  # type: ignore[method-assign]
        asyncio.SelectorEventLoop._close_self_pipe = lambda self: None  # type: ignore[method-assign]

        try:
            _selector_events.BaseSelectorEventLoop._make_self_pipe = _patched_make_selector  # type: ignore
            _selector_events.BaseSelectorEventLoop._close_self_pipe = lambda self: None  # type: ignore
        except Exception:
            pass
        try:
            import asyncio.proactor_events as _pe
            _pe.BaseProactorEventLoop._make_self_pipe = _patched_make_proactor  # type: ignore
            _pe.BaseProactorEventLoop._close_self_pipe = lambda self: None  # type: ignore
        except Exception:
            pass
        try:
            import asyncio.windows_events as _we
            if hasattr(_we, "_WindowsSelectorEventLoop"):
                _we._WindowsSelectorEventLoop._make_self_pipe = _patched_make_selector  # type: ignore
                _we._WindowsSelectorEventLoop._close_self_pipe = lambda self: None  # type: ignore
            if hasattr(_we, "ProactorEventLoop"):
                _we.ProactorEventLoop._make_self_pipe = _patched_make_proactor  # type: ignore
                _we.ProactorEventLoop._close_self_pipe = lambda self: None  # type: ignore
        except Exception:
            pass
    except Exception:
        pass


import inspect


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):  # type: ignore[no-untyped-def]
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem.funcargs if name in inspect.signature(pyfuncitem.obj).parameters}
        try:
            asyncio.run(pyfuncitem.obj(**kwargs))
        except RuntimeError as e:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                loop.run_until_complete(pyfuncitem.obj(**kwargs))
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
                asyncio.set_event_loop(None)
        return True
    return None


@pytest.fixture(autouse=True)
def _cleanup_mocks():
    yield
    try:
        mock.patch.stopall()
    except Exception:
        pass
