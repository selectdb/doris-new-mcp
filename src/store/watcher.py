"""Multi-workspace semantic reload manager.

Each workspace:
  - Own store (active_store_{name})
  - Own manifest + compiler (MetricFlowEngine)
  - Independent 60s polling for changes
  - Independent toggle (semantic_enabled per workspace)
  - New workspaces auto-discovered via table scan

Global router: metric_name → (engine, workspace_name)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.health import ComponentStatus, service_health
from store.store import DorisStore
from store.version import SemanticLayerVersion, VersionTracker

logger = logging.getLogger("doris_new_mcp.watcher")


def _check_staging_duplicates(models_dir: Path) -> tuple[list[str], list[str]]:
    """Check staging YAML files for duplicate names across semantic_models.

    Detects two kinds of duplicates:
    1. Duplicate measure names across models (MetricFlow silently drops one).
    2. Duplicate semantic_model names across files (bootstrap rejects).

    Returns (errors, warnings) where:
    - errors: hard blockers that must be fixed before commit
    - warnings: informational, non-blocking
    """
    import yaml

    # Collect: measure_name → [(model_name, filename)]
    measure_sources: dict[str, list[tuple[str, str]]] = {}
    # Collect: model_name → [filename]
    model_sources: dict[str, list[str]] = {}

    for yaml_file in sorted(models_dir.rglob("*.yml")) + sorted(models_dir.rglob("*.yaml")):
        try:
            text = yaml_file.read_text(encoding="utf-8")
            docs = list(yaml.safe_load_all(text))
        except Exception:
            continue

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            sm = doc.get("semantic_model")
            if not isinstance(sm, dict):
                continue
            model_name = sm.get("name", yaml_file.stem)
            model_sources.setdefault(model_name, []).append(yaml_file.name)

            measures = sm.get("measures", [])
            if not isinstance(measures, list):
                continue
            for m in measures:
                if not isinstance(m, dict):
                    continue
                mname = m.get("name", "")
                if mname:
                    measure_sources.setdefault(mname, []).append((model_name, yaml_file.name))

    errors: list[str] = []
    warnings: list[str] = []

    # P1-2: Duplicate model names
    for mname, files in model_sources.items():
        if len(files) > 1:
            errors.append(
                f"Duplicate semantic_model name '{mname}' in files: "
                + ", ".join(files)
                + ". Each semantic_model must have a unique name."
            )

    # P1-1: Duplicate measure names
    for mname, sources in measure_sources.items():
        if len(sources) > 1:
            files = [f"{model} ({fname})" for model, fname in sources]
            errors.append(
                f"Duplicate measure '{mname}' defined in {len(sources)} models: "
                + ", ".join(files)
                + ". Only one will survive in MetricFlow (the last one wins). "
                + "Rename one of them to avoid silent data loss."
            )

    return errors, warnings


class RWLock:
    """Read-write lock. Multiple concurrent readers OR one exclusive writer.

    Currently used only for write-side guarding of per-workspace manifest/compiler
    atomic swaps. Read-side is not locked — Python GIL makes single-attribute
    assignment atomic, and the swap window is microseconds.
    """

    def __init__(self) -> None:
        self._readers = 0
        self._readers_lock = threading.Lock()
        self._writer_lock = threading.Lock()

    def read_acquire(self) -> None:
        with self._readers_lock:
            self._readers += 1
            if self._readers == 1:
                self._writer_lock.acquire()

    def read_release(self) -> None:
        with self._readers_lock:
            self._readers -= 1
            if self._readers == 0:
                self._writer_lock.release()

    def write_acquire(self) -> None:
        self._writer_lock.acquire()

    def write_release(self) -> None:
        self._writer_lock.release()


# ---------------------------------------------------------------------------
# WorkspaceState
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceState:
    """Runtime state for one workspace."""
    name: str
    store: DorisStore
    config_dir: Path
    workspace_dir: Path
    models_dir: Path

    # Semantic state
    enabled: bool = True
    manifest: Any | None = None
    compiler: Any | None = None
    
    # Polling
    known_revision: str = ""
    parsing: bool = False

    # Version tracking
    version_tracker: VersionTracker = field(default_factory=VersionTracker)
    rwlock: RWLock = field(default_factory=RWLock)


# ---------------------------------------------------------------------------
# MetricRouter
# ---------------------------------------------------------------------------

class MetricRouter:
    """metric_name → (engine, workspace_name)"""

    def __init__(self) -> None:
        self._map: dict[str, tuple[Any, str]] = {}

    def rebuild(self, workspaces: dict[str, WorkspaceState]) -> None:
        self._map.clear()
        for ws_name, ws in workspaces.items():
            if not ws.manifest or not ws.enabled:
                continue
            for m in ws.manifest.list_metrics():
                name = m["name"]
                if name in self._map:
                    logger.warning(
                        f"Metric '{name}' exists in multiple workspaces "
                        f"({self._map[name][1]} and {ws_name}), using first"
                    )
                    continue
                self._map[name] = (ws.compiler, ws_name)

    def resolve(self, metric: str) -> tuple[Any, str] | None:
        return self._map.get(metric)

    def resolve_manifest(self, metric: str) -> tuple[Any, str] | None:
        """Resolve manifest + workspace name (for read-only tools)."""
        entry = self._map.get(metric)
        if entry is None:
            return None
        # The compiler's manifest is at compiler._project_dir / target / semantic_manifest.json
        # But manifest is on the workspace. We return compiler + ws_name, and the tool
        # uses the workspace's manifest explicitly.
        return entry


# ---------------------------------------------------------------------------
# MultiWorkspaceWatcher
# ---------------------------------------------------------------------------

class MultiWorkspaceWatcher:
    """Manages N workspaces, each with independent store/polling/engine."""

    def __init__(
        self,
        config_dir: Path,
        workspace_root: Path,
        app_config: Any,
    ):
        self._config_dir = Path(config_dir)
        self._workspace_root = Path(workspace_root)
        self._app_config = app_config

        self._workspaces: dict[str, WorkspaceState] = {}
        self._router = MetricRouter()
        self._stop_event = threading.Event()
        self._discovery_thread: threading.Thread | None = None

        # P2-1: Track which workspaces have had staging validated since last change
        self._staging_validated: set[str] = set()

        # Bootstrap existing workspaces
        self._init_all()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def router(self) -> MetricRouter:
        return self._router

    @property
    def workspaces(self) -> dict[str, WorkspaceState]:
        return self._workspaces

    def get_workspace(self, name: str) -> WorkspaceState | None:
        return self._workspaces.get(name)

    def get_manifest(self, workspace: str) -> Any | None:
        ws = self._workspaces.get(workspace)
        return ws.manifest if ws else None

    def get_compiler(self, workspace: str) -> Any | None:
        ws = self._workspaces.get(workspace)
        return ws.compiler if ws else None

    def workspace_names(self) -> list[str]:
        return sorted(self._workspaces.keys())

    def has_workspace(self, name: str) -> bool:
        return name in self._workspaces

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def _init_all(self) -> None:
        """Scan Doris for existing workspace tables and init each."""
        existing = DorisStore.discover_workspaces()
        if not existing:
            logger.warning("No workspace tables found in system_mcp (active_store_*)")
        for ws_name in existing:
            self._init_workspace(ws_name, first_load=True)

    def _init_workspace(self, ws_name: str, first_load: bool = False) -> WorkspaceState:
        store = DorisStore(workspace=ws_name)
        ws_dir = self._workspace_root / ws_name
        models_dir = ws_dir / "models_cache"
        ws_dir.mkdir(parents=True, exist_ok=True)

        ws = WorkspaceState(
            name=ws_name,
            store=store,
            config_dir=self._config_dir,
            workspace_dir=ws_dir,
            models_dir=models_dir,
            enabled=True,
        )

        self._workspaces[ws_name] = ws

        if first_load:
            # Bootstrap immediately
            self._reload_workspace(ws)
        else:
            # Set initial revision for polling
            try:
                ws.known_revision = store.check_remote().revision
            except Exception:
                ws.known_revision = ""

        self._router.rebuild(self._workspaces)
        logger.info(f"Workspace '{ws_name}' initialized (first_load={first_load})")
        return ws

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._discovery_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="ws-discovery"
        )
        self._discovery_thread.start()
        service_health.get("config_watcher").set_healthy("Multi-workspace poll (60s)")
        logger.info(f"Multi-workspace watcher started ({len(self._workspaces)} workspace(s))")

    def stop(self) -> None:
        self._stop_event.set()
        if self._discovery_thread:
            self._discovery_thread.join(timeout=5)
        self._discovery_thread = None

    def _poll_loop(self) -> None:
        _POLL_INTERVAL = 60
        while not self._stop_event.is_set():
            try:
                # 1. Check existing workspaces for version changes
                for ws_name, ws in list(self._workspaces.items()):
                    if ws.parsing:
                        continue
                    try:
                        new_ver = ws.store.check_remote().revision
                        if new_ver and new_ver != ws.known_revision:
                            logger.info(
                                f"[{ws_name}] Version change: {ws.known_revision[:8] if ws.known_revision else '?'} → {new_ver[:8]}"
                            )
                            self._reload_workspace_async(ws)
                    except Exception:
                        logger.exception(f"[{ws_name}] Poll check failed")

                # 2. Discover new / remove stale workspace tables
                current = set(DorisStore.discover_workspaces())
                known = set(self._workspaces.keys())
                for new_ws in current - known:
                    logger.info(f"New workspace discovered: {new_ws}")
                    self._init_workspace(new_ws)
                for stale in known - current:
                    logger.info(f"Workspace removed (tables dropped): {stale}")
                    self._workspaces.pop(stale, None)
                    DorisStore._table_cache.pop(stale, None)  # clear cache so re-creation works
                    self._router.rebuild(self._workspaces)

            except Exception:
                logger.exception("Poll loop error")
            self._stop_event.wait(_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def _reload_workspace_async(self, ws: WorkspaceState) -> None:
        thread = threading.Thread(
            target=self._reload_workspace, args=(ws,), daemon=True
        )
        thread.start()

    def _reload_workspace(self, ws: WorkspaceState) -> None:
        if ws.parsing:
            logger.info(f"[{ws.name}] Reload skipped: already in progress")
            return

        ws.parsing = True
        t0 = time.monotonic()
        logger.info(f"[{ws.name}] Reloading...")

        try:
            from store.bootstrap import bootstrap
            from store.manifest import SemanticManifest
            from store.compiler import MetricFlowCompiler

            # Sync from Doris to local cache
            ws.store.fetch(ws.models_dir)

            # Bootstrap
            ok, err = bootstrap(self._config_dir, ws.workspace_dir, models_dir=ws.models_dir)
            if not ok:
                logger.error(f"[{ws.name}] Bootstrap failed: {err}. Keeping old version.")
                ws.version_tracker.mark_failure()
                ws.known_revision = ws.store.check_remote().revision
                return

            # Build new manifest + compiler
            manifest_path = ws.workspace_dir / "target" / "semantic_manifest.json"
            new_manifest = SemanticManifest(manifest_path)
            new_compiler = MetricFlowCompiler(ws.workspace_dir)
            metric_count = len(new_manifest.list_metrics())

            # Atomic swap
            ws.rwlock.write_acquire()
            try:
                if ws.manifest and ws.compiler:
                    ws.manifest.replace_with(new_manifest)
                    ws.compiler.replace_with(new_compiler)
                else:
                    ws.manifest = new_manifest
                    ws.compiler = new_compiler
            finally:
                ws.rwlock.write_release()

            ws.known_revision = ws.store.check_remote().revision
            duration = (time.monotonic() - t0) * 1000

            # Update version tracker
            version = SemanticLayerVersion(
                loaded_at=SemanticLayerVersion.now_iso(),
                revision=ws.known_revision,
                source_type=ws.store.store_type,
                source_uri=ws.store.source_uri,
                metric_count=metric_count,
                last_reload_success=True,
            )
            ws.version_tracker.update(version)

            # Rebuild global router
            self._router.rebuild(self._workspaces)

            logger.info(
                f"[{ws.name}] Reload done: {metric_count} metrics in {duration:.0f}ms"
            )

        except Exception as e:
            logger.exception(f"[{ws.name}] Reload failed: {e}")
            ws.version_tracker.mark_failure()
        finally:
            ws.parsing = False

    # ------------------------------------------------------------------
    # Manual reload
    # ------------------------------------------------------------------

    def force_reload(self, workspace: str) -> tuple[str, str]:
        ws = self._workspaces.get(workspace)
        if not ws:
            return "rejected", f"Workspace not found: {workspace}"
        if ws.parsing:
            return "already_running", "Reload already in progress"
        
        ws.known_revision = ""
        thread = threading.Thread(
            target=self._reload_workspace, args=(ws,), daemon=True
        )
        thread.start()
        return "accepted", "Reload submitted"

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------

    def validate_staging(self, workspace: str) -> tuple[bool, str, dict | None]:
        import shutil, tempfile
        ws = self._workspaces.get(workspace)
        if not ws:
            return False, f"Workspace not found: {workspace}", None

        stg_files = ws.store.staging_list()
        if not stg_files:
            return False, "No staging changes to validate", None

        # P2-1: Clear validation tracking at start (re-validated on success)
        self._staging_validated.discard(workspace)

        try:
            tmp_models = Path(tempfile.mkdtemp(prefix=f"stg_{workspace}_"))
            ws.store.staging_fetch(tmp_models)

            yml_count = len(list(tmp_models.rglob("*.yml"))) + len(list(tmp_models.rglob("*.yaml")))
            if yml_count == 0:
                shutil.rmtree(str(tmp_models), ignore_errors=True)
                return False, "No valid YAML files in staging", None

            from store.bootstrap import pre_validate_physical
            ok, err = pre_validate_physical(tmp_models)
            if not ok:
                shutil.rmtree(str(tmp_models), ignore_errors=True)
                return False, f"Physical validation failed: {err}", {"phase": "physical"}

            tmp_ws = Path(tempfile.mkdtemp(prefix=f"stg_ws_{workspace}_"))
            from store.bootstrap import bootstrap
            ok, err = bootstrap(self._config_dir, tmp_ws, models_dir=tmp_models)
            if not ok:
                shutil.rmtree(str(tmp_models), ignore_errors=True)
                shutil.rmtree(str(tmp_ws), ignore_errors=True)
                return False, f"Semantic validation failed: {err}", {"phase": "semantic"}

            from store.manifest import SemanticManifest
            manifest_path = tmp_ws / "target" / "semantic_manifest.json"
            manifest = SemanticManifest(manifest_path)
            metrics = manifest.list_metrics()

            # P1-1+P1-2: Check for duplicate names across models before reporting success
            dup_errors, dup_warnings = _check_staging_duplicates(tmp_models)

            shutil.rmtree(str(tmp_models), ignore_errors=True)
            shutil.rmtree(str(tmp_ws), ignore_errors=True)

            if dup_errors:
                self._staging_validated.discard(workspace)
                return False, f"Validation failed: {dup_errors[0]}", {
                    "phase": "semantic",
                    "metric_count": len(metrics),
                    "metrics": [m["name"] for m in metrics],
                    "staging_files": stg_files,
                    "errors": dup_errors,
                }

            if dup_warnings:
                self._staging_validated.add(workspace)
                return True, f"Validation passed: {len(metrics)} metrics. WARNING: {dup_warnings}", {
                    "phase": "complete",
                    "metric_count": len(metrics),
                    "metrics": [m["name"] for m in metrics],
                    "staging_files": stg_files,
                    "warnings": dup_warnings,
                }

            self._staging_validated.add(workspace)
            return True, f"Validation passed: {len(metrics)} metrics", {
                "phase": "complete",
                "metric_count": len(metrics),
                "metrics": [m["name"] for m in metrics],
                "staging_files": stg_files,
            }
        except Exception as e:
            logger.exception(f"[{workspace}] Staging validation failed")
            return False, str(e), None

    def commit_staging(self, workspace: str) -> tuple[bool, str]:
        ws = self._workspaces.get(workspace)
        if not ws:
            return False, f"Workspace not found: {workspace}"

        # P2-1: Enforce validate-before-commit
        if workspace not in self._staging_validated:
            return False, "Staging must be validated before commit. Run 'Validate' first."

        try:
            state = ws.store.staging_commit()
        except Exception as e:
            return False, f"Commit failed: {e}"

        # Clear validation tracking after successful commit
        self._staging_validated.discard(workspace)

        status, _ = self.force_reload(workspace)
        remaining = ws.store.staging_list()
        if remaining:
            logger.warning(f"[{workspace}] {len(remaining)} staging items remain after commit")
            return True, f"Committed (revision: {state.revision[:12]}), reload triggered. {len(remaining)} items remain — retry after reload."

        return True, f"Committed and reload triggered (revision: {state.revision[:12]})"
