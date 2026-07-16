"""Semantic manifest loader — reads dbt semantic_manifest.json and YAML files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("doris_new_mcp.semantic")


class SemanticManifest:
    """Lightweight manifest parsed from target/semantic_manifest.json.

    Provides metadata access (metrics, dimensions, entities) without
    requiring the full MetricFlow engine.
    """

    def __init__(self, manifest_path: str | Path):
        self._path = Path(manifest_path)
        self._data: dict = {}
        self._metrics: list[dict] = []
        self._semantic_models: list[dict] = []
        self.load()

    def load(self) -> None:
        """Load and parse the semantic manifest JSON."""
        if not self._path.exists():
            raise FileNotFoundError(f"Semantic manifest not found: {self._path}")

        with open(self._path, "r") as f:
            self._data = json.load(f)

        self._metrics = list(self._data.get("metrics", {}).values()) if isinstance(
            self._data.get("metrics"), dict
        ) else self._data.get("metrics", [])

        self._semantic_models = list(self._data.get("semantic_models", {}).values()) if isinstance(
            self._data.get("semantic_models"), dict
        ) else self._data.get("semantic_models", [])

    def replace_with(self, other: "SemanticManifest") -> None:
        """Atomically replace content from another SemanticManifest instance."""
        self._data = other._data
        self._metrics = other._metrics
        self._semantic_models = other._semantic_models

    def get_semantic_table_names(self) -> set[str]:
        """Return all source table names referenced by semantic models.

        Used for detecting semantic conflict when execute_query hits a metric-defined table.
        """
        tables: set[str] = set()
        for sm in self._semantic_models:
            # Semantic model name often matches the staging model / source table
            name = sm.get("name", "")
            if name:
                tables.add(name.lower())
            # Also check node_relation if available
            node_rel = sm.get("node_relation", {})
            if isinstance(node_rel, dict):
                alias = node_rel.get("alias", "")
                if alias:
                    tables.add(alias.lower())
                relation_name = node_rel.get("relation_name", "")
                if relation_name:
                    tables.add(relation_name.lower())
        return tables

    def get_metric_names(self) -> set[str]:
        """Return all metric names as a set."""
        return {m.get("name", "") for m in self._metrics}

    def list_metrics(self) -> list[dict[str, Any]]:
        """Return all metrics with name, description."""
        result = []
        for m in self._metrics:
            result.append({
                "name": m.get("name", ""),
                "description": m.get("description", ""),
            })
        return result

    def get_metric(self, metric_name: str) -> dict | None:
        """Get full metric definition by name."""
        for m in self._metrics:
            if m.get("name") == metric_name:
                return m
        return None

    def list_dimensions_for_metric(self, metric_name: str) -> list[dict[str, Any]]:
        """Return dimensions available for a metric.

        Finds the semantic models referenced by the metric's measures,
        then collects their dimensions.
        """
        metric = self.get_metric(metric_name)
        if not metric:
            return []

        # Collect measure references from metric type_params
        measure_refs = set()
        type_params = metric.get("type_params", {})
        if not isinstance(type_params, dict):
            type_params = {}

        # Handle "measure" field (simple metrics: dict, derived: absent)
        measure_param = type_params.get("measure")
        if isinstance(measure_param, dict):
            measure_refs.add(measure_param.get("name", ""))
        elif isinstance(measure_param, str):
            measure_refs.add(measure_param)

        # Handle "metrics" field (derived metrics reference other metrics → resolve recursively)
        sub_metrics = type_params.get("metrics", [])
        if isinstance(sub_metrics, list):
            for sm in sub_metrics:
                sub_name = sm.get("name", "") if isinstance(sm, dict) else str(sm)
                sub_metric = self.get_metric(sub_name)
                if sub_metric:
                    sub_params = sub_metric.get("type_params", {})
                    sub_measure = sub_params.get("measure") if isinstance(sub_params, dict) else None
                    if isinstance(sub_measure, dict):
                        measure_refs.add(sub_measure.get("name", ""))
                    elif isinstance(sub_measure, str):
                        measure_refs.add(sub_measure)

        # Find semantic models containing these measures
        dimensions = []
        seen = set()
        for sm in self._semantic_models:
            sm_measures = {m.get("name") for m in sm.get("measures", [])}
            if measure_refs & sm_measures:
                for dim in sm.get("dimensions", []):
                    dim_name = dim.get("name", "")
                    if dim_name not in seen:
                        seen.add(dim_name)
                        dimensions.append({
                            "name": dim_name,
                            "type": dim.get("type", ""),
                            "description": dim.get("description", ""),
                        })
        return dimensions

    def search(self, keywords: list[str]) -> list[dict[str, Any]]:
        """Search metrics and dimensions by keyword list (priority order).

        Iterates keywords in order. Returns results from the first keyword
        that produces any match. Earlier keywords = higher priority.
        """
        for keyword in keywords:
            if not keyword or not keyword.strip():
                continue
            kw = keyword.strip().lower()
            results = []

            for m in self._metrics:
                name = m.get("name", "")
                desc = m.get("description") or ""
                if kw in name.lower() or kw in desc.lower():
                    results.append({"type": "metric", "name": name, "description": desc})

            for sm in self._semantic_models:
                for dim in sm.get("dimensions", []):
                    name = dim.get("name", "")
                    desc = dim.get("description") or ""
                    if kw in name.lower() or kw in desc.lower():
                        results.append({"type": "dimension", "name": name, "description": desc})

            if results:
                return results

        return []
