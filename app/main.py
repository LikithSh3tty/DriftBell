"""HTTP contract between n8n and the LangGraph agent.

    POST /diagnose            -> runs until the human gate, returns the proposal
    POST /diagnose/stream     -> the same run, emitted node by node as SSE
    POST /resume              -> resumes a frozen thread with a human decision
    GET  /threads/{id}        -> current state snapshot (audit / polling)
    GET  /health              -> readiness probe for the n8n error workflow
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, Field

from .graph import build_graph, make_checkpointer
from .history import (
    incident_documents,
    recent_incidents,
    recent_runs,
    record_incident,
    registry_entries,
    thread_ids,
)
from .state import DriftReport
from .training import promote as promote_model
from .training import train_challenger

logger = logging.getLogger(__name__)

app = FastAPI(title="Driftbell diagnostic agent", version="1.0.0")

CHECKPOINTER = make_checkpointer()
GRAPH = build_graph(checkpointer=CHECKPOINTER)
API_TOKEN = os.getenv("SERVICE_TOKEN", "")

if not API_TOKEN:
    logger.warning(
        "SERVICE_TOKEN is unset - /diagnose, /resume and /threads are "
        "UNAUTHENTICATED. Set it before exposing this service beyond localhost."
    )


def _auth(token: str | None) -> None:
    """Shared-secret auth for the two n8n HTTP Request nodes.

    Auth is disabled outright when SERVICE_TOKEN is unset, which is what keeps
    `docker compose up` and the local quickstart working with no configuration.
    That open default is deliberate but easy to forget, hence the startup
    warning above — set the variable before this service is reachable from
    anywhere but localhost.

    compare_digest rather than `!=` so a wrong token cannot be reconstructed by
    timing the responses.
    """
    if not API_TOKEN:
        return
    if not token or not secrets.compare_digest(token, f"Bearer {API_TOKEN}"):
        raise HTTPException(status_code=401, detail="invalid or missing token")


class DiagnoseRequest(BaseModel):
    drift_report: dict[str, Any] = Field(..., description="Output of the n8n drift node")
    thread_id: str | None = Field(None, description="Omit to have one generated")


class ResumeRequest(BaseModel):
    thread_id: str
    decision: Literal["approve", "reject"]
    note: str = ""


class TrainRequest(BaseModel):
    model_name: str = "churn_clf"


class PromoteRequest(BaseModel):
    model_name: str = "churn_clf"
    version: str


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _shape(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Flatten the graph result into the JSON n8n branches on."""
    interrupts = result.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        return {"status": "awaiting_approval", "thread_id": thread_id, "proposal": payload}
    # `status` says whether the graph is done; `decision` says what the human
    # chose. Two questions, two fields. Leaving n8n to infer the second from
    # whether `outcome` happens to be present made approve, reject and
    # never-asked indistinguishable without knowing that implicit rule.
    # `not_required` is the IGNORE path, where the gate was skipped entirely.
    human = result.get("human_decision")
    return {
        "status": "completed",
        "thread_id": thread_id,
        "decision": human if human in ("approve", "reject") else "not_required",
        "verdict": result.get("verdict"),
        "confidence": result.get("confidence"),
        "rationale": result.get("rationale"),
        "outcome": result.get("outcome", {}),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "provider": os.getenv("LLM_PROVIDER", "stub")}


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest, authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    thread_id = req.thread_id or f"drift-{uuid.uuid4().hex[:12]}"
    result = GRAPH.invoke(
        {"thread_id": thread_id, "drift_report": DriftReport(**req.drift_report)},
        config=_config(thread_id),
    )
    return _shape(thread_id, result)


MAX_TRACE_CHARS = 900


def _describe_message(message: Any) -> list[dict[str, Any]]:
    """Render one LangChain message as console lines.

    A single AI turn can be both speech and tool calls, so this returns a list
    rather than one entry.
    """
    lines: list[dict[str, Any]] = []
    text = str(getattr(message, "content", "") or "").strip()
    kind = getattr(message, "type", "")
    if kind == "tool":
        return [
            {
                "kind": "tool_result",
                "name": getattr(message, "name", "tool"),
                "text": text[:MAX_TRACE_CHARS],
            }
        ]
    if text and kind != "system":
        lines.append({"kind": "say", "text": text[:MAX_TRACE_CHARS]})
    for call in getattr(message, "tool_calls", None) or []:
        lines.append(
            {"kind": "tool_call", "name": call.get("name"), "args": call.get("args", {})}
        )
    return lines


