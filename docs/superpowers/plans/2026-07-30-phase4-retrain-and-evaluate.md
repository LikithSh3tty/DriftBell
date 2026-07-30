# Phase 4 — Retrain and Evaluate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An approved RETRAIN fits a challenger, records the run, and promotes it to champion only if it beats the incumbent on F1.

**Architecture:** Seed real feature/label samples with a deliberately drifted `live` split. A new `app/training.py` fits a scikit-learn classifier and records results; `app/main.py` gains two thin endpoints. Workflow 03 calls `/train`, compares F1 in an IF node, and calls `/promote` only on a win — keeping the one irreversible act in n8n's hands.

**Tech Stack:** scikit-learn, FastAPI, SQLite, n8n 2.32.6.

**Spec:** `docs/superpowers/specs/2026-07-30-phase4-retrain-and-evaluate-design.md`

## Global Constraints

- **Zero paid services.** `LLM_PROVIDER=stub` must keep working offline with no key.
- **The agent never performs irreversible actions.** `/train` fits and records; only n8n decides promotion and calls `/promote`. Do not auto-promote inside `/train`.
- **`interrupt()` stays in `human_gate` only.** No task here touches the graph.
- **n8n reaches the agent at `http://driftbell:8000`** — container name, never `localhost`.
- **n8n Code nodes are JavaScript with no package access.** No task here adds a Code node.
- **Never hand-write n8n workflow JSON and assume it works.** Generate, import, fix, export, commit the export.
- **Telegram/tunnel facts that already bit us:** `executeWorkflow` must be `typeVersion` 1.1 (1.2+ demands a `workflowInputs` resourceMapper), and `waitForSubWorkflow` lives inside `options`.
- Python 3.11+, type hints on function signatures, docstrings that say *why*.
- Commit messages carry NO Claude attribution trailers.
- Platform is Windows; `docker compose exec` with `/tmp/...` paths needs `MSYS_NO_PATHCONV=1` under Git Bash.
- Invoke Python as `.venv\Scripts\python.exe` — activation does not persist between tool calls.

---

### Task 1: Seed real training data and make seeding survive restarts

Deliverable: a `samples` table with a genuinely drifted `live` split, a `version` column joining `runs` to `registry`, and a container that stops wiping the database on every start.

**Files:**
- Modify: `seed_db.py`
- Modify: `Dockerfile` (the `CMD` line)
- Modify: `tests/test_seed_db.py`

**Interfaces:**
- Produces: `seed_db.seed(db_path: str | None = None, if_empty: bool = False) -> str`. The existing positional call `seed(str(db))` used by current tests keeps working. Table `samples(model_name, split, monthly_spend, sessions_7d, tenure_months, support_tickets, plan_tier, churned)` with `split` in `{'train','live'}`. Table `runs` gains a `version` column. Task 2 reads both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_seed_db.py`:

```python
def test_seed_creates_both_sample_splits(tmp_path) -> None:
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        rows = dict(
            conn.execute(
                "SELECT split, COUNT(*) FROM samples WHERE model_name='churn_clf' GROUP BY split"
            ).fetchall()
        )
    finally:
        conn.close()
    assert rows["train"] > 500
    assert rows["live"] > 200


def test_live_split_is_actually_drifted(tmp_path) -> None:
    """The challenger must be scored on data that differs from training.

    If the two splits were drawn from the same distribution the retrain would
    be theatre, so this pins the shift rather than trusting the generator.
    """
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        train_mean, live_mean = (
            conn.execute(
                "SELECT AVG(monthly_spend) FROM samples WHERE split = ?", (split,)
            ).fetchone()[0]
            for split in ("train", "live")
        )
    finally:
        conn.close()
    assert train_mean - live_mean > 5.0


def test_runs_carry_a_version(tmp_path) -> None:
    """Without this column, 'the champion's F1' cannot be answered at all."""
    db = tmp_path / "driftbell.db"

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        versions = {r[0] for r in conn.execute("SELECT version FROM runs")}
    finally:
        conn.close()
    assert "v12" in versions


