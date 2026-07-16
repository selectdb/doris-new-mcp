"""Authentication providers for doris-mcp.

Implements two token verification strategies:
- ``DorisStaticVerifier``: validates tokens against inline config (tokens.yaml-equivalent).
- ``DorisJWTVerifier``: validates JWT tokens with scope-to-tools expansion.

Both produce a unified ``AccessToken`` whose ``scopes`` field contains tool names,
consumed by the guard layer for authorization.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

from auth.config import AuthConfig, JWTAuthConfig, StaticAuthConfig

logger = logging.getLogger("doris_new_mcp.auth")


class DorisStaticVerifier(TokenVerifier):
    """Verify tokens against a static token list from config.

    Each configured token maps directly to an ``AccessToken`` with
    ``scopes = allowed_tools``.
    """

    def __init__(self, static_cfg: StaticAuthConfig) -> None:
        super().__init__(required_scopes=[])
        self._token_map: dict[str, dict[str, Any]] = {}
        for entry in static_cfg.tokens:
            self._token_map[entry.token] = {
                "name": entry.name,
                "allowed_tools": entry.allowed_tools,
            }
        logger.info(
            "DorisStaticVerifier initialized with %d token(s)", len(self._token_map),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        entry = self._token_map.get(token)
        if entry is None:
            logger.debug("Static token verification failed: token not recognized")
            return None

        client_id = entry["name"]
        logger.debug(
            "Static token verified: client_id='%s', allowed_tools=%d",
            client_id, len(entry["allowed_tools"]),
        )
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=entry["allowed_tools"],
            expires_at=None,
        )


class DorisJWTVerifier(JWTVerifier):
    """JWT verification with scope-to-tools expansion.

    After the base ``JWTVerifier`` validates the JWT signature, expiration,
    issuer, and audience, this class expands JWT scopes into tool names
    using the configured ``scope_mapping``.

    If no ``scope_mapping`` is configured, JWT scopes are used as tool names
    directly (the JWT issuer must then use tool names as scopes).
    """

    def __init__(
        self,
        scope_mapping: dict[str, list[str]],
        **jwt_kwargs: Any,
    ) -> None:
        super().__init__(**jwt_kwargs)
        self._scope_mapping = scope_mapping
        if scope_mapping:
            logger.info(
                "DorisJWTVerifier initialized with %d scope mapping(s): %s",
                len(scope_mapping), list(scope_mapping.keys()),
            )
        else:
            logger.info(
                "DorisJWTVerifier initialized without scope mapping — "
                "JWT scopes will be used as tool names directly"
            )

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            logger.debug("JWT token verification failed (signature/expiration/issuer/audience)")
            return None

        logger.debug(
            "JWT token verified: client_id='%s', raw_scopes=%s",
            access.client_id, access.scopes,
        )

        if not self._scope_mapping:
            return access  # scopes are tool names already

        expanded = self._expand_scopes(access.scopes)
        logger.debug(
            "JWT scopes expanded: %s → allowed_tools=%s",
            access.scopes, expanded,
        )
        return AccessToken(
            token=access.token,
            client_id=access.client_id,
            scopes=expanded,
            expires_at=access.expires_at,
            claims=access.claims,
        )

    def _expand_scopes(self, jwt_scopes: list[str]) -> list[str]:
        """Expand JWT scopes into tool names via scope_mapping.

        Unmapped scopes are passed through as-is (may be tool names directly).
        """
        tools: list[str] = []
        for scope in jwt_scopes:
            if scope in self._scope_mapping:
                mapped = self._scope_mapping[scope]
                logger.debug("  Scope '%s' → %s", scope, mapped)
                tools.extend(mapped)
            else:
                logger.debug("  Scope '%s' not in mapping, kept as-is", scope)
                tools.append(scope)
        # Deduplicate while preserving order
        return list(dict.fromkeys(tools))


def _build_jwt_verifier(jwt_cfg: JWTAuthConfig) -> DorisJWTVerifier:
    """Build a DorisJWTVerifier from config."""
    jwt_kwargs: dict[str, Any] = {
        "algorithm": jwt_cfg.algorithm,
    }

    if jwt_cfg.jwks_uri:
        jwt_kwargs["jwks_uri"] = jwt_cfg.jwks_uri
    elif jwt_cfg.public_key_file:
        logger.info("Loading JWT public key from: %s", jwt_cfg.public_key_file)
        with open(jwt_cfg.public_key_file, "r") as f:
            jwt_kwargs["public_key"] = f.read()

    if jwt_cfg.issuer:
        jwt_kwargs["issuer"] = jwt_cfg.issuer
    if jwt_cfg.audience:
        jwt_kwargs["audience"] = jwt_cfg.audience

    scope_mapping = jwt_cfg.build_scope_mapping()
    return DorisJWTVerifier(scope_mapping=scope_mapping, **jwt_kwargs)


def create_auth_provider(
    auth_cfg: AuthConfig | None,
    doris_oauth: AuthProvider | None = None,
) -> AuthProvider | None:
    """Create an auth provider based on config.

    At most one of auth_cfg.static / auth_cfg.jwt / auth_cfg.oauth is set
    (enforced in AuthConfig.from_dict), so this function always returns
    either ``None`` or a single provider; there is no multi-provider path.
    """
    if auth_cfg is None:
        logger.info("Auth: disabled (no 'auth' section in config)")
        return None

    if auth_cfg.static:
        logger.info("Auth: static verifier registered")
        return DorisStaticVerifier(auth_cfg.static)

    if auth_cfg.jwt:
        logger.info("Auth: JWT verifier registered")
        return _build_jwt_verifier(auth_cfg.jwt)

    if doris_oauth is not None:
        logger.info("Auth: Doris OAuth provider registered")
        return doris_oauth

    raise RuntimeError(
        "auth.oauth is configured but no DorisOAuthProvider was passed to "
        "create_auth_provider(); the caller must construct the provider "
        "alongside the pool manager before creating the server."
    )
