"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const newChat = document.querySelector(".litigation-nav__new");
  const onChat = window.location.pathname === "/";
  if (newChat && onChat) {
    newChat.addEventListener("click", (event) => {
      const trigger = document.getElementById("new-room-btn");
      if (!trigger || trigger === newChat) return;
      event.preventDefault();
      trigger.click();
    });
  }
  if (onChat && new URLSearchParams(window.location.search).get("new") === "1") {
    const trigger = document.getElementById("new-room-btn");
    if (trigger) {
      trigger.click();
      window.history.replaceState({}, "", "/");
    }
  }
  mountAuthModeControl();
});

/* Runtime flip between subscription usage and API keys. Mounted on every
   page that includes this script; persists through POST /api/setup/mode and
   broadcasts a document-level "litigation:authmode" event so open dialogs
   can re-render without a reload. */
function mountAuthModeControl() {
  const actions = document.querySelector(".litigation-nav__actions");
  if (!actions || document.querySelector(".litigation-nav__auth")) return;

  const group = document.createElement("div");
  group.className = "litigation-nav__auth";
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Inference billing mode");
  const labels = [["subscription", "Subscriptions"], ["api", "API keys"], ["mixed", "All"]];
  for (const [mode, label] of labels) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.mode = mode;
    button.textContent = label;
    button.title = `New rooms use ${label === "All" ? "every provider" : label}`;
    group.appendChild(button);
  }

  const manage = document.createElement("a");
  manage.className = "litigation-nav__setup";
  manage.href = "/setup";
  manage.title = "Auth mode and API key management";
  manage.textContent = "⚙";

  actions.prepend(group, manage);

  const reflect = (mode) => {
    for (const button of group.querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    }
    document.body.dataset.authMode = mode || "unset";
  };

  group.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-mode]");
    if (!button || button.getAttribute("aria-pressed") === "true") return;
    button.disabled = true;
    try {
      const response = await fetch("/api/setup/mode", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({mode: button.dataset.mode}),
      });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || response.statusText);
      reflect(button.dataset.mode);
      document.dispatchEvent(new CustomEvent("litigation:authmode", {detail: {mode: button.dataset.mode}}));
    } catch (error) {
      group.title = `Could not change auth mode: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  });

  fetch("/api/setup")
    .then((response) => response.ok ? response.json() : null)
    .then((state) => reflect(state && state.auth_mode))
    .catch(() => group.remove());

  document.addEventListener("litigation:authmode", (event) => {
    reflect(event.detail && event.detail.mode);
  });
}
