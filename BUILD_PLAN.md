# Driftbell — build plan

Eight phases. Each is one sitting. Finish a phase, commit, stop — the project
stays demoable at every checkpoint, which matters if placement season starts
early.

Every phase has a **prompt** you can paste into Claude Code and a **done when**
you can actually check. Don't move on until the check passes.

---

## Phase 0 — Repo and Claude Code setup

**Goal:** a git repo Claude Code understands before it writes anything.

```bash
mkdir driftbell && cd driftbell
# copy the files from this bundle in here
git init && git add -A && git commit -m "Phase 0: agent service and workflow 01"
```

Create the repo on GitHub named exactly `driftbell` (lowercase), add the
description and topics from the comment at the bottom of `README-header.md`.

Then:

```bash
claude
```

`CLAUDE.md` is already written and Claude Code loads it automatically at the
start of every session — that file is what stops it from suggesting paid
services or importing scipy into a Pyodide node. Read it once yourself so you
know what constraints you've set.

> **Prompt:** Read CLAUDE.md and the code in app/. Give me a five-line summary of
> what this service does and where the human-in-the-loop pause happens. Don't
> change anything yet.

If the summary is wrong, `CLAUDE.md` needs fixing — do that now, not later.

**Done when:** repo is on GitHub and Claude Code can describe the interrupt
mechanism back to you correctly.

---

## Phase 1 — Get the agent running

**Goal:** `/diagnose` and `/resume` work on your machine.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python seed_db.py
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000/docs> and fire `/diagnose` from the Swagger UI with
this body:

```json
{"drift_report": {"model_name": "churn_clf", "psi": 0.284,
 "drifted_features": ["monthly_spend"]}}
```

Copy the `thread_id`, then call `/resume` with `{"thread_id": "...",
"decision": "approve"}`.

> **Prompt:** Add a pytest suite covering the graph: one test that a high-PSI
> report reaches the human gate, one that a resumed thread completes, and one
> that killing and rebuilding the graph object still resumes the same thread_id.
> Use LLM_PROVIDER=stub so it runs offline.

**Done when:** `pytest -q` is green and you can explain what `thread_id` is for.

---

## Phase 2 — n8n up, workflow 01 imported

**Goal:** the drift pipeline fires end to end.

```bash
docker compose up -d
```

Open <http://localhost:5678>, create the owner account (local only, free), then
**Workflows → Import from File → `workflows/01-ingest-and-monitor.json`**.

Execute the workflow manually. Then open the **Manual drift injection** form
node, grab the Test URL, and submit `drift_magnitude = 12`. Watch the drift node
output a PSI around 0.7 and the HTTP node come back with a proposal.

If a node shows a version warning, delete just that node, drag a fresh one in,
copy the parameters, reconnect.

> **Prompt:** The workflow at workflows/01-ingest-and-monitor.json imported with
> [paste the exact error]. Fix the node parameters for my n8n version. Don't
> change the drift maths.

**Done when:** submitting the form with `12` produces `status:
awaiting_approval` in the HTTP node output, and with `0` it takes the stable
branch.

---

## Phase 3 — Workflow 02: the approval loop ⭐

**Goal:** the part the whole project exists for.

