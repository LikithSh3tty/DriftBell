"""The console's half of the contract: the SSE trace and the static page.

`_trace()` is tested directly rather than through the stream, because it is the
one piece of translation between LangChain's message objects and the browser —
if it stops naming a tool call, the console silently renders an empty run and
nothing else fails. The streaming endpoints are then checked end to end, since
their real risk is the interrupt arriving as a chunk instead of an exception.
"""

from __future__ import annotations

import json
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from seed_db import seed

# Deliberately the same literal as test_api.py. `app.main` reads SERVICE_TOKEN
# once at import, so whichever of the two modules imports it first fixes the
# token for both — a different value here would 401 depending on test order.
TOKEN = "test-service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

REPORT = {
    "model_name": "churn_clf",
    "psi": 0.284,
    "ks_statistic": 0.19,
    "p_value": 0.002,
    "drifted_features": ["monthly_spend"],
    "window_start": "2026-07-09",
    "window_end": "2026-07-23",
    "n_samples": 18422,
}


@pytest.fixture(scope="module", autouse=True)
def configured_app(tmp_path_factory):
    """Import `app.main` with a token set, before any test touches it.

    Autouse because the `_trace` tests import the module too and would
    otherwise import it first with SERVICE_TOKEN unset, which disables auth
    and makes the 401 test pass or fail depending on collection order.
    """
    tmp = tmp_path_factory.mktemp("console")
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
        import app.main as main

        yield main
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def client(configured_app) -> Iterator[TestClient]:
    yield TestClient(configured_app.app)


def frames(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event name, payload) pairs."""
    out = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        out.append((name, json.loads(data)))
    return out


# --------------------------------------------------------------------------- #
# _trace
# --------------------------------------------------------------------------- #

def test_trace_names_the_tool_being_called() -> None:
    """The console shows which tool the agent reached for; the name is the point."""
    from app.main import _trace

    message = AIMessage(
        content="",
        tool_calls=[{"name": "get_recent_runs", "args": {"model_name": "churn_clf"}, "id": "1"}],
    )

    event = _trace("reason", {"messages": [message]})

    assert event["node"] == "reason"
    assert event["lines"] == [
        {"kind": "tool_call", "name": "get_recent_runs", "args": {"model_name": "churn_clf"}}
    ]


def test_trace_carries_tool_results_back() -> None:
    from app.main import _trace

    event = _trace(
        "tools",
        {"messages": [ToolMessage(content="[]", name="get_recent_runs", tool_call_id="1")]},
    )

    assert event["lines"] == [{"kind": "tool_result", "name": "get_recent_runs", "text": "[]"}]


def test_trace_drops_the_system_prompt() -> None:
    """It is the same 60 words on every run and tells a watcher nothing."""
    from app.main import _trace

    event = _trace("gather_evidence", {"messages": [SystemMessage(content="You are...")]})

    assert event["lines"] == []


def test_trace_truncates_a_long_tool_result() -> None:
    """A tool can return the whole feature table; the browser gets a bounded slice."""
    from app.main import MAX_TRACE_CHARS, _trace

    huge = ToolMessage(content="x" * 5000, name="get_feature_stats", tool_call_id="1")

    event = _trace("tools", {"messages": [huge]})

    assert len(event["lines"][0]["text"]) == MAX_TRACE_CHARS


def test_trace_marks_the_interrupt_as_frozen() -> None:
    """This is the event the console draws the freeze band on."""
    from app.main import _trace

    payload = {"verdict": "RETRAIN", "confidence": 0.82}

    event = _trace("__interrupt__", [type("I", (), {"value": payload})()])

    assert event["node"] == "human_gate"
    assert event["frozen"] is True
    assert event["proposal"] == payload


def test_trace_passes_the_verdict_through() -> None:
    from app.main import _trace

    event = _trace("propose", {"verdict": "IGNORE", "confidence": 0.4, "rationale": "noise"})

    assert event["verdict"] == "IGNORE"
    assert event["confidence"] == 0.4


# --------------------------------------------------------------------------- #
# The streaming endpoints
# --------------------------------------------------------------------------- #

def test_stream_reports_every_node_then_freezes(client: TestClient) -> None:
    response = client.post("/diagnose/stream", json={"drift_report": REPORT}, headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = frames(response.text)
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert names[-1] == "done"

    nodes = [data["node"] for name, data in events if name == "node"]
    assert nodes[0] == "gather_evidence"
    assert "reason" in nodes and "tools" in nodes and "propose" in nodes
    assert nodes[-1] == "human_gate"

    done = events[-1][1]
    assert done["status"] == "awaiting_approval"
    assert done["proposal"]["verdict"] == "RETRAIN"


def test_stream_resume_runs_the_nodes_below_the_gate(client: TestClient) -> None:
    """The console's Approve button lands here, on the same thread it froze."""
    start = frames(
        client.post("/diagnose/stream", json={"drift_report": REPORT}, headers=AUTH).text
    )
    thread_id = start[0][1]["thread_id"]

    response = client.post(
        "/resume/stream",
        json={"thread_id": thread_id, "decision": "approve", "note": "from console"},
        headers=AUTH,
    )

    events = frames(response.text)
    assert "execute" in [data["node"] for name, data in events if name == "node"]

    done = events[-1][1]
    assert done["status"] == "completed"
    assert done["decision"] == "approve"
    assert done["outcome"]["status"] == "approved"


def test_stream_rejects_a_missing_token(client: TestClient) -> None:
    response = client.post("/diagnose/stream", json={"drift_report": REPORT})

    assert response.status_code == 401


def test_resume_stream_404s_on_an_unknown_thread(client: TestClient) -> None:
    response = client.post(
        "/resume/stream",
        json={"thread_id": "drift-does-not-exist", "decision": "approve"},
        headers=AUTH,
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Serving the page
# --------------------------------------------------------------------------- #

def test_console_is_served_without_a_token(client: TestClient) -> None:
    """The page itself is inert; every call it makes is authenticated separately."""
    response = client.get("/console/")

    assert response.status_code == 200
    assert "Driftbell" in response.text


def test_root_redirects_to_the_console(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/console/"


def test_mounting_the_console_did_not_shadow_the_api(client: TestClient) -> None:
    """The mount is last and scoped, so /health must still be /health."""
    assert client.get("/health").json()["status"] == "ok"
