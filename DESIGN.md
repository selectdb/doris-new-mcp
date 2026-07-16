# DESIGN.md — doris-mcp-server

## Overview

**doris-mcp-server** is an MCP (Model Context Protocol) server that wraps Apache Doris with a semantic metric layer powered by **MetricFlow v0.209.0**. It exposes Doris as an AI-queryable data source via FastMCP's streamable HTTP transport, supports multi-tenancy via workspaces, and includes a built-in Web UI and CLI for semantic model management.

```
                         MCP Protocol (streamable-http, stateless)
┌──────────────────────────────────────────────────────────────────┐
│                        AI Client (LLM)                           │
│    Claude Desktop / Cursor / VeloDB / Codex / custom client      │
└─────────────────────────────┬────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                     FastMCP 3.3.1 Server                         │
│                                                                  │
│  ┌───────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  10 MCP Tools │  │  Web UI      │  │  REST API              │ │
│  │  (LLM-facing) │  │  /mcp/web/*  │  │  /mcp/web/semantic/*   │ │
│  └───────┬───────┘  └──────┬───────┘  └───────────┬────────────┘ │
│          │                 │                       │              │
│  ┌───────┴─────────────────┴───────────────────────┴────────────┐ │
│  │                      Auth Layer                              │ │
│  │  MCP:  Bearer username:password → CredentialVerifier → Doris │ │
│  │  Web:  Session cookie (24h TTL, httponly)                    │ │
│  │  Cache: 10-min in-memory credential cache                    │ │
│  │  Pool: Per-user aiomysql connection pools                    │ │
│  └───────────────────────────┬──────────────────────────────────┘ │
│                              │                                    │
│  ┌───────────────────────────┴──────────────────────────────────┐ │
│  │                 Multi-Workspace Watcher                       │ │
│  │  Per-workspace: Store → Manifest → Compiler (MetricFlow)     │ │
│  │  60s polling for change detection                             │ │
│  │  Auto-discover new/deleted workspaces                         │ │
│  │  MetricRouter: metric_name → (compiler, workspace)           │ │
│  └───────────────────────────┬──────────────────────────────────┘ │
└──────────────────────────────┼────────────────────────────────────┘
                               │ pymysql / aiomysql
                               ▼
                    ┌─────────────────────┐
                    │   Apache Doris FE   │
                    │   127.0.0.1:9030    │
                    │                     │
                    │  system_mcp.*       │  ← workspace storage
                    │  dw.*               │  ← user data tables
                    └─────────────────────┘
```

---

## 1. Entry Point & Lifecycle

### 1.1 Startup (`src/main.py`)

```
main()
  ├─ parse args (--config-dir, --env-file)
  ├─ AppConfig.load(mcp-server.toml)   ← TOML with ${VAR} env interpolation
  └─ create_server()
       ├─ seed_example_data()          ← if seed_example=true (default)
       ├─ MultiWorkspaceWatcher.start() ← background poll thread (60s)
       ├─ ConnectionPool (admin)       ← aiomysql, min=0, max=10
       ├─ CredentialVerifier           ← Bearer token → Doris verification
       ├─ register 10 MCP tools
       ├─ register Web UI routes (/mcp/web/*)
       └─ register REST API routes (/mcp/web/semantic/*, /mcp/web/staging/*)
           ↓
mcp.run(transport="streamable-http", stateless_http=True, port=3000)
```

### 1.2 Shutdown

Lifespan context manager closes all connection pools (`pool_manager.close_all()`, `admin_pool.close()`).

---

## 2. Configuration (`src/config/loader.py`)

### 2.1 `mcp-server.toml`

```toml
[server]
mcp_name = "doris-new-mcp"      # MCP server name
mcp_host = "0.0.0.0"            # Bind address
mcp_port = 3000                 # HTTP port
fe_port = 9030                  # Doris FE MySQL port (localhost)
seed_example = true             # Auto-create example workspace

[logging]
level = "info"                  # debug|info|warning|error
audit_log = "./logs/audit.log"  # Audit log path
rotation_when = "midnight"      # Log rotation policy
rotation_backup_count = 30      # Retention count

[query]
pool_min_size = 0
pool_max_size = 10
pool_idle_timeout_seconds = 300
query_timeout_seconds = 600
query_max_rows = 10000           # Default row limit
```

