"use strict";

const form = document.getElementById("query-form");
const queryEl = document.getElementById("query");
const submitBtn = document.getElementById("submit");
const statusEl = document.getElementById("status");
const errorBanner = document.getElementById("error-banner");
const timelineEl = document.getElementById("timeline");
const streamingEl = document.getElementById("answer-streaming");
const finalEl = document.getElementById("answer-final");

let activeSource = null;
const startTimes = new Map(); // agent_id -> performance.now() when AGENT_START seen
const toolStartTimes = new Map(); // tool_name -> ts

function setStatus(label, cls) {
  statusEl.textContent = label;
  statusEl.className = cls;
}

function setSubmitEnabled(enabled) {
  submitBtn.disabled = !enabled;
}

function showError(msg) {
  errorBanner.textContent = msg;
  errorBanner.hidden = false;
}

function clearError() {
  errorBanner.textContent = "";
  errorBanner.hidden = true;
}

function agentClass(agentId) {
  if (agentId.startsWith("critique:")) return "agent-critique";
  if (agentId === "orchestrator") return "agent-orchestrator";
  if (agentId === "decomposition") return "agent-decomposition";
  if (agentId === "rag") return "agent-rag";
  if (agentId === "synthesis") return "agent-synthesis";
  return "agent-tools";
}

function appendTimelineEntry(agentLabel, eventLabel, latencyMs, klass) {
  const li = document.createElement("li");
  li.className = `entry ${klass}`;
  const dot = document.createElement("span"); dot.className = "dot";
  const agent = document.createElement("span"); agent.className = "agent"; agent.textContent = agentLabel;
  const ev = document.createElement("span"); ev.className = "event"; ev.textContent = eventLabel;
  const lat = document.createElement("span"); lat.className = "lat";
  lat.textContent = latencyMs == null ? "" : `${Math.round(latencyMs)} ms`;
  li.append(dot, agent, ev, lat);
  timelineEl.appendChild(li);
  li.scrollIntoView({ block: "nearest" });
}

function resetUiForNewJob() {
  clearError();
  timelineEl.innerHTML = "";
  streamingEl.textContent = "";
  streamingEl.hidden = false;
  finalEl.innerHTML = "";
  finalEl.hidden = true;
  startTimes.clear();
  toolStartTimes.clear();
}

const PREFIX_RE = /^\s*\[[^\]]*\]\s*/;

function renderFinalAnswer(finalAnswer) {
  finalEl.innerHTML = "";
  if (!Array.isArray(finalAnswer) || finalAnswer.length === 0) {
    finalEl.textContent = "(no final answer in trace)";
    finalEl.hidden = false;
    streamingEl.hidden = true;
    return;
  }
  for (const sp of finalAnswer) {
    const p = document.createElement("p");
    p.className = "sentence";
    const txt = (sp.sentence_text || "").replace(PREFIX_RE, "").trim();
    p.appendChild(document.createTextNode(txt));
    const ids = Array.isArray(sp.source_chunk_ids) ? sp.source_chunk_ids : [];
    for (const cid of ids) {
      const badge = document.createElement("span");
      badge.className = "citation";
      badge.textContent = `[${cid}]`;
      p.appendChild(badge);
    }
    finalEl.appendChild(p);
  }
  finalEl.hidden = false;
  streamingEl.hidden = true;
}

async function fetchTraceAndRender(jobId) {
  try {
    const res = await fetch(`/trace/${jobId}`);
    if (!res.ok) {
      console.warn("trace fetch failed", res.status);
      const note = document.createElement("p");
      note.className = "trace-warn";
      note.textContent = "(trace unavailable; showing live-streamed text)";
      streamingEl.appendChild(note);
      return;
    }
    const data = await res.json();
    renderFinalAnswer(data.final_answer);
  } catch (e) {
    console.warn("trace fetch error", e);
  }
}

function dispatch(evt, jobId) {
  switch (evt.type) {
    case "agent_start": {
      startTimes.set(evt.agent_id, performance.now());
      appendTimelineEntry(evt.agent_id, "start", null, agentClass(evt.agent_id));
      break;
    }
    case "agent_end": {
      const start = startTimes.get(evt.agent_id);
      const lat = start != null ? performance.now() - start : null;
      appendTimelineEntry(evt.agent_id, "end", lat, agentClass(evt.agent_id));
      break;
    }
    case "tool_call_start": {
      toolStartTimes.set(evt.tool_name, performance.now());
      appendTimelineEntry(`tool:${evt.tool_name}`, "start", null, "agent-tools");
      break;
    }
    case "tool_call_end": {
      const lat = evt.latency_ms != null
        ? evt.latency_ms
        : (toolStartTimes.has(evt.tool_name)
            ? performance.now() - toolStartTimes.get(evt.tool_name)
            : null);
      const label = evt.success ? "end" : "end (failed)";
      appendTimelineEntry(`tool:${evt.tool_name}`, label, lat, "agent-tools");
      break;
    }
    case "token": {
      if (evt.agent_id === "synthesis") {
        streamingEl.textContent += evt.text;
        streamingEl.scrollTop = streamingEl.scrollHeight;
      }
      break;
    }
    case "error": {
      showError(`${evt.error_code}: ${evt.message}`);
      setStatus("error", "status-error");
      break;
    }
    case "job_complete": {
      appendTimelineEntry("job", "complete", evt.total_latency_ms, "agent-tools");
      setStatus("done", "status-done");
      setSubmitEnabled(true);
      if (activeSource) { activeSource.close(); activeSource = null; }
      fetchTraceAndRender(jobId);
      break;
    }
    default:
      // ignore unknown event types
      break;
  }
}

async function submit(query) {
  setSubmitEnabled(false);
  setStatus("submitting…", "status-running");
  resetUiForNewJob();

  let resp;
  try {
    resp = await fetch("/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
  } catch (e) {
    showError(`Network error: ${e.message}`);
    setStatus("error", "status-error");
    setSubmitEnabled(true);
    return;
  }
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      const detail = data.detail || data;
      msg = detail.error_code
        ? `${detail.error_code}: ${detail.message ?? ""}`
        : JSON.stringify(detail);
    } catch (_) {}
    showError(msg);
    setStatus("error", "status-error");
    setSubmitEnabled(true);
    return;
  }

  const { job_id, stream_url } = await resp.json();
  setStatus(`running (${job_id.slice(0, 8)}…)`, "status-running");

  activeSource = new EventSource(stream_url);
  activeSource.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); }
    catch (err) { console.warn("bad SSE payload", err); return; }
    dispatch(evt, job_id);
  };
  activeSource.onerror = (e) => {
    // EventSource auto-retries; only treat as terminal if connection is closed and no job_complete arrived.
    if (activeSource && activeSource.readyState === EventSource.CLOSED) {
      console.warn("SSE closed", e);
      setSubmitEnabled(true);
    }
  };
}

queryEl.addEventListener("input", () => {
  setSubmitEnabled(queryEl.value.trim().length > 0);
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = queryEl.value.trim();
  if (!q) return;
  submit(q);
});

setStatus("idle", "status-idle");
