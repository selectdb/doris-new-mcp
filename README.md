<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Doris MCP Server

[English](README.md) | [Simplified Chinese](README.zh-CN.md)

Doris MCP Server is a backend service that exposes [Apache Doris](https://doris.apache.org/) through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). AI clients (Claude Desktop, Cursor, VS Code, and others) can query Doris data through a governed **semantic metrics layer** built on [MetricFlow](https://github.com/dbt-labs/metricflow), with raw-SQL discovery as a fallback path. It ships with **Semantic Web UI** for managing semantic models and a CLI client for scripting.

## Core Features

*   **Semantic Metrics Layer**: Define metrics once in YAML (simple / ratio / derived / cumulative / conversion), query them from any MCP client. MetricFlow compiles semantically correct SQL — no hand-written aggregation queries.
*   **Multi-Workspace Isolation**: Fully isolated tenants with their own models, compiler, and Doris storage tables. Models are stored in Doris itself (`active` + `staging` tables), so multiple server nodes share state without file sync.
*   **Staging Workflow**: All model changes go through *staging → validate → commit*; broken models can never affect running queries.
*   **Guided Tooling**: 10 MCP tools with an enforced workflow (`get_query_guide` → `check_service_health` → semantic query, or metadata discovery → raw SQL fallback).
*   **Credential Pass-Through**: `Authorization: Bearer <doris-user>:<password>` — every query runs under the caller's own Doris identity with per-user connection pools. No shared admin credentials.
*   **Semantic Web UI**: Log in with Doris credentials to edit, validate, and publish models, manage workspaces, and deploy the bundled example. No local YAML tooling is required.
*   **CLI Client**: `mcp-client` for calling tools and pushing/pulling model files from scripts and CI/CD.
*   **Multi-Node Ready**: Session affinity is handled in-app using the server IP recorded in the Semantic Web UI cookie; nginx stays a plain reverse proxy.
*   **Self-Contained Packaging**: Release tarballs bundle a Python 3.10 runtime and all dependencies. No network, no pip, no system Python required on target machines.

## System Requirements

*   **Server host**: Linux x86_64 or ARM64 (for running the release package)
*   **Database**: Apache Doris FE reachable via MySQL protocol (default `127.0.0.1:9030`)
*   **Building from source**: curl/wget, or a local Python 3.10.x for offline builds

## 🚀 Quick Start

### 1. Get the package

Download the latest release from [Releases](../../releases):

```bash
tar xzf doris-mcp-server-<version>-linux_x64.tar.gz
cd doris-mcp-server
```

Or build from source (see [Building from Source](#building-from-source)).

### 2. Start the server

The default points to a same-host Doris FE (`127.0.0.1:9030`). For a separate deployment, set `server.fe_host` to the FE private IP or hostname, then:

```bash
# Foreground
./start-mcp-server.sh

# Background
nohup ./start-mcp-server.sh > /dev/null 2>&1 &
```

The server listens on port **3000** by default.

### 3. Connect your MCP client

Authentication is your **Doris username and password** passed as a Bearer token:

```text
Authorization: Bearer <doris-user>:<doris-password>
```

**Claude Desktop / Claude Code:**

```bash
claude mcp add --transport http doris http://<host>:3000/mcp \
  --header "Authorization: Bearer <user>:<password>"
```

**Cursor / VS Code (`mcp.json`):**

```json
{
  "mcpServers": {
    "doris": {
      "type": "http",
      "url": "http://<host>:3000/mcp",
      "headers": {
        "Authorization": "Bearer <user>:<password>"
      }
    }
  }
}
```

A ready-to-copy template is provided at [`mcp.json.example`](mcp.json.example).

**Smoke-test the connection with the FastMCP CLI:**

```bash
fastmcp call http://<host>:3000/mcp check_service_health \
  --auth "<user>:<password>" --json
```

### 4. Deploy the example workspace (Semantic Web UI)

1. Open `http://<host>:3000/mcp/web` and log in with your Doris credentials (management operations require membership in the Doris `admin` role).
2. Click the **example deploy** button. Deployment runs in the background; the page polls progress and redirects when done.
3. Back in your AI client, ask: *"What is the total order amount by channel?"* — the agent will discover the `example` workspace and query metrics like `total_amount` grouped by `channel`.

### 5. Manage semantic models

**Semantic Web UI** (`/mcp/web`): authenticated users can inspect models; users with the Doris `admin` role can create/upload/edit YAML models → **Validate** → **Commit**. Only validated models go live.

**CLI client:**

```bash
export DORIS_MCP_SERVER=http://<host>:3000
export DORIS_MCP_TOKEN=<user>:<password>

./mcp-client.sh semantic push ./models -w my_workspace
./mcp-client.sh semantic pull -o ./backup -w my_workspace
./mcp-client.sh tool call list_metrics --json '{"workspace":"my_workspace"}'
```

## How the Agent Queries Data

The agent chooses the query path from the user's intent. Semantic metric tools
load only the requested workspace on demand; direct read-only SQL, Doris search,
and schema discovery do not initialize the semantic layer. Opening the Semantic
Web UI is an explicit semantic-layer action and initializes its workspace view.
`check_service_health` discovers workspace names with read-only metadata
statements and reports unloaded workspaces without compiling them.

`execute_query` accepts single-statement read-only Doris SQL, including Doris-specific SELECT syntax that is not yet modeled by sqlglot. DML, DDL, stacked statements, and `SELECT INTO OUTFILE` remain blocked.

## Configuration (`mcp-server.toml`)

| Key | Default | Description |
|-----|---------|-------------|
| `server.mcp_host` / `server.mcp_port` | `0.0.0.0` / `3000` | HTTP listen address |
| `server.fe_host` / `server.fe_port` | `127.0.0.1` / `9030` | Doris FE endpoint; set a private IP or hostname for remote FE deployment |
| `query.db_whitelist` | `[]` | Optional database allow-list |
| `query.query_timeout_seconds` | `600` | SQL query timeout |
| `query.query_max_rows` | `10000` | Max rows per query |

All values support `${ENV_VAR}` interpolation.

## Multi-Node Deployment

Multiple server nodes behind one load balancer work without an affinity configuration. On the first successful Semantic Web UI login, a concrete `server.mcp_host` is written directly into the session cookie; when listening on `0.0.0.0`, the server instead reads the local IPv4 address on which the request arrived. The cookie format is `session_id.<server-ip>`. If a later request reaches another node, the middleware forwards it to the node recorded in the cookie. nginx needs no cookie parsing, and `/mcp` traffic stays node-local. See [DESIGN.md](DESIGN.md) §8.3 for details.

## Building from Source

```bash
./build.sh linux-x64      # Linux x86_64
./build.sh linux-arm64    # Linux ARM64
./build.sh macos-arm64    # macOS Apple Silicon
./build.sh clean          # remove build artifacts
```

The build downloads a standalone Python 3.10 and produces a self-contained tarball in `dist/`. If GitHub is unreachable, point to a local Python 3.10:

```bash
DORIS_MCP_SYSTEM_PYTHON=/opt/miniconda3/bin/python ./build.sh linux-x64
```

**CI releases** (`.github/workflows/release.yml`): merging a PR into `main` automatically builds linux_x64 + linux_arm64 packages and publishes a GitHub Release with the next 0.2.x patch version. A release can also be triggered by pushing a tag named `doris-mcp-server-x.y.z`, or manually from the Actions page.

## Running Tests

```bash
bash test/run_all_tests.sh --offline   # unit tests, no server needed
bash test/run_all_tests.sh --smoke     # quick smoke test
bash test/run_all_tests.sh             # full suite (needs a local server)
```

## Documentation

*   [DESIGN.md](DESIGN.md) — architecture and design decisions
*   [INSTALL.html](INSTALL.html) — installation guide
*   [doris-mcp-docs.html](doris-mcp-docs.html) — semantic model reference and user guide
*   [README.zh-CN.md](README.zh-CN.md) — Simplified Chinese documentation

## License

Licensed under the [Apache License, Version 2.0](LICENSE.txt).
This distribution vendors MetricFlow (`src/metricflow/`), which carries its own license — see `src/metricflow/LICENSE` and `src/metricflow/NOTICE`.
