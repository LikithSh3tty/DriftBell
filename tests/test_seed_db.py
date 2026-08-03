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


def test_seed_creates_both_sample_splits(tmp_path) -> None:
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        rows = dict(
            conn.execute(
                "SELECT split, COUNT(*) FROM samples WHERE model_name='churn_clf' GROUP BY split"
            ).fetchall()
        )
    finally:
        conn.close()
    assert rows["train"] > 500
    assert rows["live"] > 200


def test_live_split_is_actually_drifted(tmp_path) -> None:
    """The challenger must be scored on data that differs from training.

    If both splits came from one distribution the retrain would be theatre, so
    this pins the shift rather than trusting the generator to have applied it.
    """
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        means = {
            split: conn.execute(
                "SELECT AVG(monthly_spend) FROM samples WHERE split = ?", (split,)
            ).fetchone()[0]
            for split in ("train", "live")
        }
    finally:
        conn.close()
    assert means["train"] - means["live"] > 5.0


def test_runs_carry_a_version(tmp_path) -> None:
    """Without this column, 'the champion's F1' cannot be answered at all."""
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        versions = {r[0] for r in conn.execute("SELECT version FROM runs")}
    finally:
        conn.close()
    assert "v12" in versions


def test_if_empty_preserves_existing_rows(tmp_path) -> None:
    """The container seeds on every start; it must not wipe a recorded run."""
    db = tmp_path / "driftbell.db"
    seed(str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO runs VALUES ('run-999','churn_clf','v99','2026-01-01',"
        "0.9,0.9,0.9,0.9,'success','from a previous container')"
    )
    conn.commit()
    conn.close()

    seed(str(db), if_empty=True)

    conn = sqlite3.connect(db)
    try:
        survived = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id='run-999'").fetchone()[0]
    finally:
        conn.close()
    assert survived == 1


def test_plain_seed_still_resets(tmp_path) -> None:
    """`python seed_db.py` with no flags is the documented way to reset."""
    db = tmp_path / "driftbell.db"
    seed(str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO runs VALUES ('run-999','churn_clf','v99','2026-01-01',"
        "0.9,0.9,0.9,0.9,'success','stale')"
    )
    conn.commit()
    conn.close()

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        survived = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id='run-999'").fetchone()[0]
    finally:
        conn.close()
    assert survived == 0


def test_if_empty_reseeds_a_stale_schema(tmp_path) -> None:
    """A pre-Phase-4 database has runs but no samples table.

    Treating "has rows" as "already seeded" preserved that stale schema and left
    the agent to fail at runtime on a missing table. This is the migration case,
    and it happened for real against the container volume.
    """
    db = tmp_path / "driftbell.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (run_id TEXT, model_name TEXT, started_at TEXT);"
        "INSERT INTO runs VALUES ('run-001','churn_clf','2026-01-01');"
    )
    conn.commit()
    conn.close()

    seed(str(db), if_empty=True)

    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        samples = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    finally:
        conn.close()
    assert "samples" in tables
    assert samples == 2100
