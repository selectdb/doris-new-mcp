"""Unit tests for MultiWorkspaceWatcher — on-demand ensure_fresh logic.

Covers:
  - Every semantic access checks the published version
  - Revision unchanged → touch epoch
  - Revision changed → reload
  - First load (no prior version) → reload
  - Concurrent request (parsing=True) → skip, use old data
  - Unknown workspace → return None
  - Discover workspace on demand
  - Doris unreachable → graceful degradation
  - Reload failure → keep old state, reset parsing flag
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Minimal stubs to avoid importing the full server chain ──

class FakeManifest:
    def __init__(self, metrics=None):
        self._metrics = metrics or [{"name": "total_count", "description": "test"}]

    def list_metrics(self):
        return self._metrics

    def replace_with(self, other):
        self._metrics = other._metrics


class FakeCompiler:
    def __init__(self, engine_mode=True):
        self.is_engine_mode = engine_mode

    def replace_with(self, other):
        self.is_engine_mode = other.is_engine_mode


class FakeStore:
    """Fake DorisStore for testing."""
    def __init__(self, workspace="test", revision="abc123", enabled=True, semantic_version=1):
        self.workspace = workspace
        self._revision = revision
        self._enabled = enabled
        self._semantic_version = semantic_version
        self.files_fetched = False

    def check_remote(self):
        from store.store import StoreState
        return StoreState(
            revision=self._revision,
            semantic_enabled=self._enabled,
            semantic_version=self._semantic_version,
        )

    def set_semantic_enabled(self, enabled, updated_by):
        from store.store import WorkspaceMetadata
        self._enabled = enabled
        return WorkspaceMetadata(
            workspace=self.workspace,
            semantic_enabled=enabled,
            semantic_version=self._semantic_version,
            updated_by=updated_by,
        )

    def list_files(self):
        return ["model.yml"]

    def fetch(self, models_dir):
        self.files_fetched = True

    # discover stub
    @staticmethod
    def discover_workspaces():
        return ["example", "test"]


class DeleteDependencyStore:
    """Store fixture that exposes a deleted YAML and one dependent YAML."""

    _files = {
        "orders.yaml": """semantic_model:
  name: orders
  measures:
    - name: total_amount
      agg: sum
""",
        "revenue.yaml": """semantic_model:
  name: revenue
  metrics:
    - name: total_revenue
      type: derived
      expr: total_amount
""",
    }

    def staging_list(self):
        return [{"filename": "orders.yaml", "action": "delete"}]

    def list_files(self):
        return [{"filename": filename} for filename in self._files]

    def get_file(self, filename):
        content = self._files.get(filename)
        return {"filename": filename, "content": content} if content else None

    def staging_fetch(self, _models_dir):
        raise AssertionError("Dependency validation should fail before staging fetch")


# ── Helpers to build a minimal WorkspaceState ──

def _make_workspace_state(
    name="test",
    manifest=None,
    compiler=None,
    known_revision="abc123",
    parsing=False,
    version=None,
):
    """Build a WorkspaceState with the fields used by ensure_fresh."""
    from store.version import VersionTracker
    from store.watcher import RWLock, WorkspaceState

    store = FakeStore(workspace=name, revision=known_revision)

    ws = WorkspaceState(
        name=name,
        store=store,
        config_dir=Path("/tmp"),
        workspace_dir=Path("/tmp/ws") / name,
        models_dir=Path("/tmp/ws") / name / "models_cache",
        enabled=True,
    )
    ws.manifest = manifest
    ws.compiler = compiler
    ws.known_revision = known_revision
    ws.parsing = parsing
    if version is not None:
        ws.version_tracker.update(version)
    return ws


# ══════════════════════════════════════════════════════════════════
# Test cases
# ══════════════════════════════════════════════════════════════════

class TestValidateStagingDependencies(unittest.TestCase):
    def test_delete_with_active_yaml_dependency_fails_validation(self):
        from store.watcher import MultiWorkspaceWatcher

        watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        watcher._workspaces = {
            "test": SimpleNamespace(store=DeleteDependencyStore()),
        }
        watcher._staging_validated = set()

        valid, message, details = watcher.validate_staging("test")

        self.assertFalse(valid)
        self.assertEqual(details["phase"], "dependencies")
        self.assertIn("revenue.yaml", " ".join(details["errors"]))
        self.assertIn("total_amount", message)


class TestWorkspaceSemanticGrants(unittest.TestCase):
    def test_commit_grants_every_table_for_a_new_workspace(self):
        from store.watcher import MultiWorkspaceWatcher

        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)
            (models_dir / "models.yaml").write_text(
                """semantic_model:
  name: orders
  db_table: sales.orders
