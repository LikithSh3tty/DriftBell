# Phase 3 — The Telegram approval loop

Date: 2026-07-30
Status: approved, not yet implemented

## Problem

Workflow 01 ends at a NoOp named `→ Approval workflow (02)`. The agent parks at
`human_gate` and waits, but nothing ever reaches a human and nothing ever calls
`/resume`. Phase 3 closes that circuit: a Telegram message goes out, a person
decides, and the frozen graph resumes hours later on the same `thread_id`.

This is the part of the project the rest exists to support — n8n's wait paired
with LangGraph's `interrupt()`, joined by `thread_id`, with both sides
checkpointed to disk.

## What changed since BUILD_PLAN was written

`BUILD_PLAN.md` describes hand-building the approval from a Telegram node with
an inline keyboard, a Wait node parked on webhook, and a second Telegram Trigger
workflow to catch the button press and call the resume URL.

n8n 2.x ships this natively. The Telegram node has a `sendAndWait` operation
with `responseType: approval`, approver restriction by user ID, wait-time
limits, and post-decision message editing. It uses the same underlying
wait/resume machinery, so the seam the project is built around is unchanged —
there is simply far less of it to hand-assemble.

**Reachability applies to both designs and was never optional.** Telegram can
only deliver a decision by calling into n8n. `localhost:5678` is not reachable
from Telegram's servers, so the "phone buzzes, tap approve" demo requires a
public HTTPS tunnel under either approach. n8n's own description of
`chatApproval` states this outright.

## Goals

1. An approved drift alert resumes the parked graph and reports `approve`.
2. A rejected one resumes it and reports `reject`.
3. n8n can tell approve, reject and never-asked apart from an explicit field.
4. The whole loop is verifiable on localhost, with no tunnel and no account.

## Non-goals

- No tunnel setup. Deferred; it is needed only for the phone demo recording.
- No retrain execution. The approved branch ends at a NoOp that Phase 4 replaces.
- No changes to the graph in `app/graph.py`. Only the HTTP shaping layer changes.

## Design

### 1. Agent: an explicit `decision` field

`_shape()` in `app/main.py` currently returns `status: "completed"` whether the
human approved, rejected, or was never asked. The only way to distinguish them
is whether `outcome` is present — implicit, undocumented, and exactly the
contract Phase 1 flagged as needing resolution before n8n depended on it.

`_shape()` gains one field:

```python
d = result.get("human_decision")
decision = d if d in ("approve", "reject") else "not_required"
```

| Path | `status` | `decision` | `outcome` |
| --- | --- | --- | --- |
| parked at the gate | `awaiting_approval` | absent | absent |
| human approved | `completed` | `approve` | present |
| human rejected | `completed` | `reject` | absent |
| IGNORE, never gated | `completed` | `not_required` | absent |

`status` keeps its existing meaning — is the graph done or waiting. `decision`
answers a different question — what did the human say. Two fields, two
questions, rather than one field overloaded with both.

The change is additive, so the six tests in `tests/test_api.py` that pin the
current contract keep passing untouched.

### 2. `workflows/02-approval-loop.json`

```
Execute Workflow Trigger        thread_id, verdict, confidence, rationale, model_name
  -> Telegram: send and wait    responseType = approval
  -> IF  {{ $json.data.approved }}  is true
       true  -> POST /resume    {thread_id, decision: "approve", note: "approved in Telegram"}
       false -> POST /resume    {thread_id, decision: "reject",  note: "rejected in Telegram"}
  -> IF  {{ $json.decision }} equals "approve"
       true  -> Retrain approved   (NoOp; Phase 4 replaces with workflow 03)
       false -> Rejected, logged   (NoOp)
```

`sendAndWait` returns `{ data: { approved: <boolean> } }` — confirmed by reading
`nodes/Telegram/hitl/webhook.js` in the running image, not assumed.

The second IF deliberately branches on the **agent's** reported `decision`
rather than on what Telegram returned. If a `/resume` call silently failed or
landed on the wrong thread, branching on the Telegram answer would report
success anyway; branching on the agent's own record cannot.

Both branches POST to the same endpoint. The only difference is the payload.

### 3. Workflow 01

Replace the `→ Approval workflow (02)` NoOp with an Execute Workflow node
pointing at workflow 02. One node swap; nothing else on that canvas changes.

### 4. Human prerequisites

- Bot token from **@BotFather** (`/newbot`).
- Chat ID: message the bot once, then read `result[0].message.chat.id` from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`.
- In n8n: **Credentials → New → Telegram API**, paste the token.

The token is a secret. It goes in the n8n credential store, never in committed
workflow JSON and never in `.env`.

## Verification

**Testable:** the `decision` field. `tests/test_api.py` gains coverage pinning
`approve`, `reject` and `not_required`.

**Not testable:** workflow 02. n8n workflows have no offline harness, so
verification is a real run, as it was for workflow 01. Build the JSON, import
it, fire it, watch the canvas.

**Done when:** submitting the form with `drift_magnitude = 12` sends a Telegram
message; approving it turns the parked execution green and `/threads/{id}`
reports `human_decision: approve`; rejecting a second run reports `reject` and
writes no `outcome`.

## Rejected alternatives

**Hand-built Wait node + Telegram Trigger workflow** (the BUILD_PLAN design).
More n8n constructs on the canvas, and total control of the callback payload.
Rejected: it is a hand-rolled reimplementation of a node n8n now ships, costs a
second workflow and several more failure modes, and needs the same tunnel
anyway. Reimplementing a built-in is a weaker interview story than using it.

**Making `status` the discriminator** (`approved` / `rejected` / `no_action`).
Cleaner to explain as a single field. Rejected: it breaks the contract the API
tests pin, and it overloads one field with both progress and verdict — the
ambiguity that caused this problem originally.

**Branching on empty `outcome`.** Zero code change. Rejected: it bakes an
undocumented implicit contract into the canvas, which is precisely what Phase 1
deferred this decision in order to avoid.

**Setting up the tunnel first.** Gets the phone demo immediately. Rejected for
sequencing only: debugging a brand-new workflow and an expiring tunnel URL at
the same time makes each failure ambiguous. Build on localhost, add the tunnel
once the logic is known-good.
