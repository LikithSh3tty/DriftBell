"""Create driftbell.db with synthetic run history, feature stats and incidents.

Run once: python seed_db.py

seed() is importable rather than top-level script code so the test suite can
build a throwaway database with the same schema and rows the agent sees in
development. The agent's first tool call reads feature_stats on every run, so
tests need real rows, not a mocked tool layer.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS feature_stats;
DROP TABLE IF EXISTS registry;
DROP TABLE IF EXISTS incidents;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, model_name TEXT, started_at TEXT,
    accuracy REAL, f1 REAL, precision_ REAL, recall REAL,
    status TEXT, notes TEXT
);
CREATE TABLE feature_stats (
    model_name TEXT, feature TEXT, psi REAL, null_rate REAL,
    mean_train REAL, mean_live REAL, day_offset INTEGER
);
CREATE TABLE registry (
    model_name TEXT, version TEXT, stage TEXT, promoted_at TEXT, artifact_uri TEXT
);
CREATE TABLE incidents (
    occurred_at TEXT, source TEXT, severity TEXT, description TEXT, day_offset INTEGER
);
"""


def seed(db_path: str | None = None) -> str:
    """Write the synthetic MLOps history and return the path written to.

    Timestamps are generated relative to now, so a freshly seeded database
    always looks like it describes the last two months rather than whenever
    this file was written.
    """
    db = db_path or os.getenv("DRIFTBELL_DB", "driftbell.db")
    now = datetime.now(timezone.utc)

    def iso(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")

    conn = sqlite3.connect(db)
    try:
        c = conn.cursor()
        c.executescript(SCHEMA)

        c.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("run-014", "churn_clf", iso(2), 0.842, 0.781, 0.796, 0.767, "success", "scheduled eval"),
                ("run-013", "churn_clf", iso(9), 0.869, 0.812, 0.828, 0.797, "success", "scheduled eval"),
                ("run-012", "churn_clf", iso(16), 0.877, 0.826, 0.841, 0.812, "success", "champion promoted"),
                ("run-011", "churn_clf", iso(23), 0.874, 0.821, 0.836, 0.807, "success", "challenger rejected"),
                ("run-010", "churn_clf", iso(30), 0.871, 0.818, 0.833, 0.804, "success", "baseline"),
            ],
        )

        c.executemany(
            "INSERT INTO feature_stats VALUES (?,?,?,?,?,?,?)",
            [
                ("churn_clf", "monthly_spend", 0.284, 0.001, 71.4, 58.9, 3),
                ("churn_clf", "sessions_7d", 0.212, 0.002, 12.6, 9.3, 3),
                ("churn_clf", "tenure_months", 0.041, 0.000, 18.2, 18.6, 3),
                ("churn_clf", "support_tickets", 0.033, 0.004, 0.71, 0.74, 3),
                ("churn_clf", "plan_tier", 0.019, 0.000, 2.11, 2.09, 3),
            ],
        )

        c.executemany(
            "INSERT INTO registry VALUES (?,?,?,?,?)",
            [
                ("churn_clf", "v12", "champion", iso(16), "models/churn_clf/v12.joblib"),
                ("churn_clf", "v11", "archived", iso(30), "models/churn_clf/v11.joblib"),
                ("churn_clf", "v10", "archived", iso(58), "models/churn_clf/v10.joblib"),
            ],
        )

        c.executemany(
            "INSERT INTO incidents VALUES (?,?,?,?,?)",
            [
                (iso(5), "billing_etl", "low", "Late-arriving batch, backfilled the same day", 5),
                (iso(21), "events_stream", "high", "Six hours of dropped session events", 21),
            ],
        )

        conn.commit()
    finally:
        conn.close()

    return db


if __name__ == "__main__":
    path = seed()
    print(f"Seeded {path}: 5 runs, 5 features, 3 registry entries, 2 incidents.")
