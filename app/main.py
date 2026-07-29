"""HTTP contract between n8n and the LangGraph agent.

    POST /diagnose            -> runs until the human gate, returns the proposal
    POST /resume              -> resumes a frozen thread with a human decision
    GET  /threads/{id}        -> current state snapshot (audit / polling)
    GET  /health              -> readiness probe for the n8n error workflow
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from .graph import build_graph, make_checkpointer
from .state import DriftReport

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


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _shape(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Flatten the graph result into the JSON n8n branches on."""
    interrupts = result.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        return {"status": "awaiting_approval", "thread_id": thread_id, "proposal": payload}
    return {
        "status": "completed",
        "thread_id": thread_id,
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
