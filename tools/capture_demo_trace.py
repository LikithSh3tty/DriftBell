"""Record a real stub-provider run into the JSON the deployed console replays.

The hosted console has no agent behind it, so it plays a recording instead of
streaming. The recording has to be genuine or the site is a lie, so this drives
the actual graph through a TestClient and writes down exactly what came back —
the same events a live browser would receive.

Re-run it whenever the graph, the trace shape or the stub script changes:

    python tools/capture_demo_trace.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "app" / "static" / "demo-trace.json"

REPORT = {
    "model_name": "churn_clf",
    "psi": 0.284,
    "ks_statistic": 0.31,
    "p_value": 0.001,
    "drifted_features": ["monthly_spend"],
    "window_start": "2026-07-27",
    "window_end": "2026-08-03",
    "n_samples": 5000,
}

# Roughly how long each node takes against a real provider. The stub answers
# instantly, and a trace that arrives all at once shows nothing, so playback is
# paced to the shape of a real run: the LLM turns are the slow ones.
PACING_MS = {
    "gather_evidence": 260,
    "reason": 900,
    "tools": 420,
    "critique": 780,
    "propose": 950,
    "human_gate": 500,
    "execute": 340,
}


def frames(body: str) -> list[dict]:
    """Parse an SSE body into the event list the console replays."""
    out = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        payload = json.loads(data)
        delay = PACING_MS.get(payload.get("node", ""), 200) if name == "node" else 150
        out.append({"event": name, "data": payload, "delay": delay})
    return out


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="driftbell-demo-"))
    os.environ.update(
        {
            "DRIFTBELL_DB": str(tmp / "driftbell.db"),
            "CHECKPOINT_DB": str(tmp / "checkpoints.db"),
            "LLM_PROVIDER": "stub",
            "SERVICE_TOKEN": "",
        }
    )

    from fastapi.testclient import TestClient

    from seed_db import seed

    seed(os.environ["DRIFTBELL_DB"])

    from app.main import app

    client = TestClient(app)

    def run_to_gate() -> tuple[str, list[dict]]:
        events = frames(client.post("/diagnose/stream", json={"drift_report": REPORT}).text)
        return events[0]["data"]["thread_id"], events

    approved_id, diagnose = run_to_gate()
    approve = frames(
        client.post(
            "/resume/stream",
            json={"thread_id": approved_id, "decision": "approve", "note": "from the console"},
        ).text
    )

    rejected_id, _ = run_to_gate()
    reject = frames(
        client.post(
            "/resume/stream",
            json={"thread_id": rejected_id, "decision": "reject", "note": "from the console"},
        ).text
    )

    # A third run left parked, so "resume a frozen thread" has something real to
    # recover — the state a browser would see for a thread still at the gate.
    parked_id, _ = run_to_gate()
    parked = client.get(f"/threads/{parked_id}").json()

    OUT.write_text(
        json.dumps(
            {
                "recorded_from": "LLM_PROVIDER=stub, seeded synthetic history",
                "report": REPORT,
                "diagnose": diagnose,
                "approve": approve,
                "reject": reject,
                "parked_thread": parked,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  diagnose {len(diagnose)} events, approve {len(approve)}, reject {len(reject)}")
    print(f"  parked thread {parked['thread_id']} at {parked['next_nodes']}")


if __name__ == "__main__":
    main()
