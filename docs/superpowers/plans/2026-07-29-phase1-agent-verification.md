# Phase 1 — Agent Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the Driftbell agent actually runs, then lock its interrupt/resume behaviour into an offline test suite.

**Architecture:** No new features. Bring up the environment, drive `/diagnose` and `/resume` by hand to observe real behaviour, make `seed_db.py` importable so tests can build a throwaway database, then write three graph-level tests that pin the human-in-the-loop mechanism. Tests exercise the real tool cycle against real SQLite — nothing is mocked.

**Tech Stack:** Python 3.11, LangGraph + `SqliteSaver` checkpointer, FastAPI, pytest, SQLite.

**Spec:** `docs/superpowers/specs/2026-07-29-phase1-agent-verification-design.md`

## Global Constraints

- **Zero paid services.** `LLM_PROVIDER=stub` must keep working offline with no key. Every test in this plan runs under the stub.
- **The agent never performs irreversible actions.** It proposes; n8n executes.
- **`interrupt()` stays in `human_gate` only.** No task here adds a pause anywhere else.
- **Do not silently loosen a version pin** in `requirements.txt`. If a pin is unresolvable, stop and report the actual pip error.
- Python 3.11+, type hints on function signatures, docstrings that say *why* rather than restating the code.
- Prefer small, readable modules over clever ones — this is a portfolio project that must be explainable line by line.
- Platform is Windows; use PowerShell syntax for shell steps.
- **`.env` is not loaded by the application.** There is no `python-dotenv` and no `load_dotenv()` call anywhere. Docker Compose reads `.env` for its own `${VAR}` substitution, but a bare `uvicorn` run sees only real shell environment variables and the `os.getenv` defaults (`stub`, `driftbell.db`, `checkpoints.db`). Do not add dotenv in this phase.

---

### Task 1: Environment bring-up and manual verification

Deliverable: the service runs, and the interrupt/resume path has been observed working against a real HTTP request. No test is written until this passes — writing tests first here risks encoding an assumption about the behaviour instead of the behaviour itself.

**Files:**
- Modify (only if the install forces it): `requirements.txt`

**Interfaces:**
- Consumes: nothing.
- Produces: a working `.venv`, a seeded `driftbell.db`, and confirmation that `POST /diagnose` returns `status: "awaiting_approval"` and `POST /resume` returns `status: "completed"`.

- [ ] **Step 1: Create the virtualenv and install dependencies**

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If pip reports that a pinned version does not exist or that two pins conflict, **stop**. Report the verbatim pip error and wait for a decision. Do not edit a pin on your own initiative.

- [ ] **Step 2: Create the local env file**

```powershell
Copy-Item .env.example .env
```

This is for the Docker path in Phase 2 and for your own reference. It does not affect the uvicorn run — see Global Constraints.

- [ ] **Step 3: Seed the development database**

```powershell
.venv\Scripts\python.exe seed_db.py
```

Expected output: `Seeded driftbell.db: 5 runs, 5 features, 3 registry entries, 2 incidents.`

- [ ] **Step 4: Start the service**

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Run this in the background or a second terminal. Expected: `Uvicorn running on http://127.0.0.1:8000`. Confirm health:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Expected: `status: ok`, `provider: stub`.

- [ ] **Step 5: Drive `/diagnose` by hand**

```powershell
$body = @{
    drift_report = @{
        model_name = "churn_clf"
        psi = 0.284
        ks_statistic = 0.19
        p_value = 0.002
        drifted_features = @("monthly_spend", "sessions_7d")
        window_start = "2026-07-09"
        window_end = "2026-07-23"
        n_samples = 18422
    }
} | ConvertTo-Json -Depth 5

$r = Invoke-RestMethod -Uri http://localhost:8000/diagnose -Method Post -Body $body -ContentType "application/json"
$r | ConvertTo-Json -Depth 5
```

Expected: `status` is `awaiting_approval`, `thread_id` looks like `drift-<hex>`, and `proposal.verdict` is `RETRAIN`.

- [ ] **Step 6: Drive `/resume` by hand**

```powershell
$resume = @{ thread_id = $r.thread_id; decision = "approve"; note = "manual check" } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:8000/resume -Method Post -Body $resume -ContentType "application/json" | ConvertTo-Json -Depth 5
```

Expected: `status` is `completed` and `outcome.status` is `approved` with `action: RETRAIN`.

- [ ] **Step 7: Confirm the audit endpoint**

```powershell
Invoke-RestMethod "http://localhost:8000/threads/$($r.thread_id)" | ConvertTo-Json -Depth 5
```

Expected: `next_nodes` is empty, `human_decision` is `approve`, and `evidence` is a non-empty list.

- [ ] **Step 8: Stop the service and commit only if requirements changed**

