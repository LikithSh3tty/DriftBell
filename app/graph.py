"""The Driftbell diagnostic graph.

    START -> gather_evidence -> reason -+-> tools -> reason      (tool cycle)
                                        |
                                        +-> critique -+-> reason (reflection cycle)
                                                      |
                                                      +-> propose -> human_gate
                                                                        |
                                                        approve -> execute -> END
                                                        reject  ------------> END

Two things here are impossible to express on a plain n8n canvas, and they are
the reason this service exists:
  1. the two cycles (tool loop, reflection loop) with a bounded iteration count
  2. interrupt() + a checkpointer, which freezes the run mid-graph and lets a
     completely different process resume it hours later by thread_id
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from .llm import get_llm
from .state import DiagnosisState
from .tools import TOOLS

MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "3"))

SYSTEM = (
    "You are an MLOps reliability analyst. A drift monitor has fired on a "
    "production model. Investigate with the tools available, then decide "
    "between RETRAIN (real drift worth acting on), IGNORE (noise or an "
    "expected seasonal shift), and ESCALATE (looks like a data-pipeline bug a "
    "human must inspect). Be sceptical: a drift alert that coincides with an "
    "ingestion incident is usually a bug, not drift."
)


def _parse_json(text: str) -> dict[str, Any]:
    """LLMs wrap JSON in prose and code fences. Recover the object anyway."""
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return json.loads(cleaned.strip())
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {}


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #

def gather_evidence(state: DiagnosisState) -> dict[str, Any]:
    """Deterministic first step: seed the scratchpad with the alert itself.

    Doing this without an LLM keeps the transcript reproducible and saves a
    call on every single run.
    """
    report = state.get("drift_report", {})
    summary = (
        f"Drift alert for model '{report.get('model_name', 'unknown')}'.\n"
        f"PSI={report.get('psi')}, KS={report.get('ks_statistic')}, "
        f"p={report.get('p_value')}\n"
        f"Drifted features: {', '.join(report.get('drifted_features', [])) or 'none'}\n"
        f"Window: {report.get('window_start')} to {report.get('window_end')} "
        f"({report.get('n_samples')} samples)"
    )
    return {
        "messages": [SystemMessage(content=SYSTEM), HumanMessage(content=summary)],
        "evidence": [{"source": "drift_monitor", "payload": report}],
        "iterations": 0,
        "needs_more_evidence": False,
    }


def reason(state: DiagnosisState) -> dict[str, Any]:
    """LLM turn with tools bound. May emit tool calls, which routes to `tools`."""
    llm = get_llm().bind_tools(TOOLS)
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


def critique(state: DiagnosisState) -> dict[str, Any]:
    """Self-critique: is the evidence sufficient, or do we loop again?"""
    llm = get_llm()
    prompt = HumanMessage(
        content=(
            "Perform a self-critique of the investigation so far. Have you "
            "checked recent runs, feature statistics, and pipeline incidents? "
            'Reply with JSON only: {"needs_more_evidence": bool, "reason": str}'
        )
    )
    parsed = _parse_json(llm.invoke(state["messages"] + [prompt]).content)
    iterations = state.get("iterations", 0) + 1
    return {
        "iterations": iterations,
        "needs_more_evidence": bool(parsed.get("needs_more_evidence", False)),
        "evidence": [
            {"source": "critique", "iteration": iterations, "payload": parsed}
        ],
    }


def propose(state: DiagnosisState) -> dict[str, Any]:
    """Collapse the investigation into a structured, auditable proposal."""
    llm = get_llm()
    prompt = HumanMessage(
        content=(
            "Give your final verdict. Reply with JSON only: "
            '{"verdict": "RETRAIN"|"IGNORE"|"ESCALATE", '
            '"confidence": float between 0 and 1, "rationale": str}'
        )
    )
    parsed = _parse_json(llm.invoke(state["messages"] + [prompt]).content)
    verdict = parsed.get("verdict", "ESCALATE")
    if verdict not in ("RETRAIN", "IGNORE", "ESCALATE"):
        verdict = "ESCALATE"
    return {
        "verdict": verdict,
        "confidence": float(parsed.get("confidence", 0.0)),
        "rationale": parsed.get("rationale", "No rationale produced."),
    }


def human_gate(state: DiagnosisState) -> dict[str, Any]:
    """Freeze here. The payload is what n8n renders on the Telegram approval card.

    interrupt() raises out of the graph; the checkpointer persists everything
    written so far. Nothing below this line runs until someone resumes the
    thread with Command(resume=...).
    """
    decision = interrupt(
        {
            "thread_id": state.get("thread_id"),
            "verdict": state.get("verdict"),
            "confidence": state.get("confidence"),
            "rationale": state.get("rationale"),
            "model_name": state.get("drift_report", {}).get("model_name"),
            "evidence_count": len(state.get("evidence", [])),
            "prompt": "Approve this action?",
        }
    )
    if isinstance(decision, str):
        decision = {"decision": decision}
    return {
        "human_decision": (decision or {}).get("decision", "reject"),
        "human_note": (decision or {}).get("note", ""),
    }


def execute(state: DiagnosisState) -> dict[str, Any]:
    """Carry out the approved action.

    Kept deliberately thin: it records intent and lets n8n perform the actual
    side effects (calling the training job, pushing to the registry, notifying).
    That keeps every irreversible action inside the layer that has the audit
    trail and the credentials.
    """
    return {
        "outcome": {
            "status": "approved",
            "action": state.get("verdict"),
            "note": state.get("human_note", ""),
            "model_name": state.get("drift_report", {}).get("model_name"),
        }
    }


# --------------------------------------------------------------------------- #
# Conditional edges
# --------------------------------------------------------------------------- #

def route_after_reason(state: DiagnosisState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "critique"


def route_after_critique(state: DiagnosisState) -> str:
    if state.get("needs_more_evidence") and state.get("iterations", 0) < MAX_ITERATIONS:
        return "reason"
    return "propose"


def route_after_propose(state: DiagnosisState) -> str:
    # Nothing irreversible is proposed, so skip the human entirely.
    return END if state.get("verdict") == "IGNORE" else "human_gate"


def route_after_gate(state: DiagnosisState) -> str:
    return "execute" if state.get("human_decision") == "approve" else END


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def build_graph(checkpointer=None):
    g = StateGraph(DiagnosisState)

    g.add_node("gather_evidence", gather_evidence)
    g.add_node("reason", reason)
    g.add_node("tools", ToolNode(TOOLS))
    g.add_node("critique", critique)
    g.add_node("propose", propose)
    g.add_node("human_gate", human_gate)
    g.add_node("execute", execute)

    g.add_edge(START, "gather_evidence")
    g.add_edge("gather_evidence", "reason")
    g.add_conditional_edges("reason", route_after_reason, ["tools", "critique"])
    g.add_edge("tools", "reason")
    g.add_conditional_edges("critique", route_after_critique, ["reason", "propose"])
    g.add_conditional_edges("propose", route_after_propose, ["human_gate", END])
    g.add_conditional_edges("human_gate", route_after_gate, ["execute", END])
    g.add_edge("execute", END)

    return g.compile(checkpointer=checkpointer)


def make_checkpointer(path: str | None = None) -> SqliteSaver:
    """A long-lived saver for the FastAPI process.

    SqliteSaver.from_conn_string() is a context manager and closes on exit, so
    the connection is built by hand here instead.
    """
    path = path or os.getenv("CHECKPOINT_DB", "checkpoints.db")
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)
