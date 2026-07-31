# Phase 6 — Error Handler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A failure in any Driftbell workflow produces a Telegram alert and an incident the agent can reason about later.

**Architecture:** `POST /incidents` writes into the existing `incidents` table, which the agent already treats as evidence when judging drift. Workflow 06 catches failures with an Error Trigger, classifies them with Gemini in a node that cannot block, then alerts and records in parallel.

**Tech Stack:** FastAPI, SQLite, n8n 2.32.6 Error Trigger, Gemini via Basic LLM Chain, Telegram.

**Spec:** `docs/superpowers/specs/2026-07-31-phase6-error-handler-design.md`

## Global Constraints

- **Zero paid services.** Gemini free tier only. `LLM_PROVIDER=stub` must keep working offline with no key.
- **The agent never performs irreversible actions.** Recording an incident is a write, but not an irreversible act on the world.
- **n8n reaches the agent at `http://driftbell:8000`** — container name, never `localhost`.
- **Never hand-write n8n workflow JSON and assume it works.** Generate, import, fix in the UI, export, commit the export.
- **Interface facts verified against this instance — do not substitute guesses:**
  - Error Trigger output shape: `$json.workflow.name`, `$json.workflow.id`, `$json.execution.id`, `$json.execution.url`, `$json.execution.lastNodeExecuted`, `$json.execution.error.message`, `$json.execution.error.stack`.
  - `errorTrigger` is `n8n-nodes-base.errorTrigger`, typeVersion `1`, and takes no parameters.
  - `chainLlm` versions run to `1.9`; use `promptType: "define"` with `text`. Its output is `$json.text`.
  - `lmChatGoogleGemini` typeVersion 1.1 defaults to `models/gemini-3-flash-preview`. **Do not substitute another model name** — `gemini-2.0-flash` has no free-tier quota and `gemini-2.5-flash` returns 404 for new accounts.
  - Telegram `sendMessage` uses hardcoded `parse_mode: Markdown`. Escape `_ * [` `` ` `` in any interpolated value or Telegram rejects the whole request.
- **The classify node must carry `onError: continueRegularOutput`.** An error handler cannot have a hard dependency on the component most likely to be failing.
- Python 3.11+, type hints on function signatures, docstrings that say *why*.
- Commit messages carry NO Claude attribution trailers.
- Platform is Windows; `docker compose exec` with `/tmp/...` paths needs `MSYS_NO_PATHCONV=1` under Git Bash.
- Invoke Python as `.venv\Scripts\python.exe` — activation does not persist between tool calls.

---

### Task 1: Record incidents over HTTP

Deliverable: `POST /incidents` writes a row the agent's `get_pipeline_incidents` tool can see immediately.

**Files:**
- Modify: `app/history.py`
- Modify: `app/main.py`
- Modify: `tests/test_history.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: the `incidents` schema `(occurred_at, source, severity, description, day_offset)`.
- Produces: `app.history.record_incident(source: str, severity: str, description: str, classification: str = "") -> dict` returning the stored row, and `POST /incidents`. Task 2's workflow calls the endpoint.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
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
```

Update that file's import to include the new names:

```python
from app.history import (
    incident_documents,
    recent_incidents,
    recent_runs,
    record_incident,
    registry_entries,
    thread_ids,
)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_history.py -q`
Expected: FAIL — `ImportError: cannot import name 'record_incident'`.

- [ ] **Step 3: Implement it**

In `app/history.py`, change the import line to include the datetime helpers:

```python
from datetime import datetime, timezone
```

Update the module docstring's first line, since it currently claims to be read-only:

```python
"""Access to everything Driftbell has recorded.

n8n cannot reach the SQLite files on the agent's volume, so the ops chatbot and
the error handler have no route to run history, promotions, incidents or past
reasoning without this. Reads dominate; the single write is incident recording,
which lives here because it shares the incidents schema with its readers.
"""
```

Add the function:

```python
def record_incident(
    source: str,
    severity: str,
    description: str,
    classification: str = "",
) -> dict[str, Any]:
    """Record a pipeline failure so the agent can weigh it against future drift.

    The agent's system prompt tells it that a drift alert coinciding with an
    ingestion incident is usually a bug, and get_pipeline_incidents is one of its
    tools -- so a failure recorded today changes how it reasons tomorrow.

    day_offset is 0 so get_pipeline_incidents(n_days=7) sees the row at once.
    The classification is folded into the description rather than given its own
    column: this table's consumers are an LLM tool and a vector store, both of
    which read prose, and a schema change would ripple into seed_db.py for no
    retrieval benefit.
    """
    occurred_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = f"[{classification}] {description}" if classification else description

    conn = sqlite3.connect(_driftbell_db())
    try:
        conn.execute(
            "INSERT INTO incidents VALUES (?,?,?,?,?)",
            (occurred_at, source, severity, text, 0),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "occurred_at": occurred_at,
        "source": source,
        "severity": severity,
        "description": text,
        "day_offset": 0,
    }