Get a bot token first: message **@BotFather** on Telegram, send `/newbot`,
follow the prompts, keep the token. Then message your new bot once and visit
`https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat id.

In n8n: **Credentials → New → Telegram API**, paste the token.

> **Prompt:** Build workflow 02 as an importable n8n JSON at
> workflows/02-approval-loop.json. It receives thread_id, verdict, confidence and
> rationale from workflow 01 via an Execute Workflow Trigger. It sends a Telegram
> message with inline keyboard buttons Approve and Reject carrying the thread_id
> in callback_data, then parks on a Wait node configured to resume on webhook. A
> Telegram Trigger workflow catches the button press and calls the Wait node's
> resume URL. On resume it POSTs to http://driftbell:8000/resume with the
> decision, then routes the outcome onward. Follow the "never hand-write JSON and
> assume it works" rule in CLAUDE.md.

Then wire workflow 01's `→ Approval workflow (02)` node to call it: replace the
NoOp with an **Execute Workflow** node pointing at 02.

**Done when:** you submit the form with `12`, your phone buzzes, you tap
Approve, and the n8n execution that was parked turns green. Record this — it's
your demo.

---

## Phase 4 — Workflow 03: retrain and evaluate

**Goal:** the approved action actually does something.

> **Prompt:** Add a scikit-learn training endpoint to the agent service:
> POST /train retrains a small classifier on the synthetic data, writes metrics
> to the runs table, and returns champion vs challenger metrics. Keep it under
> 100 lines and CPU-only. Then build workflows/03-retrain-and-evaluate.json as a
> sub-workflow that calls it, compares the two models, and promotes the
> challenger in the registry table only if it wins on F1.

Use `Split In Batches` if you evaluate across several data slices — it's another
n8n construct worth having on the canvas.

**Done when:** an approved RETRAIN produces a new row in `runs` and, if it wins,
a new champion in `registry`.

---

## Phase 5 — Workflow 04: the ops chatbot

**Goal:** the RAG component, plus the MCP trick.

> **Prompt:** Build workflows/04-ops-agent.json: a Chat Trigger feeding an AI
> Agent node with a Simple Memory sub-node and a Vector Store tool over the runs,
> incidents and past proposals from the SQLite database. Use the Google Gemini
> chat model node. The agent answers questions like "why was churn_clf retrained
> last Tuesday". Also add an MCP Server Trigger exposing two workflows as tools:
> trigger_retrain and get_model_status.

The MCP Server Trigger is the newest thing in n8n and almost nobody has it on a
CV. Once it's live you can point Claude Desktop or any MCP client at your n8n
instance and have it call your workflows as tools.

**Done when:** you can ask the chat "what happened to churn_clf this week" and
get an answer grounded in your actual run history.

---

## Phase 6 — Workflow 05: error handling

**Goal:** looks like production, not a demo.

> **Prompt:** Build workflows/05-error-handler.json using an Error Trigger. It
> classifies the failure with an LLM into transient / config / logic, formats an
> incident summary with the workflow name, node name and error message, sends it
> to Telegram, and appends a row to a Google Sheet. Then set it as the error
> workflow on workflows 01 through 04 and document that in the README.

**Done when:** you stop the `driftbell` container, run workflow 01, and get an
incident message instead of a silent failure.

---

## Phase 7 — Polish and interview prep

> **Prompt:** Write the final README.md. Use README-header.md as the opening.
> Include the architecture diagram, a quickstart, a table of all five workflows
> with what each one demonstrates, and an honest limitations section. Don't
> oversell it — no "production ready" badges.

Then, separately:

- Record a 60-second screen capture: inject drift → phone buzzes → tap approve →
  retrain completes. Put the GIF at the top of the README.
- Take a screenshot of each n8n canvas for the README.
- Write `ARCHITECTURE.md` explaining the two-layer split and why.

> **Prompt:** Interview me on this project like a senior ML engineer would.
> Ask about the two-layer split, why not Airflow, what happens if the LLM
> hallucinates a verdict, and how the system behaves if the approval never
> arrives. Push back on weak answers.

**Done when:** you can explain any file in the repo without opening it.

---

## Working rules for Claude Code

- **Plan mode for anything multi-file.** Shift+Tab to enter it. Read the plan,
  push back, then let it run. See
  <https://docs.claude.com/en/docs/claude-code/overview> for the current docs.
- **Never trust generated n8n JSON.** Import it, fix what breaks in the UI,
  export from the UI, commit that export. Node schemas change between versions
  and are the single most likely thing to go wrong.
- **One phase per session.** Long sessions drift; a fresh session re-reads
  CLAUDE.md and starts clean.
- **Commit before every phase boundary.** If a session goes sideways,
  `git checkout .` costs you nothing.
- When something breaks, paste the *actual* error, not a description of it.

## Pacing

Roughly seven or eight evenings if things go smoothly, and they won't entirely —
budget ten. Phases 0 to 3 give you a complete, demoable project on their own. If
placement interviews start before you finish, stop after Phase 3 and polish that
instead of rushing Phases 4 to 6 into something you can't defend.
