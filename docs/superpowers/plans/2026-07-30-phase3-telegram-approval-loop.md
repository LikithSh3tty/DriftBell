# Phase 3 — Telegram Approval Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the human-in-the-loop circuit — a drift alert reaches Telegram, a person decides, and the parked LangGraph thread resumes on the same `thread_id`.

**Architecture:** Add an explicit `decision` field to the agent's HTTP response so n8n can branch on what the human said rather than inferring it. Build workflow 02 around the Telegram node's native `sendAndWait` operation, which parks the execution and resumes it on the button press. Wire workflow 01's terminal NoOp to call it.

**Tech Stack:** n8n 2.32.6, Telegram Bot API, FastAPI, LangGraph `interrupt()` + SqliteSaver.

**Spec:** `docs/superpowers/specs/2026-07-30-phase3-telegram-approval-loop-design.md`

## Global Constraints

- **Zero paid services.** Telegram Bot API is free; n8n is self-hosted. `LLM_PROVIDER=stub` must keep working offline with no key.
- **The agent never performs irreversible actions.** It proposes; n8n executes. `execute` stays a thin recorder.
- **`interrupt()` stays in `human_gate` only.** No task here adds a pause anywhere else in the graph.
- **n8n reaches the agent at `http://driftbell:8000`** — the container name. Inside the n8n container, `localhost` is n8n itself.
- **n8n Code nodes are JavaScript with no package access.** No task here adds a Python Code node.
- **Never hand-write n8n workflow JSON and assume it works.** Generate it, import it, fix what breaks in the UI, export from the UI, commit that export.
- **The Telegram bot token is a secret.** It lives in the n8n credential store only — never in committed workflow JSON, never in `.env`, never in a commit message.
- Python 3.11+, type hints on function signatures, docstrings that say *why*.
- Commit messages carry NO Claude attribution trailers.
- Platform is Windows; `docker compose exec` paths need `MSYS_NO_PATHCONV=1` in Git Bash or `/tmp/...` is rewritten to a Windows path.

---

### Task 1: Add the `decision` field to the agent response

Deliverable: `/resume` and `/threads/{id}` report `approve`, `reject` or `not_required` explicitly, covered by tests.

**Files:**
- Modify: `app/main.py` (the `_shape` function)
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the JSON contract Task 2's workflow branches on —
  `{"status": "completed", "decision": "approve" | "reject" | "not_required", ...}`.
  On `status: "awaiting_approval"` the `decision` key is absent.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
def test_approved_resume_reports_decision_approve(client: TestClient) -> None:
    """n8n branches on this string, so it is a contract, not a convenience."""
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
    assert body["outcome"] == {}  # execute never ran


