"""Offline tests for persisted semantic workspace metadata."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from store.store import DorisStore, WorkspaceMetadata  # noqa: E402
from store.version import SemanticLayerVersion, VersionTracker  # noqa: E402


class TestWorkspaceMetadataState(unittest.TestCase):
    def test_check_remote_uses_published_workspace_version(self):
        store = DorisStore.__new__(DorisStore)
        metadata = WorkspaceMetadata(
            workspace="sales",
            semantic_enabled=False,
            semantic_version=14,
            updated_at="2026-08-26T10:00:00Z",
            updated_by="role_admin_user",
        )

        with patch.object(store, "get_workspace_metadata", return_value=metadata):
            state = store.check_remote()

        self.assertEqual(state.revision, "14")
        self.assertEqual(state.semantic_version, 14)
        self.assertFalse(state.semantic_enabled)

    def test_reload_failure_does_not_advance_loaded_version(self):
        tracker = VersionTracker()
        tracker.update(
            SemanticLayerVersion(
                loaded_at="2026-08-26T09:00:00Z",
                revision="13",
                source_type="doris",
                source_uri="db",
                semantic_version=13,
                metric_count=5,
            )
        )

        tracker.mark_failure()

        current = tracker.current
        self.assertEqual(current.semantic_version, 13)
        self.assertEqual(current.revision, "13")
        self.assertFalse(current.last_reload_success)


if __name__ == "__main__":
    unittest.main(verbosity=2)