def _trace(node: str, update: Any) -> dict[str, Any]:
    """Turn one `stream_mode="updates"` chunk into a console trace event.

    Kept separate from the streaming endpoint so it can be tested without
    running a graph, and so the console never has to understand LangChain
    message objects.
    """
    if node == "__interrupt__":
        first = update[0] if isinstance(update, (list, tuple)) and update else update
        payload = getattr(first, "value", first)
        return {"node": "human_gate", "frozen": True, "proposal": payload, "lines": []}

    update = update or {}
    lines: list[dict[str, Any]] = []
    for message in update.get("messages", []) or []:
        lines.extend(_describe_message(message))
    for item in update.get("evidence", []) or []:
        lines.append({"kind": "evidence", "source": item.get("source"), "payload": item})

    event: dict[str, Any] = {"node": node, "frozen": False, "lines": lines}
    for key in ("verdict", "confidence", "rationale", "iterations",
                "needs_more_evidence", "human_decision", "outcome"):
        if key in update:
            event[key] = update[key]
    return event


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream_graph(payload: Any, thread_id: str) -> Iterator[str]:
    """Drive the graph and emit each node as it fires.

    The interrupt arrives as a normal chunk here rather than as an exception,
    so the frozen state is just another event the console renders.
    """
    config = _config(thread_id)
    yield _sse("start", {"thread_id": thread_id})
    frozen: dict[str, Any] | None = None
    try:
        for chunk in GRAPH.stream(payload, config=config, stream_mode="updates"):
            for node, update in chunk.items():
                event = _trace(node, update)
                if event.get("frozen"):
                    frozen = event.get("proposal")
                yield _sse("node", event)
    except Exception as exc:  # a failed run must still close the stream cleanly
        logger.exception("stream failed for thread %s", thread_id)
        yield _sse("error", {"thread_id": thread_id, "detail": str(exc)})
        return

    if frozen is not None:
        yield _sse(
            "done",
            {"status": "awaiting_approval", "thread_id": thread_id, "proposal": frozen},
        )
        return
    yield _sse("done", _shape(thread_id, GRAPH.get_state(config).values))


