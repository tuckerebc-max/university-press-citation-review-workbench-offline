"use strict";

const token = document.querySelector('meta[name="workbench-token"]').content;
const form = document.getElementById("run-form");
const errorBox = document.getElementById("form-error");
const activeRun = document.getElementById("active-run");
let currentRunId = null;
let pollHandle = null;

async function api(path, options = {}) {
  const headers = {"Accept": "application/json", ...(options.headers || {})};
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
    headers["X-Workbench-Token"] = token;
  }
  const response = await fetch(path, {...options, headers});
  const payload = await response.json().catch(() => ({error: "The workbench returned an unreadable response."}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function clearError() {
  errorBox.textContent = "";
  errorBox.hidden = true;
}

function formPayload() {
  const data = new FormData(form);
  return {
    input_root: String(data.get("input_root") || "").trim(),
    output_parent: String(data.get("output_parent") || "").trim(),
    project_label: String(data.get("project_label") || "").trim(),
    case_owner: String(data.get("case_owner") || "").trim(),
    classification: String(data.get("classification") || ""),
    run_mode: String(data.get("run_mode") || "baseline"),
    provider_approved: data.get("provider_approved") === "on",
    model: String(data.get("model") || "glm-5.3-background"),
    concurrency: Number(data.get("concurrency") || 3),
    crossref_enabled: data.get("crossref_enabled") === "on"
  };
}

async function checkHealth() {
  const node = document.getElementById("health");
  try {
    const value = await api("/api/health");
    if (!value.provider_enabled) {
      document.getElementById("provider-panel").hidden = true;
    }
    const skillsReady = Object.values(value.skills).every(item => item.ready);
    const ready = value.provider_key_configured && skillsReady;
    node.textContent = ready ? "Offline ready · provider configured" : "Offline ready";
    node.className = "health ready";
  } catch (error) {
    node.textContent = "Workbench unavailable";
    node.className = "health problem";
  }
}

async function loadRecentRuns() {
  const container = document.getElementById("recent-runs");
  try {
    const data = await api("/api/runs");
    container.replaceChildren();
    if (!data.runs.length) {
      const p = document.createElement("p"); p.className = "quiet"; p.textContent = "No runs yet."; container.appendChild(p); return;
    }
    for (const run of data.runs.slice(0, 7)) {
      const row = document.createElement("div"); row.className = "recent-run";
      const title = document.createElement("strong"); title.textContent = run.project_label;
      const meta = document.createElement("span"); meta.textContent = `${run.status} · ${run.created_at}`;
      const actions = document.createElement("div"); actions.className = "recent-actions";
      const view = document.createElement("button"); view.type = "button"; view.className = "text-button"; view.textContent = "View status";
      view.addEventListener("click", () => { currentRunId = run.run_id; activeRun.hidden = false; beginPolling(); activeRun.scrollIntoView({behavior: "smooth", block: "start"}); });
      actions.appendChild(view);
      if (["interrupted", "failed", "partial", "cancelled"].includes(run.status)) {
        const resume = document.createElement("button"); resume.type = "button"; resume.className = "text-button"; resume.textContent = "Resume";
        resume.addEventListener("click", async () => {
          clearError();
          try { await api(`/api/runs/${encodeURIComponent(run.run_id)}/resume`, {method: "POST", body: "{}"}); currentRunId = run.run_id; activeRun.hidden = false; beginPolling(); }
          catch (error) { showError(error.message); }
        });
        actions.appendChild(resume);
      }
      row.append(title, meta, actions); container.appendChild(row);
    }
  } catch (_) { /* health banner already reports connectivity */ }
}

document.getElementById("scan-button").addEventListener("click", async () => {
  clearError();
  const button = document.getElementById("scan-button");
  button.disabled = true;
  try {
    const inputRoot = document.getElementById("input-root").value.trim();
    const result = await api("/api/scan", {method: "POST", body: JSON.stringify({input_root: inputRoot})});
    const megabytes = (result.total_bytes / 1024 / 1024).toFixed(1);
    document.getElementById("scan-result").textContent = `${result.count} chapter${result.count === 1 ? "" : "s"} · ${megabytes} MB · manifest ready`;
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
});

form.addEventListener("submit", async event => {
  event.preventDefault(); clearError();
  const button = document.getElementById("run-button"); button.disabled = true; button.textContent = "Creating immutable plan…";
  try {
    const result = await api("/api/runs", {method: "POST", body: JSON.stringify(formPayload())});
    currentRunId = result.run_id;
    activeRun.hidden = false;
    document.getElementById("run-title").textContent = "Review queued";
    beginPolling();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false; button.textContent = "Build author review packets";
  }
});

function beginPolling() {
  if (pollHandle) clearInterval(pollHandle);
  refreshRun();
  pollHandle = setInterval(refreshRun, 1800);
}

async function refreshRun() {
  if (!currentRunId) return;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(currentRunId)}`);
    const chapters = data.chapters;
    const completed = chapters.filter(item => item.status === "packet_built").length;
    const reviewing = chapters.filter(item => ["rci_running", "rci_done", "sei_running", "packet_building"].includes(item.status)).length;
    const exceptions = chapters.filter(item => ["failed", "blocked", "source_drifted"].includes(item.status)).length;
    document.getElementById("metric-complete").textContent = String(completed);
    document.getElementById("metric-reviewing").textContent = String(reviewing);
    document.getElementById("metric-exceptions").textContent = String(exceptions);
    document.getElementById("progress-bar").style.width = `${chapters.length ? (completed / chapters.length) * 100 : 0}%`;
    document.getElementById("run-title").textContent = `${data.run.status[0].toUpperCase()}${data.run.status.slice(1)} · ${completed} of ${chapters.length}`;
    document.getElementById("run-path").textContent = data.run.output_root;
    const list = document.getElementById("chapter-list"); list.replaceChildren();
    for (const chapter of chapters) {
      const row = document.createElement("div"); row.className = "chapter-row";
      const name = document.createElement("span"); name.textContent = chapter.relative_path;
      const status = document.createElement("span"); status.className = "status"; status.textContent = chapter.status.replaceAll("_", " ");
      row.append(name, status); list.appendChild(row);
    }
    const terminal = ["complete", "partial", "failed", "cancelled", "interrupted"].includes(data.run.status);
    document.getElementById("open-output").hidden = !terminal;
    document.getElementById("cancel-run").hidden = terminal;
    if (terminal) { clearInterval(pollHandle); pollHandle = null; loadRecentRuns(); }
  } catch (error) {
    showError(error.message);
  }
}

document.getElementById("cancel-run").addEventListener("click", async () => {
  if (!currentRunId) return;
  try { await api(`/api/runs/${encodeURIComponent(currentRunId)}/cancel`, {method: "POST", body: "{}"}); }
  catch (error) { showError(error.message); }
});

document.getElementById("open-output").addEventListener("click", async () => {
  if (!currentRunId) return;
  try { await api(`/api/runs/${encodeURIComponent(currentRunId)}/open`, {method: "POST", body: "{}"}); }
  catch (error) { showError(error.message); }
});

const privateForm = document.getElementById("private-form");
const sourceField = privateForm.elements.namedItem("source");
const inspectButton = document.getElementById("private-inspect");
const buildButton = document.getElementById("private-build");
const privateHash = document.getElementById("private-hash");
const privateResult = document.getElementById("private-result");
let selection = null;
sourceField.addEventListener("input", () => {
  selection = null;
  privateHash.textContent = "";
  privateResult.textContent = "";
  buildButton.disabled = true;
});
inspectButton.addEventListener("click", async () => {
  const source = sourceField.value.trim();
  selection = null;
  buildButton.disabled = true;
  inspectButton.disabled = true;
  privateHash.textContent = "";
  privateResult.textContent = "Reading the selected file…";
  try {
    const result = await api("/api/private/inspect", {method: "POST", body: JSON.stringify({source})});
    if (sourceField.value.trim() !== source) return;
    selection = {source, sha256: result.sha256};
    privateHash.textContent = `Selected SHA-256 · ${result.sha256}`;
    privateResult.textContent = "Confirm the edition and output folder before building.";
    buildButton.disabled = false;
  } catch (error) {
    privateResult.textContent = error.message;
  } finally {
    inspectButton.disabled = false;
  }
});
privateForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!selection || selection.source !== sourceField.value.trim()) return;
  const payload = {...Object.fromEntries(new FormData(privateForm)), ...selection};
  buildButton.disabled = true;
  inspectButton.disabled = true;
  sourceField.readOnly = true;
  privateResult.textContent = "Building the local packet…";
  try {
    const result = await api("/api/private/review", {method: "POST", body: JSON.stringify(payload)});
    privateResult.textContent = `Packet ready for human review. Open author-review.docx in ${result.output_folder}`;
  } catch (error) {
    privateResult.textContent = error.message;
  } finally {
    sourceField.readOnly = false;
    inspectButton.disabled = false;
    buildButton.disabled = !selection;
  }
});

checkHealth();
loadRecentRuns();
