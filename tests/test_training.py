"""Challenger training and champion promotion.

These run against a real seeded SQLite file rather than mocks. The point of the
phase is that a run gets recorded and a registry row moves; mocking the database
would test neither, and both are what n8n later reads to decide anything.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.training import promote, train_challenger


def test_training_records_exactly_one_run(seeded_db) -> None:
    conn = sqlite3.connect(seeded_db)
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    conn.close()

    result = train_challenger("churn_clf")

    conn = sqlite3.connect(seeded_db)
    try:
        after = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        recorded = conn.execute(
            "SELECT version FROM runs WHERE run_id = ?", (result["run_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert after == before + 1
    assert recorded[0] == result["challenger"]["version"]


def test_challenger_version_is_new(seeded_db) -> None:
    """Reusing v12 would overwrite the champion's own evaluation history."""
    result = train_challenger("churn_clf")

    assert result["challenger"]["version"] not in {"v10", "v11", "v12"}


def test_metrics_are_probabilities(seeded_db) -> None:
    result = train_challenger("churn_clf")

    for side in ("champion", "challenger"):
        for key in ("accuracy", "f1", "precision", "recall"):
            assert 0.0 <= result[side][key] <= 1.0, f"{side}.{key} out of range"


def test_champion_metrics_come_from_the_latest_champion_run(seeded_db) -> None:
    """run-014 is the most recent v12 evaluation, and v12 is the champion.

    Comparing against its score at promotion (0.826) would flatter the
    incumbent; what matters is how it performs now.
    """
    result = train_challenger("churn_clf")

    assert result["champion"]["version"] == "v12"
    assert result["champion"]["f1"] == 0.781


def test_training_is_deterministic(seeded_db) -> None:
    """An unreproducible promotion decision could not be defended or tested."""
    first = train_challenger("churn_clf")
    second = train_challenger("churn_clf")

    assert first["challenger"]["f1"] == second["challenger"]["f1"]


def test_challenger_is_registered_but_not_promoted(seeded_db) -> None:
    """Training must never move the champion; that is n8n's decision alone."""
    result = train_challenger("churn_clf")

    conn = sqlite3.connect(seeded_db)
    try:
        stage = conn.execute(
            "SELECT stage FROM registry WHERE model_name='churn_clf' AND version = ?",
            (result["challenger"]["version"],),
        ).fetchone()
        champion = conn.execute(
            "SELECT version FROM registry WHERE model_name='churn_clf' AND stage='champion'"
        ).fetchone()
    finally:
        conn.close()
    assert stage[0] == "challenger"
    assert champion[0] == "v12"


def test_promote_leaves_exactly_one_champion(seeded_db) -> None:
    trained = train_challenger("churn_clf")
    version = trained["challenger"]["version"]

    result = promote("churn_clf", version)

    conn = sqlite3.connect(seeded_db)
    try:
        champions = conn.execute(
            "SELECT version FROM registry WHERE model_name='churn_clf' AND stage='champion'"
        ).fetchall()
    finally:
        conn.close()
    assert champions == [(version,)]
    assert result["promoted"] == version
    assert result["archived"] == "v12"


def test_promote_rejects_an_unknown_version(seeded_db) -> None:
    """A typo must not silently invent a champion that was never trained."""
    with pytest.raises(ValueError, match="not in the registry"):
        promote("churn_clf", "v999")
