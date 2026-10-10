const $ = (selector) => document.querySelector(selector);
const state = { projects: [], projectId: "" };

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let body = {};
  try { body = await response.json(); } catch (_) { /* status below is enough */ }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

function namespace(projectId) {
  return `litigation.project.${projectId.replace(/[^A-Za-z0-9_.:-]+/g, ".").replace(/^\.+|\.+$/g, "").slice(0, 200)}`;
}

function capability(id, value) {
  const node = $(id);
  node.textContent = String(value || "unknown").replaceAll("_", " ");
}

async function loadStatus() {
  const status = await api("/api/seam/status");
  const badge = $("#connection");
  badge.textContent = status.reachable && status.installed ? "Alpine connected" : "Alpine unavailable";
  badge.className = `badge ${status.reachable && status.installed ? "good" : "bad"}`;
  const caps = status.capabilities || {};
  capability("#cap-ingest", caps.ingest);
  capability("#cap-search", caps.knowledge_search);
  capability("#cap-graph", caps.knowledge_graph);
  capability("#cap-reasoning", caps.reasoning_graph);
  capability("#cap-embeddings", caps.semantic_embeddings);
}

async function loadProjects() {
  const data = await api("/api/projects");
  state.projects = data.items || [];
  const select = $("#project");
  select.innerHTML = state.projects.length
    ? state.projects.map((project) => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)}</option>`).join("")
    : '<option value="">No projects yet</option>';
  state.projectId = select.value;
  $("#namespace").textContent = state.projectId ? namespace(state.projectId) : "Create a project first";
}

async function loadOutbox() {
  const data = await api("/api/seam/outbox?limit=25");
  const counts = data.counts || {};
  $("#delivered").textContent = counts.delivered || 0;
  $("#pending").textContent = counts.pending || 0;
  $("#rejected").textContent = counts.rejected || 0;
  const rows = data.items || [];
  $("#outbox").innerHTML = rows.length ? rows.map((item) => `
    <div class="outbox-row">
      <span class="status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
      <span>${escapeHtml(item.event_key)}</span>
      <span>${new Date(Number(item.created_at) * 1000).toLocaleString()}</span>
      <span>${escapeHtml(item.attempts)} tries</span>
    </div>`).join("") : '<div class="empty">No project events have been queued yet.</div>';
}

function renderKnowledge(data, mode) {
  const seam = data.seam || {};
  const nodes = Array.isArray(seam.nodes) ? seam.nodes : [];
  const edges = Array.isArray(seam.edges) ? seam.edges : [];
  if (!nodes.length) {
    $("#results").innerHTML = '<div class="empty">No matching knowledge was found in this project namespace.</div>';
    return;
  }
  const cards = nodes.map((node) => `
    <article class="result">
      <div class="meta">${escapeHtml(node.kind)} · ${escapeHtml(node.trust_state || node.status || "unknown")}</div>
      <h3>${escapeHtml(node.label || node.id)}</h3>
      <p>${escapeHtml(node.properties?.content_preview || node.properties?.reference || node.id)}</p>
    </article>`).join("");
  const graphNote = mode === "graph"
    ? `<article class="result"><div class="meta">GRAPH TRACE</div><p>${edges.length} related edges · ${nodes.length} nodes</p></article>`
    : "";
  $("#results").innerHTML = graphNote + cards;
}

async function runQuery(mode) {
  if (!state.projectId) throw new Error("Choose a project first");
  const query = $("#query").value.trim();
  if (!query) throw new Error("Enter a question or search phrase");
  $("#message").textContent = `${mode === "graph" ? "Tracing" : "Searching"} ${namespace(state.projectId)}…`;
  const data = await api(`/api/projects/${encodeURIComponent(state.projectId)}/seam/${mode}?q=${encodeURIComponent(query)}&limit=12`);
  renderKnowledge(data, mode);
  $("#message").textContent = mode === "graph"
    ? "Showing SEAM's current knowledge graph. Reasoning-graph workflows are not configured yet."
    : "Search complete.";
}

async function busy(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { $("#message").textContent = error.message; }
  finally { button.disabled = false; }
}

$("#project").addEventListener("change", (event) => {
  state.projectId = event.target.value;
  $("#namespace").textContent = state.projectId ? namespace(state.projectId) : "Select a project";
});
$("#query-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, () => runQuery("search"));
});
$("#graph").addEventListener("click", (event) => busy(event.currentTarget, () => runQuery("graph")));
$("#retry").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  const result = await api("/api/seam/retry?limit=100", { method: "POST" });
  $("#message").textContent = `Retried ${result.attempted} records; ${result.succeeded} delivered.`;
  await loadOutbox();
}));
$("#sync").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  const result = await api("/api/seam/sync", { method: "POST" });
  $("#message").textContent = `Scanned ${result.events_scanned} project events and queued ${result.items_queued}.`;
  await loadOutbox();
}));

Promise.all([loadStatus(), loadProjects(), loadOutbox()]).catch((error) => {
  $("#message").textContent = error.message;
});
