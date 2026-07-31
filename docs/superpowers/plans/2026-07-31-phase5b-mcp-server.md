# Phase 5b — MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An external MCP client can ask Driftbell for its model status and trigger a real retrain.

**Architecture:** One workflow: an MCP Server Trigger with two tool sub-nodes. `get_model_status` reads the `/history` endpoint from Phase 5a; `trigger_retrain` calls the same workflow 03 the Telegram approval calls, so there is no second retrain path. Bearer authentication, because a cloudflared tunnel is exposing this instance publicly.

**Tech Stack:** n8n 2.32.6 MCP Server Trigger, FastAPI, Claude Desktop (or any MCP client).

**Spec:** `docs/superpowers/specs/2026-07-31-phase5b-mcp-server-design.md`

## Global Constraints

- **Zero paid services.** Nothing here adds a dependency or a paid tier.
- **The agent never performs irreversible actions.** `trigger_retrain` runs workflow 03, which trains and compares; promotion still happens only when workflow 03's IF node decides it.
- **n8n reaches the agent at `http://driftbell:8000`** — container name, never `localhost`.
- **Never hand-write n8n workflow JSON and assume it works.** Generate, import, fix in the UI, export, commit the export.
- **Interface facts verified against this instance — do not substitute guesses:**
  - `mcpTrigger` versions `[1, 1.1, 2]`. Use **2** (streamable HTTP; earlier versions use the `/sse` + `/messages` pair).
  - `mcpTrigger` auth values: `none | n8nOAuth2 | bearerAuth | headerAuth`. `bearerAuth` requires an `httpBearerAuth` credential.
  - `toolWorkflow` must be **v2.2**. `ToolWorkflowV1` implements only `supplyData`; v2 implements both `execute` and `supplyData`.
  - `toolWorkflow` v2 has a **`workflowInputs` resourceMapper**. Set it explicitly — omitting the equivalent parameter on `executeWorkflow` 1.2 caused "Bad request - please check your parameters" in Phase 3.
  - `toolHttpRequest` is usable here even though it failed with Agent v3.1: `McpTrigger` resolves tools via `getInputConnectionData`, which invokes `supplyData`, and that node implements it.
  - `toolHttpRequest` has **no `options` property**. Its parameters are flat.
- **The bearer token is a secret.** It lives in the n8n credential store and the MCP client's local config only — never in committed workflow JSON, never in `.env`, never in a commit message.
- Commit messages carry NO Claude attribution trailers.
- Platform is Windows; `docker compose exec` with `/tmp/...` paths needs `MSYS_NO_PATHCONV=1` under Git Bash.

---

### Task 1: Build the MCP server workflow

Deliverable: `workflows/05-mcp-server.json` imported into n8n, publishing two authenticated tools.

**Files:**
- Create: `workflows/05-mcp-server.json`

**Interfaces:**
- Consumes: `GET /history` (Phase 5a) and workflow 03, id `ckrDz97MjSom3amS`.
- Produces: an MCP endpoint at `<n8n base>/mcp/driftbell` exposing `get_model_status` and `trigger_retrain`.

- [ ] **Step 1: Create the bearer credential**

In n8n: **Credentials → New → Bearer Auth**. Generate a random token and paste it in — for example from PowerShell:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Name it `Driftbell MCP token`. Keep the value; the MCP client needs it in Task 2. Do not put it in `.env` or any committed file.

Note its credential id — the generator needs it:

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T n8n sh -c 'n8n export:credentials --all --output=/tmp/c.json >/dev/null 2>&1; node -e "require(\"/tmp/c.json\").forEach(x=>console.log(x.id,\"|\",x.name,\"|\",x.type))"; rm -f /tmp/c.json'
```

The new entry has type `httpBearerAuth`.

- [ ] **Step 2: Generate the workflow**

Three nodes. Positions are cosmetic; the parameter shapes are not.

**MCP Server Trigger** — `@n8n/n8n-nodes-langchain.mcpTrigger`, typeVersion `2`:

```json
{
  "parameters": {
    "path": "driftbell",
    "authentication": "bearerAuth",
    "requireExecuteAccess": true
  },
  "credentials": {
    "httpBearerAuth": { "id": "<credential id from Step 1>", "name": "Driftbell MCP token" }
  }
}
```

**trigger_retrain** — `@n8n/n8n-nodes-langchain.toolWorkflow`, typeVersion `2.2`:

```json
{
  "parameters": {
    "name": "trigger_retrain",
    "description": "Train a challenger model for churn_clf, compare it against the current champion on F1, and promote it only if it wins. Returns the run id and both sets of metrics.",
    "source": "database",
    "workflowId": {
      "__rl": true,
      "value": "ckrDz97MjSom3amS",
      "mode": "list",
      "cachedResultName": "Driftbell 03 — retrain & evaluate"
    },
    "workflowInputs": { "mappingMode": "defineBelow", "value": null }
  }
}
```

**get_model_status** — `@n8n/n8n-nodes-langchain.toolHttpRequest`, typeVersion `1.1`:

```json
{
  "parameters": {
    "toolDescription": "Current state of the churn_clf model: run history with metrics, the model registry showing which version is champion, recent pipeline incidents, and the agent's past drift proposals. Returns JSON.",
    "method": "GET",
    "url": "http://driftbell:8000/history"
  }
}
```

Connections — both tools are the **source** of an `ai_tool` connection into the trigger:

```json
{
  "trigger_retrain": {
    "ai_tool": [[{ "node": "MCP Server Trigger", "type": "ai_tool", "index": 0 }]]
  },
  "get_model_status": {
    "ai_tool": [[{ "node": "MCP Server Trigger", "type": "ai_tool", "index": 0 }]]
  }
}
```

Workflow envelope, matching the other committed workflows:

```json
{
  "name": "Driftbell 05 — MCP server",
  "nodes": [ ... ],
  "connections": { ... },
  "active": false,
  "settings": { "executionOrder": "v1" },
  "pinData": {}
}
```

Write it with `indent=2`, `ensure_ascii=True`, CRLF line endings and no trailing newline, matching the other workflow files.

- [ ] **Step 3: Import it**

The CLI requires an `id` on a new workflow. Add a 16-character alphanumeric id to the import copy only, keeping the committed file instance-agnostic:

```bash
docker cp workflows/05-mcp-server.json driftbell-n8n:/tmp/wf05.json
MSYS_NO_PATHCONV=1 docker compose exec -T n8n n8n import:workflow --input=/tmp/wf05.json
```

Expected: `Successfully imported 1 workflow.`

- [ ] **Step 4: Confirm n8n kept the parameters**

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T n8n n8n export:workflow --id=<id> --output=/tmp/v.json
```