### 2.2 Config Classes

| Class | Responsibility |
|-------|---------------|
| `AppConfig` | TOML/YAML loading, `${VAR}` env interpolation via regex |
| `McpConfig` | Server identity, host/port, logging, seed flag |
| `ClusterConfig` | Doris FE connection, pool sizing, query limits, DB whitelist |

---

## 3. MCP Tools (10 total)

### 3.1 Tool Catalog

| # | Tool | Annotations | Role |
|---|------|------------|------|
| 1 | `get_query_guide` | readOnly, idempotent | **First call.** Returns complete workflow guide for AI clients. |
| 2 | `check_service_health` | readOnly, idempotent | **Second call.** Doris connectivity + per-workspace status + metric counts. |
| 3 | `list_metrics` | readOnly, idempotent | List all metrics in a workspace (name + description). |
| 4 | `list_dimensions_for_metric` | readOnly, idempotent | Valid `group_by` dimensions for a given metric. |
| 5 | `query_metric` | readOnly | **Primary query tool.** Compile via MetricFlow → execute SQL. Supports `metrics`, `group_by`, `where`, `order_by`, `limit`, `having`, `database`, `max_rows`. |
| 6 | `list_databases` | readOnly, idempotent | List Doris databases (paginated). |
| 7 | `list_tables` | readOnly, idempotent | List tables in a database (with `like` filter, paginated). |
| 8 | `describe_table` | readOnly, idempotent | Table schema (`names`/`summary`/`full` detail levels). |
| 9 | `execute_query` | readOnly | Raw SQL fallback (read-only: SELECT/SHOW/DESCRIBE/EXPLAIN). |
| 10 | `reload_semantic_layer` | idempotent | Trigger async workspace reload. |

### 3.2 Agent-Side Workflow

The system enforces a strict calling order for AI clients:

```
get_query_guide()                    ← Step 1: learn the workflow
    ↓
check_service_health()               ← Step 2: check Doris + workspace status
    ↓
    ├─ semantic layer healthy? ──→ list_metrics() → list_dimensions_for_metric() → query_metric()
    │                              (the "happy path" — counts, sums, rates, trends)
    │
    └─ semantic layer unavailable or no matching metric?
        └─→ list_databases() → list_tables() → describe_table() → execute_query()
            (the "fallback path" — raw SQL with metadata discovery)
```

**Key rule:** If the semantic layer is healthy and a metric matches the user's intent, `execute_query` must NOT be used. Always prefer `query_metric`.

### 3.3 Tool Implementation Pattern

Every tool follows the same structure:

```python
@mcp.tool(annotations=ToolAnnotations(...))
async def tool_name(param: type, ...) -> str:
    auth = check_tool_access("tool_name")     # 1. Auth gate
    if auth.denied: return auth.denied
    start = time.monotonic()                  # 2. Start timer
    pool = await _get_per_user_pool(auth.pool) # 3. Resolve connection pool
    result = await _implementation(pool, ...)  # 4. Execute
    log_tool_call("tool_name", ..., duration_ms=...) # 5. Audit log
    return result
```

All results are JSON-serialized via `success_response()` / `error_response()`.

---

## 4. Authentication & Authorization

### 4.1 MCP Protocol Auth

```
Authorization: Bearer username:password
```

| Step | Component | Action |
|------|-----------|--------|
| 1 | `CredentialVerifier.verify_token()` | Split `username:password` on first `:` |
| 2 | `CredentialCache` | Check 10-min TTL cache |
| 3 | `pymysql.connect(host=<machine_ip>, user, password)` | Verify against Doris |
| 4 | Valid → cache → return `AccessToken` | |
| 5 | Invalid → return 401 | |

The machine IP used for verification is the **non-127.0.0.1** IP (detected via UDP socket to 8.8.8.8), ensuring Doris uses real user identity.

### 4.2 Web UI Auth

```
GET  /mcp/web/login  → render login form
POST /mcp/web/login  → verify Doris credentials → set "doris_mcp_session" cookie
                       (24h TTL, httponly, samesite=lax)
GET  /mcp/web/logout → clear session + cookie
```

### 4.3 Authorization Model