```

- [ ] **Step 4: Run the history tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_history.py -q`
Expected: 12 passed.

- [ ] **Step 5: Add the endpoint**

In `app/main.py`, add `record_incident` to the existing `from .history import (...)` block, then add this route after `/history/documents`:

```python
class IncidentRequest(BaseModel):
    source: str
    severity: Literal["low", "high"] = "high"
    description: str
    classification: str = ""


@app.post("/incidents")
def create_incident(
    req: IncidentRequest, authorization: str | None = Header(None)
) -> dict[str, Any]:
    """Record a workflow failure. Called by n8n's error handler.

    Severity defaults to high: an unclassified failure is more likely to matter
    than not, and a false alarm is cheaper than a missed outage.
    """
    _auth(authorization)
    return record_incident(
        req.source, req.severity, req.description, req.classification
    )
```

- [ ] **Step 6: Test the endpoint**

Append to `tests/test_api.py`:

```python
def test_incident_endpoint_records_and_returns_the_row(client: TestClient) -> None:
    response = client.post(
        "/incidents",
        json={
            "source": "n8n:Driftbell 01",
            "severity": "high",
            "description": "Ask the Driftbell agent: connection refused",
            "classification": "transient",
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["description"].startswith("[transient]")
    assert body["day_offset"] == 0


def test_recorded_incident_appears_in_history(client: TestClient) -> None:
    """The point of recording is that the agent and chatbot can see it."""
    client.post(
        "/incidents",
        json={"source": "n8n:Driftbell 02", "description": "Telegram send failed"},
        headers=AUTH,
    )

    incidents = client.get("/history", headers=AUTH).json()["incidents"]

    assert any(i["source"] == "n8n:Driftbell 02" for i in incidents)


def test_incidents_endpoint_rejects_a_missing_token(client: TestClient) -> None:
    response = client.post(
        "/incidents", json={"source": "x", "description": "y"}
    )

    assert response.status_code == 401
```

- [ ] **Step 7: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: **53 passed** (46 existing + 4 history + 3 API).

- [ ] **Step 8: Rebuild the container so n8n can reach the endpoint**

```bash
docker compose up -d --build --force-recreate driftbell
```

Wait for readiness, then confirm from n8n's network:

```bash
docker compose exec -T n8n sh -c 'wget -qO- --header="Content-Type: application/json" --post-data="{\"source\":\"smoke-test\",\"description\":\"reachability check\"}" http://driftbell:8000/incidents'
```

Expected: JSON with `day_offset: 0`.

- [ ] **Step 9: Commit**

```powershell
git add app/history.py app/main.py tests/test_history.py tests/test_api.py
git commit -m "Record workflow failures as incidents the agent can reason about"
```

---

### Task 2: Workflow 06 and wiring it to every workflow

