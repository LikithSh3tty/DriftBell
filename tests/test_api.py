"""The HTTP contract n8n branches on.

`app/main.py` builds its checkpointer, graph and auth token at import time from
environment variables, so this module sets those before importing it — which is
why the fixture below is module-scoped and why nothing else in the suite may
import `app.main`.

These tests exist because `_shape()` and `_auth()` ARE the integration contract:
n8n's Switch node routes on `status`, its Wait node resumes against `thread_id`,
and its error workflow depends on the 401 and 404 paths. A change to any of
those shapes breaks the canvas, not the agent, and the canvas is the harder
place to debug it.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from seed_db import seed

TOKEN = "test-service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

HIGH_PSI_REPORT = {
    "model_name": "churn_clf",
    "psi": 0.284,
    "ks_statistic": 0.19,
    "p_value": 0.002,
    "drifted_features": ["monthly_spend", "sessions_7d"],
    "window_start": "2026-07-09",
    "window_end": "2026-07-23",
    "n_samples": 18422,
}


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> Iterator[TestClient]:
    """A TestClient over a throwaway database, checkpoint file and token.

    Module-scoped because `app.main` reads its configuration once at import.
    The environment is restored afterwards so this module cannot leak state
    into the graph-level tests.
    """
    tmp = tmp_path_factory.mktemp("api")
    overrides = {
        "DRIFTBELL_DB": str(tmp / "driftbell.db"),
        "CHECKPOINT_DB": str(tmp / "checkpoints.db"),
        "SERVICE_TOKEN": TOKEN,
        "LLM_PROVIDER": "stub",
    }
    seed(overrides["DRIFTBELL_DB"])

    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        from app.main import app

        yield TestClient(app)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_health_is_reachable_without_a_token(client: TestClient) -> None:
    """The n8n error workflow polls this, and it has no credential attached."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "stub"}


def test_diagnose_rejects_a_missing_token(client: TestClient) -> None:
    response = client.post("/diagnose", json={"drift_report": HIGH_PSI_REPORT})

    assert response.status_code == 401


def test_diagnose_rejects_a_wrong_token(client: TestClient) -> None:
    response = client.post(
        "/diagnose",
        json={"drift_report": HIGH_PSI_REPORT},
        headers={"Authorization": "Bearer not-the-token"},
    )

    assert response.status_code == 401


def test_full_approval_flow_over_http(client: TestClient) -> None:
    """diagnose -> resume -> threads, the exact sequence workflow 01 performs."""
    diagnosed = client.post(
        "/diagnose", json={"drift_report": HIGH_PSI_REPORT}, headers=AUTH
    )
    assert diagnosed.status_code == 200
    body = diagnosed.json()
    # n8n's Switch node branches on this string; awaiting_approval goes to Wait.
    assert body["status"] == "awaiting_approval"
    thread_id = body["thread_id"]
    assert thread_id.startswith("drift-")
    assert body["proposal"]["verdict"] == "RETRAIN"

    resumed = client.post(
        "/resume",
        json={"thread_id": thread_id, "decision": "approve", "note": "approved in Telegram"},
        headers=AUTH,
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["outcome"] == {
        "status": "approved",
        "action": "RETRAIN",
        "note": "approved in Telegram",
        "model_name": "churn_clf",
    }

    audited = client.get(f"/threads/{thread_id}", headers=AUTH)
    assert audited.status_code == 200
    audit = audited.json()
    assert audit["next_nodes"] == []
    assert audit["human_decision"] == "approve"
    assert audit["evidence"]


def test_resume_on_an_unknown_thread_is_404(client: TestClient) -> None:
    response = client.post(
        "/resume",
        json={"thread_id": "drift-does-not-exist", "decision": "approve"},
        headers=AUTH,
    )

    assert response.status_code == 404


def test_threads_on_an_unknown_thread_is_404(client: TestClient) -> None:
    response = client.get("/threads/drift-does-not-exist", headers=AUTH)

    assert response.status_code == 404


def test_approved_resume_reports_decision_approve(client: TestClient) -> None:
    """n8n branches on this string, so it is a contract rather than a convenience."""
    diagnosed = client.post(
        "/diagnose", json={"drift_report": HIGH_PSI_REPORT}, headers=AUTH
    )
    thread_id = diagnosed.json()["thread_id"]

    resumed = client.post(
        "/resume",
        json={"thread_id": thread_id, "decision": "approve", "note": "ok"},
        headers=AUTH,
    )

    assert resumed.json()["decision"] == "approve"


def test_rejected_resume_reports_decision_reject(client: TestClient) -> None:
    """Reject and approve both report status completed, so only this tells them apart."""
    diagnosed = client.post(
        "/diagnose", json={"drift_report": HIGH_PSI_REPORT}, headers=AUTH
    )
    thread_id = diagnosed.json()["thread_id"]

    resumed = client.post(
        "/resume",
        json={"thread_id": thread_id, "decision": "reject", "note": "seasonal"},
        headers=AUTH,
    )

    body = resumed.json()
    assert body["decision"] == "reject"
    assert body["status"] == "completed"
    assert body["outcome"] == {}  # execute never ran on a rejection


def test_awaiting_approval_carries_no_decision(client: TestClient) -> None:
    """Nothing has been decided yet, so the field must not claim otherwise."""
    diagnosed = client.post(
        "/diagnose", json={"drift_report": HIGH_PSI_REPORT}, headers=AUTH
    )

    body = diagnosed.json()
    assert body["status"] == "awaiting_approval"
    assert "decision" not in body
