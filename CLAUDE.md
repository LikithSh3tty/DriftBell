# Driftbell

An ML drift watchman. Two orchestration layers:

- **n8n** (self-hosted, `docker compose`) owns the macro plane: schedule and form
  triggers, HTTP calls, credentials, retries, the Telegram approval, the audit
  trail. Workflows live in `workflows/*.json`.
- **LangGraph** (FastAPI service in `app/`) owns the micro plane: a cyclic agent
  that gathers evidence, critiques itself, and calls `interrupt()` to freeze
  mid-graph until a human approves.

The interesting seam is n8n's `Wait` node paired with LangGraph's `interrupt()`,
joined by `thread_id`. Both sides checkpoint to disk, so an approval that arrives
hours later still lands on a live graph.

## Layout

```
app/            LangGraph agent + FastAPI  (state.py, graph.py, tools.py, llm.py, main.py)
workflows/      Exported n8n workflow JSON — import these into n8n
assets/         Logo and wordmark
seed_db.py      Creates synthetic MLOps history in SQLite
docker-compose.yml   n8n on :5678, agent on :8000
```

## Commands

```bash
docker compose up -d              # both services
docker compose logs -f driftbell  # agent logs
python seed_db.py                 # reset synthetic history
uvicorn app.main:app --reload     # run the agent outside docker
pytest -q                         # tests (once Phase 7 adds them)
```

## Hard constraints — do not violate

1. **Zero paid services.** LLM calls go through Gemini free tier, Groq free
   tier, or local Ollama. `LLM_PROVIDER=stub` must always keep working offline
   with no key — every change must still run under the stub.
2. **No scipy, numpy, or pandas inside n8n Code nodes.** Those nodes run under
   Pyodide. Statistics there are pure standard library. (The FastAPI service is
   a normal Python process and has no such limit.)
3. **The agent never performs irreversible actions.** It proposes; n8n executes.
   Credentials and side effects stay in n8n.
4. **`interrupt()` stays in `human_gate` only.** Nothing else in the graph may
   pause.
5. n8n reaches the agent at `http://driftbell:8000` (container name). Inside the
   n8n container, `localhost` is n8n itself.

## Working agreements

- Use plan mode for anything touching more than one file. Show the plan first.
- **Never hand-write n8n workflow JSON and assume it works.** Generate it, import
  it into n8n at localhost:5678, fix what breaks, then export from the n8n UI
  and commit that export. The UI export is the source of truth.
- Keep `.env` and any bot tokens out of git. `.env.example` is the committed one.
- Commit at the end of each phase with a message naming the phase.
- Prefer small, readable modules over clever ones — this is a portfolio project
  and I need to explain every line in an interview.

## Conventions

- Python 3.11+, type hints on function signatures, docstrings that say *why*
  rather than restating the code.
- Node names in n8n are sentence case and describe the action
  ("Ask the Driftbell agent", not "HTTP Request1").
