document.addEventListener("DOMContentLoaded", () => {
  const pollInterval = (window.MARK_CONFIG && window.MARK_CONFIG.pollInterval) || 1500;

  const startBtn = document.getElementById("startMarkBtn");
  const stopBtn = document.getElementById("stopMarkBtn");
  const video = document.getElementById("markVideo");
  const placeholder = document.getElementById("cameraPlaceholder");
  const statusText = document.getElementById("markStatus");
  const badge = document.getElementById("markBadge");
  const badgeText = document.getElementById("markBadgeText");
  const recognizedList = document.getElementById("recognizedList");
  const recognizedEmpty = document.getElementById("recognizedEmpty");

  let stream = null;
  let timer = null;
  let busy = false;
  const seenToday = new Set(); // student ids already announced this session

  const STATE_MESSAGES = {
    idle: ["idle", "Idle"],
    scanning: ["scanning", "Recognizing…"],
    no_face: ["scanning", "No face detected"],
    multiple_faces: ["unknown", "Multiple faces detected — only one person at a time"],
    unknown: ["unknown", "Face not recognized"],
    model_not_trained: ["error", "Model not trained yet — train it from the dashboard"],
    already_marked: ["recognized", "Attendance already marked today"],
    marked: ["recognized", "Attendance marked"],
    error: ["error", "Something went wrong"],
  };

  function setBadge(status, text) {
    const [cls] = STATE_MESSAGES[status] || ["idle"];
    badge.classList.remove("hidden");
    badge.className = `camera-overlay-badge state-${cls}`;
    badgeText.textContent = text;
  }

  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    stopBtn.disabled = false;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
      video.srcObject = stream;
      await video.play();
      placeholder.classList.add("hidden");
      setBadge("scanning", "Scanning…");
      statusText.textContent = "Scanning…";
      timer = setInterval(captureAndRecognize, pollInterval);
    } catch (err) {
      setBadge("error", "Camera unavailable");
      statusText.textContent = "Camera access was denied or is unavailable.";
      showToast("Camera error: " + err.message, "error");
      startBtn.disabled = false;
      stopBtn.disabled = true;
    }
  });

  stopBtn.addEventListener("click", () => {
    if (timer) clearInterval(timer);
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    placeholder.classList.remove("hidden");
    startBtn.disabled = false;
    stopBtn.disabled = true;
    setBadge("idle", "Stopped");
    statusText.textContent = "Stopped";
  });

  async function captureAndRecognize() {
    if (busy) return; // debounce: never overlap two in-flight recognitions
    busy = true;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth || 640;
      canvas.height = video.videoHeight || 480;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.85));
      const fd = new FormData();
      fd.append("image", blob, "snap.jpg");

      const res = await fetch("/recognize_face", { method: "POST", body: fd });
      const j = await res.json();
      const status = j.status || "error";
      const [, defaultMsg] = STATE_MESSAGES[status] || [null, "Unknown state"];
      setBadge(status, defaultMsg);

      if (status === "marked" || status === "already_marked") {
        const confPct = j.confidence != null ? Math.round(j.confidence * 100) : null;
        statusText.textContent = `${j.name} — ${status === "marked" ? "attendance marked" : "already marked today"}${confPct !== null ? ` (${confPct}% confidence)` : ""}`;
        if (!seenToday.has(j.student_id)) {
          seenToday.add(j.student_id);
          addRecognizedEntry(j.name, status === "marked" ? "Marked" : "Already marked");
          if (status === "marked") showToast(`${j.name} marked present.`, "success");
        }
      } else if (status === "unknown") {
        statusText.textContent = "Face not recognized. Make sure you're registered and trained.";
      } else {
        statusText.textContent = defaultMsg;
      }
    } catch (err) {
      setBadge("error", "Connection error");
      statusText.textContent = "Could not reach the server.";
    } finally {
      busy = false;
    }
  }

  function addRecognizedEntry(name, label) {
    recognizedEmpty && recognizedEmpty.remove();
    const item = document.createElement("div");
    item.className = "recognized-item";
    const initials = name.split(" ").map((p) => p[0]).slice(0, 2).join("").toUpperCase();
    item.innerHTML = `
      <span class="avatar">${initials}</span>
      <div style="flex:1;">
        <div class="font-medium text-sm">${name}</div>
        <div class="text-xs text-muted">${label} · ${new Date().toLocaleTimeString()}</div>
      </div>`;
    recognizedList.prepend(item);
  }

  window.addEventListener("beforeunload", () => {
    if (stream) stream.getTracks().forEach((t) => t.stop());
  });
});
