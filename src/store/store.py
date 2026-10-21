"""Semantic configuration store backed by Doris.

Multi-workspace: each workspace has its own pair of Doris tables:

  system_mcp.active_store_{workspace}   — active (production) models
  system_mcp.staging_store_{workspace}  — pending changes

Tables
------
active_store_{workspace}
    filename    VARCHAR(512)   PRIMARY KEY
    updated_at  DATETIME       NOT NULL
    content     STRING         NOT NULL

staging_store_{workspace}
    filename    VARCHAR(512)   PRIMARY KEY
    action      VARCHAR(16)    NOT NULL   -- 'upsert' or 'delete'
    updated_at  DATETIME       NOT NULL
    content     STRING         -- NULL for delete actions
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from pathlib import Path

import pymysql

logger = logging.getLogger("doris_new_mcp.store")

# ---------------------------------------------------------------------------
# StoreState
# ---------------------------------------------------------------------------


@dataclass
class StoreState:
    revision: str
    version_label: str | None = None
    updated_at: str | None = None
    semantic_enabled: bool = True
    semantic_version: int = 0


@dataclass(frozen=True)
class WorkspaceMetadata:
    workspace: str
    semantic_enabled: bool
    semantic_version: int
    updated_at: str | None = None
    updated_by: str | None = None


# ---------------------------------------------------------------------------
# DorisStore
# ---------------------------------------------------------------------------

_DORIS_HOST = "127.0.0.1"
_DORIS_PORT = 9030
_DORIS_DB = "system_mcp"
_WORKSPACE_METADATA_TABLE = "workspace_metadata"

# Workspace names become table-name fragments (active_store_<ws>), so they must
# be plain identifiers. Without this, a crafted "workspace" query parameter or
# request field could inject SQL into every store statement.
_WORKSPACE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _validate_workspace_name(workspace: str) -> str:
    if not isinstance(workspace, str) or not _WORKSPACE_NAME_RE.fullmatch(workspace):
        raise ValueError(
            "Workspace name must start with a letter and contain only letters, "
            "digits and underscores"
        )
    return workspace

# Request-scoped credential override. Set once at the start of each
# authenticated request, read by _get_conn(). Destroyed when the
# asyncio Task ends — no cross-request leakage.
_request_creds: contextvars.ContextVar[tuple[str, str] | None] = \
    contextvars.ContextVar('_doris_store_creds', default=None)


def set_request_credentials(user: str, password: str) -> None:
    """Inject Doris credentials for the current request.

    Called once at request entry (CredentialVerifier or HTTP auth).
    The credentials live only for the duration of this asyncio Task.
    """
    _request_creds.set((user, password))


# Request-scoped: may this caller create storage that does not exist yet?
# True only for Doris admin requests; ordinary users must never run DDL.
_provision_allowed: contextvars.ContextVar[bool] = \
    contextvars.ContextVar('_doris_store_provision_allowed', default=False)


def set_provisioning_allowed(allowed: bool) -> None:
    _provision_allowed.set(bool(allowed))


def provisioning_allowed() -> bool:
    return bool(_provision_allowed.get())


class SemanticStorageNotInitialized(RuntimeError):
    """Storage is missing and this caller may not create it."""


class SchemaMigrationRequired(RuntimeError):
    """The semantic metadata table is missing/outdated; writes are blocked
    until an administrator runs the migration from the Semantic Web UI."""


class WriteNotAllowed(RuntimeError):
    """A mutating store method was reached from a read-only request.

    Ordinary Doris users hold SELECT only, so any write on their behalf would
    fail at Doris with "Access denied". Fail here instead, with a message that
    points at the programming error rather than the caller's privileges.
    """


def set_doris_endpoint(host: str, port: int) -> None:
    """Update the Doris FE endpoint from server configuration."""
    global _DORIS_HOST, _DORIS_PORT
    _DORIS_HOST = host
    _DORIS_PORT = port


def set_doris_port(port: int) -> None:
    """Update only the Doris FE port while preserving the configured host."""
    global _DORIS_PORT
    _DORIS_PORT = port


_WRITE_STMT_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE|RENAME|"
    r"GRANT|REVOKE|LOAD|SET)\b",
    re.IGNORECASE,
)


def _assert_statement_allowed(sql: str) -> None:
    """Refuse write statements for callers that may only read.

    This is the backstop for the recurring "read path performs a write" class
    of bug: even if a read method forgets its guard, the statement never
    reaches Doris with an ordinary user's credentials.
    """
    if not provisioning_allowed() and _WRITE_STMT_RE.match(str(sql)):
        raise WriteNotAllowed(
            "This request is read-only (ordinary Doris user): "
            f"refusing to execute {str(sql).split()[0].upper()} on Doris"
        )


class _GuardedCursor:
    """pymysql cursor proxy that blocks writes in read-only requests."""

    def __init__(self, cursor: Any):
        self._cursor = cursor

    def execute(self, sql: Any, args: Any = None) -> Any:
        _assert_statement_allowed(sql)
        if args is None:
            return self._cursor.execute(sql)
        return self._cursor.execute(sql, args)

    def executemany(self, sql: Any, args: Any) -> Any:
        _assert_statement_allowed(sql)
        return self._cursor.executemany(sql, args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        # pymysql cursors return themselves here; honour whatever the wrapped
        # cursor returns (tests replace it with a different object).
        self._cursor = self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)


class _GuardedConnection:
    """pymysql connection proxy whose cursors enforce the read-only policy."""

    def __init__(self, conn: Any):
        self._conn = conn

    def cursor(self, *args: Any, **kwargs: Any) -> _GuardedCursor:
        return _GuardedCursor(self._conn.cursor(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def _get_conn() -> Any:
    creds = _request_creds.get()
    if creds is None:
        raise RuntimeError(
            "No Doris credentials in request context. "
            "Ensure the request carries a valid Bearer token or session cookie."
        )
    user, password = creds
    return _GuardedConnection(pymysql.connect(
        host=_DORIS_HOST, port=_DORIS_PORT,
        user=user, password=password,
        charset="utf8mb4", autocommit=True,
        connect_timeout=5,
    ))


class DorisStore:
    """Semantic model storage for a single workspace.

    Each workspace has its own pair of tables:
      active_store_{workspace}
      staging_store_{workspace}
    """

    def __init__(self, workspace: str = "example") -> None:
        self._workspace = _validate_workspace_name(workspace)
        self._active_table = f"active_store_{workspace}"
        self._staging_table = f"staging_store_{workspace}"
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def store_type(self) -> str:
        return "doris"

    @property
    def source_uri(self) -> str:
        return f"doris://{_DORIS_HOST}:{_DORIS_PORT}/{_DORIS_DB}/{self._active_table}"

    @property
    def active_table(self) -> str:
        return self._active_table

    @property
    def staging_table(self) -> str:
        return self._staging_table

    # ------------------------------------------------------------------
    # Workspace discovery (class-level)
    # ------------------------------------------------------------------

    @classmethod
    def discover_workspaces(cls) -> list[str]:
        """Scan system_mcp for all active_store_* tables, return workspace names.

        Admins keep the self-initializing behaviour; ordinary users only read
        (information_schema / SHOW) and get a clear error when the database is
        missing, instead of a failed CREATE they have no privilege for.
        """
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                if provisioning_allowed():
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS {_DORIS_DB}")
                elif not cls._database_exists(cur):
                    raise SemanticStorageNotInitialized(
                        f"Semantic storage is not initialized: database "
                        f"'{_DORIS_DB}' does not exist or is not readable. "
                        "Ask a Doris administrator to initialize it."
                    )
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute("SHOW TABLES LIKE 'active_store_%'")
                tables = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        names = []
        for table in tables:
            if not table.startswith("active_store_"):
                continue
            name = table[len("active_store_"):]
            if _WORKSPACE_NAME_RE.fullmatch(name):
                names.append(name)
            else:
                logger.warning("Ignoring workspace table with invalid name: %r", table)
        return names

    @staticmethod
    def _database_exists(cur) -> bool:
        cur.execute(
            "SELECT SCHEMA_NAME FROM information_schema.schemata "
            "WHERE SCHEMA_NAME = %s",
            (_DORIS_DB,),
        )
        return cur.fetchone() is not None

    @classmethod
    def drop_workspace_tables(cls, workspace: str) -> None:
        """Drop ONLY the semantic model tables for a workspace.

        Drops: active_store_{workspace}, staging_store_{workspace}
        Does NOT touch any data tables (e.g., dw.orders, dw.users).

        Safe to call when tables don't exist (uses IF EXISTS).
        """
        workspace = _validate_workspace_name(workspace)
        if not provisioning_allowed():
            raise WriteNotAllowed(
                "Dropping workspace storage requires a Doris administrator"
            )
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                try:
                    cur.execute(
                        f"DELETE FROM {_WORKSPACE_METADATA_TABLE} WHERE workspace = %s",
                        (workspace,),
                    )
                except Exception:
                    # Backward-compatible with deployments created before the
                    # shared metadata table existed.
                    pass
                cur.execute(f"DROP TABLE IF EXISTS active_store_{workspace}")
                cur.execute(f"DROP TABLE IF EXISTS staging_store_{workspace}")
            logger.info(f"Dropped semantic tables for workspace '{workspace}'")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # check_remote
    # ------------------------------------------------------------------

    def check_remote(self) -> StoreState:
        metadata = self.get_workspace_metadata()
        return StoreState(
            revision=str(metadata.semantic_version),
            updated_at=metadata.updated_at,
            semantic_enabled=metadata.semantic_enabled,
            semantic_version=metadata.semantic_version,
        )

    # ------------------------------------------------------------------
    # Workspace metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _metadata_from_row(workspace: str, row: tuple) -> WorkspaceMetadata:
        updated_at = row[2]
        if isinstance(updated_at, datetime):
            updated_at = updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif updated_at is not None:
            updated_at = str(updated_at)
        return WorkspaceMetadata(
            workspace=workspace,
            semantic_enabled=bool(row[0]),
            semantic_version=int(row[1]),
            updated_at=updated_at,
            updated_by=str(row[3]) if row[3] is not None else None,
        )

    def _get_workspace_metadata_with_cursor(
        self, cur, create_missing: bool = True,
    ) -> WorkspaceMetadata:
        """Read one workspace's metadata row.

        ``create_missing=False`` is used by read paths: a missing row (legacy or
        externally created workspace) must not turn a SELECT into an INSERT,
        because ordinary users have no INSERT privilege.
        """
        cur.execute(
            f"SELECT semantic_enabled, semantic_version, updated_at, updated_by "
            f"FROM {_WORKSPACE_METADATA_TABLE} WHERE workspace = %s",
            (self._workspace,),
        )
        row = cur.fetchone()
        if row is None:
            if not create_missing:
                return self._default_metadata()
            self._require_write()
            user = (_request_creds.get() or ("system", ""))[0]
            now = datetime.now()
            cur.execute(
                f"INSERT INTO {_WORKSPACE_METADATA_TABLE} "
                "(workspace, semantic_enabled, semantic_version, updated_at, updated_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (self._workspace, True, 1, now, user),
            )
            return WorkspaceMetadata(
                workspace=self._workspace,
                semantic_enabled=True,
                semantic_version=1,
                updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                updated_by=user,
            )
        return self._metadata_from_row(self._workspace, row)

    def _default_metadata(self) -> WorkspaceMetadata:
        """State assumed when a workspace has no published metadata row.

        Older/externally created workspaces have tables but no row; they are
        enabled with version 1 so the version check stays stable. The row is
        created by the next admin write (toggle, commit, deploy).
        """
        return WorkspaceMetadata(
            workspace=self._workspace,
            semantic_enabled=True,
            semantic_version=1,
            updated_at=None,
            updated_by=None,
        )

    def get_workspace_metadata(self) -> WorkspaceMetadata:
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                return self._get_workspace_metadata_with_cursor(cur, create_missing=False)
        finally:
            conn.close()

    def set_semantic_enabled(self, enabled: bool, updated_by: str) -> WorkspaceMetadata:
        self._require_write()
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                self._get_workspace_metadata_with_cursor(cur)
                cur.execute(
                    f"UPDATE {_WORKSPACE_METADATA_TABLE} "
                    "SET semantic_enabled = %s, updated_at = %s, updated_by = %s "
                    "WHERE workspace = %s",
                    (enabled, datetime.now(), updated_by, self._workspace),
                )
                return self._get_workspace_metadata_with_cursor(cur)
        finally:
            conn.close()

    def bump_semantic_version(self, updated_by: str | None = None) -> WorkspaceMetadata:
        self._require_write()
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                self._get_workspace_metadata_with_cursor(cur)
                user = updated_by or (_request_creds.get() or ("system", ""))[0]
                cur.execute(
                    f"UPDATE {_WORKSPACE_METADATA_TABLE} "
                    "SET semantic_version = semantic_version + 1, "
                    "updated_at = %s, updated_by = %s WHERE workspace = %s",
                    (datetime.now(), user, self._workspace),
                )
                return self._get_workspace_metadata_with_cursor(cur)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    def fetch(self, local_dir: Path) -> StoreState:
        self._ensure_tables()
        local_dir.mkdir(parents=True, exist_ok=True)

        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"SELECT filename, updated_at, content FROM {self._active_table}")
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            metadata = self.get_workspace_metadata()
            return StoreState(
                revision=str(metadata.semantic_version),
                updated_at=metadata.updated_at,
                semantic_enabled=metadata.semantic_enabled,
                semantic_version=metadata.semantic_version,
            )

        db_filenames: set[str] = set()
        latest_updated: datetime | None = None

        for filename, updated_at, content in rows:
            db_filenames.add(filename)
            filepath = local_dir / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            if isinstance(updated_at, datetime):
                if latest_updated is None or updated_at > latest_updated:
                    latest_updated = updated_at

        for root, _, filenames in os.walk(local_dir):
            for fn in filenames:
                if not fn.endswith((".yml", ".yaml")):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, local_dir)
                if rel not in db_filenames:
                    os.remove(full)

        metadata = self.get_workspace_metadata()
        updated_at = latest_updated.strftime("%Y-%m-%dT%H:%M:%SZ") if latest_updated else None
        logger.info(f"fetch [{self._workspace}]: {len(rows)} files → {local_dir}")
        return StoreState(
            revision=str(metadata.semantic_version),
            updated_at=updated_at,
            semantic_enabled=metadata.semantic_enabled,
            semantic_version=metadata.semantic_version,
        )

    # ------------------------------------------------------------------
    # Active file helpers
    # ------------------------------------------------------------------

    def list_files(self) -> list[dict]:
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(
                    f"SELECT filename, updated_at, LENGTH(content) AS size_bytes "
                    f"FROM {self._active_table} ORDER BY filename"
                )
                rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"list_files [{self._workspace}]: {e}")
            return []
        finally:
            conn.close()
        return [
            {"filename": r[0], "updated_at": r[1].strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(r[1], datetime) else str(r[1]), "size_bytes": r[2]}
            for r in rows
        ]

    def get_file(self, filename: str) -> dict | None:
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"SELECT filename, updated_at, content FROM {self._active_table} WHERE filename = %s", (filename,))
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "filename": row[0],
            "updated_at": row[1].strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(row[1], datetime) else str(row[1]),
            "content": row[2],
        }

    # ------------------------------------------------------------------
    # Staging helpers
    # ------------------------------------------------------------------

    def staging_list(self) -> list[dict]:
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(
                    f"SELECT filename, action, updated_at, LENGTH(content) AS size_bytes "
                    f"FROM {self._staging_table} ORDER BY filename"
                )
                rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"staging_list [{self._workspace}]: {e}")
            return []
        finally:
            conn.close()
        return [
            {"filename": r[0], "action": r[1], "updated_at": r[2].strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(r[2], datetime) else str(r[2]), "size_bytes": r[3] if r[3] else 0}
            for r in rows
        ]

    def staging_upsert(self, filename: str, content: str) -> None:
        self._require_write()
        self._ensure_tables()
        now = datetime.now()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"SELECT 1 FROM {self._staging_table} WHERE filename = %s", (filename,))
                if cur.fetchone():
                    cur.execute(
                        f"UPDATE {self._staging_table} SET action='upsert', updated_at=%s, content=%s WHERE filename=%s",
                        (now, content, filename),
                    )
                else:
                    cur.execute(
                        f"INSERT INTO {self._staging_table} (filename, action, updated_at, content) VALUES (%s, 'upsert', %s, %s)",
                        (filename, now, content),
                    )
        finally:
            conn.close()
        logger.info(f"staging upsert [{self._workspace}]: {filename}")

    def staging_delete(self, filename: str) -> str:
        """Mark file for deletion in staging. Smart undo-aware:
        - If file is already in staging → remove it (undo pending change)
        - If file is in active_store → add 'delete' record to staging
        Returns 'removed', 'staged', or 'not_found'.
        """
        self._require_write()
        self._ensure_tables()
        conn = _get_conn()
        now = datetime.now()
        result = "not_found"
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                # Check staging first
                cur.execute(f"SELECT 1 FROM {self._staging_table} WHERE filename = %s", (filename,))
                if cur.fetchone():
                    cur.execute(f"DELETE FROM {self._staging_table} WHERE filename = %s", (filename,))
                    result = "removed"
                    logger.info(f"staging delete [{self._workspace}]: removed {filename} from staging (undo)")
                else:
                    # Check active store
                    cur.execute(f"SELECT 1 FROM {self._active_table} WHERE filename = %s", (filename,))
                    if cur.fetchone():
                        cur.execute(
                            f"INSERT INTO {self._staging_table} (filename, action, updated_at, content) VALUES (%s, 'delete', %s, NULL)",
                            (filename, now),
                        )
                        result = "staged"
                        logger.info(f"staging delete [{self._workspace}]: queued delete for {filename}")
        finally:
            conn.close()
        return result

    def staging_discard(self) -> None:
        self._require_write()
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"DELETE FROM {self._staging_table}")
        finally:
            conn.close()
        logger.info(f"staging discarded [{self._workspace}]")

    def staging_fetch(self, local_dir: Path) -> StoreState:
        self._ensure_tables()
        local_dir.mkdir(parents=True, exist_ok=True)

        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"SELECT filename, content FROM {self._active_table}")
                active = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(f"SELECT filename, action, content FROM {self._staging_table}")
                staging = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            conn.close()

        merged: dict[str, str | None] = dict(active)
        for fn, (action, content) in staging.items():
            if action == "delete":
                merged.pop(fn, None)
            elif action == "upsert":
                merged[fn] = content

        for root, _, filenames in os.walk(local_dir):
            for fn in filenames:
                if fn.endswith((".yml", ".yaml")):
                    os.remove(os.path.join(root, fn))

        for fn, content in merged.items():
            if content is not None:
                filepath = local_dir / fn
                filepath.parent.mkdir(parents=True, exist_ok=True)
                filepath.write_text(content, encoding="utf-8")

        revision = hashlib.sha256(
            json.dumps(sorted(merged.keys()), ensure_ascii=False).encode()
        ).hexdigest()
        logger.info(f"staging fetch [{self._workspace}]: {len(merged)} files → {local_dir}")
        return StoreState(revision=revision)

    def staging_commit(self) -> StoreState:
        self._require_write()
        self._ensure_tables()
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"SELECT filename, action, content, updated_at FROM {self._staging_table}")
                stg_rows = cur.fetchall()

                if not stg_rows:
                    metadata = self._get_workspace_metadata_with_cursor(cur)
                    return StoreState(
                        revision=str(metadata.semantic_version),
                        updated_at=metadata.updated_at,
                        semantic_enabled=metadata.semantic_enabled,
                        semantic_version=metadata.semantic_version,
                    )

                for fn, action, content, stg_ts in stg_rows:
                    if action == "delete":
                        cur.execute(f"DELETE FROM {self._active_table} WHERE filename = %s", (fn,))
                    elif action == "upsert":
                        cur.execute(f"SELECT 1 FROM {self._active_table} WHERE filename = %s", (fn,))
                        if cur.fetchone():
                            cur.execute(
                                f"UPDATE {self._active_table} SET updated_at=%s, content=%s WHERE filename=%s",
                                (stg_ts, content, fn),
                            )
                        else:
                            cur.execute(
                                f"INSERT INTO {self._active_table} (filename, updated_at, content) VALUES (%s, %s, %s)",
                                (fn, stg_ts, content),
                            )

                cur.execute(f"DELETE FROM {self._staging_table}")

                self._get_workspace_metadata_with_cursor(cur)
                user = (_request_creds.get() or ("system", ""))[0]
                cur.execute(
                    f"UPDATE {_WORKSPACE_METADATA_TABLE} "
                    "SET semantic_version = semantic_version + 1, "
                    "updated_at = %s, updated_by = %s WHERE workspace = %s",
                    (datetime.now(), user, self._workspace),
                )
                metadata = self._get_workspace_metadata_with_cursor(cur)
        finally:
            conn.close()

        logger.info(f"staging committed [{self._workspace}]: {len(stg_rows)} changes, staging cleared")
        return StoreState(
            revision=str(metadata.semantic_version),
            updated_at=metadata.updated_at,
            semantic_enabled=metadata.semantic_enabled,
            semantic_version=metadata.semantic_version,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    _table_cache: dict[str, bool] = {}

    _schema_state: str | None = None

    @classmethod
    def _metadata_columns_with_cursor(cls, cur) -> set[str]:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (_DORIS_DB, _WORKSPACE_METADATA_TABLE),
        )
        return {str(row[0]).lower() for row in cur.fetchall()}

    @classmethod
    def metadata_schema_status(cls) -> dict:
        """Inspect the metadata storage without writing.

        Returns a state used to decide whether writes must be blocked:
          ok              table exists with all required columns
          fresh           no storage database yet (nothing to migrate)
          needs_migration database/tables exist but the metadata table is absent
          outdated        metadata table exists but lacks required columns
        ``missing_rows`` lists workspaces with tables but no metadata row; those
        are backfilled by the migration or by the next admin write.
        """
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                columns = cls._metadata_columns_with_cursor(cur)
                if not columns:
                    cur.execute(
                        "SELECT SCHEMA_NAME FROM information_schema.schemata "
                        "WHERE SCHEMA_NAME = %s", (_DORIS_DB,),
                    )
                    state = "fresh" if cur.fetchone() is None else "needs_migration"
                    return {"state": state, "ok": state == "fresh",
                            "missing_columns": [], "missing_rows": []}

                missing_columns = sorted(
                    {"workspace", "semantic_enabled", "semantic_version",
                     "updated_at", "updated_by"} - columns
                )
                cur.execute(f"SELECT workspace FROM {_WORKSPACE_METADATA_TABLE}")
                present = {str(row[0]) for row in cur.fetchall()}
        finally:
            conn.close()

        missing_rows = [w for w in cls.discover_workspaces() if w not in present]
        state = "outdated" if missing_columns else "ok"
        return {"state": state, "ok": state == "ok",
                "missing_columns": missing_columns, "missing_rows": missing_rows}

    @classmethod
    def migrate_metadata_schema(cls) -> dict:
        """Create/backfill the metadata storage. Admin-only (runs DDL/DML)."""
        if not provisioning_allowed():
            raise WriteNotAllowed("Schema migration requires a Doris administrator")

        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS {_DORIS_DB}")
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"""\
                    CREATE TABLE IF NOT EXISTS {_WORKSPACE_METADATA_TABLE} (
                        workspace          VARCHAR(128) NOT NULL,
                        semantic_enabled   TINYINT NOT NULL DEFAULT "1",
                        semantic_version   BIGINT NOT NULL DEFAULT "1",
                        updated_at         DATETIME NOT NULL,
                        updated_by         VARCHAR(256) NULL
                    ) UNIQUE KEY(workspace)
                    DISTRIBUTED BY HASH(workspace) BUCKETS 1
                    PROPERTIES ('replication_num' = '1')
                """)
                columns = cls._metadata_columns_with_cursor(cur)
                missing_columns = sorted(
                    {"workspace", "semantic_enabled", "semantic_version",
                     "updated_at", "updated_by"} - columns
                )
                cur.execute(f"SELECT workspace FROM {_WORKSPACE_METADATA_TABLE}")
                present = {str(row[0]) for row in cur.fetchall()}
                user = (_request_creds.get() or ("system", ""))[0]
                added = []
                for workspace in cls.discover_workspaces():
                    if workspace in present:
                        continue
                    cur.execute(
                        f"INSERT INTO {_WORKSPACE_METADATA_TABLE} "
                        "(workspace, semantic_enabled, semantic_version, updated_at, updated_by) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (workspace, True, 1, datetime.now(), user),
                    )
                    added.append(workspace)
        finally:
            conn.close()

        cls._schema_state = None
        if missing_columns:
            raise SchemaMigrationRequired(
                "Metadata table is missing columns: " + ", ".join(missing_columns)
            )
        logger.info("Metadata schema migrated; backfilled rows: %s", added or "none")
        return {"created": True, "backfilled": added}

    def _require_write(self) -> None:
        """Reject mutations unless the current request may provision storage.

        Only admin requests set the flag. Reads must never reach this: if they
        do, it is a bug in the caller, not a permission problem for the user.
        """
        if not provisioning_allowed():
            raise WriteNotAllowed(
                f"Workspace '{self._workspace}' storage is read-only for this request; "
                "mutations require a Doris administrator"
            )
        cls = type(self)
        if cls._schema_state is None:
            cls._schema_state = cls.metadata_schema_status()["state"]
        if cls._schema_state in ("needs_migration", "outdated"):
            raise SchemaMigrationRequired(
                "Semantic metadata storage needs migration: open the Semantic "
                "Web UI as an admin and run 'Migrate metadata'. Writes are blocked "
                "until it succeeds."
            )

    def _ensure_tables(self) -> None:
        # Ordinary users hold SELECT but not CREATE. This runs from read paths
        # too (DorisStore construction, metadata reads), so it must be a no-op
        # for them: a missing table then surfaces as a query error, never as a
        # CREATE that fails with Access denied.
        if not provisioning_allowed():
            return
        cls = type(self)
        if cls._schema_state is None:
            cls._schema_state = cls.metadata_schema_status()["state"]
        if cls._schema_state in ("needs_migration", "outdated"):
            raise SchemaMigrationRequired(
                "Semantic metadata storage needs migration: open the Semantic Web UI "
                "as an admin and run 'Migrate metadata'."
            )
        key = self._workspace
        if self._table_cache.get(key):
            return
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS {_DORIS_DB}")
                cur.execute(f"USE {_DORIS_DB}")
                cur.execute(f"""\
                    CREATE TABLE IF NOT EXISTS {_WORKSPACE_METADATA_TABLE} (
                        workspace          VARCHAR(128) NOT NULL,
                        semantic_enabled   TINYINT NOT NULL DEFAULT "1",
                        semantic_version   BIGINT NOT NULL DEFAULT "1",
                        updated_at         DATETIME NOT NULL,
                        updated_by         VARCHAR(256) NULL
                    ) UNIQUE KEY(workspace)
                    DISTRIBUTED BY HASH(workspace) BUCKETS 1
                    PROPERTIES ('replication_num' = '1')
                """)
                cur.execute(f"""\
                    CREATE TABLE IF NOT EXISTS {self._active_table} (
                        filename    VARCHAR(512) NOT NULL,
                        updated_at  DATETIME NOT NULL,
                        content     STRING NOT NULL
                    ) UNIQUE KEY(filename)
                    DISTRIBUTED BY HASH(filename) BUCKETS 1
                    PROPERTIES ('replication_num' = '1')
                """)
                cur.execute(f"""\
                    CREATE TABLE IF NOT EXISTS {self._staging_table} (
                        filename    VARCHAR(512) NOT NULL,
                        action      VARCHAR(16) NOT NULL,
                        updated_at  DATETIME NOT NULL,
                        content     STRING NULL
                    ) UNIQUE KEY(filename)
                    DISTRIBUTED BY HASH(filename) BUCKETS 1
                    PROPERTIES ('replication_num' = '1')
                """)
                self._get_workspace_metadata_with_cursor(cur)
            self._table_cache[key] = True
            cls._schema_state = "ok"
            logger.info(f"Tables ensured for workspace '{self._workspace}': {self._active_table}, {self._staging_table}")
        except Exception as e:
            self._table_cache.pop(key, None)
            logger.warning(f"Table init failed for workspace '{self._workspace}': {e}")
        finally:
            conn.close()
