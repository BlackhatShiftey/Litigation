"use strict";

const $ = (id) => document.getElementById(id);
const SUBSCRIPTION_IDS = ["claude", "grok", "gemini", "glm", "cline"];
const HANDOFF_AGENT_IDS = [...SUBSCRIPTION_IDS, "openai"];
const ACTIVE_STATES = new Set(["queued", "running", "cancel_requested"]);
const APPLIED_PROPOSAL_STATES = new Set(["proposal_applied", "proposal_reconciled"]);

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed (${status})`);
    this.status = status;
  }
}

const api = {
  async request(url, options = {}) {
    const response = await fetch(url, {
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      ...options,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(response.status, body.detail || response.statusText);
    }
    return response.json();
  },
  projects: () => api.request("/api/projects"),
  project: (id) => api.request(`/api/projects/${encodeURIComponent(id)}`),
  createProject: (body) => api.request("/api/projects", {method: "POST", body: JSON.stringify(body)}),
  projectExport: async (id) => {
    const response = await fetch(`/api/projects/${encodeURIComponent(id)}/export`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(response.status, body.detail || response.statusText);
    }
    return response.blob();
  },
  models: () => api.request("/api/models"),
  files: (id, ref = "") => api.request(`/api/projects/${encodeURIComponent(id)}/files${ref ? `?ref=${encodeURIComponent(ref)}` : ""}`),
  fileContent: (id, path, ref = "") => api.request(`/api/projects/${encodeURIComponent(id)}/files/content?path=${encodeURIComponent(path)}${ref ? `&ref=${encodeURIComponent(ref)}` : ""}`),
  issues: (id) => api.request(`/api/projects/${encodeURIComponent(id)}/issues`),
  createIssue: (id, body) => api.request(`/api/projects/${encodeURIComponent(id)}/issues`, {method: "POST", body: JSON.stringify(body)}),
  issueEvent: (id, body) => api.request(`/api/issues/${encodeURIComponent(id)}/events`, {method: "POST", body: JSON.stringify(body)}),
  handoffs: (id) => api.request(`/api/projects/${encodeURIComponent(id)}/handoffs`),
  handoff: (id) => api.request(`/api/handoffs/${encodeURIComponent(id)}`),
  createHandoff: (id, body) => api.request(`/api/projects/${encodeURIComponent(id)}/handoffs`, {method: "POST", body: JSON.stringify(body)}),
  cancelHandoff: (id) => api.request(`/api/handoffs/${encodeURIComponent(id)}/cancel`, {method: "POST"}),
  retryHandoff: (id, body) => api.request(`/api/handoffs/${encodeURIComponent(id)}/retry`, {method: "POST", body: JSON.stringify(body)}),
  outputs: (id) => api.request(`/api/handoffs/${encodeURIComponent(id)}/outputs`),
  events: (id, after = 0, limit = 100) => api.request(`/api/projects/${encodeURIComponent(id)}/events?${new URLSearchParams({after: String(after || 0), limit: String(limit)})}`),
  proposal: (id) => api.request(`/api/proposals/${encodeURIComponent(id)}`),
  applyProposal: (id, expectedMain) => api.request(`/api/proposals/${encodeURIComponent(id)}/apply`, {method: "POST", body: JSON.stringify({expected_main: expectedMain})}),
  createDebate: (id, body) => api.request(`/api/projects/${encodeURIComponent(id)}/debates`, {method: "POST", body: JSON.stringify(body)}),
  workItems: (id, status = "") => api.request(`/api/projects/${encodeURIComponent(id)}/work-items?${new URLSearchParams({limit: "100", ...(status ? {status} : {})})}`),
  createWorkItem: (id, body) => api.request(`/api/projects/${encodeURIComponent(id)}/work-items`, {method: "POST", body: JSON.stringify(body)}),
  pullWorkItem: (id, body) => api.request(`/api/work-items/${encodeURIComponent(id)}/pull`, {method: "POST", body: JSON.stringify(body)}),
  provenance: (id) => api.request(`/api/projects/${encodeURIComponent(id)}/provenance`),
};

const state = {
  projects: [],
  project: null,
  models: [],
  issues: [],
  handoffs: [],
  events: [],
  files: [],
  filesRef: "",
  selectedHandoffId: "",
  selectedHandoff: null,
  outputs: [],
  selectedOutputId: "",
  selectedIssueId: "",
  selectedFilePath: "",
  selectedFilePaths: new Set(),
  selectedIssueIds: new Set(),
  selectedOutputIds: new Set(),
  proposal: null,
  eventsCursor: 0,
  eventsHasMore: false,
  eventsFetching: false,
  historyVisibleCount: 100,
  debates: [],
  workItems: [],
  activeHandoffs: [],
  provenance: null,
  workRoomLoading: false,
  polling: false,
};

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}

function actionButton(label, handler, options = {}) {
  const button = element("button", options.className || "quiet", label);
  button.type = "button";
  if (options.id) button.id = options.id;
  if (options.disabled) button.disabled = true;
  button.addEventListener("click", handler);
  return button;
}

function formatTime(value) {
  if (!value) return "Unknown time";
  const numericSeconds = typeof value === "number" || (typeof value === "string" && /^\d+(\.\d+)?$/.test(value));
  const normalized = numericSeconds ? Number(value) * 1000 : value;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
}

function shortSha(value) {
  return value ? String(value).slice(0, 10) : "none";
}

function showError(error, target = $("global-error")) {
  target.textContent = error instanceof Error ? error.message : String(error);
  target.hidden = false;
  if (target === $("global-error")) window.setTimeout(() => { target.hidden = true; }, 7000);
}

function clearError(target) {
  target.textContent = "";
  target.hidden = true;
}

function showNotice(message) {
  const notice = $("global-notice");
  notice.textContent = message;
  notice.hidden = false;
  window.setTimeout(() => { notice.hidden = true; }, 10000);
}

function setBusy(button, busy, busyLabel = "Working…") {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? busyLabel : button.dataset.label;
}

function makeStatus(status) {
  const chip = element("span", `status-chip ${status || ""}`, String(status || "unknown").replaceAll("_", " "));
  return chip;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function renderProjects() {
  const list = $("project-list");
  list.replaceChildren();
  if (!state.projects.length) {
    list.appendChild(element("p", "detail-empty", "No projects yet."));
    return;
  }
  for (const project of state.projects) {
    const button = element("button", state.project?.id === project.id ? "active" : "");
    button.type = "button";
    button.dataset.projectId = project.id;
    button.append(element("strong", "", project.name), element("span", "", project.description || project.source_description || "Local project"));
    button.addEventListener("click", () => openProject(project.id));
    list.appendChild(button);
  }
}

function renderProjectHeader() {
  const project = state.project;
  $("empty-project").hidden = Boolean(project);
  $("project-workspace").hidden = !project;
  if (!project) return;
  $("project-name").textContent = project.name;
  $("project-description").textContent = project.description || "No description yet.";
  $("project-repo").textContent = `${project.source_description || "Litigation-managed local repository"} · Current HEAD ${shortSha(state.filesRef || project.initial_commit)}`;
}

async function loadProjects(preferredId = "") {
  const response = await api.projects();
  state.projects = response.items || [];
  renderProjects();
  const saved = preferredId || sessionStorage.getItem("litigation-project-id") || "";
  if (!state.project && state.projects.length) {
    const candidate = state.projects.find((project) => project.id === saved) || state.projects[0];
    await openProject(candidate.id);
  }
}

async function openProject(id) {
  try {
    clearError($("global-error"));
    state.selectedHandoffId = "";
    state.selectedHandoff = null;
    state.outputs = [];
    state.selectedOutputId = "";
    state.selectedIssueId = "";
    state.proposal = null;
    state.events = [];
    state.eventsCursor = 0;
    state.eventsHasMore = false;
    state.historyVisibleCount = 100;
    state.workItems = [];
    state.activeHandoffs = [];
    state.provenance = null;
    const [project, issues, handoffs, files, events] = await Promise.all([
      api.project(id), api.issues(id), api.handoffs(id), api.files(id), api.events(id),
    ]);
    state.project = project;
    state.issues = issues.items || [];
    state.handoffs = handoffs.items || [];
    state.files = files.items || [];
    state.filesRef = files.ref || project.initial_commit || "";
    state.events = [];
    mergeEvents(events.items || []);
    state.eventsCursor = events.next_after || 0;
    state.eventsHasMore = Boolean(events.has_more);
    await catchUpEvents();
    sessionStorage.setItem("litigation-project-id", id);
    renderProjectHeader();
    renderProjects();
    renderHandoffs();
    renderIssues();
    renderHistory();
    renderDebates();
    renderFiles();
    renderProposal();
    renderAgentRoom();
    void refreshAgentRoom({quiet: true});
    $("project-rail").classList.remove("open");
  } catch (error) {
    showError(error);
  }
}

function renderHandoffs() {
  const list = $("handoff-list");
  list.replaceChildren();
  if (!state.handoffs.length) {
    list.appendChild(element("div", "detail-empty", "No handoffs yet. Queue a bounded task to begin the work ledger."));
    return;
  }
  const handoffs = [...state.handoffs].sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at));
  for (const handoff of handoffs) {
    const row = element("button", `activity-row${state.selectedHandoffId === handoff.id ? " active" : ""}`);
    row.type = "button";
    row.dataset.handoffId = handoff.id;
    row.appendChild(element("span", "activity-spine"));
    const copy = element("span", "activity-copy");
    copy.append(
      element("strong", "", `${handoff.agent_id} · ${handoff.mode}`),
      element("span", "", `${String(handoff.status || "unknown").replaceAll("_", " ")} · updated ${formatTime(handoff.updated_at || handoff.created_at)}`),
    );
    row.append(copy, element("time", "activity-time", formatTime(handoff.updated_at || handoff.created_at)));
    row.addEventListener("click", () => selectHandoff(handoff.id));
    list.appendChild(row);
  }
}

function populateRoomAgentSelect(select) {
  const byId = new Map(state.models.map((model) => [model.id, model]));
  for (const id of HANDOFF_AGENT_IDS) {
    const model = byId.get(id);
    const option = document.createElement("option");
    option.value = id;
    option.textContent = model?.label || id;
    option.disabled = !model?.available;
    select.appendChild(option);
  }
}

function renderAgentRoom() {
  const queue = $("work-item-list");
  const active = $("active-agent-list");
  queue.replaceChildren();
  active.replaceChildren();
  $("work-item-count").textContent = `${state.workItems.length} item${state.workItems.length === 1 ? "" : "s"}`;
  $("active-agent-count").textContent = `${state.activeHandoffs.length} active`;

  if (!state.workItems.length) {
    queue.appendChild(element("div", "detail-empty room-empty", "No shared work is queued. Add a bounded item for an agent to pull."));
  }
  for (const item of state.workItems) {
    const card = element("article", "work-item-card");
    const head = element("div", "work-item-head");
    const heading = element("div");
    heading.append(element("span", "eyebrow", "Shared work"), element("h3", "", item.title));
    head.append(heading, makeStatus(item.status));
    card.appendChild(head);
    card.appendChild(element("p", "work-item-instructions", item.instructions));

    const meta = element("dl", "work-item-meta");
    appendMeta(meta, "Owner", item.worker_id || "Unclaimed");
    appendMeta(meta, "Updated", formatTime(item.updated_at || item.created_at));
    appendMeta(meta, "Paths", (item.owned_paths || []).join(", ") || "Whole project");
    if (item.handoff_id) appendMeta(meta, "Handoff", item.handoff_id);
    card.appendChild(meta);

    if (item.status === "queued") {
      const controls = element("div", "pull-work-controls");
      const agent = document.createElement("select");
      agent.setAttribute("aria-label", `Agent for ${item.title}`);
      populateRoomAgentSelect(agent);
      const model = document.createElement("input");
      model.placeholder = "Model override (optional)";
      model.setAttribute("aria-label", `Model override for ${item.title}`);
      const pull = actionButton("Pull work", async () => {
        setBusy(pull, true, "Claiming…");
        const body = {request_id: crypto.randomUUID(), agent_id: agent.value};
        if (model.value.trim()) body.model = model.value.trim();
        try {
          const result = await api.pullWorkItem(item.id, body);
          showNotice(`${agent.options[agent.selectedIndex]?.text || agent.value} claimed ${item.title}.`);
          await Promise.all([refreshAgentRoom({quiet: true}), refreshWork()]);
          if (result.handoff_id) state.selectedHandoffId = result.handoff_id;
        } catch (error) {
          showError(error);
        } finally {
          setBusy(pull, false);
        }
      }, {className: "primary"});
      controls.append(agent, model, pull);
      card.appendChild(controls);
    } else if (item.handoff_id) {
      const controls = element("div", "work-item-actions");
      controls.appendChild(actionButton("Open handoff", async () => {
        setView("work");
        await selectHandoff(item.handoff_id);
      }));
      card.appendChild(controls);
    }
    queue.appendChild(card);
  }

  if (!state.activeHandoffs.length) {
    active.appendChild(element("div", "detail-empty room-empty", "No agents are currently running project work."));
  }
  for (const handoff of state.activeHandoffs) {
    const card = element("button", "active-agent-card");
    card.type = "button";
    card.append(
      makeStatus(handoff.status),
      element("strong", "", handoff.agent_id || "Agent"),
      element("span", "", handoff.instructions || `${handoff.mode || "agent"} handoff`),
      element("time", "", formatTime(handoff.updated_at || handoff.created_at)),
    );
    if (handoff.id) card.addEventListener("click", async () => {
      setView("work");
      await selectHandoff(handoff.id);
    });
    active.appendChild(card);
  }
}

async function refreshAgentRoom({quiet = false} = {}) {
  if (!state.project || state.workRoomLoading) return;
  state.workRoomLoading = true;
  try {
    const response = await api.workItems(state.project.id);
    state.workItems = response.items || [];
    state.activeHandoffs = response.active_handoffs || [];
    renderAgentRoom();
  } catch (error) {
    if (!quiet) showError(error);
    $("work-item-list").replaceChildren(element("div", "detail-empty room-empty", "The shared queue is unavailable until the current server update is loaded."));
  } finally {
    state.workRoomLoading = false;
  }
}

async function verifyProvenance() {
  if (!state.project) return;
  const button = $("verify-provenance");
  setBusy(button, true, "Checking…");
  try {
    state.provenance = await api.provenance(state.project.id);
    const banner = $("provenance-banner");
    const issueCount = state.provenance.issues?.length || 0;
    banner.className = `provenance-banner ${state.provenance.ok ? "passed" : "failed"}`;
    banner.textContent = state.provenance.ok
      ? `Local record verified · SQLite ${state.provenance.integrity || "ok"} · append-only relationships intact.`
      : `Local record check found ${issueCount} issue${issueCount === 1 ? "" : "s"}. Review the durable history before assigning more work.`;
  } catch (error) {
    showError(error);
  } finally {
    setBusy(button, false);
  }
}

async function selectHandoff(id, outputId = "") {
  try {
    state.selectedHandoffId = id;
    renderHandoffs();
    const [handoff, outputs] = await Promise.all([api.handoff(id), api.outputs(id)]);
    state.selectedHandoff = handoff;
    state.outputs = outputs.items || [];
    state.selectedOutputId = outputId && state.outputs.some((item) => item.id === outputId)
      ? outputId : state.outputs[0]?.id || "";
    renderSelectedHandoff();
  } catch (error) {
    showError(error);
  }
}

function appendMeta(list, label, value) {
  list.append(element("dt", "", label), element("dd", "", value || "—"));
}

function renderSelectedHandoff() {
  const handoff = state.selectedHandoff;
  $("output-empty").hidden = Boolean(handoff);
  $("output-detail").hidden = !handoff;
  if (!handoff) return;

  const status = $("detail-status");
  status.className = `status-chip ${handoff.status || ""}`;
  status.textContent = String(handoff.status || "unknown").replaceAll("_", " ");
  $("detail-title").textContent = `${handoff.agent_id} ${handoff.mode}`;
  $("detail-instructions").textContent = handoff.instructions || "No instructions recorded.";
  const acceptance = $("detail-acceptance");
  acceptance.replaceChildren();
  for (const criterion of handoff.acceptance || []) acceptance.appendChild(element("li", "", criterion));
  if (!(handoff.acceptance || []).length) acceptance.appendChild(element("li", "", "No acceptance criteria recorded."));

  const meta = $("handoff-meta");
  meta.replaceChildren();
  appendMeta(meta, "Model", handoff.model || "provider default");
  appendMeta(meta, "Base commit", shortSha(handoff.base_commit));
  appendMeta(meta, "Created", formatTime(handoff.created_at));
  appendMeta(meta, "Parent", handoff.parent_handoff_id || "none");
  if (handoff.mode === "agent") {
    appendMeta(meta, "Working base", shortSha(handoff.context?.working_base));
    appendMeta(meta, "Integration base", shortSha(handoff.context?.integration_base));
    appendMeta(meta, "Lineage depth", String(handoff.context?.lineage_depth ?? 0));
  }

  const actions = $("handoff-actions");
  actions.replaceChildren();
  if (ACTIVE_STATES.has(handoff.status)) actions.appendChild(actionButton("Cancel", cancelSelectedHandoff, {className: "quiet danger", id: "cancel-handoff"}));
  if (["failed", "interrupted", "cancelled", "needs_revision"].includes(handoff.status)) actions.appendChild(actionButton("Retry", retrySelectedHandoff, {id: "retry-handoff"}));
  actions.appendChild(actionButton("Follow up", () => openHandoffDialog({parent: handoff.id}), {id: "followup-handoff"}));

  const tabs = $("output-tabs");
  tabs.replaceChildren();
  for (const [index, output] of state.outputs.entries()) {
    const button = element("button", "", `Output ${index + 1}`);
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(output.id === state.selectedOutputId));
    button.addEventListener("click", () => {
      state.selectedOutputId = output.id;
      renderSelectedHandoff();
    });
    tabs.appendChild(button);
  }
  renderSelectedOutput();
}

function proposalIdFromOutput(output) {
  if (output?.proposal_id) return output.proposal_id;
  const parsed = output?.parsed;
  if (!parsed || typeof parsed !== "object") return "";
  return parsed.proposal_id || parsed.proposal?.id || "";
}

function renderSelectedOutput() {
  const output = state.outputs.find((item) => item.id === state.selectedOutputId);
  $("output-paper").textContent = output?.raw_text || "No output has been recorded yet. Active tasks will appear here when they finish.";
  renderAgentExecution(output);
  const actions = $("output-actions");
  actions.replaceChildren();
  if (!output) return;
  const contextSelected = state.selectedOutputIds.has(output.id);
  actions.append(
    actionButton(contextSelected ? "Remove from context" : "Add to context", () => {
      if (contextSelected) state.selectedOutputIds.delete(output.id);
      else state.selectedOutputIds.add(output.id);
      renderSelectedOutput();
    }, {id: "toggle-output-context"}),
    actionButton("Follow up from output", () => openHandoffDialog({parent: state.selectedHandoff.id, outputId: output.id}), {id: "followup-output"}),
    actionButton("Export output", () => {
      downloadBlob(new Blob([output.raw_text || ""], {type: "text/plain;charset=utf-8"}), `litigation-output-${output.id}.txt`);
    }, {id: "export-output"}),
  );
  const proposalId = proposalIdFromOutput(output);
  if (proposalId) actions.appendChild(actionButton("Open code proposal", () => openProposal(proposalId), {className: "primary", id: "open-proposal"}));
}

function safeJson(value, limit = 6000) {
  let text;
  try { text = JSON.stringify(value, null, 2); }
  catch (_error) { text = String(value); }
  return text.length > limit ? `${text.slice(0, limit)}\n… UI preview truncated` : text;
}

function appendExecutionMeta(container, pairs) {
  const list = element("dl", "execution-meta");
  for (const [label, value] of pairs) appendMeta(list, label, value == null || value === "" ? "—" : String(value));
  container.appendChild(list);
}

function receiptKey(receipt) {
  return [receipt.round, receipt.action, receipt.model_turn_sha256, safeJson(receipt.request, 1000)].join("|");
}

function agentReceipts(handoff, parsed) {
  const eventReceipts = state.events
    .filter((record) => record.entity_type === "handoff" && record.entity_id === handoff.id && record.kind === "agent_tool_result")
    .map((record) => record.payload || {});
  const seen = new Set();
  return [...eventReceipts, ...(Array.isArray(parsed?.receipts) ? parsed.receipts : [])].filter((receipt) => {
    const key = receiptKey(receipt);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function agentChildLinks(handoff) {
  const ids = new Set();
  for (const record of state.events) {
    if (record.entity_type !== "handoff" || record.entity_id !== handoff.id || record.kind !== "peer_handoff_queued") continue;
    if (record.payload?.child_handoff_id) ids.add(record.payload.child_handoff_id);
  }
  for (const candidate of state.handoffs) if (candidate.parent_handoff_id === handoff.id) ids.add(candidate.id);
  return [...ids].map((id) => state.handoffs.find((item) => item.id === id) || {id, agent_id: "Peer task", status: "queued"});
}

function linkedHandoffButton(handoff, relation) {
  const button = element("button", "linked-task");
  button.type = "button";
  button.dataset.handoffId = handoff.id;
  button.append(
    element("span", "eyebrow", relation),
    element("strong", "", `${handoff.agent_id || "Agent"} · ${handoff.mode || "agent"}`),
    element("span", "", String(handoff.status || "queued").replaceAll("_", " ")),
  );
  button.addEventListener("click", () => {
    setView("work");
    selectHandoff(handoff.id);
  });
  return button;
}

function renderTestCheck(check, index) {
  const card = element("article", "check-card");
  const head = element("div", "check-head");
  const status = makeStatus(check.status || "unknown");
  head.append(element("h5", "", check.profile || `Check ${index + 1}`), status);
  card.appendChild(head);
  appendExecutionMeta(card, [
    ["Exit code", check.exit_code],
    ["Duration", check.duration_ms == null ? null : `${check.duration_ms} ms`],
    ["Tree", check.tree_sha256],
    ["Files", check.file_count],
    ["Network", check.network],
    ["Workspace", check.workspace],
    ["Command", check.command],
    ["Limits", check.limits && safeJson(check.limits, 1000)],
  ]);
  const output = element("pre", "check-output", check.output || "No test output recorded.");
  card.appendChild(output);
  if (check.truncated) card.appendChild(element("p", "truncation-note", "Runner output was truncated; this receipt preserves that fact."));
  return card;
}

function renderReceipt(receipt, index) {
  const card = element("article", "execution-receipt");
  const head = element("div", "receipt-head");
  const action = String(receipt.action || "tool action").replaceAll("_", " ");
  head.append(
    element("div", "", `Round ${receipt.round ?? "—"}`),
    element("strong", "", action),
    element("code", "", receipt.model_turn_sha256 ? shortSha(receipt.model_turn_sha256) : `step ${index + 1}`),
  );
  card.appendChild(head);
  const detail = document.createElement("details");
  const summary = element("summary", "", "Request and result receipt");
  detail.append(summary, element("h6", "", "Request"), element("pre", "receipt-json", safeJson(receipt.request ?? {})), element("h6", "", "Result"));
  if (receipt.action === "run_tests" && receipt.result && typeof receipt.result === "object") {
    detail.appendChild(renderTestCheck(receipt.result, index));
  } else {
    detail.appendChild(element("pre", "receipt-json", safeJson(receipt.result ?? {})));
  }
  card.appendChild(detail);
  return card;
}

function renderAgentExecution(output) {
  const section = $("agent-execution");
  const body = $("agent-execution-body");
  const handoff = state.selectedHandoff;
  const parsed = output?.parsed;
  const isAgentOutput = parsed?.version === 2 && parsed?.kind === "agent_execution";
  const visible = handoff?.mode === "agent" || isAgentOutput;
  section.hidden = !visible;
  body.replaceChildren();
  if (!visible) return;

  const status = $("agent-execution-status");
  status.className = `status-chip ${handoff?.status || "queued"}`;
  status.textContent = String(handoff?.status || "queued").replaceAll("_", " ");

  const context = handoff?.context || {};
  const overview = element("section", "execution-overview");
  overview.appendChild(element("p", "execution-summary", parsed?.summary || (ACTIVE_STATES.has(handoff?.status) ? "Execution is in progress. Receipts appear as tools finish." : "No terminal execution summary has been recorded.")));
  appendExecutionMeta(overview, [
    ["Working base", parsed?.working_base || context.working_base],
    ["Integration base", parsed?.integration_base || context.integration_base || handoff?.base_commit],
    ["Lineage depth", context.lineage_depth ?? 0],
  ]);
  body.appendChild(overview);

  const snapshot = context.rules_snapshot;
  if (snapshot && typeof snapshot === "object") {
    const rules = element("section", "execution-section");
    rules.appendChild(element("h4", "", "Rules snapshot"));
    appendExecutionMeta(rules, [
      ["Scope", snapshot.model_scope],
      ["Snapshot hash", snapshot.snapshot_hash],
      ["Global revision", snapshot.global?.revision],
      ["Global content hash", snapshot.global?.content_hash],
      ["Model revision", snapshot.model?.revision],
      ["Model content hash", snapshot.model?.content_hash],
    ]);
    body.appendChild(rules);
  }

  const parent = handoff?.parent_handoff_id && (state.handoffs.find((item) => item.id === handoff.parent_handoff_id) || {id: handoff.parent_handoff_id, agent_id: "Parent task", status: "recorded"});
  const children = handoff ? agentChildLinks(handoff) : [];
  if (parent || children.length) {
    const lineage = element("section", "execution-section");
    lineage.appendChild(element("h4", "", "Task lineage"));
    const links = element("div", "linked-tasks");
    if (parent) links.appendChild(linkedHandoffButton(parent, "Parent"));
    for (const child of children) links.appendChild(linkedHandoffButton(child, "Peer child"));
    lineage.appendChild(links);
    body.appendChild(lineage);
  }

  const receipts = handoff ? agentReceipts(handoff, parsed) : [];
  const trail = element("section", "execution-section");
  trail.appendChild(element("h4", "", `Execution trail · ${receipts.length} receipt${receipts.length === 1 ? "" : "s"}`));
  const receiptList = element("div", "receipt-list");
  if (receipts.length) for (const [index, receipt] of receipts.entries()) receiptList.appendChild(renderReceipt(receipt, index));
  else receiptList.appendChild(element("p", "execution-empty", "No completed tool receipts yet."));
  trail.appendChild(receiptList);
  body.appendChild(trail);

  const checks = Array.isArray(parsed?.checks) ? parsed.checks : [];
  if (checks.length) {
    const checkSection = element("section", "execution-section");
    checkSection.appendChild(element("h4", "", `Recorded checks · ${checks.length}`));
    const cards = element("div", "check-list");
    checks.forEach((check, index) => cards.appendChild(renderTestCheck(check, index)));
    checkSection.appendChild(cards);
    body.appendChild(checkSection);
  }

  const delegations = Array.isArray(parsed?.delegations) ? parsed.delegations : [];
  if (delegations.length) {
    const delegationSection = element("section", "execution-section");
    delegationSection.appendChild(element("h4", "", "Delegation requests"));
    for (const item of delegations) {
      const card = element("article", "execution-note");
      card.append(element("strong", "", item.agent_id || "Agent"), element("p", "", item.instructions || "No instructions recorded."));
      if (item.acceptance?.length) card.appendChild(element("p", "", `Acceptance: ${item.acceptance.join(" · ")}`));
      delegationSection.appendChild(card);
    }
    body.appendChild(delegationSection);
  }

  const ruleProposals = Array.isArray(parsed?.rule_proposals) ? parsed.rule_proposals : [];
  if (ruleProposals.length) {
    const proposals = element("section", "execution-section");
    proposals.appendChild(element("h4", "", "Rule proposals"));
    for (const proposal of ruleProposals) {
      const detail = document.createElement("details");
      detail.className = "execution-note";
      detail.append(element("summary", "", proposal.summary || "Proposed rule revision"), element("pre", "receipt-json", proposal.content || "No content recorded."));
      proposals.appendChild(detail);
    }
    body.appendChild(proposals);
  }
}

async function cancelSelectedHandoff() {
  if (!state.selectedHandoff) return;
  try {
    await api.cancelHandoff(state.selectedHandoff.id);
    await refreshWork();
  } catch (error) { showError(error); }
}

async function retrySelectedHandoff() {
  if (!state.selectedHandoff) return;
  try {
    const task = await api.retryHandoff(state.selectedHandoff.id, {request_id: crypto.randomUUID()});
    state.selectedHandoffId = task.id;
    await refreshWork();
  } catch (error) { showError(error); }
}

function renderIssues() {
  const list = $("issue-list");
  list.replaceChildren();
  if (!state.issues.length) {
    list.appendChild(element("div", "detail-empty", "No issues recorded. Create one to preserve a decision or work item."));
    renderIssueDetail();
    return;
  }
  if (!state.selectedIssueId || !state.issues.some((issue) => issue.id === state.selectedIssueId)) {
    state.selectedIssueId = state.issues[0].id;
  }
  for (const issue of state.issues) {
    const button = element("button", `record-button${state.selectedIssueId === issue.id ? " active" : ""}`);
    button.type = "button";
    button.append(element("strong", "", issue.title), element("span", "", `${issue.status} · ${formatTime(issue.updated_at || issue.created_at)}`));
    button.addEventListener("click", () => {
      state.selectedIssueId = issue.id;
      renderIssues();
      renderIssueDetail();
    });
    list.appendChild(button);
  }
  renderIssueDetail();
}

function renderIssueDetail() {
  const detail = $("issue-detail");
  detail.replaceChildren();
  const issue = state.issues.find((item) => item.id === state.selectedIssueId);
  if (!issue) {
    detail.appendChild(element("div", "detail-empty", "Select an issue to review its record."));
    return;
  }
  const top = element("div", "detail-head");
  const heading = element("div");
  heading.append(makeStatus(issue.status), element("h2", "", issue.title));
  const contextButton = actionButton(
    state.selectedIssueIds.has(issue.id) ? "Remove from context" : "Add to context",
    () => {
      if (state.selectedIssueIds.has(issue.id)) state.selectedIssueIds.delete(issue.id);
      else state.selectedIssueIds.add(issue.id);
      renderIssueDetail();
    },
    {id: "toggle-issue-context"},
  );
  top.append(heading, contextButton);
  detail.append(top, element("p", "", issue.body || "No details recorded."));

  const compose = element("form", "issue-compose");
  compose.id = "issue-event-form";
  const label = element("label", "", "Append comment");
  const input = document.createElement("textarea");
  input.id = "issue-comment";
  input.rows = 3;
  input.required = true;
  label.appendChild(input);
  const controls = element("div", "detail-actions");
  controls.appendChild(actionButton("Add comment", () => {}, {className: "primary", id: "issue-comment-submit"}));
  const stateKind = issue.status === "closed" ? "reopened" : "closed";
  controls.appendChild(actionButton(issue.status === "closed" ? "Reopen issue" : "Close issue", () => appendIssueEvent(issue.id, stateKind), {id: "issue-state-button"}));
  compose.append(label, controls);
  compose.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    await appendIssueEvent(issue.id, "comment", text);
  });
  controls.querySelector("#issue-comment-submit").addEventListener("click", () => compose.requestSubmit());
  detail.appendChild(compose);

  const eventList = element("div", "issue-events");
  const events = state.events
    .filter((event) => event.entity_type === "issue" && event.entity_id === issue.id)
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
  for (const record of events) {
    const entry = element("div", "issue-event");
    entry.append(element("strong", "", record.kind), element("p", "", eventText(record)), element("time", "", formatTime(record.created_at)));
    eventList.appendChild(entry);
  }
  detail.appendChild(eventList);
}

async function appendIssueEvent(issueId, kind, text = "") {
  try {
    await api.issueEvent(issueId, {kind, ...(text ? {text} : {})});
    await refreshProjectRecords();
  } catch (error) { showError(error); }
}

function eventText(record) {
  const payload = record?.payload;
  if (payload == null) return "";
  if (typeof payload === "string") return payload;
  if (["project_imported", "source_import_report"].includes(record.kind)) {
    return importReceiptText(payload);
  }
  for (const key of ["text", "title", "question", "summary", "status", "message"]) {
    if (payload[key]) return String(payload[key]);
  }
  const safe = JSON.stringify(payload);
  return safe.length > 400 ? `${safe.slice(0, 400)}…` : safe;
}

function importReceiptText(report) {
  if (!report || typeof report !== "object") return "Project import receipt recorded.";
  const included = Number(report.included_count ?? report.included?.length ?? 0);
  const excluded = Number(report.excluded_count ?? report.excluded?.length ?? 0);
  const details = report.excluded_reasons || report.excluded || [];
  const reasons = Array.isArray(details)
    ? details.map((item) => typeof item === "string" ? item : `${item.path || item.name || "item"}: ${item.reason || item.status || "excluded"}`)
    : Object.entries(details).map(([reason, count]) => `${reason}: ${count}`);
  const source = report.source_imported ? "Imported committed HEAD" : "Created managed repository";
  return `${source}: ${included} included, ${excluded} excluded.${reasons.length ? ` Exclusions: ${reasons.join("; ")}` : ""}`;
}

function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  const allEvents = [...state.events].sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const events = allEvents.slice(0, state.historyVisibleCount);
  if (!events.length) {
    list.appendChild(element("li", "detail-empty", "History begins when the first issue, handoff, debate, or proposal event is recorded."));
  }
  for (const record of events) {
    const item = element("li", "history-entry");
    item.append(
      element("strong", "", `${record.entity_type || "project"}: ${record.kind}`),
      element("p", "", eventText(record)),
      element("time", "", formatTime(record.created_at)),
    );
    list.appendChild(item);
  }
  $("history-more").hidden = state.historyVisibleCount >= allEvents.length && !state.eventsHasMore;
}

async function loadMoreHistory() {
  if (!state.project) return;
  try {
    state.historyVisibleCount += 100;
    if (state.eventsHasMore && state.historyVisibleCount >= state.events.length) await catchUpEvents();
    renderHistory();
    renderDebates();
  } catch (error) { showError(error); }
}

function renderDebates() {
  const list = $("debate-list");
  list.replaceChildren();
  const groups = new Map();
  for (const record of state.events.filter((event) => event.entity_type === "debate")) {
    const group = groups.get(record.entity_id) || {id: record.entity_id, events: []};
    group.events.push(record);
    groups.set(record.entity_id, group);
  }
  for (const local of state.debates) if (!groups.has(local.id)) groups.set(local.id, local);
  if (!groups.size) {
    list.appendChild(element("div", "detail-empty", "No debates yet. Start one with two to five agents and a specific question."));
    return;
  }
  const handoffById = new Map(state.handoffs.map((handoff) => [handoff.id, handoff]));
  for (const debate of groups.values()) {
    const entry = element("article", "debate-entry");
    const model = debateRoundModel(debate, handoffById);
    const head = element("div", "debate-head");
    const title = element("div");
    title.append(
      element("h3", "", model.question || `Debate ${debate.id}`),
      element("p", "", `${model.agents.join(", ") || "Agents pending"} · ${model.eventsCount} persisted event${model.eventsCount === 1 ? "" : "s"}`),
    );
    head.append(title, makeStatus(model.status));
    entry.appendChild(head);
    for (const round of model.rounds) {
      const section = element("section", "debate-round");
      const roundHead = element("div", "debate-round-head");
      roundHead.append(element("h4", "", round.label), makeStatus(round.status));
      const tasks = element("div", "debate-tasks");
      for (const task of round.tasks) {
        const button = element("button", "debate-task");
        button.type = "button";
        button.dataset.handoffId = task.id;
        button.append(
          element("strong", "", task.agent_id || "Agent task"),
          element("span", "", String(task.status || "queued").replaceAll("_", " ")),
        );
        button.addEventListener("click", async () => {
          setView("work");
          await selectHandoff(task.id);
          if (window.matchMedia("(max-width: 860px)").matches) {
            $("evidence-pane").scrollIntoView({block: "start"});
          }
        });
        tasks.appendChild(button);
      }
      section.append(roundHead, tasks);
      entry.appendChild(section);
    }
    list.appendChild(entry);
  }
}

function derivedTaskStatus(tasks) {
  const statuses = tasks.map((task) => task.status || "queued");
  if (!statuses.length || statuses.every((status) => status === "queued")) return "queued";
  if (statuses.every((status) => status === "completed")) return "completed";
  if (statuses.some((status) => ["running", "cancel_requested"].includes(status))) return "running";
  return "partial";
}

function debateRoundModel(debate, handoffById) {
  const events = debate.events || [];
  const created = events.find((record) => record.kind === "debate_created");
  const rounds = new Map();
  for (const record of events) {
    const ids = Array.isArray(record.payload?.handoff_ids) ? record.payload.handoff_ids : [];
    if (!ids.length) continue;
    const isRebuttal = record.kind.includes("rebuttal");
    const key = isRebuttal ? "rebuttal" : record.kind.includes("initial") ? "initial" : record.kind;
    const round = rounds.get(key) || {
      key,
      label: isRebuttal ? "Rebuttals" : key === "initial" ? "Initial positions" : record.kind.replaceAll("_", " "),
      ids: new Set(),
    };
    for (const id of ids) round.ids.add(id);
    rounds.set(key, round);
  }
  if (!rounds.size && debate.handoff_ids?.length) {
    rounds.set("initial", {key: "initial", label: "Initial positions", ids: new Set(debate.handoff_ids)});
  }
  const modeledRounds = [...rounds.values()].map((round) => {
    const tasks = [...round.ids].map((id) => handoffById.get(id) || {id, status: "queued"});
    return {...round, tasks, status: derivedTaskStatus(tasks)};
  });
  const allTasks = modeledRounds.flatMap((round) => round.tasks);
  return {
    question: created?.payload?.question || debate.question || "",
    agents: created?.payload?.agent_ids || [...new Set(allTasks.map((task) => task.agent_id).filter(Boolean))],
    eventsCount: events.length || 1,
    rounds: modeledRounds,
    status: derivedTaskStatus(allTasks),
  };
}

function renderFiles() {
  const list = $("file-list");
  list.replaceChildren();
  $("selected-file-count").textContent = `${state.selectedFilePaths.size} selected`;
  if (!state.files.length) {
    list.appendChild(element("div", "detail-empty", "This project snapshot has no files."));
    return;
  }
  for (const file of state.files) {
    const row = element("div", "file-row");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedFilePaths.has(file.path);
    checkbox.setAttribute("aria-label", `Use ${file.path} as agent context`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selectedFilePaths.add(file.path);
      else state.selectedFilePaths.delete(file.path);
      $("selected-file-count").textContent = `${state.selectedFilePaths.size} selected`;
      renderFileContextButton();
    });
    const open = element("button", "", file.path);
    open.type = "button";
    open.title = `${file.size} bytes · ${file.sha256}`;
    open.addEventListener("click", () => openFile(file.path));
    row.append(checkbox, open);
    list.appendChild(row);
  }
}

async function openFile(path) {
  if (!state.project) return;
  try {
    $("file-title").textContent = path;
    $("file-content").textContent = "Loading committed file…";
    const file = await api.fileContent(state.project.id, path, state.filesRef);
    state.selectedFilePath = file.path;
    $("file-title").textContent = file.path;
    $("file-content").textContent = file.content;
    renderFileContextButton();
  } catch (error) { showError(error); }
}

function renderFileContextButton() {
  const button = $("toggle-file-context");
  button.hidden = !state.selectedFilePath;
  if (!state.selectedFilePath) return;
  button.textContent = state.selectedFilePaths.has(state.selectedFilePath) ? "Remove from context" : "Add to context";
}

async function openProposal(id) {
  try {
    state.proposal = await api.proposal(id);
    setView("code");
    renderProposal();
  } catch (error) { showError(error); }
}

function renderProposal() {
  const proposal = state.proposal;
  const status = $("proposal-status");
  const meta = $("proposal-meta");
  const actions = $("proposal-actions");
  meta.replaceChildren();
  actions.replaceChildren();
  if (!proposal) {
    status.className = "status-chip";
    status.textContent = "None selected";
    $("proposal-summary").textContent = "Open a proposal from an agent output to inspect its branch, checks state, and diff.";
    $("proposal-diff").textContent = "";
    return;
  }
  const applied = APPLIED_PROPOSAL_STATES.has(proposal.apply_status);
  const sourceOutput = state.outputs.find((item) => proposalIdFromOutput(item) === proposal.id);
  const recordedChecks = sourceOutput?.parsed?.kind === "agent_execution" && Array.isArray(sourceOutput.parsed.checks)
    ? sourceOutput.parsed.checks : [];
  status.className = `status-chip ${applied ? "completed" : "queued"}`;
  status.textContent = applied
    ? (proposal.apply_status === "proposal_reconciled" ? "Applied · reconciled" : "Applied")
    : recordedChecks.length ? `Proposed · ${recordedChecks.length} check receipt${recordedChecks.length === 1 ? "" : "s"}` : "Proposed · checks not run";
  $("proposal-summary").textContent = proposal.summary || "Agent-created code proposal.";
  appendMeta(meta, "Branch", proposal.branch);
  appendMeta(meta, "Base", shortSha(proposal.base_commit));
  appendMeta(meta, "Commit", shortSha(proposal.commit_sha));
  appendMeta(meta, "Files", (proposal.changed_paths || []).join(", ") || "none");
  if (recordedChecks.length) {
    appendMeta(meta, "Recorded checks", recordedChecks.map((check) => `${check.profile || "check"}: ${check.status || "unknown"} @ ${shortSha(check.tree_sha256)}`).join("; "));
  }
  $("proposal-diff").textContent = proposal.diff_text || "No diff recorded.";
  if (!applied) {
    actions.appendChild(actionButton("Apply to project", applyCurrentProposal, {className: "primary", id: "apply-proposal"}));
  }
}

async function applyCurrentProposal() {
  if (!state.proposal) return;
  const button = $("apply-proposal");
  setBusy(button, true, "Applying…");
  try {
    const response = await api.applyProposal(state.proposal.id, state.proposal.base_commit);
    state.proposal = response.proposal;
    renderProposal();
    state.project = await api.project(state.project.id);
    const files = await api.files(state.project.id);
    state.files = files.items || [];
    state.filesRef = files.ref || state.project.initial_commit || "";
    renderProjectHeader();
    renderFiles();
    await refreshProjectEvents();
  } catch (error) {
    if (error.status === 409) {
      $("proposal-status").className = "status-chip failed";
      $("proposal-status").textContent = "Stale · refresh proposal";
    }
    showError(error);
    if (button) setBusy(button, false);
  }
}

function populateAgents() {
  const select = $("handoff-agent");
  const checks = $("debate-agents");
  select.replaceChildren();
  checks.replaceChildren();
  const byId = new Map(state.models.map((model) => [model.id, model]));
  for (const id of HANDOFF_AGENT_IDS) {
    const model = byId.get(id);
    const option = document.createElement("option");
    option.value = id;
    option.textContent = model ? `${model.label} · ${model.connection_status === "verified" ? "verified" : model.setup_status || "unverified"}` : `${id} · unavailable`;
    option.disabled = !model?.available;
    select.appendChild(option);

    if (!SUBSCRIPTION_IDS.includes(id)) continue;
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = id;
    checkbox.disabled = !model?.available;
    label.append(checkbox, document.createTextNode(option.textContent));
    checks.appendChild(label);
  }
}

function contextText() {
  const pieces = [
    `${state.selectedFilePaths.size} file${state.selectedFilePaths.size === 1 ? "" : "s"}`,
    `${state.selectedIssueIds.size} issue${state.selectedIssueIds.size === 1 ? "" : "s"}`,
    `${state.selectedOutputIds.size} output${state.selectedOutputIds.size === 1 ? "" : "s"}`,
  ];
  return `Selected context: ${pieces.join(", ")}. Change file context in Code, issue context in Issues, and output context in Work.`;
}

function openHandoffDialog(prefill = {}) {
  if (!state.project) return;
  const form = $("handoff-form");
  form.reset();
  clearError($("handoff-form-error"));
  $("handoff-dialog-title").textContent = prefill.parent ? "Follow-up handoff" : "New handoff";
  $("handoff-parent").value = prefill.parent || "";
  if (prefill.outputId) state.selectedOutputIds.add(prefill.outputId);
  $("handoff-context-summary").textContent = `${prefill.parent ? `Parent handoff: ${prefill.parent}. ` : ""}${contextText()}`;
  populateAgents();
  $("handoff-dialog").showModal();
  $("handoff-agent").focus();
}

async function submitHandoff(event) {
  event.preventDefault();
  if (!state.project) return;
  const error = $("handoff-form-error");
  clearError(error);
  const acceptance = $("handoff-acceptance").value.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!acceptance.length) {
    showError("Add at least one acceptance criterion.", error);
    return;
  }
  const button = $("create-handoff-submit");
  setBusy(button, true, "Queueing…");
  const body = {
    request_id: crypto.randomUUID(),
    agent_id: $("handoff-agent").value,
    mode: $("handoff-mode").value,
    instructions: $("handoff-instructions").value.trim(),
    acceptance,
    file_paths: [...state.selectedFilePaths],
    issue_ids: [...state.selectedIssueIds],
    source_output_ids: [...state.selectedOutputIds],
  };
  const model = $("handoff-model").value.trim();
  const parent = $("handoff-parent").value;
  if (model) body.model = model;
  if (parent) body.parent_handoff_id = parent;
  try {
    const task = await api.createHandoff(state.project.id, body);
    state.selectedHandoffId = task.id;
    $("handoff-dialog").close();
    await refreshWork();
    setView("work");
  } catch (requestError) {
    showError(requestError, error);
  } finally {
    setBusy(button, false);
  }
}

function openProjectDialog() {
  $("project-form").reset();
  clearError($("project-form-error"));
  $("project-dialog").showModal();
  $("project-input-name").focus();
}

async function submitProject(event) {
  event.preventDefault();
  const error = $("project-form-error");
  clearError(error);
  const button = $("create-project-submit");
  setBusy(button, true, "Creating…");
  const source = $("project-input-source").value.trim();
  const body = {
    name: $("project-input-name").value.trim(),
    description: $("project-input-description").value.trim(),
    ...(source ? {source_path: source} : {}),
  };
  try {
    const project = await api.createProject(body);
    $("project-dialog").close();
    state.project = null;
    await loadProjects(project.id);
    if (project.import_report) showNotice(importReceiptText(project.import_report));
  } catch (requestError) {
    showError(requestError, error);
  } finally {
    setBusy(button, false);
  }
}

function openIssueDialog() {
  if (!state.project) return;
  $("issue-form").reset();
  clearError($("issue-form-error"));
  $("issue-dialog").showModal();
  $("issue-title").focus();
}

function openWorkItemDialog() {
  if (!state.project) return;
  $("work-item-form").reset();
  clearError($("work-item-form-error"));
  const selected = [...state.selectedIssueIds];
  $("work-item-context").textContent = selected.length
    ? `${selected.length} selected issue${selected.length === 1 ? "" : "s"} will be linked.`
    : "No issues selected. Select issue context in Issues before opening this form to link it.";
  $("work-item-dialog").showModal();
  $("work-item-title").focus();
}

async function submitWorkItem(event) {
  event.preventDefault();
  if (!state.project) return;
  const error = $("work-item-form-error");
  clearError(error);
  const acceptance = $("work-item-acceptance").value.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!acceptance.length) {
    showError("Add at least one acceptance criterion.", error);
    return;
  }
  const button = $("create-work-item-submit");
  setBusy(button, true, "Adding…");
  try {
    await api.createWorkItem(state.project.id, {
      request_id: crypto.randomUUID(),
      title: $("work-item-title").value.trim(),
      instructions: $("work-item-instructions").value.trim(),
      acceptance,
      owned_paths: $("work-item-paths").value.split("\n").map((line) => line.trim()).filter(Boolean),
      issue_ids: [...state.selectedIssueIds],
    });
    $("work-item-dialog").close();
    await refreshAgentRoom();
    setView("agent-room");
  } catch (requestError) {
    showError(requestError, error);
  } finally {
    setBusy(button, false);
  }
}

async function submitIssue(event) {
  event.preventDefault();
  if (!state.project) return;
  const error = $("issue-form-error");
  clearError(error);
  const button = $("create-issue-submit");
  setBusy(button, true, "Creating…");
  try {
    const issue = await api.createIssue(state.project.id, {
      title: $("issue-title").value.trim(),
      body: $("issue-body").value.trim(),
    });
    state.selectedIssueId = issue.id;
    $("issue-dialog").close();
    await refreshProjectRecords();
    setView("issues");
  } catch (requestError) {
    showError(requestError, error);
  } finally {
    setBusy(button, false);
  }
}

function openDebateDialog() {
  if (!state.project) return;
  $("debate-form").reset();
  clearError($("debate-form-error"));
  $("debate-context-summary").textContent = contextText();
  populateAgents();
  $("debate-dialog").showModal();
  $("debate-question").focus();
}

async function submitDebate(event) {
  event.preventDefault();
  if (!state.project) return;
  const error = $("debate-form-error");
  clearError(error);
  const agentIds = [...$("debate-agents").querySelectorAll("input:checked")].map((input) => input.value);
  if (agentIds.length < 2 || agentIds.length > 5) {
    showError("Choose between two and five available agents.", error);
    return;
  }
  const button = $("create-debate-submit");
  setBusy(button, true, "Queueing…");
  const question = $("debate-question").value.trim();
  try {
    const task = await api.createDebate(state.project.id, {
      request_id: crypto.randomUUID(),
      question,
      agent_ids: agentIds,
      source_output_ids: [...state.selectedOutputIds],
      issue_ids: [...state.selectedIssueIds],
    });
    state.debates.push({...task, question});
    $("debate-dialog").close();
    renderDebates();
    await refreshWork();
  } catch (requestError) {
    showError(requestError, error);
  } finally {
    setBusy(button, false);
  }
}

async function exportProject() {
  if (!state.project) return;
  const button = $("export-project");
  setBusy(button, true, "Exporting…");
  try {
    const blob = await api.projectExport(state.project.id);
    const slug = state.project.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "project";
    downloadBlob(blob, `${slug}-litigation-export.json`);
  } catch (error) { showError(error); }
  finally { setBusy(button, false); }
}

function setView(view) {
  for (const button of $("project-tabs").querySelectorAll("button[data-view]")) {
    const active = button.dataset.view === view;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    $(`view-${button.dataset.view}`).hidden = !active;
  }
  if (view === "agent-room") void refreshAgentRoom({quiet: true});
}

function mergeEvents(items) {
  const known = new Set(state.events.map((record) => record.seq ?? record.id));
  for (const record of items) {
    const key = record.seq ?? record.id;
    if (!known.has(key)) {
      state.events.push(record);
      known.add(key);
    }
  }
  state.events.sort((a, b) => (a.seq || 0) - (b.seq || 0));
}

async function catchUpEvents(maxPages = 10) {
  if (!state.project || state.eventsFetching) return;
  const projectId = state.project.id;
  state.eventsFetching = true;
  try {
    let pages = 0;
    do {
      const response = await api.events(projectId, state.eventsCursor);
      if (state.project?.id !== projectId) return;
      mergeEvents(response.items || []);
      state.eventsCursor = response.next_after || state.eventsCursor;
      state.eventsHasMore = Boolean(response.has_more);
      pages += 1;
    } while (state.eventsHasMore && pages < maxPages);
    renderHistory();
    renderDebates();
    if (state.selectedIssueId) renderIssueDetail();
  } finally {
    state.eventsFetching = false;
  }
  if (state.eventsHasMore && state.project?.id === projectId) {
    window.setTimeout(() => catchUpEvents(maxPages), 0);
  }
}

async function refreshProjectEvents() {
  if (!state.project) return;
  await catchUpEvents();
}

async function refreshProjectRecords() {
  if (!state.project) return;
  const issues = await api.issues(state.project.id);
  state.issues = issues.items || [];
  await catchUpEvents();
  renderIssues();
}

async function refreshWork() {
  if (!state.project || state.polling) return;
  state.polling = true;
  try {
    const handoffs = await api.handoffs(state.project.id);
    state.handoffs = handoffs.items || [];
    await catchUpEvents();
    renderHandoffs();
    if (state.selectedHandoffId) await selectHandoff(state.selectedHandoffId, state.selectedOutputId);
  } catch (error) {
    $("polling-note").textContent = "Refresh delayed";
  } finally {
    state.polling = false;
  }
}

function closeDialogFrom(button) {
  const dialog = button.closest("dialog");
  if (dialog) dialog.close();
}

$("new-project-button").addEventListener("click", openProjectDialog);
$("empty-new-project").addEventListener("click", openProjectDialog);
$("new-handoff-button").addEventListener("click", () => openHandoffDialog());
$("new-work-item-button").addEventListener("click", openWorkItemDialog);
$("verify-provenance").addEventListener("click", verifyProvenance);
$("new-issue-button").addEventListener("click", openIssueDialog);
$("new-debate-button").addEventListener("click", openDebateDialog);
$("export-project").addEventListener("click", exportProject);
$("history-more").addEventListener("click", loadMoreHistory);
$("project-form").addEventListener("submit", submitProject);
$("handoff-form").addEventListener("submit", submitHandoff);
$("work-item-form").addEventListener("submit", submitWorkItem);
$("issue-form").addEventListener("submit", submitIssue);
$("debate-form").addEventListener("submit", submitDebate);
for (const button of document.querySelectorAll(".dialog-close")) button.addEventListener("click", () => closeDialogFrom(button));

$("project-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (button) setView(button.dataset.view);
});
$("project-tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const buttons = [...$("project-tabs").querySelectorAll("button[data-view]")];
  const index = buttons.indexOf(document.activeElement);
  if (index < 0) return;
  event.preventDefault();
  const offset = event.key === "ArrowRight" ? 1 : -1;
  const next = buttons[(index + offset + buttons.length) % buttons.length];
  next.focus();
  setView(next.dataset.view);
});

$("toggle-file-context").addEventListener("click", () => {
  if (!state.selectedFilePath) return;
  if (state.selectedFilePaths.has(state.selectedFilePath)) state.selectedFilePaths.delete(state.selectedFilePath);
  else state.selectedFilePaths.add(state.selectedFilePath);
  renderFiles();
  renderFileContextButton();
});

function openProjectRail() { $("project-rail").classList.add("open"); }
$("open-projects").addEventListener("click", openProjectRail);
$("open-projects-empty").addEventListener("click", openProjectRail);
$("close-projects").addEventListener("click", () => $("project-rail").classList.remove("open"));

window.setInterval(() => {
  if (state.project && state.handoffs.some((handoff) => ACTIVE_STATES.has(handoff.status))) refreshWork();
  if (state.project && (state.workItems.some((item) => ["claimed", "running"].includes(item.status)) || state.activeHandoffs.length)) {
    void refreshAgentRoom({quiet: true});
  }
}, 3000);

(async function init() {
  try {
    const models = await api.models();
    state.models = models;
    populateAgents();
    await loadProjects();
  } catch (error) {
    showError(error);
  }
})();

window.__litigationProjects = {
  state, api, openProject, setView, renderProjects,
  proposalIdFromOutput,
  proposalApplied: (proposal) => APPLIED_PROPOSAL_STATES.has(proposal?.apply_status),
  importReceiptText,
  debateRoundModel,
  renderDebates,
  renderSelectedHandoff,
  renderProposal,
};
