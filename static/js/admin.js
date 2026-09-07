document.addEventListener("DOMContentLoaded", () => {
  async function callAction(userId, url, options = {}) {
    try {
      const res = await apiFetch(url, { method: "POST", ...options });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast(data.error || "Action failed.", "error");
        return false;
      }
      return true;
    } catch (e) {
      showToast("Action failed.", "error");
      return false;
    }
  }

  document.querySelectorAll("[data-user-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.userAction;
      const userId = btn.dataset.userId;

      if (action === "delete") {
        const name = btn.dataset.userName;
        const ok = await confirmModal({
          title: "Delete user?",
          body: `This permanently deletes <strong>${name}</strong>'s account. This can't be undone.`,
          confirmLabel: "Delete",
          danger: true,
        });
        if (!ok) return;
        const res = await apiFetch(`/admin/users/${userId}`, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          showToast(data.error || "Could not delete user.", "error");
          return;
        }
        showToast(`${name} was deleted.`, "success");
        document.querySelector(`[data-user-row="${userId}"]`)?.remove();
        return;
      }

      if (action === "role") {
        const role = btn.dataset.role;
        const ok = await callAction(userId, `/admin/users/${userId}/role`, {
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: `role=${encodeURIComponent(role)}`,
        });
        if (ok) {
          showToast("Role updated.", "success");
          setTimeout(() => window.location.reload(), 500);
        }
        return;
      }

      // approve / deactivate / reactivate
      const ok = await callAction(userId, `/admin/users/${userId}/${action}`);
      if (ok) {
        showToast(`User ${action}d.`, "success");
        setTimeout(() => window.location.reload(), 500);
      }
    });
  });
});