| Role | How determined | Permissions |
|------|---------------|-------------|
| **admin** | `user == "admin"` | Full: push/pull/validate/commit/discard models, create/delete workspaces, execute any SQL |
| **authenticated user** | Valid Bearer token with `_check_semantic_access()` | Read: view models, list/query metrics, execute SQL (read-only validation) |
| **unauthenticated** | No token | Rejected (401 or redirect to login) |

### 4.4 Per-User Connection Pools

Each authenticated user gets a separate `aiomysql` pool connected via the machine's non-loopback IP. This ensures proper Doris user-level authorization. On auth failure, the credential cache is cleared so next request re-verifies.

---

## 5. Workspace System

### 5.1 Concept

A **workspace** is a logical tenant providing complete isolation:

- Independent YAML model files
- Independent MetricFlow compiler instance
- Independent metric namespace
- Independent Doris storage tables

Workspace A's metrics are **completely invisible** to workspace B.

**Naming:** `^[a-zA-Z][a-zA-Z0-9_]*$`

### 5.2 Workspace States

| State | Meaning | Cause |
|-------|---------|-------|
| `healthy` | Normal operation, metrics queryable | YAML committed successfully, bootstrap passed, MetricFlow engine ready |
| `no_models` | Empty workspace | New workspace, or all files deleted |
| `not_ready` | Load failed | YAML syntax errors, missing tables, missing `project.yaml`, MetricFlow validation failure |

```
  no_models  ──upload YAML──→  not_ready  ──fix + commit──→  healthy
      ↑                            ↑                              │
      └──────────────────────── upload bad YAML ──────────────────┘
```

### 5.3 Storage Architecture (`src/store/store.py`)

Each workspace has **two** Doris tables in `system_mcp` database:

```
system_mcp.active_store_{workspace}     ← production models (read-only)
  filename   VARCHAR(512) PRIMARY KEY
  updated_at DATETIME
  content    STRING

system_mcp.staging_store_{workspace}    ← pending changes
  filename   VARCHAR(512) PRIMARY KEY
  action     VARCHAR(16)   -- 'upsert' | 'delete'
  updated_at DATETIME
  content    STRING (NULL for delete)
```

### 5.4 Staging Workflow

```
  User edits YAML (WebUI/CLI)
          │
          ▼
  ┌─────────────────┐
  │  Staging Store  │   ← files enter here, no impact on running queries
  └────────┬────────┘
           │
   ┌───────┼───────┐
   ▼       ▼       ▼
Validate  Commit  Discard
   │       │       │
   │  ┌────┴────┐  │
   │  │ Active  │  │
   │  │ Store   │  │
   │  └────┬────┘  │
   │       │       │
   │  Auto-reload  │
   │  (2-5 sec)    │
   │       │       │
   ▼       ▼       ▼
  ┌─────────────┐
  │   healthy   │
  └─────────────┘
```

**Enforcement:** Commit is only allowed after a successful Validate. "Staging must be validated before commit."

### 5.5 Validation Pipeline

```
validate_staging(workspace)
  1. staging_fetch()               → merge active + staging into temp dir
  2. pre_validate_physical()       → YAML syntax, file structure, table existence
  3. bootstrap()                   → MetricFlow build in temp workspace
  4. SemanticManifest.load()       → parse generated semantic_manifest.json
  5. _check_staging_duplicates()   → detect duplicate measures/models across files
  6. Return (pass/fail, message, details with metric list)
```

### 5.6 Multi-Workspace Watcher (`src/store/watcher.py`)

```
MultiWorkspaceWatcher
├─ _init_all()                ← scan system_mcp for active_store_* tables
├─ _poll_loop()               ← background thread, 60s interval
│   ├─ check_remote()         ← detect version changes via revision hash
│   ├─ _reload_workspace()    ← fetch → bootstrap → manifest → compiler
│   └─ discover new/stale     ← SCAN system_mcp for table changes
├─ MetricRouter               ← metric_name → (compiler, workspace_name)
├─ force_reload()             ← manual trigger (API/tool)
└─ commit_staging()           ← staging_commit() → force_reload()
```

**Atomic swap:** `RWLock.write_acquire()` guards manifest/compiler replacement. No request sees partial state.

---

