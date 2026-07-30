"""Create driftbell.db with synthetic run history, feature stats and incidents.

Run once: python seed_db.py

seed() is importable rather than top-level script code so the test suite can
build a throwaway database with the same schema and rows the agent sees in
development. The agent's first tool call reads feature_stats on every run, so
tests need real rows, not a mocked tool layer.
"""

from __future__ import annotations

import math
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS feature_stats;
DROP TABLE IF EXISTS registry;
DROP TABLE IF EXISTS incidents;
DROP TABLE IF EXISTS samples;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, model_name TEXT, version TEXT, started_at TEXT,
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
CREATE TABLE samples (
    model_name TEXT, split TEXT,
    monthly_spend REAL, sessions_7d REAL, tenure_months REAL,
    support_tickets REAL, plan_tier REAL, churned INTEGER
);
"""


EXPECTED_TABLES = frozenset(
    {"runs", "feature_stats", "registry", "incidents", "samples"}
)


def _draw_samples(rng: random.Random, n: int, drifted: bool) -> list[tuple]:
    """Draw n labelled rows; `drifted` shifts the two features PSI reports on.

    The means match feature_stats exactly — monthly_spend 71.4 train / 58.9
    live, sessions_7d 12.6 / 9.3 — so a challenger is scored against the same
    drift the monitor flags and the agent diagnoses, rather than a reshuffle.
    """
    spend_mean = 58.9 if drifted else 71.4
    sessions_mean = 9.3 if drifted else 12.6
    rows = []
    for _ in range(n):
        spend = rng.gauss(spend_mean, 18.0)
        sessions = rng.gauss(sessions_mean, 4.0)
        tenure = rng.gauss(18.2, 6.0)
        tickets = max(0.0, rng.gauss(0.71, 0.6))
        tier = float(rng.choice([1, 2, 3]))
        # Churn rises as spend, sessions and tenure fall, and with more tickets.
        logit = (
            -0.055 * (spend - 65.0)
            - 0.170 * (sessions - 11.0)
            - 0.045 * (tenure - 18.0)
            + 0.55 * tickets
        )
        churned = 1 if rng.random() < 1.0 / (1.0 + math.exp(-logit)) else 0
        rows.append(
            (
                round(spend, 4),
                round(sessions, 4),
                round(tenure, 4),
                round(tickets, 4),
                tier,
                churned,
            )
        )
    return rows


def seed(db_path: str | None = None, if_empty: bool = False) -> str:
    """Write the synthetic MLOps history and return the path written to.

    Timestamps are generated relative to now, so a freshly seeded database
    always looks like it describes the last two months rather than whenever
    this file was written.

    With if_empty=True an already-populated database is left untouched. The
    container seeds on every start, and an unconditional reseed would drop the
    runs row and registry promotion that a retrain exists to produce.
    """
    db = db_path or os.getenv("DRIFTBELL_DB", "driftbell.db")
    now = datetime.now(timezone.utc)

    def iso(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")

    conn = sqlite3.connect(db)
    try:
        c = conn.cursor()

        if if_empty:
            # "Already seeded" has to mean seeded with the CURRENT schema, not
            # merely non-empty. A database written before the samples table
            # existed has rows in runs and would otherwise be preserved as-is,
            # leaving the agent to fail at runtime on a missing table.
            tables = {
                row[0]
                for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            already_seeded = (
                EXPECTED_TABLES <= tables
                and c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] > 0
            )
            if already_seeded:
                return db

        c.executescript(SCHEMA)

        # Versions tie each set of metrics to a registry entry. The three most
        # recent runs all evaluate v12, so the champion's current F1 is the
        # latest of them (0.781) rather than its score at promotion (0.826).
        c.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("run-014", "churn_clf", "v12", iso(2), 0.842, 0.781, 0.796, 0.767, "success", "scheduled eval"),
                ("run-013", "churn_clf", "v12", iso(9), 0.869, 0.812, 0.828, 0.797, "success", "scheduled eval"),
                ("run-012", "churn_clf", "v12", iso(16), 0.877, 0.826, 0.841, 0.812, "success", "champion promoted"),
                ("run-011", "churn_clf", "v11", iso(23), 0.874, 0.821, 0.836, 0.807, "success", "challenger rejected"),
                ("run-010", "churn_clf", "v10", iso(30), 0.871, 0.818, 0.833, 0.804, "success", "baseline"),
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

        # Fixed seed so a reseed is reproducible and a challenger's metrics can
        # be compared across runs without the data shifting underneath them.
        rng = random.Random(20260730)
        c.executemany(
            "INSERT INTO samples VALUES ('churn_clf','train',?,?,?,?,?,?)",
            _draw_samples(rng, 1500, drifted=False),
        )
        c.executemany(
            "INSERT INTO samples VALUES ('churn_clf','live',?,?,?,?,?,?)",
            _draw_samples(rng, 600, drifted=True),
        )

        conn.commit()
    finally:
        conn.close()

    return db


if __name__ == "__main__":
    import sys

    # The container passes --if-empty so restarts preserve recorded runs;
    # a bare `python seed_db.py` still resets, as CLAUDE.md documents.
    path = seed(if_empty="--if-empty" in sys.argv)

    # Counted rather than hardcoded: with --if-empty this may have preserved an
    # existing database, and a fixed message would claim rows it did not write.
    conn = sqlite3.connect(path)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in sorted(EXPECTED_TABLES)
        }
    finally:
        conn.close()
    print(f"{path}: " + ", ".join(f"{n} {table}" for table, n in counts.items()))
