"""Semantic conflict detection for execute_query.

When semantic layer is enabled, detects if a raw SQL query aggregates data
from tables that have MetricFlow semantic model definitions. If detected,
returns a warning + suggested query_metric call, without blocking execution.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger("doris_new_mcp.semantic_guard")

_AGG_FUNCTIONS = {"sum", "count", "avg", "min", "max", "count_distinct", "approx_count_distinct"}


def detect_semantic_conflict(
    sql: str,
    semantic_tables: set[str],
    metric_names: set[str],
) -> dict[str, Any] | None:
    """Check if a raw SQL query conflicts with semantic layer definitions.

    Returns a warning dict if conflict detected, None otherwise.

    Conflict = SQL aggregates on a table that has a semantic model defined.
    """
    if not semantic_tables:
        return None

    try:
        parsed = sqlglot.parse_one(sql, dialect="doris")
    except Exception:
        return None

    # Extract table names from FROM / JOIN clauses
    sql_tables: set[str] = set()
    for table in parsed.find_all(exp.Table):
        name = table.name.lower()
        if name:
            sql_tables.add(name)
        # Also check alias
        alias = table.alias
        if alias:
            sql_tables.add(alias.lower())

    # Check if any SQL table matches a semantic model table
    matched_tables = sql_tables & semantic_tables
    if not matched_tables:
        return None

    # Check if SQL contains aggregation functions
    has_aggregation = False
    for func in parsed.find_all(exp.AggFunc):
        has_aggregation = True
        break

    # Also check for GROUP BY
    if not has_aggregation:
        for gb in parsed.find_all(exp.Group):
            has_aggregation = True
            break

    if not has_aggregation:
        return None

    # Conflict detected: aggregation on a metric-defined table
    sorted_metrics = sorted(metric_names)[:15]
    return {
        "semantic_conflict": True,
        "WARNING": (
            f"RESULTS MAY BE INCORRECT. Table(s) {sorted(matched_tables)} have MetricFlow "
            f"semantic definitions with business logic (filters, joins, formulas) that this "
            f"raw SQL does not replicate. Re-run using query_metric with one of these metrics: "
            f"{sorted_metrics}"
        ),
        "matched_tables": sorted(matched_tables),
        "available_metrics": sorted_metrics,
        "action": "Re-run this query using query_metric for consistent results.",
    }
