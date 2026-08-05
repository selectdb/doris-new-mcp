#!/usr/bin/env bash
# =============================================================================
# start-mcp-server.sh — start doris-mcp-server
#
# All configuration is read from mcp-server.toml in the same directory.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Prefer DORIS_MCP_PYTHON; otherwise use the bundled Python.
PYTHON="${DORIS_MCP_PYTHON:-$SCRIPT_DIR/python/bin/python3}"
CONFIG="$SCRIPT_DIR/mcp-server.toml"

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: Python not found at $PYTHON" >&2
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: Config not found at $CONFIG" >&2
    exit 1
fi

cd "$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR/src"
exec "$PYTHON" -m src.main --config-dir "$SCRIPT_DIR"
