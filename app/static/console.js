/* Driftbell console.
 *
 * Two modes, chosen by whether an agent answers /health:
 *
 *   live — streams the real graph off /diagnose/stream and /resume/stream and
 *          answers the gate through /resume, the same contract Telegram uses.
 *   demo — no agent, so it replays demo-trace.json, a recording captured from
 *          an actual stub-provider run by tools/capture_demo_trace.py. The
 *          events are the agent's own; only the timing is added.
 *
 * The hosted page is always demo. Nothing here decides anything in either
 * mode: the buttons post a decision and the graph does the rest. */

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
  clear:      { psi: 0.284, ks_statistic: 0.31, p_value: 0.001, drifted_features: "monthly_spend", n_samples: 5000 },
  borderline: { psi: 0.118, ks_statistic: 0.14, p_value: 0.061, drifted_features: "session_count", n_samples: 4200 },
  quiet:      { psi: 0.031, ks_statistic: 0.04, p_value: 0.624, drifted_features: "",              n_samples: 4800 },
};

const LIVE_ALERT_HINT =
  "The payload workflow 01 posts once it has computed PSI and KS on the " +
  "canvas. Under the stub provider the verdict is scripted, so every preset " +
  "lands on RETRAIN. Point the agent at Gemini, Groq or Ollama and the " +
  "numbers start deciding.";

const DEMO_ALERT_HINT =
  "These are the values the recorded run was given, so they are fixed here. " +
  "Rewriting them would mean rewriting what the agent said back.";

const LIVE_LOAD_HINT =
  "A thread parked at the gate outlives the process that started it. Paste an " +
  "id from an earlier run. A restart in between changes nothing.";

const DEMO_LOAD_HINT =
  "One thread was left parked when this trace was recorded. Load it to see " +
  "the state a browser recovers for a run still waiting at the gate.";

/* Pacing is the point of the replay, but a reduced-motion preference means the
   page should arrive at the answer rather than perform its way there. */
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

const $ = (id) => document.getElementById(id);
const trace = $("trace");
const runBtn = $("run-btn");

let mode = "unknown";   // "live" | "demo"
let recording = null;   // demo-trace.json, once fetched
let autoplayed = false; // the demo plays itself once, not on every re-detect
let passes = {};        // node -> how many times it has fired this run
let furthest = 24;      // top of the rail, in svg units

/* ------------------------------------------------------------- settings -- */

const agentUrl = $("agent-url");
const token = $("token");

agentUrl.value = localStorage.getItem("driftbell_agent") || "";
token.value = localStorage.getItem("driftbell_token") || "";

/** Where the agent lives. Empty means this page is served by it. */
function base() {
  return agentUrl.value.trim().replace(/\/+$/, "");
}

function api(path) {
  return base() + path;
}

