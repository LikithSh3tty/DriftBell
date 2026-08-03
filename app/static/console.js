/* Driftbell console.
 *
 * Reads the SSE trace off /diagnose/stream and /resume/stream, lights the rail
 * as each node fires, and hands the frozen proposal back to the same /resume
 * contract Telegram uses. Nothing here decides anything: the buttons post a
 * decision and the graph does the rest. */

const NODE_Y = {
  gather_evidence: 24,
  reason: 94,
  tools: 158,
  critique: 224,
  propose: 290,
  human_gate: 356,
  execute: 452,
};

const PRESETS = {
  clear:      { psi: 0.284, ks_statistic: 0.31, p_value: 0.001,  drifted_features: "monthly_spend", n_samples: 5000 },
  borderline: { psi: 0.118, ks_statistic: 0.14, p_value: 0.061,  drifted_features: "session_count", n_samples: 4200 },
  quiet:      { psi: 0.031, ks_statistic: 0.04, p_value: 0.624,  drifted_features: "",              n_samples: 4800 },
};

const $ = (id) => document.getElementById(id);
const trace = $("trace");
const runBtn = $("run-btn");

let passes = {};   // node -> how many times it has fired this run
let furthest = 24; // top of the rail, in svg units

/* ----------------------------------------------------------------- auth -- */

const token = $("token");
token.value = localStorage.getItem("driftbell_token") || "";
token.addEventListener("change", () =>
  localStorage.setItem("driftbell_token", token.value.trim())
);

function headers() {
  const h = { "Content-Type": "application/json" };
  const t = token.value.trim();
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

/* ----------------------------------------------------------------- rail -- */

function resetRail() {
  passes = {};
  furthest = 24;
  document.querySelectorAll(".node").forEach((n) =>
    n.classList.remove("done", "active", "frozen", "skipped")
  );
  document.querySelectorAll(".arc").forEach((a) => a.classList.remove("hot"));
  $("spine-progress").setAttribute("y2", 24);
  $("spine-frozen").classList.remove("on");
  $("freeze-band").classList.remove("on");
}

function markNode(name, state) {
  const g = document.querySelector(`.node[data-node="${name}"]`);
  if (!g) return;
  document.querySelectorAll(".node.active").forEach((n) => {
    n.classList.remove("active");
    n.classList.add("done");
  });
  g.classList.remove("skipped", "frozen");
  g.classList.add(state);

  // The gate firing as a normal node means someone answered: thaw the rail.
  if (name === "human_gate") {
    $("spine-frozen").classList.remove("on");
    $("freeze-band").classList.remove("on");
  }

  const y = NODE_Y[name];
  if (y && y > furthest) {
    furthest = y;
    $("spine-progress").setAttribute("y2", y);
  }
  if (name === "tools") $("arc-tools").classList.add("hot");
  if (name === "reason" && passes.reason > 1) $("arc-critique").classList.add("hot");
}

function freezeRail() {
  document.querySelectorAll(".node.active").forEach((n) => {
    n.classList.remove("active");
    n.classList.add("done");
  });
  const gate = document.querySelector('.node[data-node="human_gate"]');
  gate.classList.remove("done", "active");
  gate.classList.add("frozen");
  $("spine-progress").setAttribute("y2", NODE_Y.human_gate);
  $("spine-frozen").classList.add("on");
  $("freeze-band").classList.add("on");
}

/* ---------------------------------------------------------------- trace -- */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function addLine(entry, tag, body, cls) {
  const line = el("div", "line" + (cls ? " " + cls : ""));
  line.append(el("span", "tag", tag), el("span", "body", body));
  entry.append(line);
}

/** Tool output is often a wall of JSON. Anything short stays inline; anything
 *  long collapses to its size so one call cannot bury the rest of the run. */
function addToolResult(entry, line) {
  if (line.text.length <= 200) {
    addLine(entry, "returns", line.text, "tool-result");
    return;
  }
  const wrap = el("div", "line tool-result");
  const details = el("details");
  const rows = line.text.split("\n").length;
  details.append(
    el("summary", null, `${line.name} — ${rows} lines, ${line.text.length} chars`),
    el("pre", null, line.text)
  );
  wrap.append(el("span", "tag", "returns"), details);
  entry.append(wrap);
}

function renderEvent(ev) {
  $("empty").hidden = true;
  passes[ev.node] = (passes[ev.node] || 0) + 1;

  const entry = el("li", "entry");
  const head = el("div", "entry-node", ev.node);
  // The gate firing twice is a freeze and a thaw, not a loop, so it gets no
  // pass count — only the cycles do.
  if (passes[ev.node] > 1 && ev.node !== "human_gate") {
    head.append(el("span", "pass", `  · pass ${passes[ev.node]}`));
  }
  entry.append(head);

  for (const line of ev.lines || []) {
    if (line.kind === "tool_call") {
      addLine(entry, "calls", `${line.name}(${JSON.stringify(line.args)})`, "tool-call");
    } else if (line.kind === "tool_result") {
      addToolResult(entry, line);
    } else if (line.kind === "say") {
      addLine(entry, "says", line.text);
    } else if (line.kind === "evidence") {
      addLine(entry, "evidence", line.source);
    }
  }
  // gather_evidence seeds this field too, but only critique is answering the
  // question, so only critique gets to report on it.
  if (ev.node === "critique") {
    addLine(entry, "critique", ev.needs_more_evidence
      ? "evidence insufficient — looping back to reason"
      : "evidence sufficient");
  }
  if (ev.verdict) {
    addLine(entry, "verdict", `${ev.verdict}  confidence ${ev.confidence}`);
  }
  if (ev.outcome && Object.keys(ev.outcome).length) {
    addLine(entry, "outcome", JSON.stringify(ev.outcome));
  }

  trace.append(entry);
  entry.scrollIntoView({ block: "nearest" });
  return entry;
}

function showError(message) {
  const box = el("div", "error", message);
  trace.append(box);
  box.scrollIntoView({ block: "nearest" });
}

/* ------------------------------------------------------------- proposal -- */

function renderProposal(proposal, threadId) {
  const card = el("div", "proposal");
  const row = el("div", "verdict-row");
  const chip = el("span", "verdict", proposal.verdict || "ESCALATE");
  chip.dataset.v = proposal.verdict || "ESCALATE";
  const pct = Math.round((proposal.confidence || 0) * 100);
  row.append(chip, el("span", "confidence", `confidence ${pct}%`));

  const meter = el("div", "meter");
  const fill = el("i");
  fill.style.width = pct + "%";
  meter.append(fill);

  card.append(row, meter, el("p", "rationale", proposal.rationale || ""));

  const decide = el("div", "decide");
  const yes = el("button", "approve", "Approve");
  const no = el("button", "reject", "Reject");
  decide.append(yes, no);
  card.append(decide);

  const settle = (decision) => {
    yes.disabled = no.disabled = true;
    decide.replaceWith(el("div", "settled", `${decision}d — resuming thread`));
    stream("/resume/stream", { thread_id: threadId, decision, note: "from console" });
  };
  yes.addEventListener("click", () => settle("approve"));
  no.addEventListener("click", () => settle("reject"));

  trace.lastElementChild.append(card);
  card.scrollIntoView({ block: "nearest" });
}

/* --------------------------------------------------------------- stream -- */

/** Read an SSE body off fetch(), so the request can carry an auth header —
 *  EventSource cannot, and it only speaks GET. */
async function stream(path, body) {
  runBtn.disabled = true;
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.text();
      showError(`${res.status} ${detail}`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        handleFrame(frame);
      }
    }
  } catch (err) {
    showError("stream failed: " + err.message);
  } finally {
    runBtn.disabled = false;
  }
}

