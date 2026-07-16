"""Service state management for semantic layer readiness.

States:
  INITIALIZING → bootstrap/parse in progress, reject semantic tools
  READY        → fully initialized, accept all
  RELOADING    → hot-update in progress, serve from old state (RWLock)
  ERROR        → workspace inconsistent or manifest failed, reject semantic tools

Only semantic layer tools are gated. Base tools (list_databases, execute_query, etc.)
always work regardless of state.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("doris_new_mcp.state")


class ServiceState(str, Enum):
    INITIALIZING = "initializing"
    READY = "ready"
    RELOADING = "reloading"
    ERROR = "error"


class SemanticLayerState:
    """Thread-safe semantic layer state tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = ServiceState.INITIALIZING
        self._message = "Starting up"

    @property
    def state(self) -> ServiceState:
        with self._lock:
            return self._state

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    def set_initializing(self, message: str = "Bootstrapping") -> None:
        with self._lock:
            self._state = ServiceState.INITIALIZING
            self._message = message
        logger.info(f"State → INITIALIZING: {message}")

    def set_ready(self, message: str = "Ready") -> None:
        with self._lock:
            self._state = ServiceState.READY
            self._message = message
        logger.info(f"State → READY: {message}")

    def set_reloading(self, message: str = "Hot-reloading") -> None:
        with self._lock:
            self._state = ServiceState.RELOADING
            self._message = message
        logger.info(f"State → RELOADING: {message}")

    def set_error(self, message: str) -> None:
        with self._lock:
            self._state = ServiceState.ERROR
            self._message = message
        logger.error(f"State → ERROR: {message}")

    def is_serving(self) -> bool:
        """Can semantic layer tools accept requests?"""
        with self._lock:
            return self._state in (ServiceState.READY, ServiceState.RELOADING)

    def to_dict(self) -> dict[str, str]:
        with self._lock:
            return {"state": self._state.value, "message": self._message}


def check_workspace_consistency(workspace_dir: Path, config_dir: Path) -> tuple[bool, str]:
    """Verify workspace staging files match sources.yml declarations.

    Returns (is_consistent, error_message).
    """
    sources_path = config_dir / "models" / "sources.yml"
    if not sources_path.exists():
        return True, ""  # No sources = nothing to check

    try:
        with open(sources_path) as f:
            sources_data = yaml.safe_load(f) or {}
    except Exception as e:
        return False, f"Cannot read sources.yml: {e}"

    staging_dir = workspace_dir / "staging"
    if not staging_dir.exists():
        return False, f"staging/ directory missing in workspace"

    expected_tables = set()
    for source in sources_data.get("sources", []):
        for table in source.get("tables", []):
            table_name = table.get("name", "") if isinstance(table, dict) else str(table)
            if table_name:
                expected_tables.add(table_name)

    missing = []
    for table_name in expected_tables:
        sql_path = staging_dir / f"{table_name}.sql"
        if not sql_path.exists():
            missing.append(table_name)

    if missing:
        return False, f"Missing staging files for: {', '.join(sorted(missing))}"

    manifest_path = workspace_dir / "target" / "semantic_manifest.json"
    if not manifest_path.exists():
        return False, "semantic_manifest.json not found in workspace"

    return True, ""


# Global singleton
semantic_state = SemanticLayerState()
