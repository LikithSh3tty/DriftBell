"""Access to everything Driftbell has recorded.

n8n cannot reach the SQLite files on the agent's volume, so the ops chatbot and
the error handler have no route to run history, promotions, incidents or past
reasoning without this. Reads dominate; the single write is incident recording,
which lives here because it shares the incidents schema with its readers.

Kept free of any graph dependency on purpose: turning a thread_id into a verdict
needs GRAPH.get_state, which lives in main.py, so that assembly happens there and
this stays a plain data-access module.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _rows(db: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _driftbell_db() -> str:
    """Resolved per call so tests can repoint it without reimporting."""
    return os.getenv("DRIFTBELL_DB", "driftbell.db")


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Training and evaluation history, newest first."""
    return _rows(
        _driftbell_db(),
        "SELECT run_id, model_name, version, started_at, accuracy, f1, "
        "precision_ AS precision, recall, status, notes "
        "FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )


def registry_entries() -> list[dict[str, Any]]:
    """Every known version and its stage, so 'which is champion' is answerable."""
    return _rows(
        _driftbell_db(),
        "SELECT model_name, version, stage, promoted_at FROM registry "
        "ORDER BY promoted_at DESC",
    )


def recent_incidents(limit: int = 20) -> list[dict[str, Any]]:
    """Pipeline incidents, newest first."""
    return _rows(
        _driftbell_db(),
        "SELECT occurred_at, source, severity, description FROM incidents "
        "ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    )


def incident_documents(limit: int = 20) -> list[dict[str, Any]]:
    """Incident descriptions as embeddable text.

    Only the prose is embedded. The structured columns are served whole by
    /history, because similarity search over a severity enum answers nothing a
    query could not answer better and more cheaply.
    """
    return [
        {
            "text": (
                f"Pipeline incident on {incident['occurred_at']} "
                f"from {incident['source']} ({incident['severity']} severity): "
                f"{incident['description']}"
            ),
            "metadata": {
                "source": "incident",
                "occurred_at": incident["occurred_at"],
                "severity": incident["severity"],
                "system": incident["source"],
            },
        }
        for incident in recent_incidents(limit)
    ]


def record_incident(
    source: str,
    severity: str,
    description: str,
    classification: str = "",
) -> dict[str, Any]:
    """Record a pipeline failure so the agent can weigh it against future drift.

    The agent's system prompt tells it that a drift alert coinciding with an
    ingestion incident is usually a bug, and get_pipeline_incidents is one of its
    four tools -- so a failure recorded today changes how it reasons tomorrow.

    day_offset is 0 so get_pipeline_incidents(n_days=7) sees the row at once.
    The classification is folded into the description rather than given its own
    column: this table's consumers are an LLM tool and a vector store, both of
    which read prose, and a schema change would ripple into seed_db.py and its
    tests for no retrieval benefit.
    """
    occurred_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = f"[{classification}] {description}" if classification else description

    conn = sqlite3.connect(_driftbell_db())
    try:
        conn.execute(
            "INSERT INTO incidents VALUES (?,?,?,?,?)",
            (occurred_at, source, severity, text, 0),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "occurred_at": occurred_at,
        "source": source,
        "severity": severity,
        "description": text,
        "day_offset": 0,
    }


def thread_ids(checkpoint_db: str | None = None, limit: int = 20) -> list[str]:
    """Distinct thread ids in the checkpoint store, most recently written first.

    Nothing else enumerates them: /threads/{id} requires an id you already hold,
    so without this the agent's own reasoning is unreachable to anything that
    did not witness the original run.
    """
    db = checkpoint_db or os.getenv("CHECKPOINT_DB", "checkpoints.db")
    if not os.path.exists(db):
        return []
    rows = _rows(
        db,
        "SELECT thread_id, MAX(rowid) AS latest FROM checkpoints "
        "GROUP BY thread_id ORDER BY latest DESC LIMIT ?",
        (limit,),
    )
    return [row["thread_id"] for row in rows]
