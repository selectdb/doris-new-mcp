"""Regression test: ordinary users never trigger DDL on semantic reads.

Non-admin callers hold SELECT but not CREATE. Before this fix, the first
semantic read of a non-admin user (e.g. list_metrics after a restart) went
through DorisStore.__init__ / discover_workspaces and issued
``CREATE DATABASE IF NOT EXISTS system_mcp``, which Doris rejected with
"Access denied" — even when the database already existed.

Pinned behaviour:
  * non-admin  -> reads only; missing storage raises SemanticStorageNotInitialized
  * admin      -> may create the database / tables exactly as before

Run: python test/test_readonly_storage.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import store.store as st  # noqa: E402
from store.store import (  # noqa: E402
    SchemaMigrationRequired,
    SemanticStorageNotInitialized,
    WriteNotAllowed,
)


def _sql(cursor: MagicMock) -> str:
    return " ".join(str(call.args[0]) for call in cursor.execute.call_args_list).upper()


class StorageProvisioningTest(unittest.TestCase):
    def setUp(self) -> None:
        st.set_request_credentials("backend_mcp_test", "secret")
        st.set_provisioning_allowed(False)
        st.DorisStore._table_cache.clear()

    def tearDown(self) -> None:
        st.set_provisioning_allowed(False)

    def _conn(self, *, database_exists=False, tables=()):
        patcher = patch("store.store.pymysql.connect")
        connect = patcher.start()
        self.addCleanup(patcher.stop)
        conn, cursor = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = ("system_mcp",) if database_exists else None
        cursor.fetchall.return_value = list(tables)
        connect.return_value = conn
        return cursor

    # ── construction ──

    def test_normal_user_construction_runs_no_ddl(self):
        cursor = self._conn()
        st.DorisStore(workspace="ws1")
        self.assertNotIn("CREATE", _sql(cursor))

    def test_admin_construction_still_provisions(self):
        st.set_provisioning_allowed(True)
        cursor = self._conn()
        st.DorisStore(workspace="ws2")
        self.assertIn("CREATE DATABASE IF NOT EXISTS SYSTEM_MCP", _sql(cursor))
        # A newly created metadata table must be readable by ordinary users.
        self.assertIn("GRANT SELECT_PRIV", _sql(cursor))

    # ── discovery ──

    def test_normal_user_discovery_of_missing_db_raises_without_ddl(self):
        cursor = self._conn(database_exists=False)
        with self.assertRaises(SemanticStorageNotInitialized):
            st.DorisStore.discover_workspaces()
        self.assertNotIn("CREATE", _sql(cursor))

    def test_normal_user_discovery_of_existing_db_reads_only(self):
        cursor = self._conn(database_exists=True, tables=[("active_store_demo",)])
        self.assertEqual(st.DorisStore.discover_workspaces(), ["demo"])
        self.assertNotIn("CREATE", _sql(cursor))

    def test_admin_discovery_creates_missing_db(self):
        st.set_provisioning_allowed(True)
        cursor = self._conn(database_exists=False)
        self.assertEqual(st.DorisStore.discover_workspaces(), [])
        self.assertIn("CREATE DATABASE IF NOT EXISTS SYSTEM_MCP", _sql(cursor))

    # ── workspace names are identifiers, never SQL fragments ──

    def test_invalid_workspace_name_is_rejected_before_sql(self):
        with patch("store.store.pymysql.connect") as connect:
            with self.assertRaises(ValueError):
                st.DorisStore(workspace="x UNION SELECT 1,2,3 -- ")
        connect.assert_not_called()

    def test_drop_workspace_tables_rejects_invalid_name(self):
        with patch("store.store.pymysql.connect") as connect:
            with self.assertRaises(ValueError):
                st.DorisStore.drop_workspace_tables("x, dw.users")
        connect.assert_not_called()

    def test_discovery_ignores_invalid_workspace_tables(self):
        self._conn(database_exists=True, tables=[
            ("active_store_ok",), ("active_store_bad-name",),
        ])
        self.assertEqual(st.DorisStore.discover_workspaces(), ["ok"])

    def test_missing_metadata_table_still_reads_defaults(self):
        """No workspace_metadata table: loading must continue with defaults so
        ordinary users can query models before an admin creates it."""
        cursor = self._conn(database_exists=True)
        cursor.execute.side_effect = RuntimeError("Unknown table 'workspace_metadata'")
        meta = st.DorisStore(workspace="no_meta").get_workspace_metadata()

        self.assertTrue(meta.semantic_enabled)
        self.assertEqual(meta.semantic_version, 1)
        self.assertNotIn("INSERT", _sql(cursor))

    def test_other_metadata_errors_still_raise(self):
        cursor = self._conn(database_exists=True)
        cursor.execute.side_effect = RuntimeError("connection reset")
        with self.assertRaises(RuntimeError):
            st.DorisStore(workspace="broken").get_workspace_metadata()

    # ── metadata reads never INSERT (legacy workspaces have no row) ──

    def test_metadata_read_without_row_does_not_insert(self):
        """A workspace whose tables predate workspace_metadata (or was created
        outside the app) has no row. Reading it must not try to INSERT: ordinary
        users have no INSERT privilege."""
        cursor = self._conn(database_exists=True)
        cursor.fetchone.return_value = None          # no metadata row
        meta = st.DorisStore(workspace="legacy_ws").get_workspace_metadata()

        self.assertTrue(meta.semantic_enabled)
        self.assertEqual(meta.semantic_version, 1)
        sql = _sql(cursor)
        self.assertIn("SELECT SEMANTIC_ENABLED", sql)
        self.assertNotIn("INSERT", sql)

    # ── the admin example-deploy path may still provision ──

    def test_admin_deploy_path_provisions_missing_metadata_table(self):
        """Example deployment runs as admin in a background task on a fresh
        install: bump_semantic_version() must be able to create the metadata
        table, otherwise deploy fails with "Unknown table"."""
        st.set_provisioning_allowed(True)
        cursor = self._conn(database_exists=True)
        cursor.fetchone.return_value = None   # no metadata row yet
        st.DorisStore(workspace="deploy_ws").bump_semantic_version()
        sql = _sql(cursor)
        self.assertIn("CREATE TABLE IF NOT EXISTS WORKSPACE_METADATA", sql)
        # Write path still creates the row when it is missing.
        self.assertIn("INSERT INTO WORKSPACE_METADATA", sql)
        self.assertIn("GRANT SELECT_PRIV", sql)

    # ── read helpers stay DDL-free for normal users ──

    def test_normal_user_reads_never_create_tables(self):
        cursor = self._conn(database_exists=True)
        cursor.execute.side_effect = RuntimeError("Unknown table 'active_store_x'")
        store = st.DorisStore(workspace="ws3")
        cursor.execute.reset_mock()

        self.assertEqual(store.list_files(), [])
        self.assertNotIn("CREATE", _sql(cursor))


class ConnectionGuardTest(unittest.TestCase):
    """Backstop: even a forgotten guard cannot send a write to Doris with a
    read-only caller's credentials."""

    def setUp(self) -> None:
        st.set_request_credentials("backend_mcp_test", "secret")
        st.set_provisioning_allowed(False)
        patcher = patch("store.store.pymysql.connect")
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)
        conn, cursor = MagicMock(), MagicMock()
        conn.cursor.return_value = cursor
        self.connect.return_value = conn
        self.cursor = cursor

    def tearDown(self) -> None:
        st.set_provisioning_allowed(False)

    def test_write_statements_are_refused(self):
        cur = st._get_conn().cursor()
        for sql in (
            "INSERT INTO x VALUES (1)",
            "UPDATE x SET a = 1",
            "DELETE FROM x",
            "CREATE TABLE x (a int)",
            "DROP TABLE x",
            "GRANT SELECT_PRIV ON `dw`.* TO '%'",
        ):
            with self.assertRaises(WriteNotAllowed):
                cur.execute(sql)
        self.cursor.execute.assert_not_called()

    def test_select_and_use_pass_through(self):
        cur = st._get_conn().cursor()
        cur.execute("USE system_mcp")
        cur.execute("SELECT 1")
        self.assertEqual(self.cursor.execute.call_count, 2)

    def test_admin_still_writes(self):
        st.set_provisioning_allowed(True)
        cur = st._get_conn().cursor()
        cur.execute("INSERT INTO x VALUES (1)")
        self.cursor.execute.assert_called_once()


