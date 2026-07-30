"""离线测试：force_target_ip 模式 —— 所有 Web UI 请求无条件转发到指定节点。"""

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

FORCE_TARGET = "10.23.45.67"
OTHER_NODE = "10.0.0.9"
_LOCAL_IP = "127.0.0.1"


def _scope(**overrides: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/mcp/web/models",
        "raw_path": b"/mcp/web/models",
        "query_string": b"",
        "headers": [(b"cookie", b"doris_mcp_session=whatever")],
    }
    scope.update(overrides)
    return scope


def _receiver():
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "more_body": False}

    return receive


def _sender():
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    return sent, send


class ForceTargetTests(unittest.IsolatedAsyncioTestCase):
    """force_target_ip 模式——所有 web 请求发到指定节点。"""

    def _make_proxy(
        self,
        upstream_handler: Any,
        *,
        force_target_ip: str | None = FORCE_TARGET,
        local_ip: str = _LOCAL_IP,
    ) -> tuple[SessionAffinityProxy, httpx.AsyncClient]:
        async def local_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
        proxy = SessionAffinityProxy(
            local_app,
            decoder=lambda v: ("session", "10.0.0.9"),
            local_ip=local_ip,
            target_port=8080,
            client=client,
            force_target_ip=force_target_ip,
        )
        return proxy, client

    # ── models 被强制转发 ──────────────────────────────────────

    async def test_models_page_is_proxied_to_force_target(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"from-target")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(_scope(), _receiver(), send)
        finally:
            await client.aclose()
        self.assertEqual(sent[0]["status"], 200)

    # ── login 也被强制转发 ────────────────────────────────────

    async def test_login_get_is_proxied_when_force_target_set(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"login-page")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(_scope(path="/mcp/web/login"), _receiver(), send)
        finally:
            await client.aclose()
        self.assertEqual(sent[0]["status"], 200)

    async def test_login_post_is_proxied_when_force_target_set(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"login-post")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(
                _scope(method="POST", path="/mcp/web/login"),
                _receiver(),
                send,
            )
        finally:
            await client.aclose()
        self.assertEqual(sent[0]["status"], 200)

    # ── 本机就是 target → 本地处理 ─────────────────────────────

    async def test_local_is_target_handles_locally(self) -> None:
        async def local_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"local", "more_body": False})

        proxy = SessionAffinityProxy(
            local_app,
            decoder=lambda v: ("session", "10.0.0.9"),
            local_ip=FORCE_TARGET,
            target_port=8080,
            force_target_ip=FORCE_TARGET,
        )
        sent, send = _sender()
        await proxy(_scope(), _receiver(), send)
        self.assertEqual(sent[0]["status"], 201)

    # ── /mcp 不受影响 ─────────────────────────────────────────

    async def test_mcp_endpoint_is_not_proxied(self) -> None:
        async def local_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"mcp", "more_body": False})

        proxy = SessionAffinityProxy(
            local_app,
            decoder=lambda v: ("session", "10.0.0.9"),
            local_ip=_LOCAL_IP,
            target_port=8080,
            force_target_ip=FORCE_TARGET,
        )
        sent, send = _sender()
        await proxy(_scope(path="/mcp"), _receiver(), send)
        self.assertEqual(sent[0]["status"], 201)

    # ── force_target 不可达 → 303 重登录 ──────────────────────

    async def test_force_target_unreachable_returns_503_with_html(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        proxy, client = self._make_proxy(handler)
        sent, send = _sender()
        try:
            await proxy(_scope(), _receiver(), send)
        finally:
            await client.aclose()
        self.assertEqual(sent[0]["status"], 503)
        content_type = [v for n, v in sent[0]["headers"] if n.lower() == b"content-type"]
        self.assertIn(b"text/html", content_type[0])
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertIn(b"Session lost", body)
        # 不泄露目标 IP
        self.assertNotIn(FORCE_TARGET.encode(), body)

    # ── 不配 force_target → login 保持本地 ────────────────────

    async def test_without_force_target_login_is_local(self) -> None:
        async def local_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"local-login", "more_body": False})

        proxy = SessionAffinityProxy(
            local_app,
            decoder=lambda v: ("session", OTHER_NODE),
            local_ip=_LOCAL_IP,
            target_port=8080,
            force_target_ip=None,
        )
        sent, send = _sender()
        await proxy(
            _scope(path="/mcp/web/login", headers=[]),
            _receiver(),
            send,
        )
        self.assertEqual(sent[0]["status"], 201)


if __name__ == "__main__":
    unittest.main()