function handleFrame(frame) {
  let name = "message";
  let raw = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) raw += line.slice(5).trim();
  }
  if (!raw) return;
  const data = JSON.parse(raw);

  if (name === "start") {
    $("thread-label").textContent = data.thread_id;
    $("thread_id").value = data.thread_id;
  } else if (name === "node") {
    if (data.frozen) {
      renderEvent({ node: "human_gate", lines: [] });
      freezeRail();
      renderProposal(data.proposal || {}, $("thread_id").value);
    } else {
      renderEvent(data);
      markNode(data.node, "active");
    }
  } else if (name === "done") {
    if (data.status === "completed") {
      document.querySelectorAll(".node.active").forEach((n) => {
        n.classList.remove("active");
        n.classList.add("done");
      });
      if (data.decision === "not_required") {
        document.querySelector('.node[data-node="human_gate"]').classList.add("skipped");
        document.querySelector('.node[data-node="execute"]').classList.add("skipped");
      }
    }
  } else if (name === "error") {
    showError(data.detail);
  }
}

/* ----------------------------------------------------------------- runs -- */

function newRun() {
  trace.replaceChildren();
  $("empty").hidden = true;
  resetRail();
}

$("alert-form").addEventListener("submit", (e) => {
  e.preventDefault();
  newRun();
  const features = $("drifted_features").value
    .split(",")
    .map((f) => f.trim())
    .filter(Boolean);
  // n8n derives the window from the schedule; here the last seven days stands
  // in for it, so the agent's transcript reads the same either way.
  const day = 86400000;
  const end = new Date();
  const start = new Date(end - 7 * day);

  stream("/diagnose/stream", {
    drift_report: {
      model_name: $("model_name").value.trim(),
      psi: Number($("psi").value),
      ks_statistic: Number($("ks_statistic").value),
      p_value: Number($("p_value").value),
      drifted_features: features,
      window_start: start.toISOString().slice(0, 10),
      window_end: end.toISOString().slice(0, 10),
      n_samples: Number($("n_samples").value),
    },
  });
});

document.querySelectorAll(".preset").forEach((btn) =>
  btn.addEventListener("click", () => {
    const p = PRESETS[btn.dataset.preset];
    for (const [key, value] of Object.entries(p)) $(key).value = value;
  })
);

/* ------------------------------------------------------- loading a thread */

$("load-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("thread_id").value.trim();
  if (!id) return;
  newRun();
  try {
    const res = await fetch(`/threads/${encodeURIComponent(id)}`, { headers: headers() });
    if (!res.ok) {
      $("empty").hidden = false;
      showError(`${res.status} — no thread with that id`);
      return;
    }
    const state = await res.json();
    $("thread-label").textContent = state.thread_id;

    const entry = renderEvent({ node: "loaded from checkpoint", lines: [] });
    addLine(entry, "parked at", (state.next_nodes || []).join(", ") || "nothing — run finished");
    addLine(entry, "evidence", `${(state.evidence || []).length} items`);
    addLine(entry, "updated", state.updated_at || "unknown");

    if ((state.next_nodes || []).includes("human_gate")) {
      freezeRail();
      renderProposal(state, id);
    } else if (state.human_decision) {
      addLine(entry, "decision", state.human_decision);
      markNode("execute", "done");
    }
  } catch (err) {
    showError("could not load thread: " + err.message);
  }
});

/* --------------------------------------------------------------- health -- */

(async function health() {
  try {
    const res = await fetch("/health");
    const data = await res.json();
    $("health-dot").dataset.state = "ok";
    $("health-text").textContent = `agent up · provider ${data.provider}`;
  } catch {
    $("health-dot").dataset.state = "down";
    $("health-text").textContent = "agent unreachable";
  }
})();
