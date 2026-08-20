"""Unit tests for issue #27 — errors surfaced at the call site.

Covers the four reported problems:
  1. reload failures carry the real error message, not a generic wrapper
  2. revision == "-1" is a failure sentinel and must never ride a success envelope
  3. WorkspaceState.is_ready() is the single readiness signal the metric tools share
  4. MetricFlowCompiler.init_error is captured and propagated by replace_with
  5. reload error code mapping (permission failure -> PERMISSION_DENIED)
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from test.test_watcher import _make_workspace_state, FakeCompiler, FakeManifest


# ══════════════════════════════════════════════════════════════════
# force_reload — error surfacing
# ══════════════════════════════════════════════════════════════════

class TestForceReloadErrorSurfacing(unittest.TestCase):
    def _watcher(self):
        from store.watcher import MultiWorkspaceWatcher
        watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        watcher._workspaces = {}
        return watcher

    def _version(self, revision="abc123", success=True):
        from store.version import SemanticLayerVersion
        return SemanticLayerVersion(
            loaded_at="2026-08-19T10:00:00Z",
            loaded_epoch=time.time() - 90,
            revision=revision,
            source_type="doris",
            source_uri="db",
            metric_count=5,
            last_reload_success=success,
        )

    def test_failure_surfaces_real_error_message(self):
        """The underlying cause reaches the caller, not 'check server logs'."""
        from store.watcher import MultiWorkspaceWatcher
        ws = _make_workspace_state(name="test", version=self._version())
        watcher = self._watcher()
        watcher._workspaces["test"] = ws

        # Simulate the real _reload_workspace failure path: capture the cause
        # and mark the version tracker as failed.
        def failing_reload(w):
            w.last_reload_error = "PERMISSION_DENIED: Only admin can modify semantic models"
            w.version_tracker.mark_failure()
        watcher._reload_workspace = failing_reload

        status, msg = watcher.force_reload("test")
        self.assertEqual(status, "failed")
        self.assertIn("Only admin can modify semantic models", msg)
        self.assertNotIn("check server logs", msg)

    def test_negative_one_revision_never_success_envelope(self):
        """revision '-1' is a failure sentinel — force_reload must say failed."""
        ws = _make_workspace_state(name="test", version=self._version(revision="-1", success=True))
        watcher = self._watcher()
        watcher._workspaces["test"] = ws

        def ok_reload(w):
            w.known_revision = "-1"
        watcher._reload_workspace = ok_reload

        status, msg = watcher.force_reload("test")
        self.assertEqual(status, "failed")
        self.assertNotIn("Reload completed", msg)

    def test_success_still_returns_done(self):
        """A genuinely successful reload is unaffected."""
        ws = _make_workspace_state(name="test", version=self._version(revision="abcdef123456"))
        watcher = self._watcher()
        watcher._workspaces["test"] = ws

        def ok_reload(w):
            w.known_revision = "abcdef123456"
            w.last_reload_error = ""
        watcher._reload_workspace = ok_reload

        status, msg = watcher.force_reload("test")
        self.assertEqual(status, "done")
        self.assertIn("abcdef123456", msg)

    def test_missing_workspace_is_rejected(self):
        """Workspace-not-found maps to 'rejected', a validation error."""
        watcher = self._watcher()
        status, msg = watcher.force_reload("does_not_exist")
        self.assertEqual(status, "rejected")
        self.assertIn("Workspace not found", msg)


# ══════════════════════════════════════════════════════════════════
# WorkspaceState.is_ready — single readiness signal
# ══════════════════════════════════════════════════════════════════

class TestWorkspaceIsReady(unittest.TestCase):
    def test_ready_when_manifest_and_engine(self):
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(engine_mode=True),
        )
        self.assertTrue(ws.is_ready())

    def test_not_ready_without_manifest(self):
        ws = _make_workspace_state(name="test", compiler=FakeCompiler(engine_mode=True))
        self.assertFalse(ws.is_ready())

    def test_not_ready_without_compiler(self):
        ws = _make_workspace_state(name="test", manifest=FakeManifest())
        self.assertFalse(ws.is_ready())

    def test_not_ready_when_engine_failed(self):
        """Engine init failed (is_engine_mode False) => not ready, matching health."""
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(engine_mode=False),
        )
        self.assertFalse(ws.is_ready())


# ══════════════════════════════════════════════════════════════════
# MetricFlowCompiler.init_error — captured and propagated
# ══════════════════════════════════════════════════════════════════

class TestCompilerInitError(unittest.TestCase):
    def test_replace_with_copies_init_error(self):
        from store.compiler import MetricFlowCompiler

        c1 = MetricFlowCompiler("/nonexistent/project/one")
        c2 = MetricFlowCompiler("/nonexistent/project/two")
        c2._init_error = "Engine init failed: missing agg_time_dimension"

        c1.replace_with(c2)
        self.assertEqual(c1.init_error, "Engine init failed: missing agg_time_dimension")
        self.assertEqual(c1._engine_mode, c2._engine_mode)

    def test_reload_resets_init_error(self):
        from store.compiler import MetricFlowCompiler

        c = MetricFlowCompiler("/nonexistent/project/three")
        c._init_error = "old failure"
        c.reload()
        # reload() clears _init_error before attempting init, so the stale
        # value must never survive — whether or not a new one is derived.
        self.assertNotEqual(c.init_error, "old failure")


# ══════════════════════════════════════════════════════════════════
# reload error code mapping (server-level helper)
# ══════════════════════════════════════════════════════════════════

class TestReloadErrorCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import server  # noqa: F401
        except Exception as e:  # pragma: no cover - env without full server deps
            raise unittest.SkipTest(f"server.py not importable in this env: {e}")
        cls._code = staticmethod(server._reload_error_code)

    def test_permission_failure_maps_to_permission_denied(self):
        from core.response import ErrorCode
        for msg in (
            "PERMISSION_DENIED: Only admin can modify semantic models",
            "Access denied: user has no privilege on system_mcp",
            "grant failed on semantic table",
        ):
            self.assertEqual(self._code(msg), ErrorCode.PERMISSION_DENIED, msg)

    def test_other_failures_stay_internal(self):
        from core.response import ErrorCode
        self.assertEqual(self._code("Reload failed — check server logs"), ErrorCode.INTERNAL_ERROR)
        self.assertEqual(self._code("bootstrap failed: invalid yaml"), ErrorCode.INTERNAL_ERROR)


if __name__ == "__main__":
    unittest.main()
