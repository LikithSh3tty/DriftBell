"""The human-in-the-loop seam.

These three tests cover the only claim the rest of Driftbell rests on: a run
freezes at human_gate, a decision resumes it, and the frozen state lives in
SQLite rather than in the Python object that created it.
"""

from __future__ import annotations

from langgraph.types import Command

from app.graph import build_graph, make_checkpointer

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


def test_high_psi_report_parks_at_the_human_gate(tmp_path, seeded_db):
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


def test_approved_thread_runs_to_completion(tmp_path, seeded_db):
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


def test_rebuilt_graph_resumes_a_thread_it_never_started(tmp_path, seeded_db):
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
    del graph_a  # every in-memory reference to the first run is now gone

    graph_b = build_graph(checkpointer=make_checkpointer(checkpoints))
    assert graph_b.get_state(config).next == ("human_gate",)

    result = graph_b.invoke(Command(resume={"decision": "approve"}), config=config)

    assert result["outcome"]["status"] == "approved"
    assert result["outcome"]["action"] == "RETRAIN"
