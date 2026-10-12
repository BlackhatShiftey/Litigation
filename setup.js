"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

// Mirrors the server's CHECKABLE_SUBSCRIPTIONS; the alias `antigravity` is
// hidden in favor of `gemini`.
const CHECKABLE = new Set(["claude", "grok", "gemini", "glm", "cline", "openai"]);
const SUBSCRIPTION_ORDER = ["grok", "gemini", "glm", "cline", "claude", "openai", "astra"];

const MODE_LABELS = {
  subscription: "Subscriptions",
  api: "API keys",
  mixed: "All providers",
  unset: "Not chosen yet",
};

const state = { setup: null, models: [] };

const api = {
  async json(url, opts = {}) {
    const r = await fetch(url, {headers: {"Content-Type": "application/json"}, ...opts});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  setup: () => api.json("/api/setup"),
  models: () => api.json("/api/models"),
  setMode: (mode) => api.json("/api/setup/mode", {method: "POST", body: JSON.stringify({mode})}),
  setKeys: (keys) => api.json("/api/setup/keys", {method: "POST", body: JSON.stringify({keys})}),
  clearKey: (name) => api.json("/api/setup/keys/" + encodeURIComponent(name), {method: "DELETE"}),
  checkModel: (id) => api.json("/api/models/" + encodeURIComponent(id) + "/check", {method: "POST"}),
};

let messageTimer = null;
function message(text, kind = "") {
  const box = $("message");
  box.textContent = text;
  box.className = kind;
  clearTimeout(messageTimer);
  if (text) messageTimer = setTimeout(() => { box.textContent = ""; box.className = ""; }, 6000);
}

function modelStatus(m) {
  if (m.connection_status === "verified") return ["Verified now", "verified"];
  if (m.connection_status === "error") return ["Needs attention", "error"];
  const labels = {
    installed_unverified: "Installed · not verified",
    configured_unverified: "Configured · not verified",
    not_installed: "Not installed",
    not_configured: "Setup needed",
    safety_blocked: "Held unavailable",
  };
  return [labels[m.setup_status] || "Not verified", ""];
}

function renderModeState() {
  const mode = state.setup ? state.setup.auth_mode : "unset";
  const box = $("mode-state");
  box.innerHTML = `<div class="current">Auth mode: ${esc(MODE_LABELS[mode] || mode)}</div>` +
    (state.setup && state.setup.store_error
      ? `<p class="warn">${esc(state.setup.store_error)}</p>` : "");
  $("card-subscription").classList.toggle("active", mode === "subscription");
  $("card-api").classList.toggle("active", mode === "api");
}

function renderSubscriptions() {
  const wrap = $("sub-providers");
  const subscription = state.models.filter(
    (m) => m.billing === "subscription" && m.id !== "antigravity");
  const byId = new Map(subscription.map((m) => [m.id, m]));
  const ordered = [
    ...SUBSCRIPTION_ORDER.map((id) => byId.get(id)).filter(Boolean),
    ...subscription.filter((m) => !SUBSCRIPTION_ORDER.includes(m.id)),
  ];
  if (!ordered.length) {
    wrap.innerHTML = '<div class="hint">No subscription transports are registered.</div>';
    return;
  }
  wrap.innerHTML = "";
  for (const m of ordered) {
    const [status, statusClass] = modelStatus(m);
    const row = document.createElement("div");
    row.className = "provider-row";
    row.innerHTML =
      `<span class="dot" style="background:${esc(m.color || "#8b949e")}"></span>
       <div>
         <div class="name">${esc(m.label)}</div>
         <div class="meta">${esc(m.model || "provider default")} · ${esc(m.transport || "cli")}</div>
       </div>
       <span class="status ${statusClass}">${esc(status)}</span>
       ${m.setup_hint ? `<div class="hint">${esc(m.setup_hint)}</div>` : ""}
       ${CHECKABLE.has(m.id)
         ? `<div class="actions"><button type="button" class="secondary check" data-id="${esc(m.id)}">Test connection</button></div>`
         : ""}`;
    wrap.appendChild(row);
  }
}

function renderKeys() {
  const wrap = $("api-keys");
  const keys = (state.setup && state.setup.api_keys) || [];
  if (!keys.length) {
    wrap.innerHTML = '<div class="hint">No API-key providers are registered.</div>';
    return;
  }
  wrap.innerHTML = "";
  for (const key of keys) {
    const row = document.createElement("div");
    row.className = "key-row";
    row.innerHTML =
      `<span class="key-name">${esc(key.env_key)} <span class="hint">· ${esc(key.label)}</span></span>
       <input type="password" data-key="${esc(key.env_key)}" autocomplete="off"
              placeholder="${key.set ? "set — paste a new value to replace" : "paste key (never echoed back)"}">
       <span class="key-state ${key.set ? "set" : ""}">${key.set ? "Set ✓" : "Not set"}</span>`;
    if (key.set) {
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "secondary clear";
      clear.dataset.key = key.env_key;
      clear.textContent = "Remove";
      row.appendChild(clear);
    }
    wrap.appendChild(row);
  }
  const credentials = (state.setup && state.setup.subscription_credentials) || [];
  const zai = credentials.find((c) => c.env_key === "ZAI_API_KEY");
  $("zai-state").innerHTML = zai
    ? (zai.set
        ? '<span class="set">Credential set ✓</span> — GLM subscription route is configured.'
        : "Not set — the GLM route needs this credential even in subscription mode.")
    : "";
}

async function refresh() {
  const [setup, models] = await Promise.all([api.setup(), api.models()]);
  state.setup = setup;
  state.models = models;
  renderModeState();
  renderSubscriptions();
  renderKeys();
}

async function chooseMode(mode) {
  try {
    await api.setMode(mode);
    document.dispatchEvent(new CustomEvent("litigation:authmode", {detail: {mode}}));
    await refresh();
    message(`Auth mode set to ${MODE_LABELS[mode] || mode}. New rooms follow it; existing rooms keep their routes.`, "ok");
  } catch (e) {
    message(e.message, "err");
  }
}

async function saveKeys(inputs) {
  const keys = {};
  for (const input of inputs) {
    const value = input.value.trim();
    if (value) keys[input.dataset.key] = value;
  }
  if (!Object.keys(keys).length) {
    message("Nothing to save — paste at least one key first.", "err");
    return;
  }
  try {
    await api.setKeys(keys);
    for (const input of inputs) input.value = "";
    await refresh();
    message("Keys saved locally and applied. Set keys show a green ✓.", "ok");
  } catch (e) {
    message(e.message, "err");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("use-subscription").addEventListener("click", () => chooseMode("subscription"));
  $("use-api").addEventListener("click", () => chooseMode("api"));
  $("use-mixed").addEventListener("click", () => chooseMode("mixed"));
  $("save-keys").addEventListener("click", () =>
    saveKeys($("api-keys").querySelectorAll("input[data-key]")));
  $("save-zai").addEventListener("click", () =>
    saveKeys([$("zai-key")]));
  $("api-keys").addEventListener("click", async (event) => {
    const button = event.target.closest("button.clear");
    if (!button) return;
    button.disabled = true;
    try {
      await api.clearKey(button.dataset.key);
      await refresh();
      message(`${button.dataset.key} removed from the local key file.`, "ok");
    } catch (e) {
      button.disabled = false;
      message(e.message, "err");
    }
  });
  $("sub-providers").addEventListener("click", async (event) => {
    const button = event.target.closest("button.check");
    if (!button) return;
    button.disabled = true;
    button.textContent = "Checking…";
    try {
      await api.checkModel(button.dataset.id);
      await refresh();
    } catch (e) {
      button.disabled = false;
      button.textContent = "Try again";
      message(e.message, "err");
    }
  });
  document.addEventListener("litigation:authmode", () => {
    api.setup().then((setup) => { state.setup = setup; renderModeState(); }).catch(() => {});
  });
  refresh().catch((e) => message("Could not load setup state: " + e.message, "err"));
});
