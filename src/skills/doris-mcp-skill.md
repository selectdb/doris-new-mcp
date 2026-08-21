# Doris MCP Query Reference

> Choose the query path from the user's intent. Semantic workspaces load only
> when a semantic tool or the Semantic Web UI is used.
> This guide is fetched once per conversation context. Reuse it for follow-up
> queries instead of calling `get_query_guide` again.

## tl;dr — The Six Essential Tools

```
get_query_guide()                        → call once per conversation context
check_service_health()                   → Doris and already-loaded semantic health
list_metrics(workspace)                  → what can I ask?
list_dimensions_for_metric(workspace, name) → how can I slice it?
query_metric(workspace, metrics, ...)    → give me the data
execute_query(sql, ...)                  → strictly read-only Doris SQL
```

`workspace` is required for the three semantic metric tools. Use `"example"` for the built-in sample.

Call `get_query_guide` again only when starting a new conversation or when a
context reset/compaction removed this guide. Changing databases, workspaces, or
query paths within the same context does not require another call.

---

## Step 0: Check Health

Call this when connectivity or already-loaded semantic status is useful. It
does not discover, compile, or load semantic workspaces.

```
check_service_health()
```

Returns:

```json
{
  "doris": "connected",
  "workspaces": {
    "example":   {"status": "healthy",    "metric_count": 5},
    "marketing": {"status": "no_models",  "message": "No YAML files"},
    "finance":   {"status": "not_ready",  "message": "Files present but failed to load"}
  }
}
```

**Semantic-query rules:**
- Pick a workspace with `status: "healthy"` before calling `query_metric`.
- If the user mentions a specific workspace, use it. Otherwise use `"example"`.
- If `doris` is `"unavailable"`, warn the user. `list_databases` / `execute_query` may still work.
- Calling a semantic tool loads only its requested workspace on demand.
- Do not initialize semantic workspaces unless the chosen query path needs them.

---

## Step 1: list_metrics — What Can I Ask?

```json
// Request
{"workspace": "example"}

// Response
{
  "data": [
    {"name": "total_amount", "description": "Total order amount"},
    {"name": "order_count",   "description": "Order count"},
    {"name": "avg_amount",    "description": "Average order value"},
    {"name": "unique_users",  "description": "Ordering users"},
    {"name": "user_count",    "description": "User count"}
  ],
  "meta": {"total_count": 5}
}
```

**How to match user intent to a metric:**
- "sales / revenue / GMV" → `total_amount`
- "orders / transactions" → `order_count`
- "average order value / AOV" → `avg_amount`
- "ordering users / purchasing users" → `unique_users`
- "users / customer count" → `user_count`

When using the semantic path, if the user's question does not clearly match a
metric, call `list_metrics` and scan the descriptions. If nothing matches, use
the read-only SQL path.

---

## Step 2: list_dimensions_for_metric — How Can I Slice It?

```json
// Request
{"workspace": "example", "metric_name": "total_amount"}

// Response
{
  "data": [
    {"name": "order_date",    "type": "time",        "description": "Order date (day grain)"},
    {"name": "channel",       "type": "categorical", "description": "Order channel"},
    {"name": "status",        "type": "categorical", "description": "Order status"},
    {"name": "city",          "type": "categorical", "description": "City"},
    {"name": "level",         "type": "categorical", "description": "Customer level"},
    {"name": "register_date", "type": "time",        "description": "Registration date"},
    {"name": "category",      "type": "categorical", "description": "Product category"},
    {"name": "brand",         "type": "categorical", "description": "Brand"}
  ],
  "meta": {"metric": "total_amount", "count": 8}
}
```

**Rules:**
- `type: "time"` → can group by day/week/month/quarter/year. Use `"month"` in `group_by`.
- `type: "categorical"` → discrete buckets. Use `"channel"`, `"city"`, etc.
- The engine auto-joins across tables. `city` comes from `users`, but works with `total_amount` from `orders` — no JOIN needed.
- Always check dimensions BEFORE calling `query_metric`. Using a dimension not in this list causes errors.

---

## Step 3: query_metric — Give Me the Data

