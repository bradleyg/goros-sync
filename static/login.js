"use strict";

(() => {
  const form = document.getElementById("login-form");
  const user = document.getElementById("username");
  const pass = document.getElementById("password");
  const btn = document.getElementById("login-submit");
  const err = document.getElementById("login-error");

  // Only same-site relative paths are allowed as a post-login destination.
  function nextUrl() {
    const next = new URLSearchParams(location.search).get("next") || "/";
    return next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/";
  }

  function showError(message) {
    err.textContent = message;
    err.hidden = !message;
  }

  user.focus();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!user.value.trim() || !pass.value) {
      showError("Enter your username and password.");
      (user.value.trim() ? pass : user).focus();
      return;
    }
    showError("");
    btn.classList.add("is-loading");
    btn.disabled = true;
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: user.value.trim(), password: pass.value }),
      });
      if (res.ok) {
        location.replace(nextUrl());
        return;
      }
      let data = null;
      try { data = await res.json(); } catch { /* empty */ }
      showError(data?.error || `Sign-in failed (${res.status}).`);
      pass.value = "";
      pass.focus();
      form.classList.remove("is-shaking");
      void form.offsetWidth; // restart the animation
      form.classList.add("is-shaking");
    } catch {
      showError("Can't reach the server. Check your connection and try again.");
    } finally {
      btn.classList.remove("is-loading");
      btn.disabled = false;
    }
  });
})();