function headers() {
  const h = { "Content-Type": "application/json" };
  const t = token.value.trim();
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

agentUrl.addEventListener("change", () => {
  localStorage.setItem("driftbell_agent", base());
  detectMode();
});
token.addEventListener("change", () =>
  localStorage.setItem("driftbell_token", token.value.trim())
);

/* ----------------------------------------------------------------- mode -- */

async function detectMode() {
  $("health-dot").dataset.state = "unknown";
  $("health-text").textContent = "checking for an agent";
  try {
    const res = await fetch(api("/health"), { headers: headers() });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    setLive(data.provider);
  } catch {
    await setDemo();
  }
}

function setLive(provider) {
  mode = "live";
  $("health-dot").dataset.state = "ok";
  $("health-text").textContent = `live · provider ${provider}`;
  $("banner").hidden = true;
  $("alert-hint").textContent = LIVE_ALERT_HINT;
  $("load-hint").textContent = LIVE_LOAD_HINT;
  $("thread_id").placeholder = "drift-7971e1d5e05e";
  setFormLocked(false);
}

async function setDemo() {
  mode = "demo";
  $("health-dot").dataset.state = "demo";
  $("health-text").textContent = "recorded demo · no agent";
  $("banner").hidden = false;
  $("alert-hint").textContent = DEMO_ALERT_HINT;
  $("load-hint").textContent = DEMO_LOAD_HINT;

  if (!recording) {
    try {
      recording = await fetch("demo-trace.json").then((r) => r.json());
    } catch {
      $("health-text").textContent = "no agent, and the recording failed to load";
      return;
    }
  }

  const report = recording.report;
  $("model_name").value = report.model_name;
  $("psi").value = report.psi;
  $("ks_statistic").value = report.ks_statistic;
  $("p_value").value = report.p_value;
  $("drifted_features").value = report.drifted_features.join(", ");
  $("n_samples").value = report.n_samples;
  $("thread_id").placeholder = recording.parked_thread.thread_id;
  setFormLocked(true);

  // The sheet writes itself. A visitor who does nothing still watches the pen
  // run, reach the gate and stop, which is the one thing this page exists to
  // show; making them press a button first put a chore in front of it.
  if (!autoplayed) {
    autoplayed = true;
    newRun();
    replay("/diagnose/stream", {});
  }
}

/** In demo mode the payload is part of the recording, so it is shown but not
 *  editable — a form that accepted changes it could not honour would be a lie
 *  about what the agent was asked. */
function setFormLocked(locked) {
  for (const id of ["model_name", "psi", "ks_statistic", "p_value",
                    "drifted_features", "n_samples"]) {
    $(id).readOnly = locked;
  }
  document.querySelectorAll(".preset").forEach((b) => (b.disabled = locked));
  $("alert-form").classList.toggle("locked", locked);
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

  // The gate firing as a normal node means someone answered, so the pen picks
  // up again and the stop mark comes off.
  if (name === "human_gate") $("freeze-band").classList.remove("on");

  const y = NODE_Y[name];
  if (y && y > furthest) {
    furthest = y;
    $("spine-progress").setAttribute("y2", y);
  }
  if (name === "tools") $("arc-tools").classList.add("hot");
  if (name === "reason" && passes.reason > 1) $("arc-critique").classList.add("hot");
}

function settleRail() {
  document.querySelectorAll(".node.active").forEach((n) => {
    n.classList.remove("active");
    n.classList.add("done");
  });
}

function freezeRail() {
  settleRail();
  const gate = document.querySelector('.node[data-node="human_gate"]');
  gate.classList.remove("done", "active");
  gate.classList.add("frozen");
  $("spine-progress").setAttribute("y2", NODE_Y.human_gate);
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
    el("summary", null, `${line.name}, ${rows} lines, ${line.text.length} chars`),
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
  // gather_evidence seeds needs_more_evidence too, but only critique is
  // answering the question, so only critique gets to report on it.
  if (ev.node === "critique") {
    addLine(entry, "critique", ev.needs_more_evidence
      ? "evidence insufficient, looping back to reason"
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
    const past = decision === "approve" ? "Approved" : "Rejected";
    decide.replaceWith(el("div", "settled", `${past}. Resuming the thread.`));
    stream("/resume/stream", { thread_id: threadId, decision, note: "from the console" });
  };
  yes.addEventListener("click", () => settle("approve"));
  no.addEventListener("click", () => settle("reject"));

  trace.lastElementChild.append(card);
  card.scrollIntoView({ block: "nearest" });
}

/* --------------------------------------------------------------- events -- */

function handleEvent(name, data) {
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
      settleRail();
      if (data.decision === "not_required") {
        document.querySelector('.node[data-node="human_gate"]').classList.add("skipped");
        document.querySelector('.node[data-node="execute"]').classList.add("skipped");
      }
    }
  } else if (name === "error") {
    showError(data.detail);
  }
}

/* --------------------------------------------------------------- stream -- */

function stream(path, body) {
  return mode === "demo" ? replay(path, body) : streamLive(path, body);
}

/** Read an SSE body off fetch(), so the request can carry an auth header —
 *  EventSource cannot, and it only speaks GET. */
async function streamLive(path, body) {
  runBtn.disabled = true;
  try {
    const res = await fetch(api(path), {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showError(`${res.status} ${await res.text()}`);
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
  if (raw) handleEvent(name, JSON.parse(raw));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Play a recorded run back at the pace a real provider would answer.
 *  The events are verbatim; only the waiting is manufactured. */
async function replay(path, body) {
  const events =
    path === "/diagnose/stream"
      ? recording.diagnose
      : body.decision === "approve"
      ? recording.approve
      : recording.reject;

  runBtn.disabled = true;
  try {
    for (const step of events) {
      await sleep(REDUCED ? 0 : step.delay);
      handleEvent(step.event, step.data);
    }
  } finally {
    runBtn.disabled = false;
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

  if (mode === "demo") {
    replay("/diagnose/stream", {});
    return;
  }

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

function renderThread(state, id) {
  $("thread-label").textContent = state.thread_id;

  const entry = renderEvent({ node: "loaded from checkpoint", lines: [] });
  addLine(entry, "parked at", (state.next_nodes || []).join(", ") || "nothing, the run finished");
  addLine(entry, "evidence", `${(state.evidence || []).length} items`);
  addLine(entry, "updated", state.updated_at || "unknown");

  if ((state.next_nodes || []).includes("human_gate")) {
    freezeRail();
    renderProposal(state, id);
  } else if (state.human_decision) {
    addLine(entry, "decision", state.human_decision);
    markNode("execute", "done");
  }
}

$("load-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("thread_id").value.trim() || $("thread_id").placeholder;
  newRun();

  if (mode === "demo") {
    const parked = recording.parked_thread;
    if (id !== parked.thread_id) {
      $("empty").hidden = false;
      showError(
        `Only ${parked.thread_id} exists in this recording. A live agent would ` +
        `look any id up in its checkpoint database.`
      );
      return;
    }
    $("thread_id").value = id;
    renderThread(parked, id);
    return;
  }

  try {
    const res = await fetch(api(`/threads/${encodeURIComponent(id)}`), { headers: headers() });
    if (!res.ok) {
      $("empty").hidden = false;
      showError(`${res.status}. No thread with that id.`);
      return;
    }
    renderThread(await res.json(), id);
  } catch (err) {
    showError("could not load thread: " + err.message);
  }
});

detectMode();
