"""Authentication configuration parsing and validation.

Parses the ``auth`` section of mcp-server.yaml and validates required fields.
Missing or malformed configuration causes startup failure with clear error logs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("doris_new_mcp.auth")

# ---------------------------------------------------------------------------
# Default allowed_tools: all read-only tools.
# Excludes admin/mutating tools (e.g. reload_semantic_layer).
# Used when a static token or JWT scope omits ``allowed_tools``.
# ---------------------------------------------------------------------------
DEFAULT_ALLOWED_TOOLS: list[str] = [
    # Base
    "get_query_guide",
    "check_service_health",
    "list_databases",
    "list_tables",
    "describe_table",
    "execute_query",
    # Semantic layer (runtime-gated by semantic_enabled)
    "list_metrics",
    "list_dimensions_for_metric",
    "query_metric",
]


@dataclass
class StaticTokenEntry:
    """A single static token with its permissions."""

    token: str
    name: str
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.allowed_tools:
            self.allowed_tools = list(DEFAULT_ALLOWED_TOOLS)
            logger.debug(
                "Token '%s' has no allowed_tools configured, "
                "using default read-only tool set (%d tools)",
                self.name,
                len(self.allowed_tools),
            )


@dataclass
class StaticAuthConfig:
    """Configuration for static token authentication."""

    tokens: list[StaticTokenEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StaticAuthConfig:
        """Parse ``auth.static`` section.

        Raises:
            ValueError: If tokens list is empty or a token entry lacks the
                ``token`` field.
        """
        raw_tokens = data.get("tokens")
        if not raw_tokens:
            raise ValueError(
                "auth.static.tokens must be a non-empty list. "
                "Each entry requires 'token' and 'name' fields."
            )

        entries: list[StaticTokenEntry] = []
        for idx, item in enumerate(raw_tokens):
            if not isinstance(item, dict):
                raise ValueError(
                    f"auth.static.tokens[{idx}]: expected a mapping, got {type(item).__name__}"
                )
            token_val = item.get("token")
            if not token_val or not isinstance(token_val, str):
                raise ValueError(
                    f"auth.static.tokens[{idx}]: 'token' field is required and must be a non-empty string"
                )
            name_val = item.get("name")
            if not name_val or not isinstance(name_val, str):
                raise ValueError(
                    f"auth.static.tokens[{idx}]: 'name' field is required and must be a non-empty string. "
                    f"This is a short identifier for the token (e.g. 'analyst-zhangsan'), "
                    f"used as client_id in audit logs."
                )
            name_val = name_val.strip()
            if not name_val:
                raise ValueError(
                    f"auth.static.tokens[{idx}]: 'name' must not be blank (whitespace-only)"
                )
            allowed = item.get("allowed_tools", [])
            if not isinstance(allowed, list):
                raise ValueError(
                    f"auth.static.tokens[{idx}]: 'allowed_tools' must be a list, got {type(allowed).__name__}"
                )
            entries.append(StaticTokenEntry(
                token=token_val,
                name=name_val,
                description=item.get("description", ""),
                allowed_tools=allowed,
            ))

        # Validate name uniqueness
        names = [e.name for e in entries]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ValueError(
                    f"auth.static.tokens: duplicate name '{name}'. "
                    f"Each token must have a unique 'name'."
                )
            seen.add(name)

        cfg = cls(tokens=entries)
        logger.info(
            "Static auth config loaded: %d token(s) configured", len(entries),
        )
        for entry in entries:
            tools_display = entry.allowed_tools[:3]
            suffix = f" ... (+{len(entry.allowed_tools) - 3})" if len(entry.allowed_tools) > 3 else ""
            logger.debug(
                "  Token '%s': allowed_tools=%s%s",
                entry.name,
                tools_display,
                suffix,
            )
        return cfg


@dataclass
class JWTScopeEntry:
    """A single JWT scope with its tool permissions."""

    scope: str
    allowed_tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.allowed_tools:
            self.allowed_tools = list(DEFAULT_ALLOWED_TOOLS)
            logger.debug(
                "JWT scope '%s' has no allowed_tools configured, "
                "using default read-only tool set (%d tools)",
                self.scope,
                len(self.allowed_tools),
            )


@dataclass
class JWTAuthConfig:
    """Configuration for JWT authentication."""

    jwks_uri: str | None = None
    public_key_file: str | None = None
    issuer: str | None = None
    audience: str | None = None
    algorithm: str = "RS256"
    scopes: list[JWTScopeEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JWTAuthConfig:
        """Parse ``auth.jwt`` section.

        Raises:
            ValueError: If neither ``jwks_uri`` nor ``public_key_file`` is set,
                or if both are set simultaneously.
        """
        jwks_uri = data.get("jwks_uri")
        public_key_file = data.get("public_key_file")

        if not jwks_uri and not public_key_file:
            raise ValueError(
                "auth.jwt requires either 'jwks_uri' or 'public_key_file'. "
                "jwks_uri: URL to fetch public keys from your IdP. "
                "public_key_file: path to a PEM-encoded public key file."
            )
        if jwks_uri and public_key_file:
            raise ValueError(
                "auth.jwt: provide either 'jwks_uri' or 'public_key_file', not both"
            )

        algorithm = data.get("algorithm", "RS256")
        _SUPPORTED_ALGORITHMS = {
            "HS256", "HS384", "HS512",
            "RS256", "RS384", "RS512",
            "ES256", "ES384", "ES512",
            "PS256", "PS384", "PS512",
        }
        if algorithm not in _SUPPORTED_ALGORITHMS:
            raise ValueError(
                f"auth.jwt.algorithm '{algorithm}' is not supported. "
                f"Supported: {sorted(_SUPPORTED_ALGORITHMS)}"
            )

        # Parse scopes
        scope_entries: list[JWTScopeEntry] = []
        raw_scopes = data.get("scopes", [])
        if raw_scopes and not isinstance(raw_scopes, list):
            raise ValueError(
                "auth.jwt.scopes must be a list of {{scope, allowed_tools}} entries"
            )
        for idx, item in enumerate(raw_scopes):
            if not isinstance(item, dict):
                raise ValueError(
                    f"auth.jwt.scopes[{idx}]: expected a mapping, got {type(item).__name__}"
                )
            scope_val = item.get("scope")
            if not scope_val or not isinstance(scope_val, str):
                raise ValueError(
                    f"auth.jwt.scopes[{idx}]: 'scope' field is required and must be a non-empty string"
                )
            allowed = item.get("allowed_tools", [])
            if not isinstance(allowed, list):
                raise ValueError(
                    f"auth.jwt.scopes[{idx}]: 'allowed_tools' must be a list, got {type(allowed).__name__}"
                )
            scope_entries.append(JWTScopeEntry(
                scope=scope_val,
                allowed_tools=allowed,
            ))

        cfg = cls(
            jwks_uri=jwks_uri,
            public_key_file=public_key_file,
            issuer=data.get("issuer"),
            audience=data.get("audience"),
            algorithm=algorithm,
            scopes=scope_entries,
        )

        source = f"jwks_uri={jwks_uri}" if jwks_uri else f"public_key_file={public_key_file}"
        logger.info(
            "JWT auth config loaded: %s, algorithm=%s, %d scope mapping(s)",
            source, algorithm, len(scope_entries),
        )
        if scope_entries:
            for entry in scope_entries:
                logger.debug("  Scope '%s' → allowed_tools=%s", entry.scope, entry.allowed_tools)
        else:
            logger.info(
                "JWT auth: no scope_mapping configured, "
                "JWT claim scopes will be used as tool names directly"
            )
        return cfg

    def build_scope_mapping(self) -> dict[str, list[str]]:
        """Build scope → allowed_tools lookup dict."""
        return {entry.scope: entry.allowed_tools for entry in self.scopes}


@dataclass
class OAuthClientEntry:
    """A pre-registered OAuth client (for agents that don't support DCR)."""

    client_id: str
    client_secret: str = ""
    redirect_uris: list[str] = field(default_factory=list)


@dataclass
class OAuthConfig:
    """Configuration for Doris-backed OAuth2 Authorization Code + PKCE."""

    base_url: str
    access_token_expire_seconds: int = 900
    refresh_token_expire_seconds: int = 86400
    auth_code_expire_seconds: int = 300
    gc_interval_seconds: int = 60
    idle_timeout_seconds: int | None = None
    allowed_redirect_uri_patterns: list[str] = field(default_factory=lambda: [
        "http://127.0.0.1:*/*",
        "http://localhost:*/*",
    ])
    login_page_title: str = "Doris MCP - Login"
    clients: list[OAuthClientEntry] = field(default_factory=list)

    _MAX_ACCESS_TTL = 86400
    _MAX_REFRESH_TTL = 30 * 86400
    _MAX_CODE_TTL = 1800
    _MAX_GC_INTERVAL = 3600
    _MAX_IDLE_TIMEOUT = 7 * 86400

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OAuthConfig:
        """Parse ``auth.oauth`` section.

        Raises:
            ValueError: If any field is missing / out of range / wrong type.
        """
        base_url = data.get("base_url")
        if not base_url or not isinstance(base_url, str):
            raise ValueError(
                "auth.oauth.base_url is required and must be a non-empty string "
                "(e.g. 'https://mcp.example.com')"
            )
        if "://" not in base_url:
            raise ValueError(
                "auth.oauth.base_url must include a scheme (e.g. 'https://mcp.example.com'), "
                f"got: {base_url!r}"
            )

        access_expire = cls._validate_positive_int(
            data, "access_token_expire_seconds", 900, cls._MAX_ACCESS_TTL,
        )
        refresh_expire = cls._validate_positive_int(
            data, "refresh_token_expire_seconds", 86400, cls._MAX_REFRESH_TTL,
        )
        code_expire = cls._validate_positive_int(
            data, "auth_code_expire_seconds", 300, cls._MAX_CODE_TTL,
        )
        gc_interval = cls._validate_positive_int(
            data, "gc_interval_seconds", 60, cls._MAX_GC_INTERVAL,
        )
        raw_idle = data.get("idle_timeout_seconds", None)
        if raw_idle is None:
            idle_timeout = None
        else:
            idle_timeout = cls._validate_positive_int(
                data, "idle_timeout_seconds", None, cls._MAX_IDLE_TIMEOUT,
            )

        if refresh_expire < access_expire:
            logger.warning(
                "auth.oauth.refresh_token_expire_seconds (%ds) < "
                "access_token_expire_seconds (%ds) — this is unusual; "
                "clients will be forced to re-authenticate as soon as "
                "their access token expires",
                refresh_expire, access_expire,
            )
        if idle_timeout is not None and idle_timeout > refresh_expire:
            logger.warning(
                "auth.oauth.idle_timeout_seconds (%ds) > "
                "refresh_token_expire_seconds (%ds) — idle timeout is "
                "effectively disabled (pure TTL path wins)",
                idle_timeout, refresh_expire,
            )

        patterns = data.get("allowed_redirect_uri_patterns", [
            "http://127.0.0.1:*/*",
            "http://localhost:*/*",
        ])
        if not isinstance(patterns, list):
            raise ValueError("auth.oauth.allowed_redirect_uri_patterns must be a list")

        login_title = data.get("login_page_title", "Doris MCP - Login")

        # Pre-registered clients (for agents that don't support Dynamic Client Registration)
        clients: list[OAuthClientEntry] = []
        raw_clients = data.get("clients", [])
        for idx, item in enumerate(raw_clients):
            if not isinstance(item, dict):
                raise ValueError(f"auth.oauth.clients[{idx}]: expected a mapping")
            cid = item.get("client_id")
            if not cid or not isinstance(cid, str):
                raise ValueError(f"auth.oauth.clients[{idx}]: 'client_id' is required")
            clients.append(OAuthClientEntry(
                client_id=cid,
                client_secret=item.get("client_secret", ""),
                redirect_uris=item.get("redirect_uris", []),
            ))

        cfg = cls(
            base_url=base_url.rstrip("/"),
            access_token_expire_seconds=access_expire,
            refresh_token_expire_seconds=refresh_expire,
            auth_code_expire_seconds=code_expire,
            gc_interval_seconds=gc_interval,
            idle_timeout_seconds=idle_timeout,
            allowed_redirect_uri_patterns=patterns,
            login_page_title=login_title,
            clients=clients,
        )
        logger.info(
            "OAuth config loaded: base_url=%s, access_ttl=%ds, refresh_ttl=%ds, "
            "code_ttl=%ds, gc_interval=%ds, idle_timeout=%s, pre-registered clients=%d",
            cfg.base_url, cfg.access_token_expire_seconds,
            cfg.refresh_token_expire_seconds, cfg.auth_code_expire_seconds,
            cfg.gc_interval_seconds,
            f"{cfg.idle_timeout_seconds}s" if cfg.idle_timeout_seconds else "disabled",
            len(clients),
        )
        return cfg

    @staticmethod
    def _validate_positive_int(
        data: dict[str, Any],
        key: str,
        default: int | None,
        upper_bound: int,
    ) -> int:
        if key not in data:
            if default is None:
                raise ValueError(
                    f"auth.oauth.{key} must be a positive integer (required, no default)"
                )
            return default
        val = data[key]
        if val is None or isinstance(val, bool) or type(val) is not int:
            raise ValueError(
                f"auth.oauth.{key} must be a positive integer (strict int, "
                f"no float/bool/null), got: {val!r} (type={type(val).__name__})"
            )
        if val <= 0:
            raise ValueError(
                f"auth.oauth.{key} must be a positive integer (> 0), got: {val}"
            )
        if val > upper_bound:
            raise ValueError(
                f"auth.oauth.{key}={val} exceeds the maximum allowed ({upper_bound}s). "
                f"Values larger than this silently defeat the TTL's purpose "
                f"(token theft window, resource cleanup, GC effectiveness)."
            )
        return val


@dataclass
class AuthConfig:
    """Top-level auth configuration.

    Determines which authentication strategies are active based on which
    sub-sections (``static``, ``jwt``, ``oauth``) are present.
    """

    static: StaticAuthConfig | None = None
    jwt: JWTAuthConfig | None = None
    oauth: OAuthConfig | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthConfig:
        """Parse the ``auth`` section of mcp-server.yaml.

        Raises:
            ValueError: If ``auth`` section exists but contains none of
                ``static``, ``jwt``, or ``oauth`` sub-sections.
        """
        static_cfg = None
        jwt_cfg = None
        oauth_cfg = None

        if "static" in data:
            logger.info("Parsing auth.static configuration...")
            static_cfg = StaticAuthConfig.from_dict(data["static"])

        if "jwt" in data:
            logger.info("Parsing auth.jwt configuration...")
            jwt_cfg = JWTAuthConfig.from_dict(data["jwt"])

        if "oauth" in data:
            logger.info("Parsing auth.oauth configuration...")
            oauth_cfg = OAuthConfig.from_dict(data["oauth"])

        if static_cfg is None and jwt_cfg is None and oauth_cfg is None:
            raise ValueError(
                "auth section is present but contains no valid configuration. "
                "To disable authentication, remove the 'auth' section entirely. "
                "To enable authentication, configure at least one of: "
                "auth.static (token-based), auth.jwt (JWT-based), "
                "or auth.oauth (Doris-backed OAuth2)."
            )

        modes = []
        if static_cfg:
            modes.append("static")
        if jwt_cfg:
            modes.append("jwt")
        if oauth_cfg:
            modes.append("oauth")
        if len(modes) > 1:
            raise ValueError(
                f"auth.static / auth.jwt / auth.oauth are mutually exclusive, "
                f"please configure only one. Got: {' + '.join(modes)}"
            )
        logger.info("Auth strategy active: %s", modes[0])

        return cls(static=static_cfg, jwt=jwt_cfg, oauth=oauth_cfg)
