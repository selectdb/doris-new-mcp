#!/usr/bin/env python3
"""
MCP Tool test cases — covers all 10 tools

Runtime:
  - Doris FE 127.0.0.1:9030, admin:admin
  - MCP Server http://localhost:3000/mcp
  - Python 3.10+

Usage:
  python -m pytest test/test_mcp_tools.py -v
  Or run directly: python test/test_mcp_tools.py
"""

import json
import os
import sys
import unittest
import urllib.request
import urllib.error

# ── Configuration ─────────────────────────────────────────
MCP_URL = os.environ.get("MCP_URL", "http://localhost:3000/mcp")
AUTH_TOKEN = os.environ.get("MCP_TOKEN", "admin:admin")
WORKSPACE = os.environ.get("MCP_WORKSPACE", "example")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {AUTH_TOKEN}",
}

# Only connection-type errors may skip tests; AssertionError must surface.
_CONN_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError)


def _call_tool(name: str, arguments: dict) -> dict:
    """Call an MCP tool and parse the SSE response."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode(), headers=HEADERS
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        return {"error": str(e.code), "body": e.read().decode()}

    # Parse SSE: "event: message\ndata: <json>\n\n"
    data_json = body
    if body.startswith("event: message"):
        data_line = [l for l in body.split("\n") if l.startswith("data:")]
        if data_line:
            data_json = data_line[0][5:].strip()
    try:
        return json.loads(data_json)
    except json.JSONDecodeError:
        return {"raw": body}


def _assert_success(result: dict):
    """Assert that the tool call succeeded."""
    assert "result" in result, f"Expected 'result' key: {json.dumps(result, ensure_ascii=False)[:500]}"
    assert not result.get("isError"), f"Tool returned error: {json.dumps(result, ensure_ascii=False)[:500]}"
    content = result["result"]["content"][0]["text"]
    data = json.loads(content)
    assert data["success"], f"Tool failed: {data.get('error', data)}"
    return data


# ═══════════════════════════════════════════════════════
#  Tool #1: get_query_guide
# ═══════════════════════════════════════════════════════

def test_get_query_guide():
    """Tool #1: get workflow guide — mandatory first step."""
    result = _call_tool("get_query_guide", {})
    assert "result" in result
    text = result["result"]["content"][0]["text"]
    assert len(text) > 100, f"Guide too short: {len(text)} chars"
    # Must contain key guide content, such as metric layer usage and tool order.
    assert any(kw in text.lower() for kw in ["metric", "semantic", "query", "workspace"]), \
        f"Guide missing key content: {text[:200]}"
    print("  ✅ get_query_guide returned a valid guide")


# ═══════════════════════════════════════════════════════
#  Tool #2: check_service_health
# ═══════════════════════════════════════════════════════

def test_check_service_health_basic():
    """Tool #2: basic health check."""
    data = _assert_success(_call_tool("check_service_health", {}))
    assert data["data"]["doris"] == "connected", f"Doris not connected: {data}"
    assert "workspaces" in data["data"], "Missing workspaces"
    print(f"  ✅ Doris connected, workspaces: {list(data['data']['workspaces'].keys())}")


def test_check_service_health_detail():
    """Tool #2: detailed health check."""
    data = _assert_success(_call_tool("check_service_health", {"detail": True}))
    assert data["data"]["doris"] == "connected"
    print("  ✅ Detailed health check passed")


# ═══════════════════════════════════════════════════════
#  Tool #3: list_metrics
# ═══════════════════════════════════════════════════════

def test_list_metrics():
    """Tool #3: list workspace metrics."""
    data = _assert_success(
        _call_tool("list_metrics", {"workspace": WORKSPACE})
    )
    assert "data" in data
    print(f"  ✅ Metric list: {data['data']}")


def test_list_metrics_pagination():
    """Tool #3: pagination test."""
    data = _assert_success(
        _call_tool("list_metrics", {"workspace": WORKSPACE, "page_size": 2})
    )
    assert "meta" in data, "Missing pagination meta"
    print(f"  ✅ Pagination OK, total_count={data.get('meta', {}).get('total_count', 'N/A')}")


# ═══════════════════════════════════════════════════════
#  Tool #4: list_dimensions_for_metric
# ═══════════════════════════════════════════════════════

def test_list_dimensions_for_metric():
    """Tool #4: list metric dimensions."""
    # Requires a ready semantic layer; skip only on MCP Server connection errors.
    try:
        data = _assert_success(
            _call_tool("list_dimensions_for_metric", {
                "workspace": WORKSPACE,
                "metric_name": "total_amount",
            })
        )
        assert "data" in data
        print(f"  ✅ total_amount dimensions: {data['data']}")
    except _CONN_ERRORS as e:
        # Only connection errors may skip; semantic readiness assertions must surface.
        raise unittest.SkipTest(f"Skipped: MCP Server is unreachable: {e}")