def _event_stream(generator: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        # Without this a reverse proxy can buffer the whole run and deliver it
        # in one lump, which defeats the point of streaming it.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/diagnose/stream")
def diagnose_stream(
    req: DiagnoseRequest, authorization: str | None = Header(None)
) -> StreamingResponse:
    """Same run as /diagnose, emitted node by node for the console.

    n8n keeps using /diagnose — it wants one JSON body, not a stream. This
    exists so a human can watch the tool cycle turn and the graph freeze.
    """
    _auth(authorization)
    thread_id = req.thread_id or f"drift-{uuid.uuid4().hex[:12]}"
    payload = {"thread_id": thread_id, "drift_report": DriftReport(**req.drift_report)}
    return _event_stream(_stream_graph(payload, thread_id))


@app.post("/resume/stream")
def resume_stream(
    req: ResumeRequest, authorization: str | None = Header(None)
) -> StreamingResponse:
    """Resume a frozen thread, streaming the nodes below the gate."""
    _auth(authorization)
    snapshot = GRAPH.get_state(_config(req.thread_id))
    if not snapshot.created_at:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    command = Command(resume={"decision": req.decision, "note": req.note})
    return _event_stream(_stream_graph(command, req.thread_id))


@app.post("/resume")
def resume(req: ResumeRequest, authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    snapshot = GRAPH.get_state(_config(req.thread_id))
    if not snapshot.created_at:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    result = GRAPH.invoke(
        Command(resume={"decision": req.decision, "note": req.note}),
        config=_config(req.thread_id),
    )
    return _shape(req.thread_id, result)


@app.post("/train")
def train(req: TrainRequest, authorization: str | None = Header(None)) -> dict[str, Any]:
    """Fit a challenger and record it. Deliberately does NOT promote.

    Returns champion and challenger metrics side by side so n8n can compare them
    and decide; the decision does not belong in this layer.
    """
    _auth(authorization)
    try:
        return train_challenger(req.model_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/promote")
def promote(
    req: PromoteRequest, authorization: str | None = Header(None)
) -> dict[str, Any]:
    """Move the champion. n8n calls this only after comparing F1 itself."""
    _auth(authorization)
    try:
        return promote_model(req.model_name, req.version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _proposals(limit: int = 20) -> list[dict[str, Any]]:
    """The agent's own reasoning, recovered from the checkpoint store.

    Assembled here rather than in history.py because turning a thread_id into a
    verdict needs GRAPH, and that module stays free of graph dependencies.
    Threads still parked at the gate are included: an undecided proposal is
    exactly the kind of thing someone asks the chatbot about.
    """
    out: list[dict[str, Any]] = []
    for thread_id in thread_ids(limit=limit):
        snapshot = GRAPH.get_state(_config(thread_id))
        values = snapshot.values
        if not values.get("verdict"):
            continue
        out.append(
            {
                "thread_id": thread_id,
                "model_name": values.get("drift_report", {}).get("model_name"),
                "verdict": values.get("verdict"),
                "confidence": values.get("confidence"),
                "rationale": values.get("rationale"),
                "human_decision": values.get("human_decision") or None,
                "parked": bool(snapshot.next),
                "updated_at": snapshot.created_at,
            }
        )
    return out


@app.get("/history")
def history(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Structured facts for the chatbot's HTTP tool.

    Numbers belong in a query result rather than an embedding, so the free text
    is served separately by /history/documents.
    """
    _auth(authorization)
    return {
        "runs": recent_runs(),
        "registry": registry_entries(),
        "incidents": recent_incidents(),
        "proposals": _proposals(),
    }


@app.get("/history/documents")
def history_documents(authorization: str | None = Header(None)) -> dict[str, Any]:
    """Free text worth embedding: proposal rationales and incident descriptions."""
    _auth(authorization)
    documents = [
        {
            "text": (
                f"On {proposal['updated_at']} the agent proposed "
                f"{proposal['verdict']} for {proposal['model_name']} with "
                f"confidence {proposal['confidence']}. Reasoning: "
                f"{proposal['rationale']}"
            ),
            "metadata": {
                "source": "proposal",
                "thread_id": proposal["thread_id"],
                "model_name": proposal["model_name"],
                "verdict": proposal["verdict"],
                "human_decision": proposal["human_decision"],
            },
        }
        for proposal in _proposals()
        if proposal.get("rationale")
    ]
    return {"documents": documents + incident_documents()}


class IncidentRequest(BaseModel):
    source: str
    severity: Literal["low", "high"] = "high"
    description: str
    classification: str = ""


@app.post("/incidents")
def create_incident(
    req: IncidentRequest, authorization: str | None = Header(None)
) -> dict[str, Any]:
    """Record a workflow failure. Called by n8n's error handler.

    Severity defaults to high because an unclassified failure is more likely to
    matter than not, and a false alarm is cheaper than a missed outage.
    """
    _auth(authorization)
    return record_incident(
        req.source, req.severity, req.description, req.classification
    )


@app.get("/threads/{thread_id}")
def thread_state(thread_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    snapshot = GRAPH.get_state(_config(thread_id))
    if not snapshot.created_at:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    values = snapshot.values
    return {
        "thread_id": thread_id,
        "next_nodes": list(snapshot.next),
        "verdict": values.get("verdict"),
        "confidence": values.get("confidence"),
        "rationale": values.get("rationale"),
        "iterations": values.get("iterations"),
        "human_decision": values.get("human_decision"),
        "outcome": values.get("outcome", {}),
        "evidence": values.get("evidence", []),
        "updated_at": snapshot.created_at,
    }


# --------------------------------------------------------------------------- #
# The console
#
# Mounted last so every API route above wins the match, and mounted under a
# path rather than at "/" so it can never shadow one of them.
# --------------------------------------------------------------------------- #

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def console_root() -> RedirectResponse:
    return RedirectResponse(url="/console/")


app.mount("/console", StaticFiles(directory=STATIC_DIR, html=True), name="console")
