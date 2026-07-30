# Phase 5a — The ops chatbot

Date: 2026-07-31
Status: approved, not yet implemented

## Problem

Driftbell records a lot now — run history, registry promotions, pipeline
incidents, and the agent's own reasoning for every proposal it made. None of it
is answerable. To find out why `churn_clf` was retrained you read SQLite by hand,
which is exactly the operational gap the project claims to close.

Phase 5a adds a chat interface that answers questions grounded in that history.

## Scope

`BUILD_PLAN` Phase 5 asks for two things: the chatbot, and an MCP Server Trigger
exposing workflows as tools. They are independent subsystems — different
triggers, different consumers, no shared state — so they are split. **This spec
covers the chatbot only.** MCP is Phase 5b.

Splitting also keeps a new RAG pipeline from being debugged simultaneously with
the newest, least documented node in n8n. Combining unrelated new surfaces has
already cost this project two ambiguous debugging cycles.

## Two problems found first

**1. Nothing can read the history.** `runs`, `registry` and `incidents` live in
SQLite on the agent's `/data` volume, which n8n cannot reach. Past proposals live
in the checkpoint store and are reachable only through `GET /threads/{id}` — and
nothing enumerates thread ids. Every route to the data is closed.

**2. Most of the data is structured, and RAG is the wrong tool for it.**
`BUILD_PLAN` asks for a vector store over runs, incidents and proposals. There
are seven run rows carrying numeric metrics. Embedding them and answering "what
was the champion's F1" by similarity search is strictly worse than a query, and
indefensible when asked why.

The genuinely unstructured content is real: **proposal rationales**, written by
an LLM, and **incident descriptions**. Those are what retrieval is for.

## Goals

1. Ask "why was churn_clf retrained" and get an answer grounded in real history.
2. Ask "what is the champion's F1" and get a number that is actually correct.
3. `LLM_PROVIDER=stub` keeps working; the diagnostic path still needs no key.

## Non-goals

- No MCP Server Trigger. That is Phase 5b.
- No persistent vector database. The in-memory store is rebuilt by re-running
  the indexing branch; a dedicated container is not justifiable for ~20
  documents.
- No switch of the agent's own provider to Gemini. Gemini is an n8n credential
  used by the chat model and embeddings nodes only.

## Design

### 1. Two endpoints

```
GET /history            -> {"runs": [...], "registry": [...],
                            "incidents": [...], "proposals": [...]}
GET /history/documents  -> {"documents": [{"text": str, "metadata": {...}}]}
```

Split because they feed different consumers. `/history` backs an HTTP tool the
agent calls for factual questions. `/history/documents` is loaded once into the
vector store and backs "why" questions. Merging them would mean embedding
numbers or querying prose.

Both sit behind the existing `_auth` shared-secret check, like every endpoint
except `/health`.

### 2. `app/history.py`

```python
recent_runs(limit: int = 20) -> list[dict]
registry_entries() -> list[dict]
recent_incidents(limit: int = 20) -> list[dict]
thread_ids(checkpoint_db: str | None = None) -> list[str]
```

The first three read `driftbell.db`. `thread_ids` reads the distinct
`thread_id` values from `checkpoints.db` — the same query used to inspect the
parked threads by hand in Phase 3.

Proposals are assembled in `main.py` rather than here, because turning a
`thread_id` into a verdict requires `GRAPH.get_state`, and the graph lives
there. `history.py` stays a pure data-access module with no graph dependency.

### 3. Documents

One document per proposal rationale and per incident description — the free
text. Each carries metadata (`source`, `thread_id` or `occurred_at`,
`model_name`) so an answer can cite where it came from.

Structured rows are deliberately **not** embedded. They are served whole by
`/history` and read by the agent as a tool result.

### 4. Workflow 04

Two independent branches in one workflow:

```
Manual Trigger -> GET /history/documents -> Split Out -> Vector Store Insert
                                                          (memoryKey: driftbell)

Chat Trigger -> AI Agent
                  |- Google Gemini Chat Model
                  |- Simple Memory
                  |- Tool: HTTP Request -> GET /history
                  \- Tool: Vector Store retriever <- Gemini Embeddings
                             (memoryKey: driftbell)
```

The in-memory store must be populated before the agent can retrieve anything,
hence the manual indexing branch. Both branches live in one workflow so the
`memoryKey` appears in a single place — split across two workflows, a typo
returns zero results silently, with no error anywhere.

### 5. Credentials

A Google Gemini API key, added in n8n as a Google Gemini credential and used by
both the chat model and the embeddings node. The free tier covers this volume.
The key lives in n8n's credential store only — never in committed workflow JSON,
never in `.env`.

## Verification

**Testable, and will be tested:**

- `/history` returns runs, registry and incidents with the champion present.
- `/history` surfaces proposals from parked threads, not only completed ones.
- `/history/documents` returns rationale and incident documents, each with
  metadata identifying its source.
- Documents contain no numeric-only content — structured rows are not embedded.
- Both endpoints reject a missing token.

**Not testable:** workflow 04. Verified by asking it questions.

**Done when:** the chat answers "why was churn_clf retrained this week" from
real run history, and "what is the champion's F1" with the number actually in
the registry.

## Rejected alternatives

**Pure RAG over everything, as `BUILD_PLAN` describes.** Simplest to build and
literally the documented plan. Rejected: numeric questions would be answered by
fuzzy retrieval over seven rows, which is the weakest thing on the canvas to
defend in an interview.

**Pure tools, no vector store.** Cleanest engineering for this data size and
uses no embeddings quota. Rejected: it drops the retrieval component entirely,
which `BUILD_PLAN` wanted specifically because it is a CV line — and unlike the
pure-RAG option, the hybrid keeps it without making it dishonest.

**Building the MCP trigger in the same phase.** Matches `BUILD_PLAN`'s framing
and saves a ceremony cycle. Rejected: see Scope.