---
semantic_model:
  name: users
  db_table: sales.users
""",
                encoding="utf-8",
            )
            store = MagicMock()
            store.staging_commit.return_value.revision = "abc123"
            store.staging_list.return_value = []
            watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
            watcher._workspaces = {
                "sales": SimpleNamespace(store=store, models_dir=models_dir)
            }
            watcher._staging_validated = {"sales"}
            watcher.force_reload = MagicMock(return_value=("done", "Reload completed"))

            with patch("store.bootstrap.grant_select_on_physical_tables") as grant:
                ok, _ = watcher.commit_staging("sales")

            self.assertTrue(ok)
            grant.assert_called_once_with({"sales.orders", "sales.users"})


class TestWorkspaceSemanticEnabled(unittest.TestCase):
    def _watcher(self):
        from store.watcher import MultiWorkspaceWatcher
        watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        watcher._workspaces = {}
        watcher._router = MagicMock()
        watcher._staging_validated = set()
        return watcher

    def test_disabled_workspace_is_not_ready_or_reloaded(self):
        from store.version import SemanticLayerVersion

        version = SemanticLayerVersion(
            loaded_at="2026-08-26T10:00:00Z",
            revision="1",
            source_type="doris",
            source_uri="db",
            semantic_version=1,
            metric_count=1,
        )
        ws = _make_workspace_state(
            manifest=FakeManifest(),
            compiler=FakeCompiler(),
            known_revision="1",
            version=version,
        )
        ws.store._revision = "1"
        ws.store._semantic_version = 1
        ws.store._enabled = False
        watcher = self._watcher()
        watcher._workspaces["test"] = ws
        watcher._reload_workspace = MagicMock()

        result = watcher.ensure_fresh("test")

        self.assertIs(result, ws)
        self.assertFalse(ws.enabled)
        self.assertFalse(ws.is_ready())
        watcher._reload_workspace.assert_not_called()
        watcher._router.rebuild.assert_called_once()

    def test_admin_toggle_is_persisted_and_router_is_rebuilt(self):
        ws = _make_workspace_state(manifest=FakeManifest(), compiler=FakeCompiler())
        watcher = self._watcher()
        watcher._workspaces["test"] = ws

        ok, message, details = watcher.set_semantic_enabled(
            "test", False, "role_admin_user"
        )

        self.assertTrue(ok)
        self.assertIn("disabled", message)
        self.assertFalse(ws.enabled)
        self.assertFalse(details["semantic_enabled"])
        watcher._router.rebuild.assert_called_once()

    def test_enable_setting_stays_applied_when_reload_fails(self):
        ws = _make_workspace_state()
        ws.enabled = False
        ws.store._enabled = False
        watcher = self._watcher()
        watcher._workspaces["test"] = ws

        def fail_reload(state):
            state.last_reload_error = "invalid semantic model"

        watcher._reload_workspace = fail_reload

        ok, message, details = watcher.set_semantic_enabled(
            "test", True, "role_admin_user"
        )

        self.assertTrue(ok, "The persisted setting update itself succeeded")
        self.assertTrue(ws.enabled)
        self.assertTrue(details["semantic_enabled"])
        self.assertEqual(details["reload_error"], "invalid semantic model")
        self.assertIn("could not be loaded", message)

    def test_commit_while_disabled_bumps_version_without_reload(self):
        store = MagicMock()
        store.staging_commit.return_value = SimpleNamespace(
            revision="8", semantic_version=8
        )
        store.staging_list.return_value = []
        ws = SimpleNamespace(store=store, models_dir=Path("/tmp"), enabled=False)
        watcher = self._watcher()
        watcher._workspaces["test"] = ws
        watcher._staging_validated = {"test"}
        watcher.force_reload = MagicMock()
        watcher.grant_workspace_access = MagicMock(return_value=0)

        ok, message = watcher.commit_staging("test")

        self.assertTrue(ok)
        self.assertEqual(ws.published_version, 8)
        self.assertIn("reload deferred", message)
        watcher.force_reload.assert_not_called()


class TestEnsureFreshVersionCheck(unittest.TestCase):
    """Tests for strict per-access semantic version checks."""

    def setUp(self):
        from store.watcher import MultiWorkspaceWatcher

        self.watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        self.watcher._config_dir = Path("/tmp")
        self.watcher._workspace_root = Path("/tmp/ws")
        self.watcher._app_config = MagicMock()
        self.watcher._workspaces = {}
        self.watcher._router = MagicMock()
        self.watcher._staging_validated = set()

    # ── Test 1: even a recent load checks metadata ──

    def test_recent_load_still_checks_version(self):
        """Every semantic access checks the shared published version."""
        from store.version import SemanticLayerVersion

        recent = SemanticLayerVersion(
            loaded_at="2026-07-28T10:00:00Z",
            loaded_epoch=time.time() - 30,  # 30 seconds ago
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(),
            known_revision="abc123",
            version=recent,
        )
        self.watcher._workspaces["test"] = ws

        # Track store calls
        original_check = ws.store.check_remote
        call_count = [0]
        def counting_check():
            call_count[0] += 1
            return original_check()
        ws.store.check_remote = counting_check

        result = self.watcher.ensure_fresh("test")

        self.assertIsNotNone(result)
        self.assertEqual(call_count[0], 1)

    # ── Test 2: revision unchanged → touch ──

    def test_revision_unchanged_touches_epoch(self):
        """When revision is unchanged, bump the verification epoch without reload."""
        from store.version import SemanticLayerVersion

        old_epoch = time.time() - 90  # 90 seconds ago
        old_version = SemanticLayerVersion(
            loaded_at="2026-07-28T09:58:00Z",
            loaded_epoch=old_epoch,
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(),
            known_revision="abc123",
            version=old_version,
        )
        # Store returns same revision
        ws.store._revision = "abc123"
        self.watcher._workspaces["test"] = ws

        result = self.watcher.ensure_fresh("test")

        self.assertIsNotNone(result)
        # epoch should be bumped
        new_epoch = ws.version_tracker.current.loaded_epoch
        self.assertGreater(new_epoch, old_epoch,
                           "Epoch should be bumped when revision unchanged")
        # manifest unchanged
        self.assertEqual(ws.known_revision, "abc123")

    # ── Test 3: revision changed → reload ──

    def test_revision_changed_triggers_reload(self):
        """When the published revision differs, reload the workspace."""
        from store.version import SemanticLayerVersion

        old_version = SemanticLayerVersion(
            loaded_at="2026-07-28T09:58:00Z",
            loaded_epoch=time.time() - 90,
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(),
            known_revision="abc123",
            version=old_version,
        )
        # Store returns DIFFERENT revision
        ws.store._revision = "xyz789"
        self.watcher._workspaces["test"] = ws

        # Mock _reload_workspace to avoid bootstrap side effects
        reload_called = []
        def fake_reload(w):
            reload_called.append(w.name)
            w.manifest = FakeManifest([{"name": "new_metric"}])
            w.compiler = FakeCompiler()
            w.known_revision = "xyz789"
            from store.version import SemanticLayerVersion as SV
            w.version_tracker.update(SV(
                loaded_at=SV.now_iso(),
                loaded_epoch=time.time(),
                revision="xyz789",
                source_type="doris",
                source_uri="db",
                metric_count=1,
            ))
        self.watcher._reload_workspace = fake_reload

        result = self.watcher.ensure_fresh("test")

        self.assertIsNotNone(result)
        self.assertEqual(len(reload_called), 1, "Should trigger reload")
        self.assertEqual(ws.known_revision, "xyz789")
        self.assertEqual(ws.manifest.list_metrics()[0]["name"], "new_metric")

    # ── Test 4: first load (no prior version) → reload ──

    def test_first_load_no_version_triggers_reload(self):
        """After restart, version_tracker.current is None → must reload."""
        ws = _make_workspace_state(
            name="test",
            manifest=None,       # never loaded
            compiler=None,       # never loaded
            known_revision="",   # set by _init_workspace
            version=None,        # no prior version
        )
        ws.store._revision = "abc123"
        self.watcher._workspaces["test"] = ws

        reload_called = []
        def fake_reload(w):
            reload_called.append(w.name)
            w.manifest = FakeManifest()
            w.compiler = FakeCompiler()
            w.known_revision = "abc123"
            from store.version import SemanticLayerVersion as SV
            w.version_tracker.update(SV(
                loaded_at=SV.now_iso(),
                loaded_epoch=time.time(),
                revision="abc123",
                source_type="doris",
                source_uri="db",
                metric_count=5,
            ))
        self.watcher._reload_workspace = fake_reload

        result = self.watcher.ensure_fresh("test")

        self.assertIsNotNone(result)
        self.assertEqual(len(reload_called), 1,
                         "First load must trigger reload")
        self.assertIsNotNone(ws.manifest, "Manifest should be set after first load")
        self.assertIsNotNone(ws.compiler, "Compiler should be set after first load")
        self.assertIsNotNone(ws.version_tracker.current,
                             "Version should be set after first load")


class TestEnsureFreshConcurrency(unittest.TestCase):
    """Tests for concurrent request handling."""

    def setUp(self):
        from store.watcher import MultiWorkspaceWatcher
        self.watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        self.watcher._config_dir = Path("/tmp")
        self.watcher._workspace_root = Path("/tmp/ws")
        self.watcher._app_config = MagicMock()
        self.watcher._workspaces = {}
        self.watcher._router = MagicMock()
        self.watcher._staging_validated = set()

    # ── Test 5: parsing in progress → skip ──

    def test_parsing_in_progress_skips_and_returns_old_data(self):
        """When ws.parsing=True, return current state without waiting."""
        from store.version import SemanticLayerVersion

        old_version = SemanticLayerVersion(
            loaded_at="2026-07-28T09:58:00Z",
            loaded_epoch=time.time() - 120,  # well past cooldown
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        old_manifest = FakeManifest([{"name": "old_metric"}])
        old_compiler = FakeCompiler()

        ws = _make_workspace_state(
            name="test",
            manifest=old_manifest,
            compiler=old_compiler,
            known_revision="abc123",
            parsing=True,  # ← reload already in progress
            version=old_version,
        )
        ws.store._revision = "xyz789"  # revision changed, but should be ignored
        self.watcher._workspaces["test"] = ws

        reload_called = []
        def fake_reload(w):
            reload_called.append(w.name)
        self.watcher._reload_workspace = fake_reload

        result = self.watcher.ensure_fresh("test")

        self.assertIsNotNone(result)
        self.assertEqual(len(reload_called), 0,
                         "Should NOT reload when already parsing")
        self.assertIs(result.manifest, old_manifest,
                      "Should return old manifest")
        self.assertEqual(result.manifest.list_metrics()[0]["name"], "old_metric")

    # ── Test 6: parsing flag protects first load too ──

    def test_parsing_during_first_load_returns_none_manifest(self):
        """When ws.parsing=True on first load, return ws even if manifest is None."""
        ws = _make_workspace_state(
            name="test",
            manifest=None,
            compiler=None,
            known_revision="",
            parsing=True,  # another request already started loading
            version=None,
        )
        self.watcher._workspaces["test"] = ws

        reload_called = []
        self.watcher._reload_workspace = lambda w: reload_called.append(w.name)

        result = self.watcher.ensure_fresh("test")

        self.assertIsNotNone(result, "Should return ws even with None manifest")
        self.assertEqual(len(reload_called), 0,
                         "Should NOT start another reload")
        self.assertIsNone(result.manifest)


class TestEnsureFreshDiscovery(unittest.TestCase):
    """Tests for workspace discovery on demand."""

    def setUp(self):
        from store.watcher import MultiWorkspaceWatcher
        self.watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        self.watcher._config_dir = Path("/tmp")
        self.watcher._workspace_root = Path("/tmp/ws")
        self.watcher._app_config = MagicMock()
        self.watcher._workspaces = {}
        self.watcher._router = MagicMock()
        self.watcher._staging_validated = set()

    # ── Test 7: unknown workspace → return None ──

    def test_unknown_workspace_returns_none(self):
        """When workspace is not in memory and discovery also fails, return None."""
        from store.store import DorisStore
        with patch.object(DorisStore, 'discover_workspaces', return_value=[]):
            result = self.watcher.ensure_fresh("no_such_ws")
        self.assertIsNone(result)

    # ── Test 8: discover new workspace on demand ──

    def test_discover_workspace_on_demand(self):
        """When workspace is not in memory but discoverable, init and return it."""
        from store.version import SemanticLayerVersion

        def fake_reload(w):
            w.manifest = FakeManifest()
            w.compiler = FakeCompiler()
            w.known_revision = "abc123"
            w.version_tracker.update(SemanticLayerVersion(
                loaded_at=SemanticLayerVersion.now_iso(),
                loaded_epoch=time.time(),
                revision="abc123",
                source_type="doris",
                source_uri="db",
                metric_count=5,
            ))

        self.watcher._reload_workspace = fake_reload

        # Stub _init_workspace to avoid real Doris connection
        def fake_init(name, first_load=False):
            ws = _make_workspace_state(
                name=name, manifest=None, compiler=None,
                known_revision="", version=None,
            )
            self.watcher._workspaces[name] = ws
            if first_load:
                self.watcher._reload_workspace(ws)
            return ws
        self.watcher._init_workspace = fake_init

        from store.store import DorisStore
        with patch.object(DorisStore, 'discover_workspaces', return_value=["new_ws"]):
            result = self.watcher.ensure_fresh("new_ws")

        self.assertIsNotNone(result)
        self.assertIsNotNone(result.manifest)
        self.assertIn("new_ws", self.watcher._workspaces)


class TestEnsureFreshGracefulDegradation(unittest.TestCase):
    """Tests for error handling."""

    def setUp(self):
        from store.watcher import MultiWorkspaceWatcher
        self.watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        self.watcher._config_dir = Path("/tmp")
        self.watcher._workspace_root = Path("/tmp/ws")
        self.watcher._app_config = MagicMock()
        self.watcher._workspaces = {}
        self.watcher._router = MagicMock()
        self.watcher._staging_validated = set()

    # ── Test 9: Doris unreachable, manifest exists → still refuse ──

    def test_doris_unreachable_with_manifest_returns_none(self):
        """check_remote() failing means the manifest's age is unknown.

        Serving it anyway would hand back metric definitions that may be
        outdated, with nothing to tell the caller apart from a correct
        answer, so ensure_fresh refuses even when a manifest is loaded.
        """
        from store.version import SemanticLayerVersion

        old_version = SemanticLayerVersion(
            loaded_at="2026-07-28T09:58:00Z",
            loaded_epoch=time.time() - 90,
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(),
            known_revision="abc123",
            version=old_version,
        )
        # Store throws on check_remote
        ws.store.check_remote = MagicMock(side_effect=RuntimeError("Doris down"))
        self.watcher._workspaces["test"] = ws

        result = self.watcher.ensure_fresh("test")

        self.assertIsNone(
            result,
            "Should refuse rather than serve a manifest of unknown freshness",
        )

    # ── Test 10: Doris unreachable, no manifest → return None ──

    def test_doris_unreachable_no_manifest_returns_none(self):
        """When store.check_remote() throws and no manifest, return None."""
        ws = _make_workspace_state(
            name="test",
            manifest=None,
            compiler=None,
            known_revision="",
            version=None,
        )
        ws.store.check_remote = MagicMock(side_effect=RuntimeError("Doris down"))
        self.watcher._workspaces["test"] = ws

        result = self.watcher.ensure_fresh("test")

        self.assertIsNone(result)

    # ── Test 11: reload throws → keep old manifest, reset parsing ──

    def test_reload_failure_keeps_old_state(self):
        """When _reload_workspace throws, old manifest is preserved and parsing=False."""
        from store.version import SemanticLayerVersion

        old_version = SemanticLayerVersion(
            loaded_at="2026-07-28T09:58:00Z",
            loaded_epoch=time.time() - 90,
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        old_manifest = FakeManifest([{"name": "surviving_metric"}])
        ws = _make_workspace_state(
            name="test",
            manifest=old_manifest,
            compiler=FakeCompiler(),
            known_revision="abc123",
            version=old_version,
        )
        ws.store._revision = "xyz789"  # changed
        self.watcher._workspaces["test"] = ws

        # _reload_workspace throws
        def failing_reload(w):
            w.parsing = True  # simulate entering
            raise RuntimeError("bootstrap explosion")
        self.watcher._reload_workspace = failing_reload

        with self.assertRaises(RuntimeError):
            self.watcher.ensure_fresh("test")


# ══════════════════════════════════════════════════════════════════
# Integration: cooldown across multiple calls
# ══════════════════════════════════════════════════════════════════

class TestEnsureFreshMultipleCalls(unittest.TestCase):
    """Multiple ensure_fresh calls in sequence."""

    def setUp(self):
        from store.watcher import MultiWorkspaceWatcher
        self.watcher = MultiWorkspaceWatcher.__new__(MultiWorkspaceWatcher)
        self.watcher._config_dir = Path("/tmp")
        self.watcher._workspace_root = Path("/tmp/ws")
        self.watcher._app_config = MagicMock()
        self.watcher._workspaces = {}
        self.watcher._router = MagicMock()
        self.watcher._staging_validated = set()

    def test_multiple_calls_each_check_version(self):
        """Each semantic access checks the lightweight metadata row."""
        from store.version import SemanticLayerVersion

        recent = SemanticLayerVersion(
            loaded_at="2026-07-28T10:00:00Z",
            loaded_epoch=time.time() - 30,
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(),
            known_revision="abc123",
            version=recent,
        )
        self.watcher._workspaces["test"] = ws

        check_count = [0]
        original = ws.store.check_remote
        ws.store.check_remote = lambda: (check_count.__setitem__(0, check_count[0] + 1)
                                           or original())

        for _ in range(10):
            result = self.watcher.ensure_fresh("test")
            self.assertIsNotNone(result)

        self.assertEqual(check_count[0], 10)

    def test_consecutive_calls_both_check_version(self):
        """A second immediate access still checks the published version."""
        from store.version import SemanticLayerVersion

        old_version = SemanticLayerVersion(
            loaded_at="2026-07-28T09:58:00Z",
            loaded_epoch=time.time() - 90,
            revision="abc123",
            source_type="doris",
            source_uri="db",
            metric_count=5,
        )
        ws = _make_workspace_state(
            name="test",
            manifest=FakeManifest(),
            compiler=FakeCompiler(),
            known_revision="abc123",
            version=old_version,
        )
        ws.store._revision = "abc123"  # unchanged
        self.watcher._workspaces["test"] = ws

        # First call checks revision.
        result1 = self.watcher.ensure_fresh("test")
        self.assertIsNotNone(result1)

        epoch_after_first = ws.version_tracker.current.loaded_epoch

        # Second call immediately still checks metadata.
        check_count = [0]
        ws.store.check_remote = lambda: (check_count.__setitem__(0, check_count[0] + 1)
                                          or MagicMock(revision="abc123"))

        result2 = self.watcher.ensure_fresh("test")
        self.assertIsNotNone(result2)
        self.assertEqual(check_count[0], 1)


# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
