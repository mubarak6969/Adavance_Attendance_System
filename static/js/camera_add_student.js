document.addEventListener("DOMContentLoaded", () => {
  const captureCount = (window.CAPTURE_CONFIG && window.CAPTURE_CONFIG.captureCount) || 50;

  const infoStep = document.getElementById("infoStep");
  const captureStep = document.getElementById("captureStep");
  const doneStep = document.getElementById("doneStep");
  const step1Indicator = document.getElementById("step1Indicator");
  const step2Indicator = document.getElementById("step2Indicator");
  const step3Indicator = document.getElementById("step3Indicator");

  const studentForm = document.getElementById("studentForm");
  const video = document.getElementById("video");
  const cameraPlaceholder = document.getElementById("cameraPlaceholder");
  const captureBadge = document.getElementById("captureBadge");
  const captureBadgeText = document.getElementById("captureBadgeText");
  const captureStatus = document.getElementById("captureStatus");
  const captureProgressBar = document.getElementById("captureProgressBar");
  const captureCountBadge = document.getElementById("captureCountBadge");
  const startCaptureBtn = document.getElementById("startCaptureBtn");
  const retakeBtn = document.getElementById("retakeBtn");
  const captureHeading = document.getElementById("captureHeading");
  const doneMessage = document.getElementById("doneMessage");

  captureCountBadge.textContent = `0 / ${captureCount}`;

  let studentId = null;
  let stream = null;
  let captured = 0;
  let images = [];
  let capturing = false;

  function goToStep(step) {
    infoStep.classList.toggle("hidden", step !== 1);
    captureStep.classList.toggle("hidden", step !== 2);
    doneStep.classList.toggle("hidden", step !== 3);
    [step1Indicator, step2Indicator, step3Indicator].forEach((el, i) => {
      el.classList.toggle("active", i === step - 1);
      el.classList.toggle("done", i < step - 1);
    });
  }

  function setBadge(state, text) {
    captureBadge.classList.remove("hidden");
    captureBadge.className = `camera-overlay-badge state-${state}`;
    captureBadgeText.textContent = text;
  }

  async function enterCaptureStep(name) {
    goToStep(2);
    if (name) captureHeading.textContent = `Capture photos — ${name}`;
  }

  // ---- Step 1: save info ----
  studentForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(studentForm);
    const submitBtn = studentForm.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    try {
      const res = await apiFetch("/add_student", { method: "POST", body: fd });
      const j = await res.json();
      if (!res.ok) {
        showToast(j.error || "Failed to save student info.", "error");
        submitBtn.disabled = false;
        return;
      }
      studentId = j.student_id;
      showToast("Student saved. Now capture photos.", "success");
      enterCaptureStep(fd.get("name"));
    } catch (err) {
      showToast("Failed to save student info.", "error");
      submitBtn.disabled = false;
    }
  });

  // ---- Recapture flow: student_id passed in the URL ----
  const params = new URLSearchParams(window.location.search);
  const prefilledId = params.get("student_id");
  if (prefilledId) {
    (async () => {
      try {
        const res = await apiFetch(`/students/${prefilledId}`);
        if (!res.ok) throw new Error("not found");
        const s = await res.json();
        studentId = s.id;
        document.getElementById("pageTitle").textContent = `Recapture — ${s.name}`;
        enterCaptureStep(s.name);
      } catch (e) {
        showToast("Could not load that student.", "error");
      }
    })();
  }

  // ---- Step 2: camera + capture loop ----
  startCaptureBtn.addEventListener("click", async () => {
    if (!studentId) {
      showToast("Save student info first.", "error");
      return;
    }
    startCaptureBtn.disabled = true;
    setBadge("idle", "Requesting camera…");
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
      video.srcObject = stream;
      await video.play();
      cameraPlaceholder.classList.add("hidden");
      runCaptureLoop();
    } catch (err) {
      setBadge("error", "Camera unavailable");
      captureStatus.textContent = "Camera access was denied or is unavailable. Check browser permissions and try again.";
      showToast("Camera error: " + err.message, "error");
      startCaptureBtn.disabled = false;
    }
  });

  async function runCaptureLoop() {
    capturing = true;
    captured = 0;
    images = [];
    setBadge("scanning", "Capturing…");
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    const ctx = canvas.getContext("2d");

    while (captured < captureCount && capturing) {
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
      images.push(blob);
      captured++;
      captureStatus.textContent = `Capturing image ${captured} / ${captureCount}. Keep your face centered.`;
      captureCountBadge.textContent = `${captured} / ${captureCount}`;
      captureProgressBar.style.width = `${(captured / captureCount) * 100}%`;
      await new Promise((r) => setTimeout(r, 200));
    }

    if (!capturing) return;
    setBadge("recognized", "Uploading…");
    captureStatus.textContent = "Uploading captured photos…";

    const form = new FormData();
    form.append("student_id", studentId);
    form.append("csrf_token", document.querySelector('meta[name="csrf-token"]').content);
    images.forEach((b, i) => form.append("images[]", b, `img_${i}.jpg`));

    try {
      const resp = await apiFetch("/upload_face", { method: "POST", body: form });
      const j = await resp.json();
      if (resp.ok) {
        const rejectedNote = j.rejected ? ` ${j.rejected} image(s) were rejected.` : "";
        doneMessage.textContent = `${j.saved} photo(s) uploaded successfully.${rejectedNote} Train the model from the dashboard so this student can be recognized.`;
        stopCamera();
        goToStep(3);
      } else {
        setBadge("error", "Upload failed");
        showToast(j.error || "Upload failed.", "error");
      }
    } catch (e) {
      setBadge("error", "Upload failed");
      showToast("Upload failed.", "error");
    }

    startCaptureBtn.disabled = false;
    retakeBtn.classList.remove("hidden");
  }

  function stopCamera() {
    capturing = false;
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
  }

  retakeBtn.addEventListener("click", () => {
    captured = 0;
    images = [];
    captureCountBadge.textContent = `0 / ${captureCount}`;
    captureProgressBar.style.width = "0%";
    retakeBtn.classList.add("hidden");
    startCaptureBtn.disabled = false;
    startCaptureBtn.click();
  });

  window.addEventListener("beforeunload", stopCamera);
});
