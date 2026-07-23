# Workflow 01 — ingest & monitor

The scheduled half of Driftbell. It pulls a scoring window, measures how far it
has moved from the training distribution, and hands anything suspicious to the
LangGraph agent.

```
Every morning 07:00 ─┐
                     ├─→ Build scoring window ─→ Compute drift (PSI + KS) ─→ Drift significant?
Manual drift form ───┘                                                            │
                                                    ┌─────────── no ──────────────┤
                                                    ▼                             ▼ yes
                                          Stable — log and stop        Ask the Driftbell agent
                                                                                  │
                                                             ┌──── error ─────────┤
                                                             ▼                    ▼
                                                   Error workflow (05)     Needs a human?
                                                                            │         │
                                                                  yes ──────┘         └── no
                                                                    ▼                      ▼
                                                        → Approval workflow (02)   Agent resolved it alone
```

## Start everything

```bash
docker compose up -d          # n8n on :5678, agent on :8000
docker compose logs -f driftbell
```

Open <http://localhost:5678>, create the local owner account, then
**Workflows → Import from File** and pick `workflows/01-ingest-and-monitor.json`.

n8n reaches the agent at `http://driftbell:8000` — the container name, not
`localhost`. Inside the n8n container, `localhost` is n8n itself.

## Demo it

Open the **Manual drift injection** form node, copy its Test URL, and submit:

| drift_magnitude | What happens |
|---|---|
| `0` | PSI ≈ 0.01 → stable branch, nothing escalates |
| `4` | PSI ≈ 0.11 → still under threshold, logged as moderate |
| `12` | PSI ≈ 0.70 → agent invoked, proposal comes back, waits for you |

Those numbers are from actually running the node, not estimates. Being able to
dial drift up and down on demand is what makes this demo work in a five-minute
interview slot.

## What each node is doing

**Two triggers into one pipeline.** Schedule for production, Form for demos.
Worth pointing out in an interview — it shows you thought about how the thing
gets demonstrated, not just how it runs.

**Compute drift (PSI + KS)** is pure-stdlib Python because n8n's Python node
runs under Pyodide and cannot reliably import scipy or numpy. The KS statistic
this produces matches `scipy.stats.ks_2samp` exactly; the p-value uses the
asymptotic approximation, which agrees to the same order of magnitude.

Rule of thumb for PSI: under 0.1 stable, 0.1–0.2 minor shift, above 0.2 worth
investigating. The threshold lives in the IF node so you can tune it without
touching code.

**Ask the Driftbell agent** has `retryOnFail` with 3 tries and a separate error
output. A cold-start LLM call can take 30 seconds and a free tier can rate-limit
you; retrying is not optional here. The error branch goes to the error workflow
rather than failing silently.

**Needs a human?** branches on the `status` the service returns. `IGNORE`
verdicts come back as `completed` and never interrupt you — deliberately, so the
approval channel stays credible.

## Next

Workflow 02 takes the `thread_id` from here, sends the Telegram approval card,
parks on a Wait node, and calls `POST /resume` when you tap a button.