class WriteGuardTest(unittest.TestCase):
    """Mutations must refuse to run for a read-only caller, regardless of which
    code path reaches them. This is the structural net for the recurring
    "read path performs a write" bug class."""

    def setUp(self) -> None:
        st.set_request_credentials("backend_mcp_test", "secret")
        st.set_provisioning_allowed(False)
        st.DorisStore._table_cache.clear()

    def tearDown(self) -> None:
        st.set_provisioning_allowed(False)

    def _store(self):
        patcher = patch("store.store.pymysql.connect")
        connect = patcher.start()
        self.addCleanup(patcher.stop)
        conn, cursor = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        return st.DorisStore(workspace="guarded_ws"), connect

    def test_every_mutating_method_raises(self):
        store, connect = self._store()
        cases = [
            lambda: store.set_semantic_enabled(True, "x"),
            lambda: store.bump_semantic_version(),
            lambda: store.staging_upsert("a.yaml", "x"),
            lambda: store.staging_delete("a.yaml"),
            lambda: store.staging_discard(),
            lambda: store.staging_commit(),
            lambda: st.DorisStore.drop_workspace_tables("guarded_ws"),
        ]
        for call in cases:
            with self.assertRaises(WriteNotAllowed):
                call()
        # Nothing reached Doris at all.
        connect.assert_not_called()

    def test_reads_emit_no_write_sql(self):
        store, connect = self._store()
        cursor = connect.return_value.cursor.return_value.__enter__.return_value
        store.get_workspace_metadata()
        store.list_files()
        store.get_file("a.yaml")
        store.staging_list()
        stores = _sql(cursor)
        for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "GRANT"):
            # Word boundary: "UPDATED_AT" is a column, not an UPDATE statement.
            self.assertIsNone(re.search(rf"\b{verb}\b", stores), verb)


