#!/bin/bash
# =============================================================================
# test/run_all_tests.sh — run all tests
#
# Usage:
#   bash test/run_all_tests.sh              # run all tests (requires MCP Server)
#   bash test/run_all_tests.sh --offline    # offline unit tests only (no MCP Server)
#   bash test/run_all_tests.sh --tools      # offline tests + Tool online tests only
#   bash test/run_all_tests.sh --web        # offline tests + Web/API online tests only
#   bash test/run_all_tests.sh --smoke      # offline tests + smoke test only (fast)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MCP_URL="${MCP_URL:-http://localhost:3000/mcp}"
MCP_BASE_URL="${MCP_BASE_URL:-http://localhost:3000}"

# Prefer the project venv interpreter; offline tests depend on its packages.
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ── Check whether MCP Server is running ────────────────────
check_server() {
    if curl -s -o /dev/null -w "%{http_code}" "$MCP_BASE_URL/mcp/web/login" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ MCP Server is running${NC}"
    else
        echo -e "${RED}❌ MCP Server is not running at $MCP_BASE_URL${NC}"
        echo "   Start it first: cd $PROJECT_DIR && ./start-mcp-server.sh"
        exit 1
    fi
}

# ── Run Python tests ────────────────────────────
run_python_test() {
    local test_file="$1"
    local label="$2"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  $label${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    if "$PYTHON_BIN" "$test_file"; then
        echo -e "${GREEN}✅ $label passed${NC}"
        return 0
    else
        echo -e "${RED}❌ $label failed${NC}"
        return 1
    fi
}

# ── Smoke test (fast) ────────────────────────────
smoke_test() {
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  Smoke test${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    # 1. Health check
    echo -n "  1. Health check ... "
    RESULT=$(curl -s -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "Authorization: Bearer admin:admin" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"check_service_health","arguments":{}}}')
    if echo "$RESULT" | grep -q "connected"; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}: $RESULT"
        return 1
    fi

    # 2. List databases
    echo -n "  2. List databases ... "
    RESULT=$(curl -s -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "Authorization: Bearer admin:admin" \
        -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_databases","arguments":{}}}')
    # SSE response has escaped JSON: \"success\": true
    if echo "$RESULT" | grep -qE 'success.*true'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
        return 1
    fi

    # 3. Execute query
    echo -n "  3. Execute query ... "
    RESULT=$(curl -s -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "Authorization: Bearer admin:admin" \
        -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"execute_query","arguments":{"sql":"SELECT 1 AS n"}}}')
    if echo "$RESULT" | grep -qE 'n.*:.*1[^0-9]'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}: $RESULT"
        return 1
    fi

    # 4. Web UI
    echo -n "  4. Web UI login page ... "
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$MCP_BASE_URL/mcp/web/login")
    if [ "$HTTP_CODE" = "200" ]; then
        echo -e "${GREEN}OK ($HTTP_CODE)${NC}"
    else
        echo -e "${RED}FAIL (HTTP $HTTP_CODE)${NC}"
        return 1
    fi

    echo -e "${GREEN}✅ Smoke test passed${NC}"
    return 0
}

# ── Offline unit tests (no MCP Server required) ─────────
run_offline_unit_tests() {
    run_python_test "$SCRIPT_DIR/test_sql_validator.py" "SQL read-only validation offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_sensitive_mask.py" "Sensitive data masking offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_pagination.py" "Pagination offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_private_ip_config.py" "Request node IP offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_deps.py" "Runtime dependency guard offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_cross_file_deps.py" "Cross-file dependency detection offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_semantic_grant.py" "Semantic table grant offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_credential_pass.py" "Credential pass-through offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_watcher.py" "Workspace watcher offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_semantic_loading.py" "On-demand semantic loading offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_manifest_dims.py" "Metric dimension extraction offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_compiler_where.py" "WHERE literal handling offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_web_session_cookie.py" "Web session cookie offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_session_affinity_proxy_routing.py" "Session affinity proxy routing offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_session_affinity_proxy_streaming.py" "Session affinity proxy streaming offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_session_affinity_proxy_relogin.py" "Session affinity proxy relogin offline unit test" || ((FAIL_COUNT += 1))
    run_python_test "$SCRIPT_DIR/test_session_affinity_proxy_force_target.py" "Session affinity proxy request target offline unit test" || ((FAIL_COUNT += 1))
}

# ── Main ─────────────────────────────────────────
FAIL_COUNT=0
MODE="${1:-}"

# Keep these deterministic tests ahead of every live-server test stage.
run_offline_unit_tests

if [ "$MODE" = "--offline" ]; then
    : # Offline mode: do not check the server or run online stages.
else
    check_server
fi

case "$MODE" in
    --offline)
        ;;
    --tools)
        run_python_test "$SCRIPT_DIR/test_mcp_tools.py" "MCP Tools test" || ((FAIL_COUNT += 1))
        ;;
    --web)
        run_python_test "$SCRIPT_DIR/test_web_api.py" "Web UI & API test" || ((FAIL_COUNT += 1))
        ;;
    --smoke)
        smoke_test || ((FAIL_COUNT += 1))
        ;;
    "")
        # Run everything.
        smoke_test || ((FAIL_COUNT += 1))
        run_python_test "$SCRIPT_DIR/test_mcp_tools.py" "MCP Tools test" || ((FAIL_COUNT += 1))
        run_python_test "$SCRIPT_DIR/test_web_api.py" "Web UI & API test" || ((FAIL_COUNT += 1))
        ;;
    *)
        echo "Usage: $0 [--offline|--tools|--web|--smoke]"
        echo "  (no args)  Run all tests (requires MCP Server)"
        echo "  --offline  Offline unit tests only (no MCP Server)"
        echo "  --tools    MCP Tool tests only"
        echo "  --web      Web UI & API tests only"
        echo "  --smoke    Smoke test only (fast)"
        exit 1
        ;;
esac

echo ""
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo -e "${GREEN}═══════════════════════════════════${NC}"
    echo -e "${GREEN}  🎉 All tests passed!${NC}"
    echo -e "${GREEN}═══════════════════════════════════${NC}"
else
    echo -e "${RED}═══════════════════════════════════${NC}"
    echo -e "${RED}  ❌ $FAIL_COUNT test(s) failed${NC}"
    echo -e "${RED}═══════════════════════════════════${NC}"
    exit 1
fi
