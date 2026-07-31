<div align="center">

<img src="assets/driftbell-wordmark.svg" width="380" alt="Driftbell">

**An ML drift watchman that investigates before it acts, and asks you first.**

[![License: MIT](https://img.shields.io/badge/license-MIT-8A8F98?style=flat-square)](LICENSE)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-7F77DD?style=flat-square)](https://github.com/langchain-ai/langgraph)
[![n8n](https://img.shields.io/badge/n8n-2.32-1D9E75?style=flat-square)](https://n8n.io)
[![Python](https://img.shields.io/badge/python-3.11+-7F8C99?style=flat-square)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-53%20passing-3FA45B?style=flat-square)](#tests)
[![Cost](https://img.shields.io/badge/running%20cost-%240-EF9F27?style=flat-square)](#cost)

</div>

---

<!-- Replace with the demo recording: inject drift → phone buzzes → approve → retrain -->
<!-- <p align="center"><img src="assets/demo.gif" width="720" alt="Driftbell end to end"></p> -->

Production models fail quietly. The data shifts, accuracy slides, and nobody
notices for weeks. Driftbell watches for that shift, works out *why* it happened,
and rings your phone before anything changes.

The part I find most interesting isn't the drift maths — it's the pause. A model
retrain is a decision somebody should sign off on, but a human takes hours to
answer and most automation can't wait that long without either blocking a worker
or forgetting what it was doing. Driftbell freezes the agent mid-execution,
writes its entire reasoning state to disk, and picks up exactly where it stopped
when you tap **Approve** — even if the process died in between.

```
 drift detected → agent investigates → proposal → 🔔 you approve → retrain → promote
      n8n             LangGraph        LangGraph       n8n           n8n       n8n
```

**Two layers, deliberately.** A canvas can't loop, and an agent shouldn't hold
your credentials. **n8n** owns the macro plane: schedules, integrations, the
audit trail, and the approval you tap on your phone. **LangGraph** owns the micro
plane: cycles, conditional edges, self-critique, and checkpointed state. Every
irreversible action lives in n8n; the agent only ever proposes.

## What it does

- **Detects drift** on a schedule or on demand, computing Population Stability
  Index and a two-sample Kolmogorov–Smirnov statistic in plain JavaScript — no
  numpy, no scipy, nothing to install inside the automation layer.
- **Investigates before concluding.** The agent calls tools against real run
  history, feature statistics, the model registry and past pipeline incidents,
  then critiques its own conclusion in a bounded reflection loop before
  committing to a verdict of RETRAIN, IGNORE or ESCALATE.
- **Stops and asks.** A verdict that would change something freezes the graph at
  a `human_gate` node and sends the proposal to Telegram with Approve and Reject
  buttons. A verdict of IGNORE skips the gate entirely — nobody gets paged for a
  non-action.
- **Survives anything.** State is checkpointed to SQLite against a `thread_id`.
  Kill the container mid-decision, restart it, tap Approve an hour later, and the
  same thread resumes at the node it stopped on.
- **Retrains and evaluates** on approval: fits a challenger, scores it against
  the drifted live split, and promotes it to champion **only if n8n decides it
  won** — the comparison happens on the canvas, not inside the agent.
- **Answers questions about itself.** An in-app chatbot fuses two sources: exact
  numbers from a query, and reasoning from a vector store over the agent's own
  recorded rationales. Ask *"why was churn_clf retrained?"* and it quotes what
  the agent actually concluded.
- **Exposes itself over MCP,** so Claude Desktop or any MCP client can read the
  model status and trigger a retrain through the same workflow a human approval
  triggers. Bearer-authenticated.
- **Tells you when it breaks.** Any workflow failure classifies itself, alerts
  Telegram, and records an incident the agent will weigh against the *next* drift
  alert.

## How the two layers meet

This is the part worth reading. n8n's wait-and-resume pairs with LangGraph's
`interrupt()`, joined by nothing but a `thread_id`.

```
START → gather_evidence → reason ─┬─→ tools → reason        (tool cycle)
                                  │
                                  └─→ critique ─┬─→ reason   (reflection cycle)
                                                │
                                                └─→ propose → human_gate
                                                                  │
                                              approve → execute → END
                                              reject  ─────────── END
                                              IGNORE  ─── skips the gate entirely
```

`gather_evidence` is deliberately not an LLM call — it seeds the scratchpad from
the alert itself, which keeps the transcript reproducible and saves a call on
every run. `reason` may emit tool calls, which routes back through `tools` and
around again. `critique` asks the model whether its own evidence is sufficient,
looping back to `reason` up to `MAX_ITERATIONS` times before forcing a verdict.

Then `human_gate` calls `interrupt()`. That raises out of the graph entirely. The
checkpointer has already written every message, every tool result and the
proposal to SQLite. Nothing below that line runs until someone resumes the thread
with `Command(resume=...)`.

**Why this matters:** the process holding that state can die. A different
process, hours later, loads the same `thread_id` and continues:

```
process A paused: awaiting_approval
--- process A exited, memory gone ---
process B sees: ['human_gate']
process B resumes: {'status': 'approved', 'action': 'RETRAIN', ...}
```

There is a test for exactly this. It builds a graph, runs it to the gate, drops
every reference to it, constructs a brand-new graph and checkpointer against the
same file, and resumes. It passes because nothing is held in the Python object —
each `make_checkpointer` call opens its own connection, so the only channel
between the two graphs is the file on disk.

## The six workflows

| # | Workflow | What it demonstrates |
|---|---|---|
| **01** | Ingest & monitor | Schedule + form triggers, drift maths in a Code node, HTTP call to the agent, branching on the response, error output routing |
| **02** | Approval loop | Telegram `sendAndWait` with approval buttons, execution parked for as long as the human takes, resume by `thread_id` |
| **03** | Retrain & evaluate | Sub-workflow invocation, champion-vs-challenger comparison **on the canvas**, conditional promotion |
| **04** | Ops agent | Chat trigger, AI Agent with memory, in-memory vector store over the agent's own reasoning, Gemini chat + embeddings |
| **05** | MCP server | MCP Server Trigger exposing two workflows as tools to external clients, bearer auth |
| **06** | Error handler | Error Trigger on all five above, LLM triage that cannot block the alert, incident recorded back into the agent's evidence |

Workflow 06 is set as the error workflow on 01–05. It points at nothing itself —
an error handler that reports its own failures to itself would loop.

**The loop worth noticing:** an n8n failure becomes a row in `incidents`. The
agent's system prompt tells it that *a drift alert coinciding with an ingestion
incident is usually a bug, not drift*, and `get_pipeline_incidents` is one of its
four tools. So a pipeline failure today makes tomorrow's diagnosis more sceptical.

## Project layout

```
driftbell/
├── app/
│   ├── graph.py          # the LangGraph agent: nodes, cycles, interrupt()
│   ├── state.py          # typed graph state; reducers for messages + evidence
│   ├── tools.py          # the four tools the agent can call, all over SQLite
│   ├── llm.py            # provider factory: gemini | groq | ollama | stub
│   ├── training.py       # challenger fitting, metrics, champion promotion
│   ├── history.py        # everything n8n can't reach: runs, registry, incidents
│   └── main.py           # the HTTP contract; thin routing over the above
├── workflows/            # exported n8n JSON — import these, don't hand-edit
├── tests/                # 53 tests, all offline under LLM_PROVIDER=stub
├── docs/superpowers/     # the design docs and plans each phase was built from
├── seed_db.py            # synthetic MLOps history + labelled training samples
├── docker-compose.yml    # n8n :5678, agent :8000, cloudflared tunnel
└── Dockerfile            # the agent image
```

## Running it locally

You need Python 3.11+ and Docker.

### 1. The agent on its own

```bash
python -m venv .venv
.venv\Scripts\activate          # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env
python seed_db.py
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000/docs> and fire `/diagnose` from the Swagger UI:

```json
{"drift_report": {"model_name": "churn_clf", "psi": 0.284,
 "drifted_features": ["monthly_spend"]}}
```

You'll get back `status: awaiting_approval` and a `thread_id`. Post that to
`/resume` with `{"decision": "approve"}` and the graph completes.

**No API key is needed.** `LLM_PROVIDER=stub` is a scripted offline model that
walks the graph through every edge, including the tool loop and the reflection
cycle. Swap it for `gemini`, `groq` or `ollama` and nothing else changes.

### 2. The whole stack

```bash
docker compose up -d
```

n8n comes up on <http://localhost:5678>. Create the owner account (local, free),
then **Workflows → Import from File** for each file in `workflows/`, and publish
them. The agent is reachable from n8n at `http://driftbell:8000` — inside the n8n
container, `localhost` is n8n itself.

### 3. Telegram approvals

Message **@BotFather**, send `/newbot`, keep the token. Message your new bot
once, then read your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`. Add the token in n8n as a
**Telegram API** credential and put your chat id in workflow 02's Telegram node.

### 4. The tunnel (required for Telegram, not optional)

Telegram rejects inline keyboard buttons pointing at `localhost` — it validates
the URL when the message is sent, so **no approval message can be delivered
without a public HTTPS address.** `docker compose up -d` starts a cloudflared
quick tunnel for this. Take the hostname from its logs, put it in `.env` as
`WEBHOOK_URL`, and recreate n8n:

```bash
docker compose logs cloudflared | grep trycloudflare.com
# then set WEBHOOK_URL in .env
docker compose up -d --force-recreate n8n
```

Quick-tunnel hostnames change on every restart. See [Limitations](#limitations).

### Tests

```bash
pytest -q          # 53 passed
```

Every test runs offline with no API key. They cover the drift maths, the seeding,
the graph's four terminal paths, training and promotion, the history endpoints,
and the full HTTP contract.

## The HTTP contract

Everything except `/health` sits behind a shared-secret header when
`SERVICE_TOKEN` is set. In n8n, add a Header Auth credential with the name
`Authorization` and the value `Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness probe. Also reports which LLM provider is active. |
| `POST` | `/diagnose` | Runs the graph until the human gate. Returns the proposal and a `thread_id`. |
| `POST` | `/resume` | Resumes a frozen thread with `approve` or `reject`. |
| `POST` | `/train` | Fits a challenger, records a run. **Never promotes.** |
| `POST` | `/promote` | Moves the champion. Called by n8n only after it compares F1. |
| `GET` | `/history` | Runs, registry, incidents and past proposals, as structured JSON. |
| `GET` | `/history/documents` | The same history as prose, for embedding. |
| `POST` | `/incidents` | Records a workflow failure. Called by the error handler. |
| `GET` | `/threads/{id}` | Full audit trail: verdict, evidence, which node the graph is parked on. |

`/diagnose` returns one of two shapes, and n8n's Switch node branches on
`status`:

```json
{ "status": "awaiting_approval", "thread_id": "drift-7971e1d5e05e",
  "proposal": { "verdict": "RETRAIN", "confidence": 0.82, "rationale": "..." } }
```

```json
{ "status": "completed", "decision": "approve", "verdict": "RETRAIN",
  "outcome": { "status": "approved", "action": "RETRAIN" } }
```

`decision` is `approve`, `reject`, or `not_required` when the gate was skipped.
It exists because all three cases return `status: completed`, and leaving n8n to
infer the difference from whether `outcome` happened to be present was an
implicit contract nothing documented.

## Limitations

Written honestly, because most of these were found by using the thing rather
than imagined while designing it.

- **A retrain incorporates no new information.** Training is deterministic
  (`random_state=42`) and the `samples` table is generated once from a fixed
  seed, so every challenger is the same model fitted on the same data. `v13` and
  `v14` have identical metrics to four decimal places. Determinism was chosen so
  the promotion decision would be testable; the consequence — that retraining is
  substantively a no-op — follows from it. A real system's live split would
  accumulate fresh production data between runs.
- **Promotion on F1 alone traded accuracy away silently.** The champion moved
  from v12 to v14 because F1 rose from 0.781 to 0.8348. Accuracy simultaneously
  *fell* from 0.842 to 0.757, while recall rose to 0.896. For churn that trade is
  defensible, but the single-metric gate makes it without recording that
  accuracy regressed.
- **The error handler can't record failures of the agent itself.** Incidents live
  in the agent's database, so if that container is down you get the Telegram
  alert and no incident row. That's why the alert is a sibling of the recorder
  rather than downstream of it. Fixing it properly needs a store outside both
  containers.
- **The tunnel is the most fragile part of the system.** Cloudflare quick tunnels
  are ephemeral — ours died after roughly 14 hours and took Telegram approvals
  *and* the MCP server down with it, silently. Recovery is four manual steps. A
  named tunnel or a reserved ngrok domain would fix it.
- **The vector store is in-memory.** An n8n restart empties it, and the chatbot's
  retrieval then returns nothing with no error. Re-run the indexing branch.
- **`SERVICE_TOKEN` unset means the agent is open.** Auth is skipped entirely
  when the variable is empty, which keeps `docker compose up` working with no
  configuration. The service warns about this at startup, but the default is
  still open.
- **Under the stub model, `IGNORE` and `ESCALATE` are only reachable in tests.**
  The scripted stub always concludes RETRAIN, so those branches are exercised by
  substituting a model in the test suite rather than by running the system.
- **n8n's AI node ecosystem has version-compatibility gaps.** `toolHttpRequest`
  implements only `supplyData` while the AI Agent v3.1 invokes tools through
  `execute`, so the two cannot work together despite shipping in the same
  release. The error names neither node nor version.

## Things I'd add next

- **Make retraining mean something** — append live samples over time so a
  challenger sees data the champion never did, which is the single change that
  would turn the retrain loop from mechanism into substance.
- **Gate promotion on more than one metric**, or record explicitly that accuracy
  regressed and why that was acceptable.
- **A durable tunnel** so approval links survive a restart, and the demo doesn't
  depend on a hostname that expires.
- **Persist the vector store** so the chatbot survives an n8n restart without a
  manual reindex.
- **Alert on agent death**, which the current error handler structurally cannot
  do — a lightweight external watchdog on `/health`.
- **Richer drift signals** than PSI and KS: prediction drift and label delay
  matter more than input drift for most real churn models.

## Cost

Zero. LangGraph, FastAPI, n8n and SQLite are open source; the LLM runs on a free
tier or locally; the tunnel is a free quick tunnel; hosting is your own machine.
There is no paid service anywhere in the stack, and `LLM_PROVIDER=stub` runs the
entire graph with no key and no network at all.
