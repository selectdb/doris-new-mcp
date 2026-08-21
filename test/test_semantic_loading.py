"""Tests for automatic query routing and on-demand semantic loading."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config.loader import AppConfig
from server import _SERVER_INSTRUCTIONS, create_server


class TestReadOnlyWorkspaceDiscovery(unittest.TestCase):
    def test_discovers_workspaces_without_ddl(self):
        from store.store import DorisStore

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [
            [("information_schema",), ("system_mcp",)],
            [("active_store_finance",), ("staging_store_finance",),
             ("active_store_example",)],
        ]

        with patch("store.store._get_conn", return_value=conn):
            workspaces = DorisStore.discover_workspaces()

        self.assertEqual(workspaces, ["example", "finance"])
        self.assertEqual(
            cursor.execute.call_args_list,
            [
                call("SHOW DATABASES"),
                call("SHOW TABLES FROM `system_mcp` LIKE 'active_store_%'"),
            ],
        )
        self.assertFalse(
            any("CREATE" in args[0] for args, _ in cursor.execute.call_args_list)
        )
        conn.close.assert_called_once_with()

    def test_missing_metadata_database_returns_no_workspaces(self):
        from store.store import DorisStore

        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [("information_schema",)]

        with patch("store.store._get_conn", return_value=conn):
            workspaces = DorisStore.discover_workspaces()

        self.assertEqual(workspaces, [])
        cursor.execute.assert_called_once_with("SHOW DATABASES")
        conn.close.assert_called_once_with()


class TestSemanticConfiguration(unittest.TestCase):
    def test_app_config_has_no_semantic_routing_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config = AppConfig(config_dir)

        self.assertFalse(hasattr(config, "semantic"))


class TestSemanticInstructions(unittest.TestCase):
    def test_instructions_describe_automatic_on_demand_routing(self):
        self.assertIn("load only when semantic tools", _SERVER_INSTRUCTIONS)
        self.assertIn("read-only SQL", _SERVER_INSTRUCTIONS)
        self.assertNotIn("query mode", _SERVER_INSTRUCTIONS.lower())


class TestSemanticToolDescriptions(unittest.IsolatedAsyncioTestCase):
    def _server(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_dir = Path(temporary.name) / "config"
        config_dir.mkdir()
        return create_server(
            config_dir=str(config_dir), config=AppConfig(config_dir)
        )

    async def test_sql_is_direct_and_semantics_are_on_demand(self):
        server = self._server()
        execute_query = await server.get_tool("execute_query")
        list_metrics = await server.get_tool("list_metrics")
        check_health = await server.get_tool("check_service_health")

        self.assertIn("loading is not required", execute_query.description)
        self.assertIn("on demand", list_metrics.description)
        self.assertIn("without initializing", check_health.description)

    async def test_server_auth_does_not_eagerly_initialize_semantics(self):
        server = self._server()
        self.assertFalse(hasattr(server.auth, "_on_authenticated"))

    async def test_health_lists_discovered_workspace_without_loading_it(self):
        from core.pool_manager import PoolManager
        from store.store import DorisStore

        server = self._server()
        check_health = await server.get_tool("check_service_health")
        pool = MagicMock()
        pool.execute = AsyncMock(return_value=[[1]])
        access_token = SimpleNamespace(client_id="reader", token="reader:secret")

        with (
            patch(
                "mcp.server.auth.middleware.auth_context.get_access_token",
                return_value=access_token,
            ),
            patch.object(
                PoolManager,
                "get_or_create_local_pool",
                new=AsyncMock(return_value=pool),
            ),
            patch.object(
                DorisStore,
                "discover_workspaces",
                return_value=["finance"],
            ) as discover,
        ):
            result = json.loads(await check_health.fn())

        self.assertTrue(result["success"])
        self.assertEqual(
            result["data"]["workspaces"],
            {"finance": {"status": "not_loaded"}},
        )
        self.assertEqual(result["data"]["semantic"]["status"], "not_loaded")
        discover.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
