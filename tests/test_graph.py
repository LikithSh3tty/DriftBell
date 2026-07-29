"""The human-in-the-loop seam.

These tests cover the only claim the rest of Driftbell rests on: a run freezes
at human_gate, a decision resumes it, and the frozen state lives in SQLite
rather than in the Python object that created it. They also pin the two ways a
run reaches END without anything being executed — a human rejection, and an
IGNORE verdict that never asks a human at all.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from langchain_core.messages import AIMessage
from langgraph.types import Command

from app.graph import build_graph, make_checkpointer
from app.llm import StubChatModel

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


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class IgnoreVerdictModel(StubChatModel):
    """The stub, but it concludes IGNORE instead of RETRAIN.

    The shipped stub always returns RETRAIN, which leaves the IGNORE branch of
    route_after_propose unreachable — and that branch is the one that decides a
    human is never consulted at all. Overriding only the final-verdict reply
    keeps the tool cycle and the critique loop exercised exactly as the real
    stub exercises them.
    """

    def bind_tools(self, tools: Sequence[Any]) -> "IgnoreVerdictModel":
        return IgnoreVerdictModel(tools)

    def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        if "final verdict" in text.lower():
            return AIMessage(
                content=json.dumps(
                    {
                        "verdict": "IGNORE",
                        "confidence": 0.64,
                        "rationale": (
                            "PSI sits just over the alert threshold and lines up "
                            "with a known seasonal shift; no action warranted."
                        ),
                    }
                )
            )
        return super().invoke(messages, **kwargs)


def test_high_psi_report_parks_at_the_human_gate(tmp_path, seeded_db) -> None:
    graph = build_graph(checkpointer=make_checkpointer(str(tmp_path / "cp.db")))
    config = _config("test-gate")

    result = graph.invoke(
        {"thread_id": "test-gate", "drift_report": HIGH_PSI_REPORT}, config=config
    )

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["verdict"] == "RETRAIN"
    assert payload["model_name"] == "churn_clf"
    assert graph.get_state(config).next == ("human_gate",)


def test_approved_thread_runs_to_completion(tmp_path, seeded_db) -> None:
    graph = build_graph(checkpointer=make_checkpointer(str(tmp_path / "cp.db")))
    config = _config("test-resume")
    graph.invoke(
        {"thread_id": "test-resume", "drift_report": HIGH_PSI_REPORT}, config=config
    )

    result = graph.invoke(
        Command(resume={"decision": "approve", "note": "approved in Telegram"}),
        config=config,
    )

    assert "__interrupt__" not in result
    assert result["outcome"] == {
        "status": "approved",
        "action": "RETRAIN",
        "note": "approved in Telegram",
        "model_name": "churn_clf",
    }
    assert graph.get_state(config).next == ()


def test_rebuilt_graph_resumes_a_thread_it_never_started(tmp_path, seeded_db) -> None:
    """The claim the n8n Wait node depends on.

    An approval can arrive hours after the process that produced the proposal
    has gone. Nothing may be held in the graph object.
    """
    checkpoints = str(tmp_path / "cp.db")
    config = _config("test-durable")

    graph_a = build_graph(checkpointer=make_checkpointer(checkpoints))
    graph_a.invoke(
        {"thread_id": "test-durable", "drift_report": HIGH_PSI_REPORT}, config=config
    )
    del graph_a  # graph_b shares nothing with graph_a but the file on disk

    graph_b = build_graph(checkpointer=make_checkpointer(checkpoints))
    assert graph_b.get_state(config).next == ("human_gate",)

    result = graph_b.invoke(Command(resume={"decision": "approve"}), config=config)

    assert result["outcome"]["status"] == "approved"
    assert result["outcome"]["action"] == "RETRAIN"


def test_rejected_thread_finishes_without_executing(tmp_path, seeded_db) -> None:
    """Reject is the other half of the Telegram card, and it takes a shortcut.

    route_after_gate sends a rejection straight to END without ever running
    `execute`, so the `outcome` key is never written into state at all — it is
    not present-but-empty, it is simply absent. This test pins that, and also
    pins that `verdict` still reads "RETRAIN": a rejected thread keeps the
    verdict the agent proposed, it does not get overwritten to reflect what
    the human decided. That is the trap this test exists to document.
    """
    graph = build_graph(checkpointer=make_checkpointer(str(tmp_path / "cp.db")))
    config = _config("test-reject")
    graph.invoke(
        {"thread_id": "test-reject", "drift_report": HIGH_PSI_REPORT}, config=config
    )

    result = graph.invoke(
        Command(resume={"decision": "reject", "note": "waiting on the on-call lead"}),
        config=config,
    )

    assert "__interrupt__" not in result
    assert graph.get_state(config).next == ()
    assert result["human_decision"] == "reject"
    assert "outcome" not in result  # execute never ran, so the key was never written
    # The verdict is the agent's recommendation, not a record of what happened:
    # a rejected thread keeps proposing RETRAIN even though nothing was executed.
    assert result["verdict"] == "RETRAIN"


def test_ignore_verdict_never_wakes_a_human(tmp_path, seeded_db, monkeypatch) -> None:
    """The branch that decides nobody gets paged.

    route_after_propose sends an IGNORE verdict straight to END, deliberately
    skipping human_gate — the reasoning being that proposing nothing needs no
    approval. That makes it the most safety-relevant edge in the graph and the
    only one where the agent decides *not* to involve a person, so it is worth
    pinning that it neither interrupts nor executes.

    The shipped stub always returns RETRAIN, so this is unreachable without
    substituting a model that concludes otherwise.
    """
    monkeypatch.setattr("app.graph.get_llm", lambda *a, **kw: IgnoreVerdictModel())

    graph = build_graph(checkpointer=make_checkpointer(str(tmp_path / "cp.db")))
    config = _config("test-ignore")

    result = graph.invoke(
        {"thread_id": "test-ignore", "drift_report": HIGH_PSI_REPORT}, config=config
    )

    assert result["verdict"] == "IGNORE"
    # No human was ever asked: the graph ran to completion in a single call.
    assert "__interrupt__" not in result
    assert graph.get_state(config).next == ()
    assert "human_decision" not in result
    # And nothing was executed, so there is no outcome to report to n8n.
    assert "outcome" not in result
