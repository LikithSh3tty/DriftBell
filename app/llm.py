"""LLM provider factory.

Every option here is free:
  gemini  -> Google AI Studio free tier   (LLM_PROVIDER=gemini,  GOOGLE_API_KEY=...)
  groq    -> Groq free tier               (LLM_PROVIDER=groq,    GROQ_API_KEY=...)
  ollama  -> fully local, no key at all   (LLM_PROVIDER=ollama)
  stub    -> deterministic offline fake   (LLM_PROVIDER=stub)  <- default

The stub exists so the whole graph runs with zero network access. Use it for
unit tests, for CI, and for demoing on a laptop with no internet.
"""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

from langchain_core.messages import AIMessage, BaseMessage


def get_llm(temperature: float = 0.0):
    provider = os.getenv("LLM_PROVIDER", "stub").lower()

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=os.getenv("LLM_MODEL", "gemini-2.0-flash"),
            temperature=temperature,
        )

    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
            temperature=temperature,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=os.getenv("LLM_MODEL", "llama3.1:8b"),
            temperature=temperature,
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        )

    return StubChatModel()


class StubChatModel:
    """Duck-typed stand-in for a chat model.

    Implements only what the graph uses: .bind_tools() and .invoke(). It walks a
    tiny scripted policy so the graph exercises every edge, including the tool
    loop and the self-critique cycle.
    """

    def __init__(self, tools: Sequence[Any] | None = None) -> None:
        self._tools = list(tools or [])

    def bind_tools(self, tools: Sequence[Any]) -> "StubChatModel":
        return StubChatModel(tools)

    def invoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        called = text.count("tool_call_id") + sum(
            1 for m in messages if getattr(m, "tool_calls", None)
        )

        # First pass: ask for evidence via a tool call.
        if self._tools and called == 0:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_feature_stats",
                        "args": {"model_name": "churn_clf", "n_days": 14},
                        "id": "stub_call_1",
                        "type": "tool_call",
                    }
                ],
            )

        # Critique node asks for a strict JSON verdict.
        if "self-critique" in text.lower():
            return AIMessage(
                content=json.dumps(
                    {"needs_more_evidence": False, "reason": "evidence is sufficient"}
                )
            )

        if "final verdict" in text.lower():
            return AIMessage(
                content=json.dumps(
                    {
                        "verdict": "RETRAIN",
                        "confidence": 0.82,
                        "rationale": (
                            "PSI above threshold on two input features with a "
                            "matching drop in production precision; the shift "
                            "looks like real covariate drift, not a pipeline bug."
                        ),
                    }
                )
            )

        return AIMessage(content="Evidence collected; ready to critique.")
