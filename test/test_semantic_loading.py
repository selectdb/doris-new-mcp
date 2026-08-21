"""Tests for automatic query routing and on-demand semantic loading."""

from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config.loader import AppConfig
from server import _SERVER_INSTRUCTIONS, create_server


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

    def test_query_guide_is_requested_once_per_conversation(self):
        self.assertIn("once before the first data query", _SERVER_INSTRUCTIONS)
        self.assertIn("Reuse that guide", _SERVER_INSTRUCTIONS)
        self.assertNotIn("Before any data query", _SERVER_INSTRUCTIONS)


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
        self.assertIn("does not initialize", check_health.description)

    async def test_server_auth_does_not_eagerly_initialize_semantics(self):
        server = self._server()
        self.assertFalse(hasattr(server.auth, "_on_authenticated"))

    async def test_query_guide_tool_is_described_as_once_per_context(self):
        server = self._server()
        query_guide = await server.get_tool("get_query_guide")

        self.assertIn("once per conversation context", query_guide.description)
        self.assertIn("Call once before the first data query", query_guide.fn.__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
