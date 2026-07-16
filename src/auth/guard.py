"""Tool-level authorization guard (simplified for token-based auth)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.response import ErrorCode, error_response

if TYPE_CHECKING:
    from core.connection import ConnectionPool
    from core.pool_manager import PoolManager

logger = logging.getLogger("doris_new_mcp.auth")

_pool_manager: PoolManager | None = None
_service_pool: ConnectionPool | None = None
_transport: str = "streamable-http"


def init_guard(
    pool_manager: PoolManager | None = None,
    service_pool: ConnectionPool | None = None,
    oauth_provider=None,  # kept for backward compat
    transport: str = "streamable-http",
) -> None:
    global _pool_manager, _service_pool, _transport
    _pool_manager = pool_manager
    _service_pool = service_pool
    _transport = transport


@dataclass
class AuthResult:
    client_id: str | None = None
    denied: str | None = None
    pool: "ConnectionPool | None" = None


def check_tool_access(tool_name: str) -> AuthResult:
    if _transport == "stdio":
        return AuthResult(pool=_service_pool)
    # HTTP mode: allow all with service pool.
    # Per-user isolation for semantic tools via Bearer token in server.py.
    return AuthResult(pool=_service_pool)
