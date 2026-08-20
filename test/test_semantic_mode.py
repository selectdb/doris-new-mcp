"""Tests for configurable semantic-layer loading and query routing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config.loader import AppConfig, SemanticConfig  # noqa: E402
from server import (  # noqa: E402
    _server_instructions_for_mode,
    create_server,
)


class TestSemanticConfig(unittest.TestCase):
    def test_default_is_backward_compatible_preferred_mode(self):
        config = SemanticConfig({})
        self.assertEqual(config.mode, "preferred")
        self.assertTrue(config.initialize_on_auth)

    def test_optional_is_not_initialized_on_auth(self):
        config = SemanticConfig({"semantic": {"mode": "optional"}})
        self.assertEqual(config.mode, "optional")
        self.assertFalse(config.initialize_on_auth)

    def test_unsupported_modes_are_rejected(self):
        for mode in ("disabled", "always"):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                ValueError, "Invalid semantic.mode"
            ):
                SemanticConfig({"semantic": {"mode": mode}})


class TestSemanticModeInstructions(unittest.TestCase):
    def test_optional_policy_allows_direct_read_only_sql(self):
        instructions = _server_instructions_for_mode("optional")
        self.assertIn("read-only SQL may be used directly", instructions)
        self.assertIn("Query mode: optional", instructions)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported semantic mode"):
            _server_instructions_for_mode("unknown")


class TestSemanticModeToolDescriptions(unittest.IsolatedAsyncioTestCase):
    def _server_for_mode(self, mode: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config_dir = Path(temporary.name) / "config"
        config_dir.mkdir()
        (config_dir / "mcp-server.toml").write_text(
            f'[semantic]\nmode = "{mode}"\n', encoding="utf-8"
        )
        return create_server(
            config_dir=str(config_dir), config=AppConfig(config_dir)
        )

    async def test_optional_mode_marks_sql_as_direct_and_semantics_on_demand(self):
        server = self._server_for_mode("optional")
        execute_query = await server.get_tool("execute_query")
        list_metrics = await server.get_tool("list_metrics")
        check_health = await server.get_tool("check_service_health")
        self.assertIn("loading is not required", execute_query.description)
        self.assertIn("on demand", list_metrics.description)
        self.assertIn("does not initialize", check_health.description)

    async def test_preferred_mode_keeps_semantic_tools_preferred(self):
        server = self._server_for_mode("preferred")
        execute_query = await server.get_tool("execute_query")
        query_metric = await server.get_tool("query_metric")
        self.assertIn("fallback", execute_query.description)
        self.assertIn("preferred", query_metric.description)


if __name__ == "__main__":
    unittest.main(verbosity=2)
