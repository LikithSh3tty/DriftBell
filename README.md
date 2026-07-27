# Driftbell: diagnostic agent service

The reasoning half of Driftbell. n8n handles triggers, integrations, credentials
and the approval UI; this service handles everything n8n's canvas cannot express:
cyclic reasoning, self-critique with a bounded iteration count, and a graph that
freezes mid-execution while a human decides.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # LLM_PROVIDER=stub works with no key at all
python seed_db.py             # synthetic run history, feature stats, incidents
uvicorn app.main:app --reload --port 8000
```

Swap `LLM_PROVIDER=stub` for `gemini`, `groq` or `ollama` once you have a key
(or Ollama running locally). Nothing else changes.

## The graph

```
START -> gather_evidence -> reason -+-> tools -> reason        (tool cycle)
                                    |
                                    +-> critique -+-> reason   (reflection cycle)
                                                  |
                                                  +-> propose -> human_gate
                                                                    |
                                                    approve -> execute -> END
                                                    reject  ------------> END
```

`propose` short-circuits to END when the verdict is IGNORE, so no human is
interrupted for a non-action.

## HTTP contract for n8n

### 1. Diagnose (n8n HTTP Request node, after drift detection)

```bash
curl -X POST localhost:8000/diagnose \
  -H "Content-Type: application/json" \
  -d '{"drift_report":{"model_name":"churn_clf","psi":0.284,"ks_statistic":0.19,
       "p_value":0.002,"drifted_features":["monthly_spend","sessions_7d"],
       "window_start":"2026-07-09","window_end":"2026-07-23","n_samples":18422}}'
```

```json
{
  "status": "awaiting_approval",
  "thread_id": "drift-7971e1d5e05e",
  "proposal": { "verdict": "RETRAIN", "confidence": 0.82, "rationale": "..." }
}
```

Branch your n8n Switch node on `status`: `awaiting_approval` goes to the Wait
node, `completed` goes straight to logging.

### 2. Resume (n8n HTTP Request node, after the Wait node returns)

```bash
curl -X POST localhost:8000/resume \
  -H "Content-Type: application/json" \
  -d '{"thread_id":"drift-7971e1d5e05e","decision":"approve","note":"approved in Telegram"}'
```

### 3. Audit / poll

`GET /threads/{thread_id}` returns the verdict, iteration count, full evidence
trail and which node the graph is parked on. Feed this into Google Sheets for a
human-readable run log, and into the vector store that backs the n8n ops chatbot.

### 4. Health

`GET /health`. Point the n8n error workflow at this.

## Why the state survives a restart

State is checkpointed to SQLite against `thread_id`. Kill the process while a
thread is parked at `human_gate`, start it again, and `/resume` still works:

```
process A paused: awaiting_approval
--- process A exited, memory gone ---
process B sees: ['human_gate']
process B resumes: {'status': 'approved', 'action': 'RETRAIN', ...}
```

That is the point of pairing `interrupt()` with n8n's Wait node. An approval
that arrives six hours later still lands on a live graph.

## Auth

Set `SERVICE_TOKEN` once the service is reachable from outside your machine. In
n8n, add a Header Auth credential with name `Authorization` and value
`Bearer <token>`, and attach it to both HTTP Request nodes.

## Cost

Zero. LangGraph, FastAPI and SQLite are open source; the LLM runs on a free tier
or locally; hosting is your own machine (or a free Hugging Face Space).
