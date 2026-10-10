"use strict";

const $ = (id) => document.getElementById(id);
const SCOPE_DESCRIPTIONS = {
  global: "The authority every agent receives with its model-specific section.",
  claude: "Working guidance for Claude subscription agents.",
  openai: "Working guidance for Codex, GPT, and Astra agents.",
  glm: "Working guidance for GLM subscription agents.",
  gemini: "Working guidance for Gemini agents through Antigravity.",
  cline: "Working guidance for Cline subscription agents.",
  grok: "Working guidance for Grok subscription agents.",
};
const SIGILS = {global: "§", claude: "C", openai: "O", glm: "G", gemini: "G", cline: "C", grok: "X"};

class ApiError extends Error {
  constructor(status, detail) { super(detail || `Request failed (${status})`); this.status = status; }
}
const api = {
  async request(url, options = {}) {
    const response = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(response.status, body.detail || response.statusText);
    }
    return response.json();
  },
  scopes: () => api.request("/api/rules"),
  active: (scope) => api.request(`/api/rules/${encodeURIComponent(scope)}`),
  history: (scope, before = null) => api.request(`/api/rules/${encodeURIComponent(scope)}/history${before ? `?before_revision=${encodeURIComponent(before)}` : ""}`),
  proposals: (scope) => api.request(`/api/rules/proposals?scope=${encodeURIComponent(scope)}`),
  publish: (scope, body) => api.request(`/api/rules/${encodeURIComponent(scope)}/revisions`, {method: "POST", body: JSON.stringify(body)}),
  propose: (body) => api.request("/api/rules/proposals", {method: "POST", body: JSON.stringify(body)}),
  accept: (id, expectedRevision) => api.request(`/api/rules/proposals/${encodeURIComponent(id)}/accept`, {method: "POST", body: JSON.stringify({expected_revision: expectedRevision})}),
  reject: (id) => api.request(`/api/rules/proposals/${encodeURIComponent(id)}/reject`, {method: "POST", body: JSON.stringify({reason: "Rejected during review"})}),
};

const state = {scopes: [], scope: "global", active: null, history: [], historyCursor: null, proposals: [], dialogMode: "publish"};

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}
function formatTime(value) {
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
}
function showMessage(message, error = false) {
  const box = error ? $("error") : $("notice");
  box.textContent = message instanceof Error ? message.message : String(message);
  box.hidden = false;
  window.setTimeout(() => { box.hidden = true; }, 7000);
}

function renderIndex() {
  const list = $("scope-list");
  list.replaceChildren();
  for (const scope of state.scopes) {
    const button = el("button", scope.id === state.scope ? "active" : "");
    button.type = "button";
    if (scope.id === state.scope) button.setAttribute("aria-current", "page");
    const sigil = el("span", "sigil", SIGILS[scope.id] || scope.title.slice(0, 1));
    const title = el("strong", "", scope.title);
    const rev = el("small", "", `r${scope.revision}`);
    button.append(sigil, title, rev);
    button.addEventListener("click", () => loadScope(scope.id));
    list.appendChild(button);
  }
}

function renderActive() {
  const item = state.active;
  if (!item) return;
  const scope = state.scopes.find((entry) => entry.id === state.scope);
  $("scope-title").textContent = scope?.title || state.scope;
  $("scope-description").textContent = SCOPE_DESCRIPTIONS[state.scope] || "Model-specific working guidance.";
  $("active-revision").textContent = `r${item.revision}`;
  $("active-origin").textContent = item.origin.replaceAll("_", " ");
  $("active-time").textContent = formatTime(item.created_at);
  $("active-hash").textContent = item.content_hash.slice(0, 16);
  $("active-hash").title = item.content_hash;
  $("active-content").textContent = item.content;
}

function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  for (const item of state.history) {
    const card = el("li", "history-item");
    const header = el("header");
    header.append(el("h3", "", `Revision ${item.revision} · ${item.summary || "Published rules"}`), el("span", "meta", formatTime(item.created_at)));
    const details = el("details");
    details.append(el("summary", "", "Inspect immutable snapshot"), el("pre", "", item.content));
    const evidence = item.evidence_handoff_id ? ` · evidence ${item.evidence_handoff_id}` : "";
    card.append(header, el("p", "", `Origin: ${item.origin.replaceAll("_", " ")} · hash ${item.content_hash.slice(0, 12)}${evidence}`), details);
    list.appendChild(card);
  }
  $("history-more").hidden = !state.historyCursor;
}