## 6. Semantic Layer

### 6.1 MetricFlow Integration (`src/store/compiler.py`)

```
YAML models (Doris active_store)
      │
      ▼
  bootstrap()          ← MetricFlow build (dbt parse + manifest generation)
      │
      ▼
  semantic_manifest.json
      │
      ├── SemanticManifest.load()   ← metadata: metrics, dimensions, entities
      │
      └── MetricFlowCompiler
            │
            ├── MetricFlowEngine (compile-only mode)
            │     └── _DorisSqlClientStub  ← satisfies SqlClient interface
            │           for dialect rendering, no actual queries
            │
            └── query_metric() flow:
                  explain(sql) → Doris SQL → ConnectionPool.execute(sql) → rows
```

### 6.2 Semantic Model Structure

A `semantic_model` YAML document consists of:

| Section | Required | Purpose |
|---------|----------|---------|
| `name` | ✅ | Globally unique model name |
| `db_table` | ✅ | Doris physical table (`db.table`) |
| `defaults.agg_time_dimension` | ✅ | Default time dimension for metrics |
| `entities` | ✅ | Primary/foreign/unique/natural keys |
| `dimensions` | ✅ | Time dimensions (day/week/month/quarter/year/hour/minute) and categorical dimensions |
| `measures` | Recommended | Aggregation definitions (sum/count/count_distinct/average/min/max/median/percentile/sum_boolean) |
| `description` | Optional | Human-readable description |
| `primary_entity` | Conditional | Required if no `type: primary` entity exists |

### 6.3 Advanced Metric Types

Beyond auto-generated simple metrics from `measures`, YAML supports four advanced types:

| Type | Purpose | Example |
|------|---------|---------|
| `ratio` | numerator ÷ denominator | Conversion rate = orders / visits |
| `derived` | Expression over input metrics | Month-over-month growth, YoY change |
| `cumulative` | Running total over time window | Last 7 days sales, MTD registrations |
| `conversion` | User funnel conversion | Visit → order conversion rate |

### 6.4 Manifest (`src/store/manifest.py`)

```python
SemanticManifest(semantic_manifest.json)
  .list_metrics()                    # → [{name, description}, ...]
  .get_metric(name)                  # → full metric definition
  .list_dimensions_for_metric(name)  # → [{name, type, description}, ...]
  .search(keywords)                  # → [{type, name, description}, ...]
  .get_semantic_table_names()        # → set of table names (for conflict detection)
```

---

## 7. Connection Management

### 7.1 Connection Pool (`src/core/connection.py`)

```python
ConnectionPool
  ├─ aiomysql.Pool (lazy init, asyncio.Lock guarded)
  ├─ execute(sql, database, max_rows, timeout) → ([{col: val}, ...], [col_names])
  ├─ Per-user pools via PoolManager (non-127.0.0.1 IP)
  └─ close() → pool.close() + wait_closed()
```

### 7.2 Pool Types

| Pool | User | Min/Max | Purpose |
|------|------|---------|---------|
| Admin pool | `admin` | 0/10 | Semantic file storage, workspace management, health checks |
| Per-user pools | `<authenticated_user>` | 0/10 | SQL query execution with proper Doris authorization |

---

## 8. Web UI

### 8.1 Routes

| Route | Method | Auth | Purpose |
|-------|--------|------|---------|
| `/mcp/web/login` | GET | None | Login form |
| `/mcp/web/login` | POST | None | Process login, set session cookie |
| `/mcp/web/logout` | GET | Session | Clear session |
| `/mcp/web` | GET | Session | Redirect to models page |
| `/mcp/web/models` | GET | Session | Active/staging file list + workspace status |
| `/mcp/web/{filename}` | GET | Session | Edit YAML file |
| `/mcp/web/new` | GET | Admin | New file form |
| `/mcp/web/create` | POST | Admin | Create new file |
| `/mcp/web/{filename}/save` | POST | Admin | Save edited file |
| `/mcp/web/{filename}/delete` | GET | Admin | Mark file for deletion |
| `/mcp/web/upload` | POST | Admin | Upload YAML files (multipart) |

### 8.2 REST API

