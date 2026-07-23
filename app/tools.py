"""Tools the diagnostic agent can call.

All of them read a local SQLite file, so the service has no paid dependency and
no external state. Swap the connection for Supabase Postgres later without
touching the graph.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from langchain_core.tools import tool

DB_PATH = os.getenv("DRIFTBELL_DB", "driftbell.db")


def _query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@tool
def get_recent_runs(model_name: str, limit: int = 5) -> str:
    """Return the most recent training/evaluation runs for a model.

    Use this to see whether performance was already degrading before this alert.
    """
    rows = _query(
        "SELECT run_id, started_at, accuracy, f1, precision_, recall, status, notes "
        "FROM runs WHERE model_name = ? ORDER BY started_at DESC LIMIT ?",
        (model_name, limit),
    )
    return json.dumps(rows, indent=2) if rows else "No runs recorded for this model."


@tool
def get_feature_stats(model_name: str, n_days: int = 14) -> str:
    """Return per-feature distribution statistics for the recent scoring window.

    Use this to tell genuine covariate drift apart from an upstream data bug
    (for example a feature that suddenly became all-null).
    """
    rows = _query(
        "SELECT feature, psi, null_rate, mean_train, mean_live "
        "FROM feature_stats WHERE model_name = ? AND day_offset <= ? "
        "ORDER BY psi DESC",
        (model_name, n_days),
    )
    return json.dumps(rows, indent=2) if rows else "No feature statistics available."


@tool
def get_model_registry(model_name: str) -> str:
    """Return the registry entry: current champion version, when it was promoted,
    and which versions are available for rollback."""
    rows = _query(
        "SELECT version, stage, promoted_at, artifact_uri FROM registry "
        "WHERE model_name = ? ORDER BY promoted_at DESC",
        (model_name,),
    )
    return json.dumps(rows, indent=2) if rows else "Model not found in registry."


@tool
def get_pipeline_incidents(n_days: int = 7) -> str:
    """Return recent data-pipeline incidents. A drift alert that lines up with an
    ingestion failure is usually a bug, not drift."""
    rows = _query(
        "SELECT occurred_at, source, severity, description FROM incidents "
        "WHERE day_offset <= ? ORDER BY occurred_at DESC",
        (n_days,),
    )
    return json.dumps(rows, indent=2) if rows else "No incidents in this window."


TOOLS = [get_recent_runs, get_feature_stats, get_model_registry, get_pipeline_incidents]
