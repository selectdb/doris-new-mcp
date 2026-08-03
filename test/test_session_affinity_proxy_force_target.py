"""Offline tests for request-derived Web UI session affinity."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import httpx

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.session_affinity_proxy import SessionAffinityProxy  # noqa: E402


class RequestDerivedAffinityTests(unittest.IsolatedAsyncioTestCase):
    TARGET_IP = "10.0.0.13"
    OTHER_IP = "10.0.0.11"

    def make_proxy(self):
        local_scopes: list[dict] = []
        requests: list[httpx.Request] = []

        async def app(scope, receive, send):
            local_scopes.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"local"})

        def upstream(request):
            requests.append(request)
            return httpx.Response(200, content=b"upstream")

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))

        def decoder(value):
            return ("session", self.TARGET_IP) if value == "target" else None

        proxy = SessionAffinityProxy(
            app,
            decoder=decoder,
            local_ip="127.0.0.1",
            target_port=3000,
            client=client,
        )
        return proxy, local_scopes, requests, client

    async def invoke(self, proxy, *, local_ip, path="/mcp/web", cookie=None):
        sent: list[dict] = []
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        headers = [] if cookie is None else [(b"cookie", f"doris_mcp_session={cookie}".encode())]
        scope = {
            "type": "http",
            "path": path,
            "method": "GET",
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "server": (local_ip, 3000),
        }
        await proxy(scope, receive, send)
        return sent

    async def test_cookie_target_matching_request_socket_is_local(self) -> None:
        proxy, local, requests, client = self.make_proxy()
        try:
            await self.invoke(proxy, local_ip=self.TARGET_IP, cookie="target")
            self.assertEqual(len(local), 1)
            self.assertEqual(requests, [])
        finally:
            await client.aclose()

    async def test_cookie_target_different_from_request_socket_is_forwarded(self) -> None:
        proxy, local, requests, client = self.make_proxy()
        try:
            await self.invoke(proxy, local_ip=self.OTHER_IP, cookie="target")
            self.assertEqual(local, [])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.host, self.TARGET_IP)
        finally:
            await client.aclose()

    async def test_login_without_cookie_stays_on_receiving_node(self) -> None:
        proxy, local, requests, client = self.make_proxy()
        try:
            await self.invoke(proxy, local_ip=self.OTHER_IP, path="/mcp/web/login")
            self.assertEqual(len(local), 1)
            self.assertEqual(requests, [])
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