| Route | Method | Auth | Purpose |
|-------|--------|------|---------|
| `/mcp/web/semantic/push` | POST | Admin (Bearer) | CLI: upload YAML (multipart) |
| `/mcp/web/semantic/pull` | GET | Bearer | CLI: download active YAML as `.tar.gz` |
| `/mcp/web/semantic/reload` | POST | Admin | HTTP: trigger workspace reload |
| `/mcp/web/semantic/files` | GET | Bearer | List active files |
| `/mcp/web/semantic/files/{filename}` | GET | Bearer | Get file content |
| `/mcp/web/semantic/files` | POST | Admin | Save file to staging |
| `/mcp/web/semantic/files/{filename}` | DELETE | Admin | Delete file from staging |
| `/mcp/web/staging/validate` | POST | Admin | Validate staging changes |
| `/mcp/web/staging/commit` | POST | Admin | Commit staging → active |
| `/mcp/web/staging/discard` | POST | Admin | Discard staging changes |
| `/mcp/web/workspace/create` | POST | Admin | Create new workspace |
| `/mcp/web/workspace/delete` | POST | Admin | Delete workspace (drops tables) |

---

## 9. CLI Client (`mcp-client/`)

A standalone command-line client shipped as a separate tarball. Configuration via environment variables or `doris-mcp-client.toml`:

```bash
export DORIS_MCP_SERVER=http://<host>:<port>
export DORIS_MCP_TOKEN=admin:admin
```

**MCP Tool calls:**
```bash
doris-mcp-client tool list
doris-mcp-client tool call list_metrics --json '{"workspace":"example"}'
doris-mcp-client tool call query_metric --json '{"metrics":["total_amount"],"group_by":["channel"]}'
```

**Semantic management:**
```bash
doris-mcp-client semantic push ./models -w example
doris-mcp-client semantic pull -o ./backup -w example
doris-mcp-client semantic list -w example
doris-mcp-client semantic reload -w example
doris-mcp-client semantic status
```

---

## 10. Example Workspace

On first boot (if `seed_example=true`), the system auto-creates:

| Table | Rows | Description |
|-------|------|-------------|
| `dw.orders` | 12 | Orders with order_id, user_id, product_id, amount, channel, status, order_date |
| `dw.users` | 5 | Users with user_id, name, city, level, register_date |
| `dw.products` | 5 | Products with product_id, name, category, brand, price |
| `dw.dim_date` | 365 | Date dimension for time spine alignment |

**5 example metrics:** `total_amount`, `order_count`, `avg_amount`, `unique_users` (from orders), `user_count` (from users)

---

## 11. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `stateless_http=True` | No MCP session tracking. Required for clients (VeloDB proxy, Claude Desktop) that don't maintain session state across requests. |
| Doris-backed stores | YAML stored in Doris tables (`system_mcp`), not filesystem. Enables multi-server deployment with shared state, no file sync needed. |
| Two-tier staging (active + staging) | Prevents broken models from reaching production. Validate-before-commit gate with comprehensive checks. |
| Compile-only `_DorisSqlClientStub` | MetricFlow needs `SqlClient` for dialect rendering; actual execution via `aiomysql` pool with user credentials. |
| Per-user connection pools | Each authenticated user gets their own aiomysql pool with their credentials. Preserves Doris-native authorization. |
| Embedded HTML templates | Web UI is self-contained (no external CDN), works behind proxy/VPN. Single-file deployment. |
| Python 3.10 standalone build | Self-contained distribution via `python-build-standalone`. No system Python needed at runtime. |
| Audit logging with timed rotation | Every tool call logged with client_id, params, duration, success/failure. Rotated at midnight, 30-day retention. |

---

## 12. Build & Distribution

### 12.1 Build (`build.sh`)

```bash
./build.sh linux-x64       # Linux x86_64
./build.sh linux-arm64     # Linux ARM64
./build.sh macos-x64       # macOS Intel
./build.sh macos-arm64     # macOS Apple Silicon
./build.sh                 # Auto-detect platform
./build.sh clean           # Remove python/, dist/, build artifacts
```

Downloads Python 3.10 standalone from `astral-sh/python-build-standalone`, installs dependencies from `requirements.txt`, produces two self-contained tarballs in `dist/`:

