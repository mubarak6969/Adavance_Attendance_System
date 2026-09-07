document.addEventListener("DOMContentLoaded", () => {
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  let chart = null;
  const buttons = document.querySelectorAll("[data-days]");

  async function loadChart(days) {
    const res = await apiFetch(`/attendance_stats?days=${days}`);
    const data = await res.json();
    const ctx = document.getElementById("trendChart").getContext("2d");
    const lineColor = cssVar("--chart-line") || "#4338ca";
    const fillColor = cssVar("--chart-fill") || "rgba(67,56,202,0.14)";
    const gridColor = cssVar("--border") || "#e4e6f0";
    const textColor = cssVar("--text-muted") || "#8a8fa3";

    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.dates,
        datasets: [{ label: "Attendance", data: data.counts, backgroundColor: fillColor, borderColor: lineColor, borderWidth: 1, borderRadius: 4 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, ticks: { color: textColor, maxTicksLimit: 12 } },
          y: { beginAtZero: true, grid: { color: gridColor }, ticks: { color: textColor, precision: 0 } },
        },
      },
    });
  }

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.replace("btn-primary", "btn-secondary"));
      btn.classList.replace("btn-secondary", "btn-primary");
      loadChart(btn.dataset.days);
    });
  });

  loadChart(30);
});
