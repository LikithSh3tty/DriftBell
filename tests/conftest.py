"""Shared fixtures. Every test here runs offline with no API key."""

from __future__ import annotations

from pathlib import Path

import pytest

from seed_db import seed


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch) -> None:
    """Force the scripted offline model regardless of the developer's shell.

    get_llm() reads LLM_PROVIDER at call time, so setting the variable is
    enough; nothing needs to be imported in a particular order.
    """
    monkeypatch.setenv("LLM_PROVIDER", "stub")


@pytest.fixture
def seeded_db(tmp_path, monkeypatch) -> Path:
    """A throwaway driftbell.db, with the agent's tools pointed at it.

    The stub model opens every run with a get_feature_stats tool call, so the
    graph cannot reach its verdict without real rows. app.tools._query resolves
    the DB_PATH global at call time, so patching the attribute is sufficient.

    app.training resolves DRIFTBELL_DB from the environment per call instead, so
    it needs the variable set too. Without it the training tests would write
    runs and move the champion in the developer's real driftbell.db while
    appearing to pass.
    """
    db = tmp_path / "driftbell.db"
    seed(str(db))
    monkeypatch.setattr("app.tools.DB_PATH", str(db))
    monkeypatch.setenv("DRIFTBELL_DB", str(db))
    return db