Deliverable: any workflow failure sends a Telegram alert and records an incident.

**Files:**
- Create: `workflows/06-error-handler.json`
- Modify: `workflows/01-ingest-and-monitor.json`, `02-approval-loop.json`, `03-retrain-and-evaluate.json`, `04-ops-agent.json`, `05-mcp-server.json` (each gains `settings.errorWorkflow`)

**Interfaces:**
- Consumes: `POST /incidents` from Task 1, the Telegram credential `ELi5e2C4XR0TAkag`, and the Gemini credential `FNkvkZfEl7yx4BXM`.

- [ ] **Step 1: Generate workflow 06**

Five nodes.

**Something failed** — `n8n-nodes-base.errorTrigger`, typeVersion `1`, `"parameters": {}`.

**Classify the failure** — `@n8n/n8n-nodes-langchain.chainLlm`, typeVersion `1.9`, **with `"onError": "continueRegularOutput"`**:

```json
{
  "promptType": "define",
  "text": "=Classify this n8n workflow failure as exactly one word: transient, config, or logic.\n\ntransient = network error, timeout, service unavailable, rate limit\nconfig = missing credential, bad URL, wrong parameter\nlogic = code error, bad data, failed assertion\n\nWorkflow: {{ $json.workflow.name }}\nNode: {{ $json.execution.lastNodeExecuted }}\nError: {{ $json.execution.error.message }}\n\nReply with one word only."
}
```

**Gemini for triage** — `@n8n/n8n-nodes-langchain.lmChatGoogleGemini`, typeVersion `1.1`, `{"modelName": "models/gemini-3-flash-preview", "options": {}}`, credential `googlePalmApi` → `FNkvkZfEl7yx4BXM`.

**Notify on Telegram** — `n8n-nodes-base.telegram`, typeVersion `1.2`, credential `telegramApi` → `ELi5e2C4XR0TAkag`:

