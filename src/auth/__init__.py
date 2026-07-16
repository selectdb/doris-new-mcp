"""Authentication and authorization for doris-mcp.

Public API:
    - ``create_auth_provider(auth_cfg, doris_oauth)`` — build a FastMCP auth provider.
    - ``check_tool_access(tool_name)`` — per-tool authorization guard.
    - ``init_guard(pool_manager, service_pool, oauth_provider, transport)`` — initialize guard.
    - ``AuthResult`` — structured result from tool access check.
    - ``AuthConfig`` / ``OAuthConfig`` — parsed auth configuration.
    - ``CredentialVerifier`` — username:password credential validator.
    - ``CredentialCache`` — in-memory TTL cache for verified credentials.
    - ``DEFAULT_ALLOWED_TOOLS`` — default read-only tool set.
"""

from auth.config import AuthConfig, DEFAULT_ALLOWED_TOOLS, OAuthConfig
from auth.credential_cache import CredentialCache
from auth.credential_verifier import CredentialVerifier
from auth.guard import AuthResult, check_tool_access, init_guard
from auth.provider import create_auth_provider

__all__ = [
    "AuthConfig",
    "AuthResult",
    "CredentialCache",
    "CredentialVerifier",
    "DEFAULT_ALLOWED_TOOLS",
    "OAuthConfig",
    "check_tool_access",
    "create_auth_provider",
    "init_guard",
]
