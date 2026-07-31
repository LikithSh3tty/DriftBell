"""The data the ops chatbot is grounded in.

Runs against a real seeded database and a real checkpoint file. The whole point
of the phase is that answers come from actual recorded history, so mocking the
stores would test nothing worth testing.
"""

from __future__ import annotations

from langgraph.types import Command

from app.graph import build_graph, make_checkpointer
from app.history import (
    incident_documents,
    recent_incidents,
    recent_runs,
    record_incident,
    registry_entries,
    thread_ids,
)

HIGH_PSI_REPORT = {
    "model_name": "churn_clf",
    "psi": 0.284,
    "drifted_features": ["monthly_spend"],
}


def test_recent_runs_are_newest_first(seeded_db) -> None:
    runs = recent_runs()

    assert runs
    started = [r["started_at"] for r in runs]
    assert started == sorted(started, reverse=True)


def test_runs_carry_the_version_for_joining_to_registry(seeded_db) -> None:
    """Without version, a metric cannot be attributed to a model release."""
    runs = recent_runs()

    assert all("version" in r for r in runs)
    assert "v12" in {r["version"] for r in runs}


def test_registry_reports_exactly_one_champion(seeded_db) -> None:
    entries = registry_entries()

    champions = [e for e in entries if e["stage"] == "champion"]
    assert len(champions) == 1


def test_incidents_are_returned(seeded_db) -> None:
    incidents = recent_incidents()

    assert len(incidents) == 2
    assert all("description" in i for i in incidents)


def test_incident_documents_carry_source_metadata(seeded_db) -> None:
    """An answer has to be able to say where it came from."""
    documents = incident_documents()

    assert documents
    for document in documents:
        assert document["text"]
        assert document["metadata"]["source"] == "incident"
        assert document["metadata"]["severity"]


def test_thread_ids_lists_parked_threads(tmp_path, seeded_db) -> None:
    """Proposals live in the checkpoint store, and nothing else enumerates them."""
    checkpoints = str(tmp_path / "cp.db")
    graph = build_graph(checkpointer=make_checkpointer(checkpoints))
    config = {"configurable": {"thread_id": "hist-parked"}}
    graph.invoke(
        {"thread_id": "hist-parked", "drift_report": HIGH_PSI_REPORT}, config=config
    )

    found = thread_ids(checkpoints)

    assert "hist-parked" in found


def test_thread_ids_includes_completed_threads(tmp_path, seeded_db) -> None:
    checkpoints = str(tmp_path / "cp.db")
    graph = build_graph(checkpointer=make_checkpointer(checkpoints))
    config = {"configurable": {"thread_id": "hist-done"}}
    graph.invoke(
        {"thread_id": "hist-done", "drift_report": HIGH_PSI_REPORT}, config=config
    )
    graph.invoke(Command(resume={"decision": "approve"}), config=config)

    found = thread_ids(checkpoints)

    assert "hist-done" in found


def test_thread_ids_is_empty_when_no_checkpoint_file_exists(tmp_path) -> None:
    """A fresh install has no checkpoints; that is not an error."""
    assert thread_ids(str(tmp_path / "absent.db")) == []


def test_recorded_incident_is_immediately_visible(seeded_db) -> None:
    """day_offset must be 0, or get_pipeline_incidents(n_days=7) will not see it."""
    before = len(recent_incidents())

    record_incident("n8n:Driftbell 01", "high", "Compute drift failed")

    incidents = recent_incidents()
    assert len(incidents) == before + 1
    assert incidents[0]["source"] == "n8n:Driftbell 01"


def test_classification_is_folded_into_the_description(seeded_db) -> None:
    """The consumers are an LLM tool and a vector store; both read prose."""
    stored = record_incident("n8n:X", "low", "Timed out", classification="transient")

    assert stored["description"].startswith("[transient]")
    assert "Timed out" in stored["description"]


def test_description_is_untouched_without_a_classification(seeded_db) -> None:
    """Gemini may have failed; the incident still has to be readable."""
    stored = record_incident("n8n:X", "high", "Something broke")

    assert stored["description"] == "Something broke"


def test_recorded_incident_becomes_a_document(seeded_db) -> None:
    """A failure should reach the ops chatbot's retrieval without extra work."""
    record_incident("n8n:Driftbell 03", "high", "Promotion call refused")

    texts = [d["text"] for d in incident_documents()]
    assert any("Promotion call refused" in t for t in texts)