```json
{
  "chatId": "1386558529",
  "text": "=Driftbell failure\n\nWorkflow: {{ $('Something failed').item.json.workflow.name.replace(/([_*\\[\\]`])/g, '\\\\$1') }}\nNode: {{ $('Something failed').item.json.execution.lastNodeExecuted.replace(/([_*\\[\\]`])/g, '\\\\$1') }}\nType: {{ ($json.text || 'unclassified').trim().replace(/([_*\\[\\]`])/g, '\\\\$1') }}\n\n{{ $('Something failed').item.json.execution.error.message.replace(/([_*\\[\\]`])/g, '\\\\$1') }}",
  "additionalFields": {"appendAttribution": false}
}
```

The escaping is not optional: Telegram parses these as Markdown and a node name like `Compute_drift` would otherwise open an unterminated italic span and get the whole request rejected.

**Record the incident** — `n8n-nodes-base.httpRequest`, typeVersion `4.2`, **with `"onError": "continueRegularOutput"`**:

```json
{
  "method": "POST",
  "url": "http://driftbell:8000/incidents",
  "sendBody": true,
  "specifyBody": "json",
  "jsonBody": "={{ JSON.stringify({ source: 'n8n:' + $('Something failed').item.json.workflow.name, severity: ($json.text || '').toLowerCase().includes('transient') ? 'low' : 'high', description: $('Something failed').item.json.execution.lastNodeExecuted + ': ' + $('Something failed').item.json.execution.error.message, classification: ($json.text || 'unclassified').trim() }) }}",
  "options": {"timeout": 30000}
}
```

Connections — Telegram and the recorder are **siblings** of the classify node, Telegram first, so a failed recording cannot suppress the alert:

```json
{
  "Something failed": {
    "main": [[{"node": "Classify the failure", "type": "main", "index": 0}]]
  },
  "Gemini for triage": {
    "ai_languageModel": [[{"node": "Classify the failure", "type": "ai_languageModel", "index": 0}]]
  },
  "Classify the failure": {
    "main": [[
      {"node": "Notify on Telegram", "type": "main", "index": 0},
      {"node": "Record the incident", "type": "main", "index": 0}
    ]]
  }
}
```

Envelope: `name` `Driftbell 06 — error handler`, `active: false`, `settings: {"executionOrder": "v1"}`, `pinData: {}`. Write with `indent=2`, `ensure_ascii=True`, CRLF, no trailing newline.

- [ ] **Step 2: Import it**

```bash
docker cp workflows/06-error-handler.json driftbell-n8n:/tmp/wf06.json
MSYS_NO_PATHCONV=1 docker compose exec -T n8n n8n import:workflow --input=/tmp/wf06.json
```

Add a 16-character alphanumeric `id` to the import copy only. Note the id — the next step needs it.

- [ ] **Step 3: Point every workflow at it**

Add to each of workflows 01–05, in the repo files:

```json
"settings": {"executionOrder": "v1", "errorWorkflow": "<workflow 06 id>"}
```

Re-import each with its existing id: `OFsYBs6PAnpU1X89` (01), `uK58bSXnT27q21kj` (02), `ckrDz97MjSom3amS` (03), `DsTCUjH7GLaVvYDw` (04), `1Imh3aSAcTYOGRHd` (05).

- [ ] **Step 4: Publish everything**

Reload n8n. Publish workflow 06 and re-publish 01–05, since every CLI import deactivates them. Check each for warning triangles.

- [ ] **Step 5: Test it the way BUILD_PLAN describes**

```bash
docker compose stop driftbell
```

Run workflow 01 with `drift_magnitude = 12`. `Ask the Driftbell agent` will fail to connect.

Expected: a Telegram message naming the workflow, the node and the error. **No incident row** — the agent that stores incidents is the one you just stopped. That gap is documented in the spec and is why Telegram is a sibling of the recorder rather than downstream of it.

```bash
docker compose start driftbell
```

- [ ] **Step 6: Test the case where recording works**

With the agent running, break something else instead — temporarily set the `Record the incident` URL in **workflow 03** to `http://driftbell:8000/nope`, publish, and trigger a retrain from the MCP client or Telegram approval.

Expected: a Telegram alert **and** a new incident row:

```bash
docker compose exec -T driftbell python -c "import sqlite3;c=sqlite3.connect('/data/driftbell.db');print(list(c.execute('SELECT occurred_at, source, severity, description FROM incidents ORDER BY occurred_at DESC LIMIT 2')))"
```

Restore the URL afterwards and re-publish.

- [ ] **Step 7: Confirm the loop closes**

The point of using incidents rather than a spreadsheet is that the agent reads them:

```bash
docker compose exec -T n8n sh -c 'wget -qO- http://driftbell:8000/history/documents' | head -c 400
```

Expected: the recorded failure appears as a document, so the ops chatbot can explain it and the diagnostic agent can weigh it against the next drift alert.

- [ ] **Step 8: Export and commit**

Export all six workflows from the UI, strip the instance-specific keys (`id`, `versionId`, `versionCounter`, `activeVersionId`, `createdAt`, `updatedAt`, `shared`, `staticData`, `meta`, `tags`, `triggerCount`, `isArchived`, `sourceWorkflowId`, `nodeGroups`, `versionMetadata`, `description`), keeping only `name`, `nodes`, `connections`, `settings`, `pinData`, `active`.

Confirm no secrets travelled:

```bash
grep -ci "AIza\|AAEuSsDe" workflows/06-error-handler.json
```

Expected: `0`.

```powershell
git add workflows/
git commit -m "Add workflow 06: error handler alerting and recording failures"
```

---

## Done when

Stopping the `driftbell` container and running workflow 01 produces a Telegram message naming the workflow, node and error instead of a silent red execution; a failure with the agent running also produces an incident row that appears in `/history/documents`; and `pytest -q` reports 53 passed.
