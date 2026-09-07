document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-delete-student]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-delete-student");
      const name = btn.getAttribute("data-name");
      const ok = await confirmModal({
        title: "Delete student?",
        body: `This permanently deletes <strong>${name}</strong>, their captured photos, and their attendance history. This can't be undone.`,
        confirmLabel: "Delete",
        danger: true,
      });
      if (!ok) return;
      try {
        const res = await apiFetch(`/students/${id}`, { method: "DELETE" });
        if (!res.ok) throw new Error("failed");
        showToast(`${name} was deleted.`, "success");
        btn.closest("tr").remove();
      } catch (e) {
        showToast("Failed to delete student.", "error");
      }
    });
  });
});