### Parameters

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `workspace` | string | **Yes** | — | `"example"` |
| `metrics` | list[string] | **Yes** | — | e.g. `["total_amount", "order_count"]` |
| `group_by` | list[string] | No | `[]` | Dimension names from Step 2. Time grains: `"day"`, `"week"`, `"month"`, `"quarter"`, `"year"` |
| `where` | string | No | `""` | SQL predicate or JSON object |
| `order_by` | list[string] | No | `[]` | `-` prefix = DESC, e.g. `["-total_amount"]` |
| `limit` | int | No | `0` | Max rows. `0` = no limit |
| `having` | string | No | `""` | Filter on aggregated value, e.g. `"total_amount > 1000"` |
| `database` | string | No | `""` | Target Doris database (auto-detected if empty) |
| `max_rows` | int | No | `0` | Hard row cap for execution. `0` = server default (10,000) |

### Response

```json
{
  "data": {
    "columns": ["channel", "total_amount"],
    "rows": [
      {"channel": "APP",  "total_amount": 2396.00},
      {"channel": "WEB",  "total_amount": 2096.00},
      {"channel": "MINI", "total_amount": 298.00}
    ]
  },
  "meta": {"duration_ms": 12.5, "row_count": 3}
}
```

### WHERE Syntax

All forms are auto-normalized — pick whichever is easiest:

```python
# Plain SQL
where="channel = 'APP'"
where="order_date >= '2026-02-01' AND order_date <= '2026-02-28'"
where="channel IN ('APP', 'MINI')"

# JSON object (AND-joined)
where='{"channel": "APP", "status": "completed"}'

# JSON with array values (IN clause)
where='{"channel": ["APP", "MINI"]}'
```

### HAVING Syntax

Filter on the **aggregated result**. References metric names from the output columns:

```python
# Single condition
having="total_amount > 1000"

# Multiple conditions
having="total_amount > 500 AND order_count > 2"
```

**Do NOT** pass Jinja templates, JSON objects, or double-quoted strings in `having`. Plain SQL comparisons only.

### Ordering

```python
order_by=["-total_amount"]   # DESC
order_by=["channel"]          # ASC
order_by=["-total_amount", "channel"]  # multi-column
```

### Full Examples

```json
// "Sales by channel"
{"workspace": "example", "metrics": ["total_amount"], "group_by": ["channel"]}

// "Daily order trend in February"
{"workspace": "example", "metrics": ["order_count"], "group_by": ["order_date"],
 "where": "order_date >= '2026-02-01' AND order_date <= '2026-02-28'",
 "order_by": ["order_date"]}

// "Top 3 channels by sales"
{"workspace": "example", "metrics": ["total_amount"], "group_by": ["channel"],
 "order_by": ["-total_amount"], "limit": 3}

// "Channel distribution for completed orders"
{"workspace": "example", "metrics": ["total_amount", "order_count"],
 "group_by": ["channel"], "where": "status = 'completed'"}

// "Sales by brand, showing only strong sellers"
{"workspace": "example", "metrics": ["total_amount"], "group_by": ["brand"],
 "order_by": ["-total_amount"], "having": "total_amount > 500"}
```

---

## When to Use Read-only SQL (`execute_query`)

- Use it directly for explicit SQL, Doris full-text search, or physical-schema exploration.
- Use it when the semantic layer is unavailable or has no matching metric.
- No semantic health check is required before the raw SQL path.

The SQL path is:

### Fallback: Raw SQL

```
list_databases()                              → find database
list_tables(database="dw")                    → find tables
describe_table(database="dw", table="orders") → check columns
execute_query(sql="SELECT ... FROM dw.orders ...")
```

When falling back from governed metrics to raw SQL, warn:

> "No semantic metrics match your query. Results below come from raw SQL and may have incorrect aggregation or duplicate counting. Use with caution."

---

## Common Mistakes

| ❌ Don't | ✅ Do |
|----------|------|
| Forget `workspace` parameter | Every semantic tool requires it |
| Load semantics without need | Use semantic tools only when that path is chosen |
| Call `query_metric` before checking dimensions | `list_dimensions_for_metric` first to verify `group_by` values |
| Write `having='{"x": 10}'` (JSON) | `having` takes plain SQL: `"x > 10"` |
| Use `describe_table` to plan metric queries | Use `list_metrics` — metrics handle joins automatically |
| Report raw SQL results as authoritative | Always add the warning |
