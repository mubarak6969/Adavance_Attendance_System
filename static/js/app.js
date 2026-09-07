// Shared app shell behavior: theme, toasts, CSRF-aware fetch, mobile nav, confirm modal.
(function () {
  "use strict";

  // ---------- CSRF-aware fetch ----------
  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  async function apiFetch(url, options = {}) {
    const opts = Object.assign({}, options);
    opts.headers = Object.assign({}, options.headers || {});
    const method = (opts.method || "GET").toUpperCase();
    if (method !== "GET") {
      opts.headers["X-CSRFToken"] = csrfToken();
    }
    const res = await fetch(url, opts);
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("authentication required");
    }
    return res;
  }
  window.apiFetch = apiFetch;

  // ---------- Toasts ----------
  function toast(message, type = "info", timeout = 4200) {
    const container = document.getElementById("toastContainer");
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.innerHTML = `<span>${message}</span><button class="toast-close" aria-label="Dismiss">&times;</button>`;
    el.querySelector(".toast-close").addEventListener("click", () => el.remove());
    container.appendChild(el);
    if (timeout) setTimeout(() => el.remove(), timeout);
  }
  window.showToast = toast;

  // ---------- Confirm modal ----------
  function confirmModal({ title, body, confirmLabel = "Confirm", danger = false }) {
    return new Promise((resolve) => {
      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
          <h3 id="modalTitle">${title}</h3>
          <p>${body}</p>
          <div class="modal-actions">
            <button class="btn btn-secondary" data-action="cancel">Cancel</button>
            <button class="btn ${danger ? "btn-danger-solid" : "btn-primary"}" data-action="confirm">${confirmLabel}</button>
          </div>
        </div>`;
      document.body.appendChild(backdrop);
      const close = (result) => { backdrop.remove(); resolve(result); };
      backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(false); });
      backdrop.querySelector('[data-action="cancel"]').addEventListener("click", () => close(false));
      backdrop.querySelector('[data-action="confirm"]').addEventListener("click", () => close(true));
      document.addEventListener("keydown", function esc(e) {
        if (e.key === "Escape") { close(false); document.removeEventListener("keydown", esc); }
      });
    });
  }
  window.confirmModal = confirmModal;

  // ---------- Theme ----------
  function applyThemeUI(theme) {
    document.querySelectorAll("[data-theme-choice]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.themeChoice === theme);
    });
  }

  function setTheme(theme) {
    if (theme === "system") {
      document.documentElement.removeAttribute("data-theme");
      try { localStorage.setItem("theme", "system"); } catch (e) {}
    } else {
      document.documentElement.setAttribute("data-theme", theme);
      try { localStorage.setItem("theme", theme); } catch (e) {}
    }
    applyThemeUI(theme);
  }
  window.setTheme = setTheme;

  document.addEventListener("DOMContentLoaded", () => {
    let saved = "system";
    try { saved = localStorage.getItem("theme") || "system"; } catch (e) {}
    applyThemeUI(saved);
    document.querySelectorAll("[data-theme-choice]").forEach((btn) => {
      btn.addEventListener("click", () => setTheme(btn.dataset.themeChoice));
    });

    // ---------- Mobile sidebar ----------
    const sidebar = document.getElementById("sidebar");
    const scrim = document.getElementById("sidebarScrim");
    const hamburger = document.getElementById("hamburgerBtn");
    function openSidebar() { sidebar && sidebar.classList.add("open"); scrim && scrim.classList.add("open"); }
    function closeSidebar() { sidebar && sidebar.classList.remove("open"); scrim && scrim.classList.remove("open"); }
    hamburger && hamburger.addEventListener("click", openSidebar);
    scrim && scrim.addEventListener("click", closeSidebar);
  });
})();