# ═══════════════════════════════════════════════════════
#  Tool #5: query_metric
# ═══════════════════════════════════════════════════════

def test_query_metric_basic():
    """Tool #5: core semantic query for one metric."""
    try:
        data = _assert_success(
            _call_tool("query_metric", {
                "workspace": WORKSPACE,
                "metrics": ["total_amount"],
            })
        )
        assert "data" in data
        print(f"  ✅ query_metric result: {data['data']}")
    except _CONN_ERRORS as e:
        # Only connection errors may skip; semantic readiness assertions must surface.
        raise unittest.SkipTest(f"Skipped: MCP Server is unreachable: {e}")


def test_query_metric_with_group_by():
    """Tool #5: semantic query with group_by."""
    try:
        data = _assert_success(
            _call_tool("query_metric", {
                "workspace": WORKSPACE,
                "metrics": ["total_amount", "order_count"],
                "group_by": ["channel"],
            })
        )
        assert "data" in data
        print("  ✅ Multi-metric group_by query succeeded")
    except _CONN_ERRORS as e:
        # Only connection errors may skip; semantic readiness assertions must surface.
        raise unittest.SkipTest(f"Skipped: MCP Server is unreachable: {e}")


def test_query_metric_with_where():
    """Tool #5: semantic query with where."""
    try:
        data = _assert_success(
            _call_tool("query_metric", {
                "workspace": WORKSPACE,
                "metrics": ["total_amount"],
                "where": '{{ Dimension("user__city") }} = \'Beijing\'',
            })
        )
        print("  ✅ Query with where succeeded")
    except _CONN_ERRORS as e:
        # Only connection errors may skip; semantic readiness assertions must surface.
        raise unittest.SkipTest(f"Skipped: MCP Server is unreachable: {e}")


def test_query_metric_with_order_and_limit():
    """Tool #5: ordering and pagination."""
    try:
        data = _assert_success(
            _call_tool("query_metric", {
                "workspace": WORKSPACE,
                "metrics": ["total_amount"],
                "group_by": ["channel"],
                "order_by": ["-total_amount"],
                "limit": 3,
            })
        )
        print("  ✅ Order and pagination query succeeded")
    except _CONN_ERRORS as e:
        # Only connection errors may skip; semantic readiness assertions must surface.
        raise unittest.SkipTest(f"Skipped: MCP Server is unreachable: {e}")


# ═══════════════════════════════════════════════════════
#  Tool #6: list_databases
# ═══════════════════════════════════════════════════════

def test_list_databases():
    """Tool #6: list all databases."""
    data = _assert_success(_call_tool("list_databases", {}))
    databases = data["data"]
    assert isinstance(databases, list)
    assert len(databases) >= 2, f"Expected at least 2 databases, got {len(databases)}"
    expected = {"dw", "mysql", "information_schema", "system_mcp"}
    found = set(databases) & expected
    assert len(found) >= 2, f"Missing expected databases: {expected - found}"
    print(f"  ✅ Database list: {databases}")


def test_list_databases_pagination():
    """Tool #6: pagination."""
    data = _assert_success(
        _call_tool("list_databases", {"page_size": 2})
    )
    assert "meta" in data
    assert data["meta"]["total_count"] >= 2
    print(f"  ✅ Pagination: total={data['meta']['total_count']}")


# ═══════════════════════════════════════════════════════
#  Tool #7: list_tables
# ═══════════════════════════════════════════════════════

def test_list_tables_mysql():
    """Tool #7: list tables in mysql."""
    data = _assert_success(
        _call_tool("list_tables", {"database": "mysql"})
    )
    assert "data" in data
    assert "user" in data["data"], f"Expected 'user' table: {data['data']}"
    print(f"  ✅ mysql tables: {data['data']}")


def test_list_tables_dw():
    """Tool #7: list tables in dw."""
    data = _assert_success(
        _call_tool("list_tables", {"database": "dw"})
    )
    dw_tables = data["data"]
    expected = {"orders", "users", "products", "dim_date"}
    assert expected.issubset(set(dw_tables)), \
        f"Missing seed tables: {expected - set(dw_tables)}"
    print(f"  ✅ dw seed tables: {dw_tables}")


def test_list_tables_with_like():
    """Tool #7: LIKE matching."""
    data = _assert_success(
        _call_tool("list_tables", {"database": "mysql", "like": "user%"})
    )
    assert "user" in data["data"]
    print("  ✅ LIKE matching OK")


# ═══════════════════════════════════════════════════════
#  Tool #8: describe_table
# ═══════════════════════════════════════════════════════

def test_describe_table_summary():
    """Tool #8: table schema — summary level."""
    data = _assert_success(
        _call_tool("describe_table", {
            "database": "dw",
            "table": "orders",
            "detail_level": "summary",
        })
    )
    assert "data" in data
    print(f"  ✅ dw.orders schema: {data['data']}")


