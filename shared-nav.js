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
  mountSettingsLink();
  mountAlpineStatus();
});

/* A single gear link into the provider settings page. Provider billing is
   chosen per conversation in the New-chat dialog, not globally, so there is
   no mode control here anymore. */
function mountSettingsLink() {
  const actions = document.querySelector(".litigation-nav__actions");
  if (!actions || document.querySelector(".litigation-nav__setup")) return;
  const manage = document.createElement("a");
  manage.className = "litigation-nav__setup";
  manage.href = "/setup";
  manage.title = "Provider settings — subscriptions, API keys, local models";
  manage.setAttribute("aria-label", "Provider settings");
  manage.textContent = "⚙";
  actions.prepend(manage);
}

/* Alpine (SEAM) connection chip on every page, not just Knowledge. The
   status endpoint probes the host over SSH, so its result is cached in
   sessionStorage and refreshed at most every few minutes. */
function mountAlpineStatus() {
  const actions = document.querySelector(".litigation-nav__actions");
  if (!actions || document.querySelector(".litigation-nav__alpine")) return;
  const chip = document.createElement("a");
  chip.className = "litigation-nav__alpine";
  chip.href = "/knowledge";
  chip.title = "SEAM knowledge service on Alpine (private Tailscale link)";
  chip.innerHTML =
    '<span class="alpine-dot" aria-hidden="true"></span><span class="alpine-text">Alpine</span>';
  actions.prepend(chip);

  const apply = (state) => { chip.dataset.state = state; };
  const TTL = 5 * 60 * 1000;
  let cached = null;
  try {
    cached = JSON.parse(sessionStorage.getItem("litigation.alpine") || "null");
  } catch (_) { /* corrupted cache entry: just re-probe */ }
  if (cached && Date.now() - cached.at < TTL) {
    apply(cached.state);
    return;
  }
  fetch("/api/seam/status")
    .then((response) => (response.ok ? response.json() : null))
    .then((status) => {
      const state = status && status.reachable && status.installed ? "ok" : "down";
      try {
        sessionStorage.setItem("litigation.alpine",
                               JSON.stringify({state, at: Date.now()}));
      } catch (_) { /* private mode: the chip just loses its cache */ }
      apply(state);
    })
    .catch(() => apply("down"));
}
