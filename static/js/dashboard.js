document.addEventListener("DOMContentLoaded", () => {
  const trainBtn = document.getElementById("trainBtn");
  const trainProgress = document.getElementById("trainProgress");
  const trainMsg = document.getElementById("trainMsg");
  let pollTimer = null;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  async function pollStatus() {
    try {
      const res = await apiFetch("/train_status");
      const data = await res.json();
      if (trainProgress) trainProgress.style.width = (data.progress || 0) + "%";
      if (trainMsg) trainMsg.textContent = data.message || "";
      return data;
    } catch (e) {
      console.error(e);
      return null;
    }
  }

  function setTrainingUI(running) {
    trainBtn.disabled = running;
    trainBtn.textContent = running ? "Training…" : "Start Training";
  }

  async function startPollingIfRunning() {
    const status = await pollStatus();
    if (status && status.running) {
      if (trainBtn) setTrainingUI(true);
      pollTimer = setInterval(async () => {
        const s = await pollStatus();
        if (!s || !s.running) {
          clearInterval(pollTimer);
          if (trainBtn) setTrainingUI(false);
          if (s) {
            if (s.status === "success") showToast(s.message, "success");
            else if (s.status === "error") showToast(s.message, "error", 7000);
          }
        }
      }, 1500);
    }
  }

  // The training button/progress panel only renders for super admins (see
  // index.html) -- everyone else still polls /train_status for the badge
  // text above, but has nothing here to wire up.
  if (trainBtn) {
    trainBtn.addEventListener("click", async () => {
      setTrainingUI(true);
      if (trainMsg) trainMsg.textContent = "Starting training…";
      try {
        const res = await apiFetch("/train_model", { method: "POST" });
        const data = await res.json();
        if (res.status === 409) {
          showToast(data.message || "Training is already running.", "warning");
          startPollingIfRunning();
          return;
        }
        if (!res.ok) {
          showToast("Failed to start training.", "error");
          setTrainingUI(false);
          return;
        }
        pollTimer = setInterval(async () => {
          const s = await pollStatus();
          if (s && !s.running) {
            clearInterval(pollTimer);
            setTrainingUI(false);
            if (s.status === "success") showToast(s.message, "success");
            else if (s.status === "error") showToast(s.message, "error", 7000);
          }
        }, 1500);
      } catch (e) {
        showToast("Failed to start training.", "error");
        setTrainingUI(false);
      }
    });
  }

  startPollingIfRunning();

  // ---------- Chart ----------
  let chart = null;
  async function updateChart() {
    try {
      const res = await apiFetch("/attendance_stats?days=30");
      const data = await res.json();
      const ctx = document.getElementById("attendanceChart").getContext("2d");
      const lineColor = cssVar("--chart-line") || "#4338ca";
      const fillColor = cssVar("--chart-fill") || "rgba(67,56,202,0.14)";
      const gridColor = cssVar("--border") || "#e4e6f0";
      const textColor = cssVar("--text-muted") || "#8a8fa3";

      if (!chart) {
        chart = new Chart(ctx, {
          type: "line",
          data: {
            labels: data.dates,
            datasets: [{
              label: "Attendance",
              data: data.counts,
              borderColor: lineColor,
              backgroundColor: fillColor,
              fill: true,
              tension: 0.35,
              pointRadius: 0,
              borderWidth: 2,
            }],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { grid: { display: false }, ticks: { color: textColor, maxTicksLimit: 8 } },
              y: { beginAtZero: true, grid: { color: gridColor }, ticks: { color: textColor, precision: 0 } },
            },
          },
        });
      } else {
        chart.data.labels = data.dates;
        chart.data.datasets[0].data = data.counts;
        chart.update();
      }
    } catch (e) {
      console.error(e);
    }
  }
  updateChart();
  setInterval(updateChart, 20000);

  // Redraw chart colors when theme changes
  document.querySelectorAll("[data-theme-choice]").forEach((btn) => {
    btn.addEventListener("click", () => setTimeout(() => { chart && chart.destroy(); chart = null; updateChart(); }, 50));
  });
});