Stop uvicorn. If and only if Step 1 forced an agreed change to `requirements.txt`:

```powershell
git add requirements.txt
git commit -m "deps: resolve install for Python 3.11 on Windows"
```

If nothing changed, make no commit — `.venv/`, `.env`, and `*.db` are already gitignored, so there is nothing else to record.

---

### Task 2: Make database seeding importable

Deliverable: `seed_db.seed(db_path)` creates the full schema and rows at an arbitrary path, covered by its own tests.

The stub model's first turn emits a `get_feature_stats` tool call, so **every graph run reads SQLite**. Against an unseeded file the tool node raises `no such table: feature_stats`. Task 3's tests therefore need a genuinely seeded temporary database, which means seeding has to be callable from Python rather than only from the command line.

**Files:**
- Create: `pytest.ini`
- Create: `tests/test_seed_db.py`
- Modify: `seed_db.py` (whole file rewritten)
- Modify: `requirements.txt` (add pytest to the existing `# Dev only` block)

**Interfaces:**
- Consumes: nothing.
- Produces: `seed_db.seed(db_path: str | None = None) -> str` — creates tables `runs`, `feature_stats`, `registry`, `incidents`, populates them, and returns the path written to. Passing `None` falls back to `$DRIFTBELL_DB` then `"driftbell.db"`. Task 3 depends on this signature.

- [ ] **Step 1: Add pytest to requirements**

Replace the `# Dev only` block at the end of `requirements.txt` with:

```
# Dev only
httpx>=0.27
pytest>=8.0
```

- [ ] **Step 2: Create `pytest.ini`**

`app/` is a package at the repo root and `seed_db.py` is a top-level module, so the repo root must be on `sys.path`. pytest's default import mode inserts `tests/`, not the root, so set it explicitly:

```ini
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_seed_db.py`:

```python
"""The test suite builds throwaway databases, so seeding must be callable."""

from __future__ import annotations

import sqlite3

from seed_db import seed

EXPECTED_TABLES = {"runs", "feature_stats", "registry", "incidents"}


def test_seed_creates_every_table(tmp_path):
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        assert EXPECTED_TABLES <= {r[0] for r in rows}
    finally:
        conn.close()


def test_seed_populates_the_feature_stats_the_agent_reads(tmp_path):
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


def test_seed_returns_the_path_it_wrote(tmp_path):
    db = tmp_path / "driftbell.db"

    assert seed(str(db)) == str(db)
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_seed_db.py -v`

Expected: FAIL — `ImportError: cannot import name 'seed' from 'seed_db'`, or the import triggers the current top-level seeding side effect.

- [ ] **Step 5: Rewrite `seed_db.py`**

Replace the entire file with:

```python
"""Create driftbell.db with synthetic run history, feature stats and incidents.

Run once: python seed_db.py

seed() is importable rather than top-level script code so the test suite can
build a throwaway database with the same schema and rows the agent sees in
development. The agent's first tool call reads feature_stats on every run, so
tests need real rows, not a mocked tool layer.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS feature_stats;
DROP TABLE IF EXISTS registry;
DROP TABLE IF EXISTS incidents;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, model_name TEXT, started_at TEXT,
    accuracy REAL, f1 REAL, precision_ REAL, recall REAL,
    status TEXT, notes TEXT
);
CREATE TABLE feature_stats (
    model_name TEXT, feature TEXT, psi REAL, null_rate REAL,
    mean_train REAL, mean_live REAL, day_offset INTEGER
);
CREATE TABLE registry (
    model_name TEXT, version TEXT, stage TEXT, promoted_at TEXT, artifact_uri TEXT
);
CREATE TABLE incidents (
    occurred_at TEXT, source TEXT, severity TEXT, description TEXT, day_offset INTEGER
);
"""


def seed(db_path: str | None = None) -> str:
    """Write the synthetic MLOps history and return the path written to.

    Timestamps are generated relative to now, so a freshly seeded database
    always looks like it describes the last two months rather than whenever
    this file was written.
    """
    db = db_path or os.getenv("DRIFTBELL_DB", "driftbell.db")
    now = datetime.now(timezone.utc)

    def iso(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")

    conn = sqlite3.connect(db)
    try:
        c = conn.cursor()
        c.executescript(SCHEMA)

        c.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("run-014", "churn_clf", iso(2), 0.842, 0.781, 0.796, 0.767, "success", "scheduled eval"),
                ("run-013", "churn_clf", iso(9), 0.869, 0.812, 0.828, 0.797, "success", "scheduled eval"),
                ("run-012", "churn_clf", iso(16), 0.877, 0.826, 0.841, 0.812, "success", "champion promoted"),
                ("run-011", "churn_clf", iso(23), 0.874, 0.821, 0.836, 0.807, "success", "challenger rejected"),
                ("run-010", "churn_clf", iso(30), 0.871, 0.818, 0.833, 0.804, "success", "baseline"),
            ],
        )

        c.executemany(
            "INSERT INTO feature_stats VALUES (?,?,?,?,?,?,?)",
            [
                ("churn_clf", "monthly_spend", 0.284, 0.001, 71.4, 58.9, 3),
                ("churn_clf", "sessions_7d", 0.212, 0.002, 12.6, 9.3, 3),
                ("churn_clf", "tenure_months", 0.041, 0.000, 18.2, 18.6, 3),
                ("churn_clf", "support_tickets", 0.033, 0.004, 0.71, 0.74, 3),
                ("churn_clf", "plan_tier", 0.019, 0.000, 2.11, 2.09, 3),
            ],
        )

        c.executemany(
            "INSERT INTO registry VALUES (?,?,?,?,?)",
            [
                ("churn_clf", "v12", "champion", iso(16), "models/churn_clf/v12.joblib"),
                ("churn_clf", "v11", "archived", iso(30), "models/churn_clf/v11.joblib"),
                ("churn_clf", "v10", "archived", iso(58), "models/churn_clf/v10.joblib"),
            ],
        )

        c.executemany(
            "INSERT INTO incidents VALUES (?,?,?,?,?)",
            [
                (iso(5), "billing_etl", "low", "Late-arriving batch, backfilled the same day", 5),
                (iso(21), "events_stream", "high", "Six hours of dropped session events", 21),
            ],
        )

        conn.commit()
    finally:
        conn.close()

    return db


if __name__ == "__main__":
    path = seed()
    print(f"Seeded {path}: 5 runs, 5 features, 3 registry entries, 2 incidents.")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_seed_db.py -v`