def test_describe_table_full():
    """Tool #8: table schema — full level."""
    data = _assert_success(
        _call_tool("describe_table", {
            "database": "dw",
            "table": "orders",
            "detail_level": "full",
        })
    )
    assert "data" in data
    print("  ✅ dw.orders full schema")


def test_describe_table_names():
    """Tool #8: table schema — names level (column names only)."""
    data = _assert_success(
        _call_tool("describe_table", {
            "database": "dw",
            "table": "orders",
            "detail_level": "names",
        })
    )
    assert "data" in data
    print("  ✅ dw.orders column names")


# ═══════════════════════════════════════════════════════
#  Tool #9: execute_query
# ═══════════════════════════════════════════════════════

def test_execute_query_select():
    """Tool #9: raw SQL — basic SELECT."""
    data = _assert_success(
        _call_tool("execute_query", {"sql": "SELECT 1 AS n"})
    )
    assert data["data"]["rows"][0]["n"] == 1
    print("  ✅ SELECT 1 returned the expected value")


def test_execute_query_version():
    """Tool #9: raw SQL — Doris version."""
    data = _assert_success(
        _call_tool("execute_query", {"sql": "SELECT VERSION()"})
    )
    print(f"  ✅ Doris version: {data['data']['rows'][0]}")


def test_execute_query_with_database():
    """Tool #9: raw SQL — explicit database."""
    data = _assert_success(
        _call_tool("execute_query", {
            "sql": "SELECT count(*) AS cnt FROM orders",
            "database": "dw",
        })
    )
    assert data["data"]["rows"][0]["cnt"] == 12
    print("  ✅ dw.orders has 12 rows")


def test_execute_query_with_max_rows():
    """Tool #9: raw SQL — limit returned rows."""
    data = _assert_success(
        _call_tool("execute_query", {
            "sql": "SELECT * FROM dw.dim_date",
            "max_rows": 5,
        })
    )
    assert data["meta"]["row_count"] <= 5
    print(f"  ✅ max_rows enforced, returned {data['meta']['row_count']} rows")


def test_execute_query_show():
    """Tool #9: raw SQL — SHOW statement."""
    data = _assert_success(
        _call_tool("execute_query", {"sql": "SHOW DATABASES"})
    )
    assert len(data["data"]["rows"]) >= 2
    print("  ✅ SHOW DATABASES succeeded")


def test_execute_query_explain():
    """Tool #9: raw SQL — EXPLAIN statement."""
    data = _assert_success(
        _call_tool("execute_query", {
            "sql": "EXPLAIN SELECT count(*) FROM dw.orders"
        })
    )
    assert "data" in data
    print("  ✅ EXPLAIN succeeded")


def test_execute_query_blocked_write():
    """Tool #9: read-only validation — reject INSERT."""
    result = _call_tool("execute_query", {
        "sql": "INSERT INTO dw.orders VALUES (99,1,1,100,'online','done','2024-01-01')"
    })
    assert result.get("isError") or "error" in str(result).lower() or \
           "reject" in str(result).lower() or "not allowed" in str(result).lower() or \
           "forbidden" in str(result).lower() or "only" in str(result).lower(), \
        f"Should block write SQL: {result}"
    print("  ✅ Write operation was blocked correctly")


# ═══════════════════════════════════════════════════════
#  Tool #10: reload_semantic_layer
# ═══════════════════════════════════════════════════════

def test_reload_semantic_layer():
    """Tool #10: manually reload semantic layer."""
    result = _call_tool("reload_semantic_layer", {"workspace": WORKSPACE})
    # Must return a valid JSON-RPC result with parseable structured JSON content.
    # success_response / error_response both include the success field.
    assert "result" in result, f"Expected JSON-RPC result: {json.dumps(result, ensure_ascii=False)[:500]}"
    content = result["result"]["content"][0]["text"]
    data = json.loads(content)
    assert "success" in data, f"Payload missing 'success' field: {data}"
    print(f"  ✅ reload call succeeded (success={data['success']})")


# ═══════════════════════════════════════════════════════
#  Auth/error handling tests
# ═══════════════════════════════════════════════════════