Check that the trigger still has `authentication: bearerAuth` with its credential attached, that `trigger_retrain` is typeVersion 2.2 and targets `ckrDz97MjSom3amS`, and that both tools appear as `ai_tool` sources. If n8n dropped a parameter it did not recognise, fix that node in the UI and re-export.

- [ ] **Step 5: Publish and note the URL**

Reload n8n, open the workflow, check for warning triangles, and **Publish**. Open the MCP Server Trigger node and copy the **Production URL**. It looks like:

```
https://<tunnel-host>.trycloudflare.com/mcp/driftbell
```

Publishing matters: an unpublished trigger returns 404 to the client.

- [ ] **Step 6: Verify the token is not in the file, then commit**

```bash
grep -c "<first 8 chars of the token>" workflows/05-mcp-server.json
```

Expected: `0`. If not, stop and do not commit.

```powershell
git add workflows/05-mcp-server.json
git commit -m "Add workflow 05: MCP server exposing status and retrain"
```

---

### Task 2: Connect a client and verify

Deliverable: an MCP client lists both tools and both work.

**Files:** none committed. The client config holds a secret and is per-machine.

**Interfaces:**
- Consumes: the MCP endpoint and bearer token from Task 1.

- [ ] **Step 1: Point Claude Desktop at it**

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "driftbell": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://<tunnel-host>.trycloudflare.com/mcp/driftbell",
        "--header", "Authorization: Bearer <your token>"
      ]
    }
  }
}
```

Restart Claude Desktop. `mcp-remote` bridges a stdio client to an HTTP MCP server; it needs no install beyond `npx`.

This file contains the token. It is per-machine and must never be committed.

- [ ] **Step 2: Confirm the tools are listed**

In Claude Desktop, check that `driftbell` appears with `get_model_status` and `trigger_retrain`.

If the server fails to connect, check in this order: the workflow is published; the URL matches the trigger's Production URL exactly; the tunnel is still up (quick-tunnel hostnames change on restart); and the `Authorization` header is spelled exactly as above.

- [ ] **Step 3: Read the status**

Ask the client: *"What's the current status of the churn_clf model?"*

Expected: it calls `get_model_status` and reports the champion version and its F1 — matching what the database holds:

```bash
docker compose exec -T driftbell python -c "import sqlite3;c=sqlite3.connect('/data/driftbell.db');print(c.execute(\"SELECT version FROM registry WHERE model_name='churn_clf' AND stage='champion'\").fetchone())"
```

- [ ] **Step 4: Trigger a retrain**

Record the current run count first:

```bash
docker compose exec -T driftbell python -c "import sqlite3;c=sqlite3.connect('/data/driftbell.db');print(c.execute('SELECT COUNT(*) FROM runs').fetchone())"
```

Ask the client: *"Trigger a retrain for churn_clf."*

Then re-run the count. Expected: one higher, with a new `challenger trained` row — the same observable outcome as a Telegram approval, because it is the same workflow.

- [ ] **Step 5: Confirm the endpoint rejects an unauthenticated caller**

The whole reason for bearer auth is that this endpoint is publicly reachable. Verify it actually refuses strangers:

```bash
docker compose exec -T n8n sh -c 'wget -qS -O- http://localhost:5678/mcp/driftbell 2>&1 | head -5'
```

Expected: a 401 or 403, not a tool listing.

---

## Done when

An MCP client lists both tools, `get_model_status` reports the champion version currently in the registry, `trigger_retrain` adds a `challenger trained` row to `runs`, and an unauthenticated request to the endpoint is refused.
