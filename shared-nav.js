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
});
