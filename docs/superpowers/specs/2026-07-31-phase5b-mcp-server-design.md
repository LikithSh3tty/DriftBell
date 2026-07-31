# Phase 5b — MCP server

Date: 2026-07-31
Status: approved, not yet implemented

## Problem

Driftbell's capabilities are reachable only from inside n8n. An external agent —
Claude Desktop, or any MCP client — cannot ask what the current champion is, or
trigger a retrain, without a human opening the n8n canvas.

Phase 5b exposes two of Driftbell's existing capabilities as MCP tools.

## Scope

The second half of `BUILD_PLAN`'s Phase 5, split from the chatbot because the
two share no state and have different consumers. The chatbot shipped as Phase
5a.

`BUILD_PLAN` assumed the MCP trigger would live inside workflow 04. Since the
phases were split, it gets its own file, and Phase 6's error handler moves from
`05-error-handler.json` to `06-error-handler.json`.

## What was verified before designing

Phase 5a lost four debugging cycles to a node incompatibility, so the relevant
interfaces were checked against the running instance first.

**MCP obtains its tools through `supplyData`.** `McpTrigger` calls
`getConnectedTools`, which resolves them with:

```js
await ctx.getInputConnectionData(NodeConnectionTypes.AiTool, 0)
```

That is the standard sub-node path and invokes `supplyData`. The failure in
Phase 5a — `toolHttpRequest` implementing only `supplyData` while Agent v3.1
invokes tools through `execute` — is therefore **specific to the agent**, not to
tool nodes generally. `toolHttpRequest` is usable here, which removes the need
for a wrapper workflow around the HTTP call.

**`toolWorkflow` must be v2.** Checked directly:

| Node | `execute` | `supplyData` |
| --- | --- | --- |
| `ToolWorkflowV1` | absent | present |
| `ToolWorkflowV2` (2, 2.1, 2.2) | present | present |
| `toolHttpRequest` (1, 1.1) | absent | present |

v2 is used so the node also works if it is ever attached to an agent.

**`mcpTrigger` has versions 1, 1.1 and 2.** At v2 the webhook path drops the
`/sse` suffix — v2 is the streamable-HTTP transport, earlier versions the older
SSE pair. v2 is used.

## Goals

1. An MCP client can ask Driftbell for its current model status.
2. An MCP client can trigger a retrain, which runs the real workflow 03.
3. Neither is callable by an anonymous stranger.

## Non-goals

- No new agent capability. Both tools wrap things that already exist.
- No new endpoints. `get_model_status` reads the existing `GET /history`.
- No MCP client configuration committed to the repo. Client config holds a
  secret and is per-machine.

## Design

### Workflow 05

```
MCP Server Trigger        (mcpTrigger v2, path: driftbell, auth: bearer)
  |- trigger_retrain      (toolWorkflow v2.2   -> workflow 03)
  \- get_model_status     (toolHttpRequest v1.1 -> GET http://driftbell:8000/history)
```

`trigger_retrain` calls the same workflow the Telegram approval calls, so an
MCP-triggered retrain follows the identical path: train a challenger, compare
F1, promote only on a win. There is no second implementation to drift out of
sync.

`get_model_status` returns the full history payload. The client's own model
summarises it, so no Driftbell-side shaping is needed.

### Authentication

The trigger uses **Bearer** authentication, with the token stored as an n8n
credential.

This is not optional in the current setup. A cloudflared tunnel is exposing this
n8n instance publicly so Telegram can reach it, and `trigger_retrain` starts
real work. An unauthenticated public endpoint that retrains models on demand is
a genuinely bad thing to leave running, and the tunnel hostname appears in
cloudflared's logs and n8n's own UI.

`requireExecuteAccess` stays at its default of `true`.

### What is not committed

The MCP client configuration — the JSON pointing Claude Desktop at the tunnel
URL with the bearer token — is per-machine and contains a secret. The README
documents its shape; the file itself is never committed.

## Verification

**Nothing here is unit-testable.** Both tools wrap existing, already-tested
paths: `GET /history` has four tests from Phase 5a, and workflow 03 was verified
end to end in Phase 4. This phase adds only n8n wiring, which has no offline
harness.

**Done when:** an MCP client lists both tools, `get_model_status` returns the
current champion, and `trigger_retrain` produces a new row in `runs` — the same
observable outcome as a Telegram approval.

## Rejected alternatives

**Wrapping `get_model_status` in its own sub-workflow.** Would have used
`toolWorkflow` for both tools, avoiding any question about `toolHttpRequest`.
Rejected once the MCP trigger was confirmed to use `supplyData`: it would add a
whole workflow file purely to wrap one HTTP GET.

**Leaving the endpoint unauthenticated.** Simplest, and defensible for a
local-only demo. Rejected because the tunnel is live: the endpoint would be
publicly callable the moment it is published.

**Putting the MCP trigger inside workflow 04**, as `BUILD_PLAN` describes.
Rejected: the chatbot and the MCP server have different consumers and no shared
state, and combining unrelated surfaces has cost this project debugging cycles
twice already.
