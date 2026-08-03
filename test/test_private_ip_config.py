"""Offline tests for request-derived Web UI node addresses and startup wiring."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import main as app_main  # noqa: E402
import server  # noqa: E402
from config.loader import ClusterConfig, McpConfig  # noqa: E402


class RemovedConfigTests(unittest.TestCase):
    def test_removed_settings_are_not_exposed(self) -> None:
        config_data = {
            "server": {
                "privateIp": "10.0.0.13",
                "seed_example": True,
                "admin_users": ["root"],
                "fe_user": "root",
                "fe_password": "secret",
            }
        }
        config = McpConfig(config_data)
        self.assertFalse(hasattr(config, "private_ip"))
        self.assertFalse(hasattr(config, "seed_example"))
        self.assertFalse(hasattr(config, "admin_users"))
        cluster = ClusterConfig(config_data["server"], {})
        self.assertFalse(hasattr(cluster, "user_name"))
        self.assertFalse(hasattr(cluster, "user_password"))


class RequestServerIpTests(unittest.TestCase):
    def test_uses_asgi_local_socket_ip(self) -> None:
        request = SimpleNamespace(
            scope={"server": ("10.23.45.67", 3000)},
            headers={"host": "attacker.invalid", "x-forwarded-host": "192.168.1.9"},
        )
        self.assertEqual(server._request_server_ip(request, "10.0.0.1"), "10.23.45.67")

    def test_normalizes_asgi_ipv4(self) -> None:
        request = SimpleNamespace(scope={"server": ("010.0.0.1", 3000)})
        self.assertEqual(server._request_server_ip(request, "192.168.1.5"), "192.168.1.5")

    def test_falls_back_for_missing_invalid_or_unspecified_address(self) -> None:
        for scope in ({}, {"server": None}, {"server": ("hostname", 3000)}, {"server": ("0.0.0.0", 3000)}):
            with self.subTest(scope=scope):
                request = SimpleNamespace(scope=scope)
                self.assertEqual(server._request_server_ip(request, "10.9.8.7"), "10.9.8.7")


class ResolveMachineIpTests(unittest.TestCase):
    def test_detected_ipv4_is_used_as_fallback(self) -> None:
        with patch.object(server, "get_machine_ip", return_value="10.9.8.7") as detected:
            self.assertEqual(server.resolve_machine_ip(), "10.9.8.7")
            detected.assert_called_once_with()

    def test_failed_or_garbage_detection_falls_back_to_loopback(self) -> None:
        for detected_ip in (None, "not-an-ip", "2001:db8::1"):
            with self.subTest(detected_ip=detected_ip), patch.object(
                server, "get_machine_ip", return_value=detected_ip
            ):
                self.assertEqual(server.resolve_machine_ip(), "127.0.0.1")


class MainNodeIpFlowTests(unittest.TestCase):
    def test_main_detects_fallback_ip_without_private_ip_config(self) -> None:
        cfg = SimpleNamespace(mcp=SimpleNamespace(host="127.0.0.1", port=3000))
        mcp = MagicMock()
        with (
            patch.object(app_main, "AppConfig", return_value=cfg),
            patch.object(app_main, "resolve_machine_ip", return_value="10.20.30.40") as resolve,
            patch.object(app_main, "create_server", return_value=mcp) as create,
            patch.object(sys, "argv", ["main.py", "--config-dir", "/offline-config"]),
        ):
            app_main.main()

        resolve.assert_called_once_with()
        create.assert_called_once_with(
            config_dir="/offline-config",
            env_file=None,
            machine_ip="10.20.30.40",
            config=cfg,
        )
        mcp.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
