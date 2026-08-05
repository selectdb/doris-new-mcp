"""Offline test: clear the session cookie and redirect to login when upstream is unreachable.

After the target node goes down, the proxy must:
1. Clear the browser's ``doris_mcp_session`` cookie (Set-Cookie: max-age=0)
2. Return a 303 redirect to ``/mcp/web/login`` so the user can log in on a healthy node
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

import httpx

_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from core.session_affinity_proxy import SessionAffinityProxy  # noqa: E402

REMOTE_IP = "10.23.45.67"
LOCAL_IP = "127.0.0.1"
_WEBUI_SESSION_COOKIE = b"doris_mcp_session"


def _remote_scope(**overrides: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/web/models",
        "raw_path": b"/mcp/web/models",
        "query_string": b"",
        "headers": [(b"cookie", b"doris_mcp_session=remote")],
    }
    scope.update(overrides)
    return scope


def _receiver(messages: list[dict[str, Any]]):
    msgs = iter(messages)

    async def receive() -> dict[str, Any]:
        return next(msgs)

    return receive


def _sender():
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    return sent, send


class UpstreamUnreachableReloginTests(unittest.IsolatedAsyncioTestCase):
    """Unreachable upstream nodes must clear cookies and force relogin."""

    def _make_proxy(
        self,
        upstream_handler: Any,
    ) -> tuple[SessionAffinityProxy, httpx.AsyncClient]:
        async def local_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))

        proxy = SessionAffinityProxy(
            local_app,
            decoder=lambda v: {"remote": ("session", REMOTE_IP), "local": ("session", LOCAL_IP)}.get(v),
            local_ip=LOCAL_IP,
            target_port=8080,
            client=client,
        )
        return proxy, client

    # ── Connection failures ──────────────────────────────────────────────────

    async def test_connect_error_clears_cookie_and_redirects_to_login(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self._assert_clears_cookie_and_redirects_to_login(sent)

    async def test_connect_error_with_query_params_still_clears_cookie(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(method="POST", path="/mcp/web/staging/commit", query_string=b"workspace=foo"),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self._assert_clears_cookie_and_redirects_to_login(sent)

    # ── Timeouts ──────────────────────────────────────────────────────

    async def test_read_timeout_clears_cookie_and_redirects_to_login(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self._assert_clears_cookie_and_redirects_to_login(sent)

    async def test_connect_timeout_clears_cookie_and_redirects_to_login(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timeout")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self._assert_clears_cookie_and_redirects_to_login(sent)

    async def test_pool_timeout_clears_cookie_and_redirects_to_login(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.PoolTimeout("pool exhausted")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self._assert_clears_cookie_and_redirects_to_login(sent)

    # ── Unreachable logout must still clear cookies ─────────────────────────────

    async def test_logout_upstream_unreachable_still_clears_local_cookie(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(path="/mcp/web/logout"),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self._assert_clears_cookie_and_redirects_to_login(sent)

    # ── Successful proxying must not over-clear ────────────────────────────────────────

    async def test_successful_upstream_does_not_clear_cookie(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers=[("set-cookie", "keep=me")], content=b"ok")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self.assertEqual(sent[0]["status"], 200)
        # Upstream Set-Cookie passes through without injecting an extra clearing cookie.
        set_cookie_values = [
            v for n, v in sent[0]["headers"] if n.lower() == b"set-cookie"
        ]
        self.assertIn(b"keep=me", set_cookie_values)
        self.assertNotIn(b"doris_mcp_session=;", b"".join(set_cookie_values))

    async def test_upstream_5xx_does_not_clear_cookie(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"internal error")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        self.assertEqual(sent[0]["status"], 500)
        self.assertNotIn(
            _WEBUI_SESSION_COOKIE,
            {n.lower() for n, _ in sent[0]["headers"]},
        )

    # ── Target IP must not leak ─────────────────────────────────────────

    async def test_unreachable_response_does_not_leak_target_ip(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _remote_scope(),
                _receiver([{"type": "http.request", "more_body": False}]),
                send,
            )
        finally:
            await client.aclose()

        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertNotIn(REMOTE_IP.encode(), body)
        header_bytes = b"".join(n + b":" + v for n, v in sent[0]["headers"])
        self.assertNotIn(REMOTE_IP.encode(), header_bytes)

    # ── Assertion helpers ──────────────────────────────────────────────────

    def _assert_clears_cookie_and_redirects_to_login(
        self, sent: list[dict[str, Any]]
    ) -> None:
        start = sent[0]
        self.assertEqual(start["type"], "http.response.start")
        self.assertIn(start["status"], (302, 303))

        # Location points to the login page.
        location = [
            v for n, v in start["headers"] if n.lower() == b"location"
        ]
        self.assertEqual(location, [b"/mcp/web/login"])

        # Set-Cookie clears doris_mcp_session.
        set_cookie_headers = [
            v for n, v in start["headers"] if n.lower() == b"set-cookie"
        ]
        # At least one Set-Cookie header must clear the cookie.
        clearing = [
            v for v in set_cookie_headers
            if _WEBUI_SESSION_COOKIE in v and (b"Max-Age=0" in v or b"Expires=" in v)
        ]
        self.assertTrue(len(clearing) >= 1, f"No clearing Set-Cookie in {set_cookie_headers}")


if __name__ == "__main__":
    unittest.main()
