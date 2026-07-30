# Phase 5a — Ops Chatbot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ask Driftbell questions about its own history and get answers grounded in real runs, promotions, incidents and past reasoning.

**Architecture:** A new `app/history.py` exposes the data n8n cannot otherwise reach, behind two endpoints: `/history` for structured facts and `/history/documents` for the free text worth embedding. Workflow 04 indexes the documents into an in-memory vector store, then an AI Agent answers using both a retrieval tool and an HTTP tool.

**Tech Stack:** FastAPI, SQLite, LangGraph checkpoints, n8n 2.32.6 AI nodes, Google Gemini (chat + embeddings).

**Spec:** `docs/superpowers/specs/2026-07-31-phase5a-ops-chatbot-design.md`

## Global Constraints

- **Zero paid services.** Gemini free tier only. `LLM_PROVIDER=stub` must keep working offline with no key — the agent's own provider does not change.
- **The agent never performs irreversible actions.** Both new endpoints are read-only.
- **`interrupt()` stays in `human_gate` only.** No task here touches the graph's structure.
- **n8n reaches the agent at `http://driftbell:8000`** — container name, never `localhost`.
- **Never hand-write n8n workflow JSON and assume it works.** Generate, import, fix in the UI, export, commit the export.
- **The Gemini API key lives in n8n's credential store only** — never in committed workflow JSON, never in `.env`, never in a commit message.
- **`executeWorkflow` must be `typeVersion` 1.1**, and `waitForSubWorkflow` lives inside `options`. (Not used here, but it is how this repo's workflows call each other.)
- Python 3.11+, type hints on function signatures, docstrings that say *why*.
- Commit messages carry NO Claude attribution trailers.
- Platform is Windows; `docker compose exec` with `/tmp/...` paths needs `MSYS_NO_PATHCONV=1` under Git Bash.
- Invoke Python as `.venv\Scripts\python.exe` — activation does not persist between tool calls.

---

### Task 1: Expose the history

Deliverable: `/history` and `/history/documents` return real data from SQLite and the checkpoint store, covered by tests.

**Files:**
- Create: `app/history.py`
- Create: `tests/test_history.py`
- Modify: `app/main.py`

**Interfaces:**
- Consumes: the `runs` / `registry` / `incidents` schema, including the `version` column added in Phase 4.
- Produces:
  - `app.history.recent_runs(limit: int = 20) -> list[dict]`
  - `app.history.registry_entries() -> list[dict]`
  - `app.history.recent_incidents(limit: int = 20) -> list[dict]`
  - `app.history.thread_ids(checkpoint_db: str | None = None, limit: int = 20) -> list[str]`
  - `app.history.incident_documents(limit: int = 20) -> list[dict]`
  - `GET /history` → `{"runs": [...], "registry": [...], "incidents": [...], "proposals": [...]}`
  - `GET /history/documents` → `{"documents": [{"text": str, "metadata": dict}]}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_history.py`:

```python
"""The data the ops chatbot is grounded in.

Runs against a real seeded database and a real checkpoint file. The whole point
is that answers come from actual recorded history, so mocking the stores would
test nothing worth testing.
"""

from __future__ import annotations

from langgraph.types import Command

from app.graph import build_graph, make_checkpointer
from app.history import (
    incident_documents,
    recent_incidents,
    recent_runs,
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
    graph.invoke({"thread_id": "hist-parked", "drift_report": HIGH_PSI_REPORT}, config=config)

    found = thread_ids(checkpoints)

    assert "hist-parked" in found


def test_thread_ids_includes_completed_threads(tmp_path, seeded_db) -> None:
    checkpoints = str(tmp_path / "cp.db")
    graph = build_graph(checkpointer=make_checkpointer(checkpoints))
    config = {"configurable": {"thread_id": "hist-done"}}
    graph.invoke({"thread_id": "hist-done", "drift_report": HIGH_PSI_REPORT}, config=config)
    graph.invoke(Command(resume={"decision": "approve"}), config=config)

    found = thread_ids(checkpoints)

    assert "hist-done" in found
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_history.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.history'`.

- [ ] **Step 3: Write `app/history.py`**

```python
"""Read-only access to everything Driftbell has recorded.

n8n cannot reach the SQLite files on the agent's volume, so the ops chatbot has
no route to run history, promotions, incidents or past reasoning without this.
Kept free of any graph dependency: turning a thread_id into a verdict needs
GRAPH.get_state, which lives in main.py, so that assembly happens there.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any


def _rows(db: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _driftbell_db() -> str:
    """Resolved per call so tests can repoint it without reimporting."""
    return os.getenv("DRIFTBELL_DB", "driftbell.db")


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Training and evaluation history, newest first."""
    return _rows(
        _driftbell_db(),
        "SELECT run_id, model_name, version, started_at, accuracy, f1, "
        "precision_ AS precision, recall, status, notes "
        "FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )


def registry_entries() -> list[dict[str, Any]]:
    """Every known version and its stage, so 'which is champion' is answerable."""
    return _rows(
        _driftbell_db(),
        "SELECT model_name, version, stage, promoted_at FROM registry "
        "ORDER BY promoted_at DESC",
    )


def recent_incidents(limit: int = 20) -> list[dict[str, Any]]:
    return _rows(
        _driftbell_db(),
        "SELECT occurred_at, source, severity, description FROM incidents "
        "ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    )


def incident_documents(limit: int = 20) -> list[dict[str, Any]]:
    """Incident descriptions as embeddable text.

    Only the prose is embedded. The structured columns are served whole by
    /history, because similarity search over a severity enum answers nothing a
    query could not answer better.
    """
    return [
        {
            "text": (
                f"Pipeline incident on {incident['occurred_at']} "
                f"from {incident['source']} ({incident['severity']} severity): "
                f"{incident['description']}"
            ),
            "metadata": {
                "source": "incident",
                "occurred_at": incident["occurred_at"],
                "severity": incident["severity"],
                "system": incident["source"],
            },
        }
        for incident in recent_incidents(limit)
    ]


def thread_ids(checkpoint_db: str | None = None, limit: int = 20) -> list[str]:
    """Distinct thread ids in the checkpoint store, newest first.

    Nothing else enumerates them: /threads/{id} requires an id you already have,
    so without this the agent's own reasoning is unreachable.
    """
    db = checkpoint_db or os.getenv("CHECKPOINT_DB", "checkpoints.db")
    if not os.path.exists(db):
        return []
    rows = _rows(
        db,
        "SELECT thread_id, MAX(rowid) AS latest FROM checkpoints "
        "GROUP BY thread_id ORDER BY latest DESC LIMIT ?",
        (limit,),
    )
    return [row["thread_id"] for row in rows]
```

- [ ] **Step 4: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_history.py -q`
Expected: 7 passed.

- [ ] **Step 5: Add the endpoints**

In `app/main.py`, import the module and add both routes after `/promote`:

```python
from .history import (
    incident_documents,
    recent_incidents,
    recent_runs,
    registry_entries,
    thread_ids,
)


def _proposals(limit: int = 20) -> list[dict[str, Any]]:
    """The agent's own reasoning, recovered from the checkpoint store.

    Assembled here rather than in history.py because turning a thread_id into a
    verdict needs GRAPH, and history.py stays a pure data-access module.
    """
    out: list[dict[str, Any]] = []
    for thread_id in thread_ids(limit=limit):
        snapshot = GRAPH.get_state(_config(thread_id))
        values = snapshot.values
        if not values.get("verdict"):
            continue
        out.append(
            {
                "thread_id": thread_id,
                "model_name": values.get("drift_report", {}).get("model_name"),
                "verdict": values.get("verdict"),
                "confidence": values.get("confidence"),
                "rationale": values.get("rationale"),
                "human_decision": values.get("human_decision") or None,
                "parked": bool(snapshot.next),
                "updated_at": snapshot.created_at,
            }
        )
    return out


@app.get("/history")
def history(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Structured facts for the chatbot's HTTP tool.

    Numbers belong in a query result, not in an embedding — the free text is
    served separately by /history/documents.
    """
    _auth(authorization)
    return {
        "runs": recent_runs(),
        "registry": registry_entries(),
        "incidents": recent_incidents(),
        "proposals": _proposals(),
    }


@app.get("/history/documents")
def history_documents(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Free text worth embedding: proposal rationales and incident descriptions."""
    _auth(authorization)
    documents = [
        {
            "text": (
                f"On {proposal['updated_at']} the agent proposed "
                f"{proposal['verdict']} for {proposal['model_name']} with "
                f"confidence {proposal['confidence']}. Reasoning: "
                f"{proposal['rationale']}"
            ),
            "metadata": {
                "source": "proposal",
                "thread_id": proposal["thread_id"],
                "model_name": proposal["model_name"],
                "verdict": proposal["verdict"],
                "human_decision": proposal["human_decision"],
            },
        }
        for proposal in _proposals()
        if proposal.get("rationale")
    ]
    return {"documents": documents + incident_documents()}
```

- [ ] **Step 6: Test the endpoints**

Append to `tests/test_api.py`:

```python
def test_history_returns_every_section(client: TestClient) -> None:
    response = client.get("/history", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["runs"]
    assert body["incidents"]
    assert [e for e in body["registry"] if e["stage"] == "champion"]


def test_history_surfaces_the_agents_own_proposals(client: TestClient) -> None:
    """A parked thread has reasoning worth answering questions about."""
    client.post("/diagnose", json={"drift_report": HIGH_PSI_REPORT}, headers=AUTH)

    body = client.get("/history", headers=AUTH).json()

    assert body["proposals"]
    assert any(p["verdict"] == "RETRAIN" for p in body["proposals"])


def test_documents_are_prose_not_structured_rows(client: TestClient) -> None:
    """Only free text gets embedded; metrics are served by /history instead."""
    client.post("/diagnose", json={"drift_report": HIGH_PSI_REPORT}, headers=AUTH)

    documents = client.get("/history/documents", headers=AUTH).json()["documents"]

    assert documents
    sources = {d["metadata"]["source"] for d in documents}
    assert sources <= {"proposal", "incident"}
    assert all(len(d["text"]) > 40 for d in documents)


def test_history_rejects_a_missing_token(client: TestClient) -> None:
    assert client.get("/history").status_code == 401
    assert client.get("/history/documents").status_code == 401
```

- [ ] **Step 7: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: **45 passed** (34 existing + 7 history + 4 API).

- [ ] **Step 8: Commit**

```powershell
git add app/history.py app/main.py tests/test_history.py tests/test_api.py
git commit -m "Expose run history, incidents and past reasoning over HTTP"
```

---

### Task 2: Workflow 04, the ops chatbot

Deliverable: a chat interface answering questions from real history.

**Files:**
- Create: `workflows/04-ops-agent.json`

**Interfaces:**
- Consumes: `GET /history` and `GET /history/documents` from Task 1.

**This task is UI-led, deliberately.** n8n's AI nodes connect through `ai_languageModel`, `ai_memory`, `ai_tool` and `ai_embedding` ports rather than ordinary `main` connections, and the Gemini credential can only be attached in the UI. Hand-writing that JSON is exactly what the repo's rule forbids. The workflow is therefore assembled on the canvas and exported.

- [ ] **Step 1: Rebuild and restart the agent**

```bash
docker compose up -d --build --force-recreate driftbell
```

Then confirm the new endpoints answer from n8n's network:

```bash
docker compose exec -T n8n sh -c 'wget -qO- http://driftbell:8000/history/documents' | head -c 400
```

Expected: JSON with a `documents` array. If it is empty, run a diagnose first so there is at least one proposal to embed.

- [ ] **Step 2: Add the Gemini credential**

In n8n: **Credentials → New → Google Gemini (PaLM) API**, paste the key from
<https://aistudio.google.com/apikey>, save. n8n tests the connection on save.

- [ ] **Step 3: Build the indexing branch**

New workflow named `Driftbell 04 — ops agent`. Three nodes, ordinary `main` connections:

1. **Manual Trigger**
2. **HTTP Request** — GET `http://driftbell:8000/history/documents`
3. **Split Out** — field to split out: `documents`
4. **Simple Vector Store** in **Insert Documents** mode, **Memory Key** `driftbell`, with a **Default Data Loader** sub-node reading `text` and an **Embeddings Google Gemini** sub-node using the credential

- [ ] **Step 4: Build the chat branch**

1. **Chat Trigger**
2. **AI Agent**, with these sub-nodes attached:
   - **Google Gemini Chat Model** (credential from Step 2)
   - **Simple Memory**
   - **Simple Vector Store** in **Retrieve Documents (As Tool)** mode, Memory Key `driftbell`, described as *"Past agent reasoning and pipeline incidents, in prose"*
   - **HTTP Request Tool** — GET `http://driftbell:8000/history`, described as *"Structured run history, model registry and incident records. Use for any question about numbers, versions or dates."*

Give the agent this system message, so it uses each tool for what it is good at:

```
You are Driftbell's operations assistant. Answer only from the tools.
Use the structured history tool for anything numeric: metrics, versions,
dates, which model is champion. Use the retrieval tool for questions about
why something happened. If the tools do not contain the answer, say so
rather than guessing.
```

- [ ] **Step 5: Index, then ask**

Run the Manual Trigger branch once to populate the store. Then open the chat and ask:

- *"Which model version is champion and what is its F1?"* — should come from the HTTP tool with the real number.
- *"Why was churn_clf retrained?"* — should come from retrieval and quote the agent's own rationale.
- *"Were there any pipeline incidents recently?"* — should surface the two seeded incidents.

If the retrieval tool returns nothing, the memory key does not match between the two Simple Vector Store nodes. That mismatch fails silently with no error, so check it first.

- [ ] **Step 6: Export and commit**

Export from the UI, strip the instance-specific keys (`id`, `versionId`, `versionCounter`, `activeVersionId`, `createdAt`, `updatedAt`, `shared`, `staticData`, `meta`, `tags`, `triggerCount`, `isArchived`, `sourceWorkflowId`, `nodeGroups`, `versionMetadata`, `description`), keeping only `name`, `nodes`, `connections`, `settings`, `pinData`, `active`.

Confirm the API key did not travel with the export — it should be referenced by credential id and name only:

```bash
grep -ci "AIza" workflows/04-ops-agent.json
```

Expected: `0`. If not, stop and do not commit.

```powershell
git add workflows/04-ops-agent.json
git commit -m "Add workflow 04: the ops chatbot over run history"
```

---

## Done when

The chat answers "which version is champion and what is its F1" with the number actually in the registry, and "why was churn_clf retrained" by quoting the agent's own recorded reasoning. `pytest -q` reports 45 passed.
