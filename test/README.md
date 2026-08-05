# Test Cases — doris-new-mcp

Generated from [DESIGN.md](../DESIGN.md), [INSTALL.html](../INSTALL.html), and [doris-mcp-docs.html](../doris-mcp-docs.html).

## File Overview

### Offline Unit Tests (No MCP Server / Doris Required)

| File | Coverage |
|------|------|
| `test_sql_validator.py` | `core.sql_validator.validate_readonly` read-only SQL validation (allow/block/multiple statements/comment bypass/known prefixes) |
| `test_sensitive_mask.py` | `core.sensitive_mask` password/token masking |
| `test_pagination.py` | `core.pagination` pagination and token TTL behavior |
| `test_private_ip_config.py` | Request node IP detection and startup wiring |
| `test_deps.py` | Runtime dependency guard (imports real modules) |
| `test_cross_file_deps.py` | Cross-file dependency detection before delete |
| `test_credential_pass.py` | Request-scoped credential pass-through to the store layer |
| `test_watcher.py` | `MultiWorkspaceWatcher.ensure_fresh` cooldown/reload/degradation |
| `test_web_session_cookie.py` | Web session cookie |
| `test_session_affinity_proxy_routing.py` | Session affinity proxy routing |
| `test_session_affinity_proxy_streaming.py` | Session affinity proxy streaming |
| `test_session_affinity_proxy_relogin.py` | Session affinity proxy relogin |
| `test_session_affinity_proxy_force_target.py` | Session affinity proxy request-address routing |

### Online Tests (Require Running MCP Server + Doris)

| File | Coverage |
|------|------|
| `test_mcp_tools.py` | All 10 MCP tools + auth + E2E (30 cases) |
| `test_web_api.py` | Web UI + REST API + workspace management (12 cases) |

### Entry Script

| File | Coverage |
|------|------|
| `run_all_tests.sh` | One-command runner supporting `--offline` / `--tools` / `--web` / `--smoke` |

## Requirements

| Requirement | Description |
|------|------|
| MCP Server | Running at `localhost:3000` (online tests only) |
| Doris FE | Running at `127.0.0.1:9030` (online tests only) |
| Auth | `admin:admin` |
| Python | 3.10+; offline tests use the project `.venv` (`PYTHONPATH=src`) |

Override defaults with environment variables:
```bash
export MCP_URL=http://192.168.1.100:3000/mcp
export MCP_BASE_URL=http://192.168.1.100:3000
export MCP_TOKEN=admin:admin
export MCP_WORKSPACE=example
export DORIS_USER=admin          # test_web_api.py login credentials
export DORIS_PASS=admin
export DORIS_MCP_TEST_DESTRUCTIVE=1  # Enable destructive cases (see below)
```

## How to Run

```bash
# Offline unit tests only (no services required)
bash test/run_all_tests.sh --offline

# Or use unittest discover for all offline cases; online files collect no unittest cases
PYTHONPATH=src .venv/bin/python -m unittest discover -s test -p 'test_*.py'

# Individual offline files can also run directly (sys.path bootstrap is built in)
.venv/bin/python test/test_watcher.py

# Full suite (requires MCP Server online)
bash test/run_all_tests.sh

# Smoke test only (fast, about 5 seconds)
bash test/run_all_tests.sh --smoke

# MCP Tool tests only
bash test/run_all_tests.sh --tools

# Web/API tests only
bash test/run_all_tests.sh --web

# Or run Python directly; unreachable server skips everything with exit code 0
python test/test_mcp_tools.py
python test/test_web_api.py
```

## Destructive Cases

The following cases in `test_web_api.py` affect shared server state and are
**skipped by default**. Set `DORIS_MCP_TEST_DESTRUCTIVE=1` explicitly to run them:

- `test_api_staging_discard` — discards real user staging changes
- `test_api_workspace_create_and_delete` — creates/deletes a real workspace

## Coverage Matrix

### MCP Tool Tests (10 Tools)

| Tool | Scenario | Checks |
|------|----------|--------|
| `get_query_guide` | Get workflow guide | Returned text >100 chars and contains keywords |
| `check_service_health` | Basic/detail | Doris=connected, workspaces exist |
| `list_metrics` | List/pagination | data array, meta.total_count |
| `list_dimensions_for_metric` | Dimensions by metric | data contains dimensions |
| `query_metric` | Basic/group_by/where/order+limit | 4 query modes |
| `list_databases` | List/pagination | dw,mysql,system_mcp,information_schema |
| `list_tables` | mysql DB / dw seed tables / LIKE matching | 4 seed tables verified |
| `describe_table` | summary/full/names | 3 detail levels |
| `execute_query` | SELECT/VERSION/SHOW/EXPLAIN/max_rows/write blocking | 7 scenarios |
| `reload_semantic_layer` | Manual reload | Returns structured JSON with success field |

Note: semantic-layer cases (`list_dimensions_for_metric` and the `query_metric`
series) skip only when the MCP Server is unreachable due to connection errors.
Semantic-layer readiness assertion failures are counted as FAIL.

### Web UI & API Tests

| Category | Checks |
|------|--------|
| **Web UI** | Login page, login submit, unauthenticated guard, model management page |
| **REST API** | Semantic file list, pull download, reload, staging validate/discard |
| **Workspace** | Create → verify existence → delete (full lifecycle, destructive, skipped by default) |
| **Auth** | Admin permission control, Bearer token format validation |

### Boundary & Error Tests

| Scenario | Expected |
|------|------|
| No Authorization | 401/403 |
| Invalid token | 401/403 |
| Write SQL (INSERT) | Blocked |
| SQL syntax error | Friendly error message |
| Non-admin workspace creation | 403 |
| Missing metric | Friendly error |

### End-to-End Test (Agent Workflow)

```
get_query_guide → check_service_health → list_databases
  → list_tables → describe_table → execute_query
```
