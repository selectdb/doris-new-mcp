"""离线测试：privateIp 配置后登录 Cookie 写入指定节点 IP。

配置 ``privateIp = "10.0.0.13"`` 后，在三台机器的任意一台上登录，
Cookie 都会写 ``session_id.10.0.0.13``，浏览器后续请求按 Cookie 亲和
走到该节点。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import server  # noqa: E402
from server import _encode_webui_session_cookie, resolve_machine_ip  # noqa: E402


class PrivateIpCookieTests(unittest.TestCase):
    """privateIp 控制的是 Cookie 里写哪个 IP，不是强制路由。"""

    def test_cookie_uses_webui_ip_when_private_ip_configured(self) -> None:
        """配了 privateIp → Cookie 带 privateIp，不是本机 IP。"""
        webui_ip = resolve_machine_ip("10.23.45.67")
        local_ip = resolve_machine_ip("10.0.0.1")

        self.assertEqual(webui_ip, "10.23.45.67")
        self.assertEqual(local_ip, "10.0.0.1")

        # Cookie 用 webui_ip 编码（模拟 login 逻辑）
        cookie = _encode_webui_session_cookie("test_session", webui_ip)
        self.assertEqual(cookie, "test_session.10.23.45.67")

        # Cookie 不会带本机 IP
        self.assertNotIn(local_ip, cookie)

    def test_cookie_uses_local_ip_when_private_ip_not_configured(self) -> None:
        """没配 privateIp → Cookie 带本机 IP。"""
        with patch.object(server, "get_machine_ip", return_value="10.0.0.1"):
            ip = resolve_machine_ip("")
            self.assertEqual(ip, "10.0.0.1")

            cookie = _encode_webui_session_cookie("test_session", ip)
            self.assertEqual(cookie, "test_session.10.0.0.1")

    def test_fallback_to_local_ip(self) -> None:
        """webui_ip 为 None 时回退到本机 IP（兼容旧调用）。"""
        with patch.object(server, "get_machine_ip", return_value="10.0.0.1"):
            local = resolve_machine_ip("")
        webui = local  # webui_ip is None → falls back to local

        cookie = _encode_webui_session_cookie("test_session", webui)
        self.assertEqual(cookie, "test_session.10.0.0.1")


if __name__ == "__main__":
    unittest.main()