function proposalButton(label, handler, className = "quiet") {
  const button = el("button", className, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}
function renderProposals() {
  const list = $("proposal-list");
  list.replaceChildren();
  const pending = state.proposals.filter((item) => item.status === "proposed").length;
  $("proposal-count").textContent = pending ? `(${pending})` : "";
  if (!state.proposals.length) {
    list.appendChild(el("p", "meta", "No suggestions for this section yet."));
    return;
  }
  for (const item of state.proposals) {
    const statusClass = item.status === "proposed" ? "pending" : item.status;
    const card = el("article", `proposal-card ${statusClass}`);
    const header = el("header");
    header.append(el("h3", "", item.summary || "Rule improvement"), el("span", "meta", item.status));
    const origin = item.proposer === "user" ? "User suggestion" : `${item.proposer} agent suggestion`;
    const evidence = item.evidence_handoff_id ? ` · evidence ${item.evidence_handoff_id}` : " · no handoff evidence";
    const details = el("details");
    details.append(el("summary", "", "Review proposed full revision"), el("pre", "", item.content));
    card.append(header, el("p", "origin", origin), el("p", "", `${formatTime(item.created_at)}${evidence}`), details);
    if (item.status === "proposed") {
      const actions = el("div", "proposal-actions");
      actions.append(
        proposalButton("Accept as next revision", () => decideProposal(item.id, true), "primary"),
        proposalButton("Reject", () => decideProposal(item.id, false)),
      );
      card.appendChild(actions);
    }
    list.appendChild(card);
  }
}

async function loadScope(scope) {
  state.scope = scope;
  try {
    const [active, history, proposals] = await Promise.all([api.active(scope), api.history(scope), api.proposals(scope)]);
    state.active = active;
    state.history = history.items || [];
    state.historyCursor = history.next_before_revision || null;
    state.proposals = proposals.items || [];
    renderIndex(); renderActive(); renderHistory(); renderProposals();
    $("rule-index").classList.remove("open");
  } catch (error) { showMessage(error, true); }
}

async function loadOlderHistory() {
  if (!state.historyCursor) return;
  try {
    const page = await api.history(state.scope, state.historyCursor);
    state.history.push(...(page.items || []));
    state.historyCursor = page.next_before_revision || null;
    renderHistory();
  } catch (error) { showMessage(error, true); }
}

function openRevisionDialog(mode) {
  state.dialogMode = mode;
  $("revision-dialog-title").textContent = mode === "publish" ? "Publish revision" : "Suggest revision";
  $("revision-dialog-note").textContent = mode === "publish"
    ? `Create immutable revision ${Number(state.active.revision) + 1} of ${state.scope}.`
    : `Create a reviewable suggestion for ${state.scope}. It will not become active.`;
  $("revision-help").textContent = state.scope === "global"
    ? "Global rules control every model. Review permission and safety changes carefully."
    : "This section adds model guidance and cannot expand permissions granted by Global rules.";
  $("revision-submit").textContent = mode === "publish" ? "Publish revision" : "Create proposal";
  $("revision-summary").value = "";
  $("revision-content").value = state.active.content;
  $("revision-error").textContent = "";
  $("revision-dialog").showModal();
}

async function submitRevision(event) {
  event.preventDefault();
  const summary = $("revision-summary").value.trim();
  const content = $("revision-content").value;
  const button = $("revision-submit");
  button.disabled = true;
  try {
    if (state.dialogMode === "publish") {
      await api.publish(state.scope, {summary, content, expected_revision: state.active.revision});
      showMessage("Revision published. The previous revision remains in history.");
    } else {
      await api.propose({scope: state.scope, summary, content});
      showMessage("Proposal recorded for review.");
    }
    $("revision-dialog").close();
    const scopes = await api.scopes();
    state.scopes = scopes.items || [];
    await loadScope(state.scope);
  } catch (error) { $("revision-error").textContent = error.message; }
  finally { button.disabled = false; }
}

async function decideProposal(id, accept) {
  try {
    if (accept) {
      await api.accept(id, state.active.revision);
      showMessage("Proposal accepted as a new immutable revision.");
    } else {
      await api.reject(id);
      showMessage("Proposal rejected. Its record remains in history.");
    }
    const scopes = await api.scopes();
    state.scopes = scopes.items || [];
    await loadScope(state.scope);
  } catch (error) { showMessage(error, true); }
}

function showView(name) {
  for (const value of ["active", "history", "proposals"]) {
    const selected = value === name;
    $(`tab-${value}`).setAttribute("aria-selected", String(selected));
    $(`view-${value}`).hidden = !selected;
  }
}

async function init() {
  try {
    const response = await api.scopes();
    state.scopes = response.items || [];
    await loadScope("global");
  } catch (error) { showMessage(error, true); }
}

$("publish-button").addEventListener("click", () => openRevisionDialog("publish"));
$("suggest-button").addEventListener("click", () => openRevisionDialog("propose"));
$("revision-form").addEventListener("submit", submitRevision);
for (const button of document.querySelectorAll(".dialog-close")) button.addEventListener("click", () => $("revision-dialog").close());
for (const button of document.querySelectorAll(".view-tabs button")) button.addEventListener("click", () => showView(button.dataset.view));
$("open-index").addEventListener("click", () => $("rule-index").classList.add("open"));
$("close-index").addEventListener("click", () => $("rule-index").classList.remove("open"));
$("history-more").addEventListener("click", loadOlderHistory);

init();
