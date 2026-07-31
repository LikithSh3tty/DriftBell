# Phase 6 — Error handling

Date: 2026-07-31
Status: approved, not yet implemented

## Problem

Every workflow in Driftbell fails silently. If the drift pipeline throws, the
execution turns red in a list nobody is watching and nothing else happens. The
project claims to be an operations tool; an operations tool that cannot tell you
it broke is a demo.

## Goals

1. A failure in any workflow produces a Telegram message.
2. The failure is recorded where the rest of the system can reason about it.
3. Neither depends on a component that is likely to be broken at the time.

## Non-goals

- No Google Sheets. See below.
- No retry or self-healing. This phase reports; it does not recover.
- No alerting on the agent's own crashes. n8n's Error Trigger fires on n8n
  workflow failures; a dead FastAPI process is out of its reach.

## Two departures from BUILD_PLAN

**1. Failures are recorded as incidents, not appended to a Google Sheet.**

`BUILD_PLAN` specifies a Google Sheet. That needs Google OAuth, a new
credential, and lands the data somewhere nothing else in Driftbell can read.

The `incidents` table already exists, and the agent already treats it as
evidence. From the system prompt in `app/graph.py`:

> Be sceptical: a drift alert that coincides with an ingestion incident is
> usually a bug, not drift.

`get_pipeline_incidents` is one of the agent's four tools. So recording n8n
failures as incidents **closes a loop that is already half-built**: a pipeline
failure today makes the agent more sceptical of a drift alert tomorrow.
Incidents also already flow into `/history/documents`, so the ops chatbot can
explain them without any further work.

A spreadsheet gives you rows nobody reads. The incidents table makes the system
reason about its own failures.

**2. LLM classification is enrichment, and cannot block the alert.**

`BUILD_PLAN` puts Gemini classification in the path: classify, then notify. That
gives the error handler a hard dependency on the most failure-prone component
available — and free-tier quota is exactly what exhausts during a burst of
retries, which is exactly when something is already wrong.

The classify node keeps `onError: continueRegularOutput`, so a Gemini failure
passes through instead of halting the workflow. The alert falls back to
`unclassified`. Labelled when possible, never silent.

## Design

### 1. `POST /incidents`

```
POST /incidents
{
  "source": "n8n:Driftbell 01 — ingest & monitor",
  "severity": "high",
  "description": "Compute drift (PSI + KS): ...",
  "classification": "transient"
}
```

Writes to the existing `incidents` table. `occurred_at` defaults to now and
`day_offset` to 0, so a recorded failure is visible to
`get_pipeline_incidents(n_days=7)` immediately. Behind the existing `_auth`
shared-secret check.

`classification` is folded into the stored description rather than adding a
column: the table is read by an LLM tool and a vector store, both of which read
prose, and a schema change would ripple into `seed_db.py` and its tests for no
retrieval benefit.

### 2. `workflows/06-error-handler.json`

```
Error Trigger
  -> Classify the failure          (Gemini, onError: continueRegularOutput)
       |- Notify on Telegram
       \- Record the incident      (POST /incidents, onError: continueRegularOutput)
```

Telegram and the incident write are **siblings of the classify node, not a
chain**, with Telegram first. If recording fails, the alert has already gone out.

Workflow number 06: `05` is the MCP server from Phase 5b, since `BUILD_PLAN`
assumed MCP would live inside workflow 04.

### 3. Wiring it up

Each of workflows 01–05 gets `settings.errorWorkflow` pointing at 06. n8n then
invokes it whenever one of them fails.

## The limitation this phase cannot fix

`BUILD_PLAN`'s test is: stop the `driftbell` container, run workflow 01, expect
an incident message.

That test exposes a real gap. If the agent container is down, `POST /incidents`
fails too — it is the same container. You get the Telegram alert and **no
incident row**.

This is inherent to recording incidents inside the thing that can fail, and it
is why Telegram is a sibling rather than downstream of the write. Stating it
plainly: **incidents are recorded when an n8n workflow fails, not when the agent
itself is down.** Fixing it properly means a store outside both containers,
which is a larger change than this phase justifies.

## Verification

**Testable:** `POST /incidents` — that it writes a row, defaults `occurred_at`
and `day_offset`, folds the classification into the description, and rejects a
missing token.

**Not testable:** workflow 06, as with every workflow.

**Done when:** stopping the `driftbell` container and running workflow 01
produces a Telegram message naming the workflow, the node and the error, instead
of a silent red execution.

## Rejected alternatives

**Google Sheets, as `BUILD_PLAN` specifies.** Demonstrates a third-party
integration and produces something easy to show. Rejected: OAuth setup for data
nothing else can read, when a table the agent already reasons over exists.

**Classify before alerting.** Simpler linear flow and a better-looking message.
Rejected: it makes the alert depend on Gemini being up and in quota, during the
one scenario where things are already failing.

**Rule-based classification only.** No quota, no dependency, fully
deterministic. Rejected: with `onError` continuation the LLM path is already
non-blocking, so the robustness argument for dropping it disappears — and
pattern rules go stale as new failure modes appear.

**Adding a `classification` column to `incidents`.** Cleaner relationally.
Rejected: the table's consumers are an LLM tool and a vector store, both of
which read prose, and the change would ripple into `seed_db.py` and its tests
for no retrieval benefit.
