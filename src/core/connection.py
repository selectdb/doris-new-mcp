"""Async connection pool for a single Doris cluster."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiomysql

from config.loader import ClusterConfig

logger = logging.getLogger("doris_new_mcp.connection")

# The query pool is the one place tool SQL reaches Doris. Everything it runs is
# supposed to be read-only already (sqlglot validation for execute_query,
# validate_readonly for compiled metric SQL, fixed SHOW/DESCRIBE elsewhere);
# this is the last-line backstop so a missed validation cannot become a write.
_READ_ONLY_PREFIXES = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
_STATEMENT_HEAD = re.compile(
    r"^\s*(?:(?:--[^\n]*\n|#[^\n]*\n|/\*.*?\*/)\s*)*([A-Za-z]+)",
    re.DOTALL,
)


def assert_read_only_statement(sql: str) -> None:
    """Reject anything that is not a read statement, before it reaches Doris."""
    match = _STATEMENT_HEAD.match(sql)
    keyword = match.group(1).upper() if match else ""
    if keyword not in _READ_ONLY_PREFIXES:
        raise ValueError(
            f"Only read-only SQL is allowed on the query pool (got "
            f"{keyword or 'empty'} statement)"
        )


class ConnectionPool:
    """Manages an aiomysql pool for the Doris cluster."""

    def __init__(
        self,
        cluster: ClusterConfig,
        user: str,
        password: str,
        min_size: int | None = None,
        max_size: int | None = None,
        host: str | None = None,
    ):
        self._cluster = cluster
        self._user = user
        self._password = password
        self._min_size = min_size if min_size is not None else cluster.pool_min_size
        self._max_size = max_size if max_size is not None else cluster.pool_max_size
        self._host = host or cluster.fe_host
        self._pool: aiomysql.Pool | None = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> aiomysql.Pool:
        if self._pool is not None and not self._pool.closed:
            return self._pool
        async with self._lock:
            if self._pool is not None and not self._pool.closed:
                return self._pool
            self._pool = await aiomysql.create_pool(
                host=self._host,
                port=self._cluster.fe_mysql_port,
                user=self._user,
                password=self._password,
                minsize=self._min_size,
                maxsize=self._max_size,
                connect_timeout=10,
                autocommit=True,
                # Recycle connections idle longer than this so a connection
                # silently dropped by Doris wait_timeout is never reused.
                pool_recycle=self._cluster.pool_idle_timeout,
            )
            return self._pool

    async def execute(
        self,
        sql: str,
        database: str | None = None,
        max_rows: int | None = None,
        timeout: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Execute read-only SQL and return (rows_as_dicts, column_names)."""
        assert_read_only_statement(sql)
        pool = await self._ensure_pool()
        _timeout = timeout or self._cluster.query_timeout
        _max_rows = max_rows or self._cluster.max_rows

        async with pool.acquire() as conn:
            try:
                if database:
                    await conn.select_db(database)
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await asyncio.wait_for(cur.execute(sql), timeout=_timeout)
                    rows = await cur.fetchmany(_max_rows)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    return list(rows), columns
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Timed out or cancelled mid-query: the connection's protocol
                # state is unknown, so close it — pool.release() discards
                # closed connections instead of returning them to the pool.
                # asyncio.TimeoutError is builtin TimeoutError on 3.11+ and
                # a distinct class on 3.10; both are covered here.
                conn.close()
                raise

    async def close(self) -> None:
        if self._pool and not self._pool.closed:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