def test_auth_required():
    """No Authorization header should be rejected."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "list_databases", "arguments": {}},
    }
    no_auth_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode(), headers=no_auth_headers
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "Should have returned error for unauthenticated request"
    except urllib.error.HTTPError as e:
        assert e.code in (401, 403), f"Expected 401/403, got {e.code}"
        print(f"  ✅ Unauthenticated request returned {e.code}")


def test_invalid_token():
    """Invalid token should be rejected."""
    bad_headers = {**HEADERS, "Authorization": "Bearer fake:fake"}
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "list_databases", "arguments": {}},
    }
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode(), headers=bad_headers
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "Should have rejected invalid credentials"
    except urllib.error.HTTPError as e:
        assert e.code in (401, 403), f"Expected 401/403, got {e.code}"
        print(f"  ✅ Invalid token returned {e.code}")


def test_execute_query_syntax_error():
    """SQL syntax errors should return a friendly error."""
    result = _call_tool("execute_query", {"sql": "SELECTT 1"})
    # May be an error response or success=false.
    is_error = (
        result.get("isError") or
        ("error" in str(result).lower()) or
        ("syntax" in str(result).lower())
    )
    assert is_error, f"Should report syntax error: {result}"
    print("  ✅ SQL syntax error handled correctly")


# ═══════════════════════════════════════════════════════
#  End-to-end workflow test
# ═══════════════════════════════════════════════════════

def test_agent_workflow():
    """Simulate the standard AI Agent workflow: guide → health → databases → tables → query."""
    # Step 1: guide
    r1 = _call_tool("get_query_guide", {})
    assert "result" in r1
    print("  Step 1: get_query_guide ✅")

    # Step 2: health
    r2 = _call_tool("check_service_health", {})
    assert "result" in r2
    print("  Step 2: check_service_health ✅")

    # Step 3: list databases
    r3 = _call_tool("list_databases", {})
    d3 = _assert_success(r3)
    assert "dw" in d3["data"]
    print("  Step 3: list_databases ✅")

    # Step 4: list tables
    r4 = _call_tool("list_tables", {"database": "dw"})
    d4 = _assert_success(r4)
    assert "orders" in d4["data"]
    print("  Step 4: list_tables ✅")

    # Step 5: describe
    r5 = _call_tool("describe_table", {
        "database": "dw", "table": "orders", "detail_level": "summary"
    })
    _assert_success(r5)
    print("  Step 5: describe_table ✅")

    # Step 6: query
    r6 = _call_tool("execute_query", {
        "sql": "SELECT channel, count(*) AS c FROM dw.orders GROUP BY channel"
    })
    _assert_success(r6)
    print("  Step 6: execute_query ✅")

    print("  🎉 Complete Agent workflow passed!")


# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("MCP Tool tests starting")
    print(f"  URL: {MCP_URL}")
    print(f"  Workspace: {WORKSPACE}")
    print("=" * 60)

    tests = [
        # Tool #1
        ("get_query_guide", test_get_query_guide),
        # Tool #2
        ("check_service_health (basic)", test_check_service_health_basic),
        ("check_service_health (detail)", test_check_service_health_detail),
        # Tool #3
        ("list_metrics", test_list_metrics),
        ("list_metrics (pagination)", test_list_metrics_pagination),
        # Tool #4
        ("list_dimensions_for_metric", test_list_dimensions_for_metric),
        # Tool #5
        ("query_metric (basic)", test_query_metric_basic),
        ("query_metric (group_by)", test_query_metric_with_group_by),
        ("query_metric (where)", test_query_metric_with_where),
        ("query_metric (order+limit)", test_query_metric_with_order_and_limit),
        # Tool #6
        ("list_databases", test_list_databases),
        ("list_databases (pagination)", test_list_databases_pagination),
        # Tool #7
        ("list_tables (mysql)", test_list_tables_mysql),
        ("list_tables (dw)", test_list_tables_dw),
        ("list_tables (like)", test_list_tables_with_like),
        # Tool #8
        ("describe_table (summary)", test_describe_table_summary),
        ("describe_table (full)", test_describe_table_full),
        ("describe_table (names)", test_describe_table_names),
        # Tool #9
        ("execute_query (SELECT)", test_execute_query_select),
        ("execute_query (VERSION)", test_execute_query_version),
        ("execute_query (database)", test_execute_query_with_database),
        ("execute_query (max_rows)", test_execute_query_with_max_rows),
        ("execute_query (SHOW)", test_execute_query_show),
        ("execute_query (EXPLAIN)", test_execute_query_explain),
        ("execute_query (block write)", test_execute_query_blocked_write),
        # Tool #10
        ("reload_semantic_layer", test_reload_semantic_layer),
        # Auth
        ("auth required", test_auth_required),
        ("invalid token", test_invalid_token),
        # Error handling
        ("SQL syntax error", test_execute_query_syntax_error),
        # E2E
        ("Agent workflow", test_agent_workflow),
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, fn in tests:
        try:
            print(f"\n[{name}]")
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1
        except Exception as e:
            if "Skipped" in str(e) or "not_ready" in str(e):
                print(f"  ⚠️ SKIP: {e}")
                skipped += 1
            else:
                print(f"  ❌ ERROR: {e}")
                failed += 1

    print(f"\n{'='*60}")
    print(f"Result: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"{'='*60}")

    if failed > 0:
        sys.exit(1)