```
dist/
├── doris-mcp-server-0.3.0-{platform}.tar.gz    ← python/ + src/ + config
└── doris-mcp-client-0.3.0-{platform}.tar.gz    ← python/ + mcp-client/
```

### 12.2 Deployment

```bash
# 1. Extract
tar xzf doris-mcp-server-0.3.0-linux-x64.tar.gz
cd doris-mcp-server

# 2. Configure (optional — defaults work for localhost:9030)
vim mcp-server.toml

# 3. Start
./start-mcp-server.sh                     # foreground
nohup ./start-mcp-server.sh > /tmp/doris-mcp.log 2>&1 &   # background
```

No network, no pip, no system Python required at runtime. Use `DORIS_MCP_PYTHON` env var to override the bundled Python:

```bash
DORIS_MCP_PYTHON=/usr/bin/python3.10 ./start-mcp-server.sh
```

### 12.3 Verification

```bash
# WebUI
curl http://<IP>:3000/mcp/web
# Login with admin:<empty_password>

# MCP Agent
claude mcp add --transport http doris http://<IP>:3000/mcp \
  --header "Authorization: Bearer admin:admin"
```

---

## 13. Directory Structure

```
doris-mcp-server/
├── build.sh                     # Build script (setup → pack)
├── requirements.txt             # Python 3.10 dependencies
├── mcp-server.toml              # Server configuration
├── start-mcp-server.sh          # Server launcher
├── mcp-client.sh                # Client launcher
├── INSTALL.html                 # Installation guide
├── doris-mcp-docs.html          # Full documentation (semantic models + user guide)
├── DESIGN.md                    # This file
├── src/
│   ├── main.py                  # Entry point + FastMCP.run()
│   ├── server.py                # Server factory, 10 tools, Web UI routes, REST API
│   ├── auth/                    # Credential verifier, cache, token-based auth
│   │   ├── credential_cache.py  # 10-min TTL in-memory cache
│   │   ├── credential_verifier.py # Bearer token → Doris verification
│   │   ├── guard.py             # Tool-level access gate
│   │   ├── provider.py          # StaticTokenVerifier, JWTVerifier
│   │   └── config.py            # Auth config parsing
│   ├── config/
│   │   └── loader.py            # TOML/YAML config with ${VAR} interpolation
│   ├── core/
│   │   ├── connection.py        # aiomysql async connection pool
│   │   ├── pool_manager.py      # Per-user pool factory
│   │   ├── audit.py             # Timed rotating audit log
│   │   ├── health.py            # Service health component tracking
│   │   ├── response.py          # JSON success/error response helpers
│   │   ├── sql_validator.py     # SQL read-only enforcement (sqlglot-based)
│   │   ├── charset.py           # Charset middleware
│   │   ├── request_logger.py    # Request logging middleware
│   │   ├── pagination.py        # Cursor-based pagination
│   │   ├── semantic_guard.py    # Semantic conflict detection
│   │   └── sensitive_mask.py    # Sensitive data masking
│   ├── store/
│   │   ├── store.py             # DorisStore: active/staging tables per workspace
│   │   ├── watcher.py           # MultiWorkspaceWatcher: poll, reload, validate, commit
│   │   ├── compiler.py          # MetricFlowCompiler with _DorisSqlClientStub
│   │   ├── manifest.py          # SemanticManifest: parse semantic_manifest.json
│   │   ├── bootstrap.py         # MetricFlow build (dbt parse + manifest generation)
│   │   ├── seed.py              # Example data seeding
│   │   └── version.py           # Version tracking for each workspace
│   ├── tools/
│   │   ├── discovery.py         # list_databases, list_tables, describe_table
│   │   ├── query.py             # execute_query (SQL execution)
│   │   └── semantic.py          # list_metrics, list_dimensions_for_metric, query_metric
│   ├── skills/
│   │   └── doris-mcp-skill.md   # Query guide markdown (served by get_query_guide)
│   └── metricflow/              # Vendored MetricFlow engine (compile-only)
└── mcp-client/                  # CLI client (separate package)
    └── client/
        ├── cli.py               # CLI entry (cyclopts)
        ├── config.py            # Env/file config loading
        ├── http_client.py       # HTTP API client
        ├── mcp_client.py        # MCP streamable-http transport
        └── formatting.py        # Output formatting
```
