"""The test suite builds throwaway databases, so seeding must be callable."""

from __future__ import annotations

import sqlite3

from seed_db import seed

EXPECTED_TABLES = {"runs", "feature_stats", "registry", "incidents"}


def test_seed_creates_every_table(tmp_path) -> None:
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        assert EXPECTED_TABLES <= {r[0] for r in rows}
    finally:
        conn.close()


def test_seed_populates_the_feature_stats_the_agent_reads(tmp_path) -> None:
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM feature_stats WHERE model_name = 'churn_clf'"
        ).fetchone()
    finally:
        conn.close()
    assert count == 5


def test_seed_returns_the_path_it_wrote(tmp_path) -> None:
    db = tmp_path / "driftbell.db"

    assert seed(str(db)) == str(db)
