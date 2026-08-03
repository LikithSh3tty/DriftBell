"""Fit a challenger, record the run, and move the champion when told to.

Split out of main.py so that file stays a routing layer. Nothing here decides
whether to promote: n8n compares the metrics and calls promote() explicitly,
because promotion is the only irreversible act in this phase, and irreversible
acts belong in the layer that owns the credentials and the audit trail.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score

FEATURES = (
    "monthly_spend",
    "sessions_7d",
    "tenure_months",
    "support_tickets",
    "plan_tier",
)


def _connect() -> sqlite3.Connection:
    """Resolve the path per call so tests can repoint it without reimporting."""
    conn = sqlite3.connect(os.getenv("DRIFTBELL_DB", "driftbell.db"))
    conn.row_factory = sqlite3.Row
    return conn


def load_samples(model_name: str) -> tuple[list, list, list, list]:
    """Return X_train, y_train, X_live, y_live for a model."""
    columns = ", ".join(FEATURES)
    conn = _connect()
    try:
        out: list = []
        for split in ("train", "live"):
            rows = conn.execute(
                f"SELECT {columns}, churned FROM samples "
                "WHERE model_name = ? AND split = ?",
                (model_name, split),
            ).fetchall()
            out.append([[row[column] for column in FEATURES] for row in rows])
            out.append([row["churned"] for row in rows])
        return out[0], out[1], out[2], out[3]
    finally:
        conn.close()


def _next_version(conn: sqlite3.Connection, model_name: str) -> str:
    """One past the highest vN in either table, so history is never overwritten."""
    seen = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM registry WHERE model_name = ? "
            "UNION SELECT version FROM runs WHERE model_name = ?",
            (model_name, model_name),
        )
        if row[0]
    ]
    numbers = [
        int(match.group(1))
        for value in seen
        if (match := re.fullmatch(r"v(\d+)", str(value)))
    ]
    return f"v{max(numbers) + 1 if numbers else 1}"


def _next_run_id(conn: sqlite3.Connection) -> str:
    seen = [row[0] for row in conn.execute("SELECT run_id FROM runs")]
    numbers = [
        int(match.group(1))
        for value in seen
        if (match := re.fullmatch(r"run-(\d+)", str(value)))
    ]
    return f"run-{max(numbers) + 1 if numbers else 1:03d}"


def _champion(conn: sqlite3.Connection, model_name: str) -> dict[str, Any]:
    """The incumbent's most recent evaluation.

    Deliberately the latest run rather than the score it was promoted on: the
    question is how the champion performs now, and flattering it with an older
    number would bias every promotion decision against the challenger.
    """
    row = conn.execute(
        "SELECT r.version, r.accuracy, r.f1, r.precision_, r.recall FROM runs r "
        "JOIN registry g ON g.version = r.version AND g.model_name = r.model_name "
        "WHERE g.model_name = ? AND g.stage = 'champion' "
        "ORDER BY r.started_at DESC LIMIT 1",
        (model_name,),
    ).fetchone()
    if row is None:
        return {
            "version": None,
            "accuracy": 0.0,
            "f1": 0.0,
            "precision": 0.0,
            "recall": 0.0,
        }
    return {
        "version": row["version"],
        "accuracy": row["accuracy"],
        "f1": row["f1"],
        "precision": row["precision_"],
        "recall": row["recall"],
    }


def train_challenger(model_name: str) -> dict[str, Any]:
    """Fit on the training split, score on the drifted live split, record the run."""
    x_train, y_train, x_live, y_live = load_samples(model_name)
    if not x_train or not x_live:
        raise ValueError(f"no samples for model {model_name!r}")

    # random_state fixed so the same data always yields the same metrics. A
    # promotion decision that moved between identical runs would be untestable
    # and impossible to defend after the fact.
    classifier = LogisticRegression(max_iter=1000, random_state=42)
    classifier.fit(x_train, y_train)
    predicted = classifier.predict(x_live)

    metrics = {
        "accuracy": round(float(accuracy_score(y_live, predicted)), 4),
        "f1": round(float(f1_score(y_live, predicted, zero_division=0)), 4),
        "precision": round(float(precision_score(y_live, predicted, zero_division=0)), 4),
        "recall": round(float(recall_score(y_live, predicted, zero_division=0)), 4),
    }

    conn = _connect()
    try:
        champion = _champion(conn, model_name)
        version = _next_version(conn, model_name)
        run_id = _next_run_id(conn)
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")

        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                model_name,
                version,
                started,
                metrics["accuracy"],
                metrics["f1"],
                metrics["precision"],
                metrics["recall"],
                "success",
                "challenger trained",
            ),
        )
        # Registered as a challenger, never as champion. Promotion is n8n's call.
        conn.execute(
            "INSERT INTO registry VALUES (?,?,?,?,?)",
            (
                model_name,
                version,
                "challenger",
                started,
                f"models/{model_name}/{version}.joblib",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "model_name": model_name,
        "run_id": run_id,
        "champion": champion,
        "challenger": {"version": version, **metrics},
    }


def promote(model_name: str, version: str) -> dict[str, Any]:
    """Archive the incumbent and make `version` the champion."""
    conn = _connect()
    try:
        known = conn.execute(
            "SELECT COUNT(*) FROM registry WHERE model_name = ? AND version = ?",
            (model_name, version),
        ).fetchone()[0]
        if not known:
            raise ValueError(f"{version} is not in the registry for {model_name}")

        previous = conn.execute(
            "SELECT version FROM registry WHERE model_name = ? AND stage = 'champion'",
            (model_name,),
        ).fetchone()
        conn.execute(
            "UPDATE registry SET stage = 'archived' "
            "WHERE model_name = ? AND stage = 'champion'",
            (model_name,),
        )
        conn.execute(
            "UPDATE registry SET stage = 'champion', promoted_at = ? "
            "WHERE model_name = ? AND version = ?",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                model_name,
                version,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "model_name": model_name,
        "promoted": version,
        "archived": previous["version"] if previous else None,
    }
