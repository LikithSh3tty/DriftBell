# Phase 1 — Verify the agent and lock its behaviour in tests

Date: 2026-07-29
Status: approved, not yet implemented

## Problem

`app/` contains a complete-looking LangGraph agent, but it has never run. There
is no virtualenv, no `.env`, no seeded database, and no test suite. Every claim
in `README.md` — that a high-PSI report parks at `human_gate`, that `/resume`
lands on a live graph, that state outlives the process — is currently unproven.

Phases 2 and 3 (n8n import, the Telegram approval loop) both build directly on
the interrupt/resume seam. Building them on unexecuted code means any failure
is ambiguous: n8n misconfiguration and a broken agent look identical from the
n8n canvas.

Phase 1 closes that gap. Nothing new is designed; existing behaviour is
executed, observed, and pinned down.

## Goals

1. The service runs locally and answers `/diagnose` and `/resume` correctly.
2. Three tests prove the human-in-the-loop mechanism, offline, with no API key.
3. `pytest -q` is green and repeatable.

## Non-goals

- No new agent features, nodes, or tools.
- No n8n work (that is Phase 2).
- No real LLM provider. `LLM_PROVIDER=stub` throughout.
- No API-level `TestClient` tests. See "Rejected alternatives".

## Work

### 1. Environment bring-up

```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python seed_db.py
```

The pins in `requirements.txt` (`langgraph==1.2.9`, `langchain-core==1.5.0`,
`fastapi==0.139.2`, `pydantic==2.13.4`, `uvicorn==0.51.0`,
`langgraph-checkpoint-sqlite==3.1.0`) have never been resolved together on this
machine. If a pin is unresolvable or conflicts, report the actual pip error and
decide explicitly — do not silently loosen a pin.

### 2. Manual verification before any test is written

Start `uvicorn app.main:app --reload --port 8000` and drive it by hand:

- `POST /diagnose` with the README's high-PSI `churn_clf` report.
  Expect `status: "awaiting_approval"` and a `thread_id`.
- `POST /resume` with that `thread_id` and `{"decision": "approve"}`.
  Expect `status: "completed"` and an `outcome`.

Tests are written only after the real request path is observed working. Writing
tests first here would risk encoding an assumption about behaviour rather than
the behaviour itself.

### 3. Refactor: make `seed_db.py` importable

`seed_db.py` currently executes at import. Wrap its body in
`seed(db_path: str) -> None` behind an `if __name__ == "__main__":` guard. CLI
behaviour is unchanged.

This is required, not cosmetic. The stub model's first turn emits a
`get_feature_stats` tool call, so **every graph run touches SQLite**. Against an
unseeded database the tool node raises `no such table: feature_stats`. The tests
therefore need a genuinely seeded temporary database, not a mocked tool layer —
mocking the tools would mean the tool cycle is never exercised.

### 4. Test suite — `tests/test_graph.py`

All tests run with `LLM_PROVIDER=stub`, offline, against `tmp_path`.

`tests/conftest.py` provides a fixture that seeds a temp database and points
`app.tools.DB_PATH` at it with `monkeypatch`. `_query` reads that module-level
global at call time, so patching the attribute is sufficient; no import-order
manipulation is needed.

| Test | Asserts |
| --- | --- |
| `test_high_psi_reaches_human_gate` | invoke returns `__interrupt__`; `get_state().next == ("human_gate",)` |
| `test_resumed_thread_completes` | `Command(resume={"decision": "approve"})` yields `outcome["status"] == "approved"` and `outcome["action"] == "RETRAIN"` |
| `test_rebuilt_graph_resumes_same_thread` | graph A runs to the gate and is dropped; a fresh checkpointer + graph B on the same SQLite file still resumes the thread |

The third test builds graph A against a temp SQLite file, runs to the gate,
drops every reference to it, then constructs a brand-new `SqliteSaver` and graph
against the same file and resumes. It proves the claim that matters — state
lives in SQLite, not in the Python object.

`pytest` is added to the existing `# Dev only` block in `requirements.txt`.

## Rejected alternatives

**Subprocess-kill durability test.** Spawning a child process that runs to the
gate and exits would literally prove process death, matching the README trace.
Rejected: slower, harder to debug, and a known source of flakiness on Windows.
The in-process rebuild proves the same underlying property — that no state is
held in the graph object.

**API-level `TestClient` tests.** `app/main.py` constructs `CHECKPOINTER` and
`GRAPH` at import time from environment variables, so testing through FastAPI
requires setting env vars before import. The three graph tests prove the
mechanism and the manual verification in step 2 proves the HTTP wrapper; a
fourth test fighting import order is not worth the maintenance.

**Mocking the tool layer.** Would avoid the seeding work, but the tool cycle is
one of the two cycles this service exists to demonstrate. Mocking it out would
leave the most interesting edge untested.

## Done when

`pytest -q` is green, and `/diagnose` followed by `/resume` has been driven by
hand against a running uvicorn.

## Commits

1. `seed_db: make seeding importable for tests`
2. `Phase 1: pytest suite for the interrupt/resume path`
3. a dependency commit only if the install forces a change to `requirements.txt`