def test_if_empty_preserves_existing_rows(tmp_path) -> None:
    """The container seeds on every start; it must not wipe a recorded run."""
    db = tmp_path / "driftbell.db"
    seed(str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO runs VALUES ('run-999','churn_clf','v99','2026-01-01',"
        "0.9,0.9,0.9,0.9,'success','from a previous container')"
    )
    conn.commit()
    conn.close()

    seed(str(db), if_empty=True)

    conn = sqlite3.connect(db)
    try:
        survived = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id='run-999'").fetchone()[0]
    finally:
        conn.close()
    assert survived == 1


def test_plain_seed_still_resets(tmp_path) -> None:
    """CLAUDE.md documents `python seed_db.py` as resetting history."""
    db = tmp_path / "driftbell.db"
    seed(str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO runs VALUES ('run-999','churn_clf','v99','2026-01-01',"
        "0.9,0.9,0.9,0.9,'success','stale')"
    )
    conn.commit()
    conn.close()

    seed(str(db))

    conn = sqlite3.connect(db)
    try:
        survived = conn.execute("SELECT COUNT(*) FROM runs WHERE run_id='run-999'").fetchone()[0]
    finally:
        conn.close()
    assert survived == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_seed_db.py -q`
Expected: FAIL — `no such table: samples`, `no such column: version`, and a `TypeError` on the unexpected `if_empty` keyword.

- [ ] **Step 3: Add the samples generator to `seed_db.py`**

Add `import math` and `import random` at the top, and this above `seed()`:

```python
# Feature means match feature_stats: monthly_spend 71.4 train / 58.9 live,
# sessions_7d 12.6 / 9.3. The live split is drawn shifted on exactly the two
# features the drift monitor flags, so a challenger is scored against the drift
# the agent actually diagnosed rather than a reshuffle.
FEATURE_COLUMNS = (
    "monthly_spend",
    "sessions_7d",
    "tenure_months",
    "support_tickets",
    "plan_tier",
)


def _draw_samples(rng: random.Random, n: int, drifted: bool) -> list[tuple]:
    """Draw n labelled rows. `drifted` shifts the two features PSI reports on."""
    spend_mean = 58.9 if drifted else 71.4
    sessions_mean = 9.3 if drifted else 12.6
    rows = []
    for _ in range(n):
        spend = rng.gauss(spend_mean, 18.0)
        sessions = rng.gauss(sessions_mean, 4.0)
        tenure = rng.gauss(18.2, 6.0)
        tickets = max(0.0, rng.gauss(0.71, 0.6))
        tier = float(rng.choice([1, 2, 3]))
        # Churn rises as spend, sessions and tenure fall, and with more tickets.
        logit = (
            -0.055 * (spend - 65.0)
            - 0.170 * (sessions - 11.0)
            - 0.045 * (tenure - 18.0)
            + 0.55 * tickets
        )
        churned = 1 if rng.random() < 1.0 / (1.0 + math.exp(-logit)) else 0
        rows.append((round(spend, 4), round(sessions, 4), round(tenure, 4),
                     round(tickets, 4), tier, churned))
    return rows
```

- [ ] **Step 4: Update the schema and `seed()`**

In `SCHEMA`, add `DROP TABLE IF EXISTS samples;` alongside the others, change the `runs` table to include `version`, and add the `samples` table:

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, model_name TEXT, version TEXT, started_at TEXT,
    accuracy REAL, f1 REAL, precision_ REAL, recall REAL,
    status TEXT, notes TEXT
);
CREATE TABLE samples (
    model_name TEXT, split TEXT,
    monthly_spend REAL, sessions_7d REAL, tenure_months REAL,
    support_tickets REAL, plan_tier REAL, churned INTEGER
);
```

Change the `runs` insert to 10 placeholders and attribute versions — the three most recent runs are evaluations of the current champion, so its F1 is the most recent of them:

```python
        c.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("run-014", "churn_clf", "v12", iso(2), 0.842, 0.781, 0.796, 0.767, "success", "scheduled eval"),
                ("run-013", "churn_clf", "v12", iso(9), 0.869, 0.812, 0.828, 0.797, "success", "scheduled eval"),
                ("run-012", "churn_clf", "v12", iso(16), 0.877, 0.826, 0.841, 0.812, "success", "champion promoted"),
                ("run-011", "churn_clf", "v11", iso(23), 0.874, 0.821, 0.836, 0.807, "success", "challenger rejected"),
                ("run-010", "churn_clf", "v10", iso(30), 0.871, 0.818, 0.833, 0.804, "success", "baseline"),
            ],
        )
```

Add the samples insert after the incidents insert:

```python
        rng = random.Random(20260730)  # fixed so a reseed is reproducible
        c.executemany(
            "INSERT INTO samples VALUES ('churn_clf','train',?,?,?,?,?,?)",
            _draw_samples(rng, 1500, drifted=False),
        )
        c.executemany(
            "INSERT INTO samples VALUES ('churn_clf','live',?,?,?,?,?,?)",
            _draw_samples(rng, 600, drifted=True),
        )
```

Change the signature and add the early return:

```python
def seed(db_path: str | None = None, if_empty: bool = False) -> str:
    """Write the synthetic MLOps history and return the path written to.

    With if_empty=True, an already-populated database is left untouched. The
    container seeds on every start, and an unconditional reseed would drop the
    runs row and registry promotion that Phase 4 exists to produce.
    """
    db = db_path or os.getenv("DRIFTBELL_DB", "driftbell.db")
    now = datetime.now(timezone.utc)

    def iso(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")

    conn = sqlite3.connect(db)
    try:
        c = conn.cursor()
        if if_empty:
            existing = c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()[0]
            if existing and c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]:
                return db
        c.executescript(SCHEMA)
        # ... the rest unchanged
```

- [ ] **Step 5: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_seed_db.py -q`
Expected: 8 passed.

- [ ] **Step 6: Stop the container wiping data**

In `Dockerfile`, change the `CMD` to pass the flag:

```dockerfile
CMD ["sh", "-c", "python seed_db.py --if-empty && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

And teach the entry point to accept it:

```python
if __name__ == "__main__":
    import sys

    path = seed(if_empty="--if-empty" in sys.argv)
    print(f"Seeded {path}: 5 runs, 5 features, 3 registry entries, 2 incidents, 2100 samples.")
```

- [ ] **Step 7: Verify both modes from the CLI**

```powershell
.venv\Scripts\python.exe seed_db.py
.venv\Scripts\python.exe -m pytest -q
```

Expected: the seed line prints, and the full suite is **22 passed** (17 existing + 5 new).

- [ ] **Step 8: Commit**

```powershell
git add seed_db.py Dockerfile tests/test_seed_db.py
git commit -m "Seed labelled samples and stop wiping the database on restart"
```

---

### Task 2: Train a challenger and promote a winner

Deliverable: `/train` fits a classifier, records a run and a challenger registry row; `/promote` flips the champion. Both covered by tests.

**Files:**
- Create: `app/training.py`
- Create: `tests/test_training.py`
- Modify: `app/main.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `seed_db.seed(db_path, if_empty)` and the `samples` / `runs` / `registry` schema from Task 1.
- Produces:
  - `app.training.train_challenger(model_name: str) -> dict` with keys `model_name`, `run_id`, `champion`, `challenger`; each of `champion`/`challenger` is `{"version": str, "accuracy": float, "f1": float, "precision": float, "recall": float}`.
  - `app.training.promote(model_name: str, version: str) -> dict` with keys `model_name`, `promoted`, `archived`.
  - `POST /train` and `POST /promote`. Task 3's workflow reads `$json.challenger.f1` and `$json.champion.f1`.

- [ ] **Step 1: Add scikit-learn**

In `requirements.txt`, add below the pinned core block:

```
# Phase 4 — challenger training. Pulls numpy and scipy (~150MB in the image).
scikit-learn>=1.5
```

Install it: `.venv\Scripts\python.exe -m pip install "scikit-learn>=1.5"`

- [ ] **Step 2: Point the shared fixture at the temp database**

`tests/conftest.py`'s `seeded_db` fixture currently only patches
`app.tools.DB_PATH`. `app/training.py` resolves its path from the `DRIFTBELL_DB`
environment variable instead, so without this the training tests would run
against the developer's real `driftbell.db` — writing runs and moving the
champion in it. Add the env var alongside the existing patch:

```python
@pytest.fixture
def seeded_db(tmp_path, monkeypatch) -> Path:
    """A throwaway driftbell.db, with the agent's tools pointed at it.

    The stub model opens every run with a get_feature_stats tool call, so the
    graph cannot reach its verdict without real rows. app.tools._query resolves
    the DB_PATH global at call time, so patching the attribute is sufficient.
    app.training resolves DRIFTBELL_DB per call instead, so it needs the
    environment variable — without it, training tests would write runs and move
    the champion in the developer's real database.
    """
    db = tmp_path / "driftbell.db"
    seed(str(db))
    monkeypatch.setattr("app.tools.DB_PATH", str(db))
    monkeypatch.setenv("DRIFTBELL_DB", str(db))
    return db
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_training.py`:

```python
"""Challenger training and champion promotion.

These run against a real seeded SQLite file rather than mocks: the point of the
phase is that a run is recorded and a registry row moves, and mocking the
database would test neither.
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
    """Reusing v12 would overwrite the champion's own history."""
    result = train_challenger("churn_clf")

    assert result["challenger"]["version"] not in {"v10", "v11", "v12"}


def test_metrics_are_probabilities(seeded_db) -> None:
    result = train_challenger("churn_clf")

    for side in ("champion", "challenger"):
        for key in ("accuracy", "f1", "precision", "recall"):
            assert 0.0 <= result[side][key] <= 1.0, f"{side}.{key} out of range"


def test_champion_metrics_come_from_the_latest_champion_run(seeded_db) -> None:
    """run-014 is the most recent v12 evaluation, and v12 is champion."""
    result = train_challenger("churn_clf")

    assert result["champion"]["version"] == "v12"
    assert result["champion"]["f1"] == 0.781


def test_training_is_deterministic(seeded_db) -> None:
    first = train_challenger("churn_clf")
    second = train_challenger("churn_clf")

    assert first["challenger"]["f1"] == second["challenger"]["f1"]


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
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_training.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.training'`.

- [ ] **Step 4: Write `app/training.py`**

```python
"""Fit a challenger, record the run, and move the champion when told to.

Splitting this out of main.py keeps that file a routing layer. Nothing here
decides whether to promote — n8n compares the metrics and calls promote()
explicitly, because promotion is the only irreversible act in this phase.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score

FEATURES = (
    "monthly_spend",
    "sessions_7d",
    "tenure_months",
    "support_tickets",
    "plan_tier",
)


def _db() -> str:
    """Resolved per call, so tests can repoint DRIFTBELL_DB without reimporting."""
    return os.getenv("DRIFTBELL_DB", "driftbell.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db())
    conn.row_factory = sqlite3.Row
    return conn


def load_samples(model_name: str) -> tuple[list, list, list, list]:
    """Return X_train, y_train, X_live, y_live for a model."""
    cols = ", ".join(FEATURES)
    conn = _connect()
    try:
        out = []
        for split in ("train", "live"):
            rows = conn.execute(
                f"SELECT {cols}, churned FROM samples WHERE model_name = ? AND split = ?",
                (model_name, split),
            ).fetchall()
            out.append([[r[c] for c in FEATURES] for r in rows])
            out.append([r["churned"] for r in rows])
        return out[0], out[1], out[2], out[3]
    finally:
        conn.close()


def _next_version(conn: sqlite3.Connection, model_name: str) -> str:
    """One past the highest vN seen in either table, so history is never reused."""
    seen = [
        r[0]
        for r in conn.execute(
            "SELECT version FROM registry WHERE model_name = ? "
            "UNION SELECT version FROM runs WHERE model_name = ?",
            (model_name, model_name),
        )
        if r[0]
    ]
    numbers = [int(m.group(1)) for v in seen if (m := re.fullmatch(r"v(\d+)", str(v)))]
    return f"v{max(numbers) + 1 if numbers else 1}"


def _next_run_id(conn: sqlite3.Connection) -> str:
    seen = [r[0] for r in conn.execute("SELECT run_id FROM runs")]
    numbers = [int(m.group(1)) for s in seen if (m := re.fullmatch(r"run-(\d+)", str(s)))]
    return f"run-{max(numbers) + 1 if numbers else 1:03d}"


def _champion(conn: sqlite3.Connection, model_name: str) -> dict[str, Any]:
    """The incumbent's most recent evaluation — how it performs now, not at promotion."""
    row = conn.execute(
        "SELECT r.version, r.accuracy, r.f1, r.precision_, r.recall FROM runs r "
        "JOIN registry g ON g.version = r.version AND g.model_name = r.model_name "
        "WHERE g.model_name = ? AND g.stage = 'champion' "
        "ORDER BY r.started_at DESC LIMIT 1",
        (model_name,),
    ).fetchone()
    if row is None:
        return {"version": None, "accuracy": 0.0, "f1": 0.0, "precision": 0.0, "recall": 0.0}
    return {
        "version": row["version"],
        "accuracy": row["accuracy"],
        "f1": row["f1"],
        "precision": row["precision_"],
        "recall": row["recall"],
    }


def train_challenger(model_name: str) -> dict[str, Any]:
    """Fit on the training split, score on the drifted live split, record the run."""
    X_train, y_train, X_live, y_live = load_samples(model_name)
    if not X_train or not X_live:
        raise ValueError(f"no samples for model {model_name!r}")

    # random_state fixed so the same data always yields the same metrics; an
    # unreproducible promotion decision would be untestable and indefensible.
    clf = LogisticRegression(max_iter=1000, random_state=42)
    clf.fit(X_train, y_train)
    predicted = clf.predict(X_live)

    metrics = {
        "accuracy": round(float(accuracy_score(y_live, predicted)), 4),
        "f1": round(float(f1_score(y_live, predicted, zero_division=0)), 4),
        "precision": round(float(precision_score(y_live, predicted, zero_division=0)), 4),
        "recall": round(float(recall_score(y_live, predicted, zero_division=0)), 4),
    }

    conn = _connect()
    try:
        champion = _champion(conn, model_name)
        version = _next_version(conn, model_name)
        run_id = _next_run_id(conn)
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")

        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, model_name, version, started, metrics["accuracy"], metrics["f1"],
             metrics["precision"], metrics["recall"], "success", "challenger trained"),
        )
        # Registered as a challenger, never as champion: promotion is n8n's call.
        conn.execute(
            "INSERT INTO registry VALUES (?,?,?,?,?)",
            (model_name, version, "challenger", started,
             f"models/{model_name}/{version}.joblib"),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "model_name": model_name,
        "run_id": run_id,
        "champion": champion,
        "challenger": {"version": version, **metrics},
    }


def promote(model_name: str, version: str) -> dict[str, Any]:
    """Archive the incumbent and make `version` champion."""
    conn = _connect()
    try:
        known = conn.execute(
            "SELECT COUNT(*) FROM registry WHERE model_name = ? AND version = ?",
            (model_name, version),
        ).fetchone()[0]
        if not known:
            raise ValueError(f"{version} is not in the registry for {model_name}")

        previous = conn.execute(
            "SELECT version FROM registry WHERE model_name = ? AND stage = 'champion'",
            (model_name,),
        ).fetchone()
        conn.execute(
            "UPDATE registry SET stage = 'archived' WHERE model_name = ? AND stage = 'champion'",
            (model_name,),
        )
        conn.execute(
            "UPDATE registry SET stage = 'champion', promoted_at = ? "
            "WHERE model_name = ? AND version = ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), model_name, version),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "model_name": model_name,
        "promoted": version,
        "archived": previous["version"] if previous else None,
    }
```

- [ ] **Step 5: Run the training tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_training.py -q`
Expected: 7 passed.

- [ ] **Step 6: Add the endpoints**

In `app/main.py`, import the module and add two routes after `/resume`:

```python
from .training import promote as promote_model, train_challenger


class TrainRequest(BaseModel):
    model_name: str = "churn_clf"


class PromoteRequest(BaseModel):
    model_name: str = "churn_clf"
    version: str


@app.post("/train")
def train(req: TrainRequest, authorization: str | None = Header(None)) -> dict[str, Any]:
    """Fit a challenger and record it. Deliberately does NOT promote."""
    _auth(authorization)
    try:
        return train_challenger(req.model_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/promote")
def promote_endpoint(
    req: PromoteRequest, authorization: str | None = Header(None)
) -> dict[str, Any]:
    """Move the champion. n8n calls this only after comparing F1 itself."""
    _auth(authorization)
    try:
        return promote_model(req.model_name, req.version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
```

- [ ] **Step 7: Test the endpoints**

Append to `tests/test_api.py`:

```python
def test_train_then_promote_over_http(client: TestClient) -> None:
    """The exact two calls workflow 03 makes."""
    trained = client.post("/train", json={"model_name": "churn_clf"}, headers=AUTH)
    assert trained.status_code == 200
    body = trained.json()
    assert body["champion"]["version"] == "v12"
    version = body["challenger"]["version"]

    promoted = client.post(
        "/promote", json={"model_name": "churn_clf", "version": version}, headers=AUTH
    )
    assert promoted.status_code == 200
    assert promoted.json()["promoted"] == version


def test_train_rejects_a_missing_token(client: TestClient) -> None:
    assert client.post("/train", json={"model_name": "churn_clf"}).status_code == 401


def test_promote_rejects_an_unknown_version(client: TestClient) -> None:
    response = client.post(
        "/promote", json={"model_name": "churn_clf", "version": "v999"}, headers=AUTH
    )
    assert response.status_code == 404
```

- [ ] **Step 8: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: **32 passed** (22 after Task 1, plus 7 training and 3 API).

- [ ] **Step 9: Commit**

```powershell
git add app/training.py app/main.py tests/test_training.py tests/test_api.py requirements.txt
git commit -m "Train a challenger and promote it only when n8n says so"
```

---

### Task 3: Workflow 03, and wiring it to the approval

Deliverable: an approved RETRAIN trains a challenger and promotes it only if it wins.

**Files:**
- Create: `workflows/03-retrain-and-evaluate.json`
- Modify: `workflows/02-approval-loop.json`

**Interfaces:**
- Consumes: `POST /train` and `POST /promote` from Task 2.

- [ ] **Step 1: Rebuild the agent image so the container has scikit-learn**

```bash
docker compose up -d --build --force-recreate driftbell
docker compose logs driftbell | tail -5
```

Expected: the seed line, then uvicorn running. The seed line now reports samples.

- [ ] **Step 2: Confirm the endpoints work from n8n's network**

```bash
docker compose exec -T n8n sh -c 'wget -qO- --header="Content-Type: application/json" --post-data="{\"model_name\":\"churn_clf\"}" http://driftbell:8000/train'
```

Expected: JSON with `champion` and `challenger` blocks. Note the challenger's F1 — Step 5 depends on whether it beats 0.781.

- [ ] **Step 3: Generate workflow 03**

Five nodes. Copy the node shapes from `workflows/02-approval-loop.json`, which are known to load in this n8n version.

| Node | Type | typeVersion |
| --- | --- | --- |
| Retrain requested | `n8n-nodes-base.executeWorkflowTrigger` | 1.1 |
| Train a challenger | `n8n-nodes-base.httpRequest` | 4.2 |
| Challenger wins on F1? | `n8n-nodes-base.if` | 2 |
| Promote the challenger | `n8n-nodes-base.httpRequest` | 4.2 |
| Champion held | `n8n-nodes-base.noOp` | 1 |
| Challenger promoted | `n8n-nodes-base.noOp` | 1 |

- Trigger: `{"inputSource": "passthrough"}`
- Train: POST `http://driftbell:8000/train`, `jsonBody` `={{ JSON.stringify({ model_name: 'churn_clf' }) }}`, `options.timeout` 120000, `retryOnFail` true
- IF: `leftValue` `={{ $json.challenger.f1 }}`, `rightValue` `={{ $json.champion.f1 }}`, operator `{"type": "number", "operation": "gt"}`
- Promote: POST `http://driftbell:8000/promote`, `jsonBody` `={{ JSON.stringify({ model_name: 'churn_clf', version: $('Train a challenger').item.json.challenger.version }) }}`
- Connections: trigger → train → IF; IF true → promote → `Challenger promoted`; IF false → `Champion held`

- [ ] **Step 4: Import it and wire workflow 02**

```bash
docker cp workflows/03-retrain-and-evaluate.json driftbell-n8n:/tmp/wf03.json
MSYS_NO_PATHCONV=1 docker compose exec -T n8n n8n import:workflow --input=/tmp/wf03.json
```

The CLI requires an `id` on a new workflow — add a 16-character alphanumeric id to the import copy only, keeping the committed file instance-agnostic.

Then replace workflow 02's `Retrain approved` NoOp with an Execute Workflow node targeting 03, exactly as workflow 01 calls 02:

```json
{
  "source": "database",
  "workflowId": {"__rl": true, "value": "<workflow 03 id>", "mode": "list",
                 "cachedResultName": "Driftbell 03 — retrain & evaluate"},
  "mode": "once",
  "options": {"waitForSubWorkflow": false}
}
```

with `"type": "n8n-nodes-base.executeWorkflow"` and `"typeVersion": 1.1`. Keep the node's name so the connection from `Agent recorded approval?` survives.

- [ ] **Step 5: Run the whole chain**

Publish 02 and 03, then submit the drift form with `drift_magnitude = 12` and approve on Telegram.

Verify:

```powershell
Invoke-RestMethod http://localhost:8000/threads/<thread_id> | ConvertTo-Json -Depth 5
```

and inspect the database:

```bash
docker compose exec -T driftbell python -c "import sqlite3;c=sqlite3.connect('/data/driftbell.db');print(list(c.execute('SELECT run_id,version,f1,notes FROM runs ORDER BY started_at DESC LIMIT 3')));print(list(c.execute('SELECT version,stage FROM registry WHERE model_name=\"churn_clf\"')))"
```

Expected: a new `run-0NN` row with `challenger trained`, and the registry showing the challenger as champion **only if** its F1 beat 0.781. If it lost, `Champion held` ran and v12 is still champion — that is a correct outcome, not a failure.

- [ ] **Step 6: Confirm it survives a restart**

```bash
docker compose restart driftbell
docker compose exec -T driftbell python -c "import sqlite3;c=sqlite3.connect('/data/driftbell.db');print(c.execute('SELECT COUNT(*) FROM runs').fetchone())"
```

Expected: the count includes the new run. Before Task 1 this would have reset to 5.

- [ ] **Step 7: Export both workflows and commit**

Export from n8n, strip the instance-specific keys (`id`, `versionId`, `versionCounter`, `activeVersionId`, `createdAt`, `updatedAt`, `shared`, `staticData`, `meta`, `tags`, `triggerCount`, `isArchived`, `sourceWorkflowId`, `nodeGroups`, `versionMetadata`, `description`), keeping only `name`, `nodes`, `connections`, `settings`, `pinData`, `active`.

```powershell
git add workflows/03-retrain-and-evaluate.json workflows/02-approval-loop.json
git commit -m "Add workflow 03: retrain, evaluate, promote on F1"
```

---

## Done when

Approving a RETRAIN from Telegram produces a new `runs` row, `registry` shows a new champion if and only if the challenger's F1 beat the incumbent's, both survive `docker compose restart`, and `pytest -q` reports 32 passed.