class SchemaMigrationTest(unittest.TestCase):
    """Old storage (no metadata table / missing columns) must be migrated by an
    admin before any write is allowed."""

    def setUp(self) -> None:
        st.set_request_credentials("admin", "pw")
        st.set_provisioning_allowed(True)
        st.DorisStore._schema_state = None
        st.DorisStore._table_cache.clear()

    def tearDown(self) -> None:
        st.set_provisioning_allowed(False)
        st.DorisStore._schema_state = None

    def _mock_conn(self, columns, rows, schema_exists=True):
        patcher = patch("store.store.pymysql.connect")
        connect = patcher.start()
        self.addCleanup(patcher.stop)
        conn, cursor = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchall.side_effect = [columns, rows]
        cursor.fetchone.return_value = ("system_mcp",) if schema_exists else None
        connect.return_value = conn
        return cursor

    def test_fresh_install_is_not_blocked(self):
        cursor = self._mock_conn(columns=[], rows=[], schema_exists=False)
        with patch.object(st.DorisStore, "discover_workspaces", return_value=[]):
            status = st.DorisStore.metadata_schema_status()
        self.assertEqual(status["state"], "fresh")
        self.assertTrue(status["ok"])
        self.assertNotIn("CREATE", _sql(cursor))

    def test_missing_table_requires_migration(self):
        self._mock_conn(columns=[], rows=[], schema_exists=True)
        with patch.object(st.DorisStore, "discover_workspaces", return_value=[]):
            status = st.DorisStore.metadata_schema_status()
        self.assertEqual(status["state"], "needs_migration")
        self.assertFalse(status["ok"])

    def test_missing_columns_require_migration(self):
        self._mock_conn(columns=[("workspace",), ("semantic_enabled",)],
                        rows=[("ws1",)])
        with patch.object(st.DorisStore, "discover_workspaces", return_value=["ws1"]):
            status = st.DorisStore.metadata_schema_status()
        self.assertEqual(status["state"], "outdated")
        self.assertIn("updated_by", status["missing_columns"])

    def test_writes_blocked_until_migrated(self):
        st.DorisStore._schema_state = "needs_migration"
        st.set_provisioning_allowed(False)     # read-only construction is allowed
        store = st.DorisStore(workspace="legacy")
        st.set_provisioning_allowed(True)      # admin write is blocked
        with self.assertRaises(SchemaMigrationRequired):
            store.staging_commit()
        with patch("store.store.pymysql.connect") as connect:
            with self.assertRaises(SchemaMigrationRequired):
                st.DorisStore(workspace="legacy2")   # constructor must not migrate
        connect.assert_not_called()

    def test_migration_creates_and_backfills(self):
        cursor = self._mock_conn(
            columns=[("workspace",), ("semantic_enabled",), ("semantic_version",),
                     ("updated_at",), ("updated_by",)],
            rows=[],
        )
        with patch.object(st.DorisStore, "discover_workspaces", return_value=["legacy"]):
            result = st.DorisStore.migrate_metadata_schema()
        self.assertEqual(result["backfilled"], ["legacy"])
        sql = _sql(cursor)
        self.assertIn("CREATE TABLE IF NOT EXISTS WORKSPACE_METADATA", sql)
        self.assertIn("INSERT INTO WORKSPACE_METADATA", sql)
        self.assertEqual(st.DorisStore._schema_state, None)   # cache invalidated

    def test_migration_requires_admin(self):
        st.set_provisioning_allowed(False)
        with self.assertRaises(WriteNotAllowed):
            st.DorisStore.migrate_metadata_schema()


class ProvisioningFlagTest(unittest.TestCase):
    """The admin flag must reach the store from the MCP auth layer."""

    def tearDown(self) -> None:
        st.set_provisioning_allowed(False)

    def _verifier(self, user, is_admin):
        import asyncio
        from auth.credential_cache import CredentialCache
        from auth.credential_verifier import CredentialVerifier

        cache = CredentialCache(ttl_seconds=600)
        cache.add(user, "pw")

        async def verify_fn(u, p):  # pragma: no cover - cache hit
            return True

        verifier = CredentialVerifier(cache, verify_fn, admin_resolver=lambda u: is_admin)

        async def scenario():
            token = await verifier.verify_token(f"{user}:pw")
            return token, st.provisioning_allowed()

        return asyncio.run(scenario())

    def test_admin_token_allows_provisioning(self):
        token, allowed = self._verifier("admin", True)
        self.assertIsNotNone(token)
        self.assertTrue(allowed)

    def test_normal_token_forbids_provisioning(self):
        token, allowed = self._verifier("backend_mcp_test", False)
        self.assertIsNotNone(token)
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
