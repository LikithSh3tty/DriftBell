"""Graph state for the Driftbell diagnostic agent.

The state is the contract between every node. Two fields use reducers so that
parallel or repeated writes accumulate instead of overwriting:
  - messages : LangGraph's add_messages reducer (append + dedupe by id)
  - evidence : plain list concatenation
Everything else uses last-write-wins, which is the LangGraph default.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Verdict = Literal["RETRAIN", "IGNORE", "ESCALATE"]


class DriftReport(TypedDict, total=False):
    """What n8n posts in: the output of the drift-detection Code node."""

    model_name: str
    psi: float
    ks_statistic: float
    p_value: float
    drifted_features: list[str]
    window_start: str
    window_end: str
    n_samples: int


class DiagnosisState(TypedDict, total=False):
    # --- inputs ---
    thread_id: str
    drift_report: DriftReport

    # --- reasoning scratchpad ---
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: Annotated[list[dict[str, Any]], operator.add]
    iterations: int
    needs_more_evidence: bool

    # --- proposal ---
    verdict: Verdict
    confidence: float
    rationale: str

    # --- human-in-the-loop ---
    human_decision: Literal["approve", "reject", ""]
    human_note: str

    # --- terminal ---
    outcome: dict[str, Any]