def test_awaiting_approval_carries_no_decision(client: TestClient) -> None:
    """Nothing has been decided yet, so the field must not claim otherwise."""
    diagnosed = client.post(
        "/diagnose", json={"drift_report": HIGH_PSI_REPORT}, headers=AUTH
    )

    body = diagnosed.json()
    assert body["status"] == "awaiting_approval"
    assert "decision" not in body
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -k decision -v`
Expected: FAIL — `KeyError: 'decision'` on the first two.

- [ ] **Step 3: Implement**

In `app/main.py`, replace the completed-branch return inside `_shape` with:

```python
def _shape(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Flatten the graph result into the JSON n8n branches on."""
    interrupts = result.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        return {"status": "awaiting_approval", "thread_id": thread_id, "proposal": payload}

    # `status` says whether the graph is done; `decision` says what the human
    # chose. Two questions, two fields — inferring the second from whether
    # `outcome` happens to be present is the ambiguity this removes.
    human = result.get("human_decision")
    return {
        "status": "completed",
        "thread_id": thread_id,
        "decision": human if human in ("approve", "reject") else "not_required",
        "verdict": result.get("verdict"),
        "confidence": result.get("confidence"),
        "rationale": result.get("rationale"),
        "outcome": result.get("outcome", {}),
    }
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: **17 passed** (14 existing + 3 new). The six pre-existing API tests must still pass untouched — the change is additive. If any previously-passing test now fails, stop and report it rather than editing the old test.

- [ ] **Step 5: Verify the IGNORE path reports `not_required`**

An IGNORE verdict never reaches the gate, so `human_decision` is absent. Confirm the derived value directly:

```powershell
.venv\Scripts\python.exe -c "from app.main import _shape; print(_shape('t', {'verdict':'IGNORE'})['decision'])"
```

Expected: `not_required`

- [ ] **Step 6: Commit**

```powershell
git add app/main.py tests/test_api.py
git commit -m "Report the human decision explicitly in the resume response"
```

---

### Task 2: Build workflow 02, the approval loop

Deliverable: `workflows/02-approval-loop.json`, imported into n8n, Telegram credential attached, exported from the UI, committed.

The Telegram node's credential can only be selected in the n8n UI, and its `sendAndWait` parameter schema is not reliably derivable from the minified node source. So this task generates a skeleton, imports it, finishes configuration in the UI, and commits the UI export — exactly the process the Global Constraints require.

**Files:**
- Create: `workflows/02-approval-loop.json`

**Interfaces:**
- Consumes: the `decision` contract from Task 1.
- Produces: a workflow whose Execute Workflow Trigger accepts `thread_id`, `verdict`, `confidence`, `rationale`, `model_name`. Task 3 calls it.

- [ ] **Step 1: Confirm the human prerequisites are done**

The bot token and chat ID must exist before this task can be finished:

- Message **@BotFather**, send `/newbot`, keep the token.
- Message the new bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id`.
- In n8n: **Credentials → New → Telegram API**, paste the token, save.

If the credential does not exist yet, stop here — the rest of the task cannot be verified.

- [ ] **Step 2: Generate the skeleton**

Write a generator script rather than hand-editing JSON, so the structure is reproducible. Node types, verified present in n8n 2.32.6:

| Node | Type | typeVersion |
| --- | --- | --- |
| Approval requested | `n8n-nodes-base.executeWorkflowTrigger` | 1.1 |
| Ask on Telegram | `n8n-nodes-base.telegram` | 1.2 |
| Approved on Telegram? | `n8n-nodes-base.if` | 2 |
| Resume as approved | `n8n-nodes-base.httpRequest` | 4.2 |
| Resume as rejected | `n8n-nodes-base.httpRequest` | 4.2 |
| Agent recorded approval? | `n8n-nodes-base.if` | 2 |
| Retrain approved | `n8n-nodes-base.noOp` | 1 |
| Rejected, logged | `n8n-nodes-base.noOp` | 1 |

Both HTTP nodes POST to `http://driftbell:8000/resume` with JSON bodies:

```json
{"thread_id": "={{ $('Approval requested').item.json.thread_id }}", "decision": "approve", "note": "approved in Telegram"}
```

and the same with `"decision": "reject"`, `"note": "rejected in Telegram"`.

`Approved on Telegram?` branches on `={{ $json.data.approved }}` being true — the payload shape confirmed by reading `nodes/Telegram/hitl/webhook.js` in the running image.

`Agent recorded approval?` branches on `={{ $json.decision }}` equalling `approve` — the agent's own record from Task 1, deliberately not Telegram's answer, so a `/resume` that silently failed cannot be reported as an approval.

- [ ] **Step 3: Import it**

```bash
docker cp workflows/02-approval-loop.json driftbell-n8n:/tmp/wf02.json
MSYS_NO_PATHCONV=1 docker compose exec -T n8n n8n import:workflow --input=/tmp/wf02.json
```

Expected: `Successfully imported 1 workflow.`

- [ ] **Step 4: Finish the Telegram node in the UI**

Open the workflow at <http://localhost:5678>, open **Ask on Telegram**, and set:

- **Credential:** the Telegram API credential from Step 1
- **Resource:** Message, **Operation:** Send and Wait for Response
- **Chat ID:** your chat ID
- **Response Type:** Approval
- **Message:** include the model, verdict, confidence and rationale, e.g.
  `Drift on {{ $json.model_name }}: {{ $json.verdict }} (confidence {{ $json.confidence }})\n\n{{ $json.rationale }}`
- Leave **Approve Within Chat** OFF — it requires public HTTPS, which localhost is not.

Fix any node showing a parameter warning. Publish.

- [ ] **Step 5: Export what n8n actually stored**

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T n8n n8n export:workflow --id=<ID> --output=/tmp/wf02-out.json
docker cp driftbell-n8n:/tmp/wf02-out.json ./wf02-out.json
```

Strip the instance-specific keys before committing — `id`, `versionId`, `versionCounter`, `activeVersionId`, `createdAt`, `updatedAt`, `shared`, `staticData`, `meta`, `tags`, `triggerCount`, `isArchived`, `sourceWorkflowId`, `nodeGroups`, `versionMetadata`, `description` — keeping only `name`, `nodes`, `connections`, `settings`, `pinData`, `active`. Those extras tie the file to this n8n instance and embed the owning user's share record.

**Verify no credential secret leaked:** the export must reference the credential by id and name only. Confirm the bot token does not appear:

```bash
grep -ci "<first 8 chars of your bot token>" workflows/02-approval-loop.json
```

Expected: `0`. If it is not 0, stop — do not commit.

- [ ] **Step 6: Commit**

```powershell
git add workflows/02-approval-loop.json
git commit -m "Add workflow 02: the Telegram approval loop"
```

---

### Task 3: Wire workflow 01 to call workflow 02

Deliverable: submitting the drift form sends a Telegram message; approving it resumes the parked thread.

**Files:**
- Modify: `workflows/01-ingest-and-monitor.json` (the `→ Approval workflow (02)` node)

**Interfaces:**
- Consumes: workflow 02 from Task 2, by workflow id.

- [ ] **Step 1: Replace the NoOp with an Execute Workflow node**

In the n8n UI, open workflow 01, delete `→ Approval workflow (02)`, and add an **Execute Workflow** node in its place:

- **Source:** Database, **Workflow ID:** workflow 02's id
- **Mode:** Run once with all items
- Pass through `thread_id`, `verdict`, `confidence`, `rationale`, `model_name` from the agent's response
- Reconnect `Needs a human?` → true → this node
- Name it `→ Approval workflow (02)` so the canvas keeps reading the same

Publish.

- [ ] **Step 2: Fire the loop**

Execute workflow 01, open `Manual drift injection`, submit `drift_magnitude = 12`.

Expected: a Telegram message arrives containing the model name, verdict and rationale, with Approve and Decline options. The n8n execution stays running — parked — rather than completing.

- [ ] **Step 3: Approve it**

Click the approval link from your laptop browser (the phone link will not resolve until a tunnel exists).

Expected: the parked execution turns green and routes through `Resume as approved` → `Agent recorded approval?` → `Retrain approved`.

- [ ] **Step 4: Confirm the agent agrees**

```powershell
Invoke-RestMethod "http://localhost:8000/threads/<thread_id>" | ConvertTo-Json -Depth 5
```

Expected: `next_nodes` empty, `human_decision` is `approve`, `outcome.status` is `approved`, `outcome.action` is `RETRAIN`.

This is the whole point of the phase: the thread that froze at `human_gate` was resumed by a decision that arrived from a different process, joined only by `thread_id`.

- [ ] **Step 5: Test the reject path**

Submit `drift_magnitude = 12` again, and Decline this time.

Expected: routes through `Resume as rejected` → `Rejected, logged`. `/threads/{id}` reports `human_decision: reject` and no `outcome`.

- [ ] **Step 6: Export workflow 01 and commit**

Export from the UI, strip the same instance-specific keys listed in Task 2 Step 5, and commit:

```powershell
git add workflows/01-ingest-and-monitor.json
git commit -m "Wire workflow 01 into the approval loop"
```

---

## Done when

Submitting the form sends a Telegram message; approving turns the parked execution green and `/threads/{id}` reports `human_decision: approve` with a `RETRAIN` outcome; declining a second run reports `reject` with no outcome. `pytest -q` reports 17 passed.