Expected: 3 passed.

- [ ] **Step 7: Confirm the command line still behaves identically**

Run: `.venv\Scripts\python.exe seed_db.py`

Expected: `Seeded driftbell.db: 5 runs, 5 features, 3 registry entries, 2 incidents.`

- [ ] **Step 8: Commit**

```powershell
git add seed_db.py tests/test_seed_db.py pytest.ini requirements.txt
git commit -m "seed_db: make seeding importable for tests"
```

---

### Task 3: Pin the interrupt and resume path in tests

Deliverable: three tests proving a high-PSI report parks at `human_gate`, that an approved thread runs to completion, and that a rebuilt graph resumes a thread it never started.

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_graph.py`

**Interfaces:**
- Consumes: `seed_db.seed(db_path: str | None = None) -> str` from Task 2. `app.graph.build_graph(checkpointer=None)` and `app.graph.make_checkpointer(path: str | None = None) -> SqliteSaver`, both already in the codebase.
- Produces: pytest fixtures `stub_provider` (autouse) and `seeded_db`.

- [ ] **Step 1: Write the fixtures**

Create `tests/conftest.py`:

```python
"""Shared fixtures. Every test here runs offline with no API key."""

from __future__ import annotations

import pytest

from seed_db import seed


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch):
    """Force the scripted offline model regardless of the developer's shell.

    get_llm() reads LLM_PROVIDER at call time, so setting the variable is
    enough; nothing needs to be imported in a particular order.
    """
    monkeypatch.setenv("LLM_PROVIDER", "stub")


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A throwaway driftbell.db, with the agent's tools pointed at it.

    The stub model opens every run with a get_feature_stats tool call, so the
    graph cannot reach its verdict without real rows. app.tools._query resolves
    the DB_PATH global at call time, so patching the attribute is sufficient.
    """
    db = tmp_path / "driftbell.db"
    seed(str(db))
    monkeypatch.setattr("app.tools.DB_PATH", str(db))
    return db
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_graph.py`:

```python
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
```

- [ ] **Step 3: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`

Expected: 3 passed. These tests describe behaviour that already exists, so unlike Task 2 they should pass on the first run — Task 1 observed the same path working over HTTP. If any of them fails, the failure is real information about the agent: do not weaken the assertion to make it pass. Report it.

- [ ] **Step 4: Prove the tests are actually exercising the gate**

Temporarily change the assertion in `test_high_psi_report_parks_at_the_human_gate` from `("human_gate",)` to `("propose",)` and re-run that one test. Expected: FAIL. Revert the change immediately.

This guards against the tests passing for the wrong reason — a graph that silently completed would still satisfy a carelessly written assertion.

- [ ] **Step 5: Run the whole suite**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: 6 passed.

- [ ] **Step 6: Commit**

```powershell
git add tests/conftest.py tests/test_graph.py
git commit -m "Phase 1: pytest suite for the interrupt/resume path"
```

---

## Done when

`pytest -q` reports 6 passed, and `/diagnose` followed by `/resume` has been driven by hand against a running uvicorn (Task 1, steps 5 and 6).
