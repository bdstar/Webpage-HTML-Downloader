(() => {
  "use strict";

  const STORAGE_KEY = "html-downloader-active-job";
  const POLL_INTERVAL_MS = 1000;
  const FINAL = new Set(["completed", "failed", "cancelled"]);

  const $ = (id) => document.getElementById(id);
  const form = $("download-form");
  const urlInput = $("url-input");
  const downloadBtn = $("download-btn");
  const fieldError = $("field-error");
  const progressCard = $("progress-card");
  const badge = $("status-badge");
  const statusMessage = $("status-message");
  const track = $("progress-track");
  const fill = $("progress-fill");
  const percentText = $("progress-percent");
  const bytesText = $("progress-bytes");
  const jobUrl = $("job-url");
  const cancelBtn = $("cancel-btn");
  const errorCard = $("error-card");
  const errorMessage = $("error-message");
  const resultCard = $("result-card");
  const saveBtn = $("save-btn");

  let currentJobId = null;
  let eventSource = null;
  let pollTimer = null;
  let lastVersion = -1;

  function formatBytes(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return `${n} bytes`;
    const units = ["KB", "MB", "GB"];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(1)} ${units[i]}`;
  }

  function showFieldError(msg) {
    fieldError.textContent = msg;
    fieldError.hidden = !msg;
  }

  function setBusy(busy) {
    downloadBtn.disabled = busy;
    urlInput.readOnly = busy;
    downloadBtn.textContent = busy ? "Downloading..." : "Download";
    cancelBtn.hidden = !busy;
    cancelBtn.disabled = false;
  }

  function stopUpdates() {
    if (eventSource) { eventSource.close(); eventSource = null; }
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function render(job) {
    if (job.id !== currentJobId || job.version < lastVersion) return;
    lastVersion = job.version;

    progressCard.hidden = false;
    badge.textContent = job.stage_label;
    badge.className = "badge " + (FINAL.has(job.status) ? job.status : "");
    statusMessage.textContent = job.message || "";
    jobUrl.textContent = (job.mode === "rendered" ? "Rendered DOM: " : "Raw HTML: ") + job.url;

    track.classList.remove("indeterminate", "completed", "failed", "cancelled");
    const known = job.total_bytes !== null && job.total_bytes !== undefined && job.total_bytes > 0;

    if (job.status === "completed") {
      track.classList.add("completed");
      fill.style.width = "100%";
      track.setAttribute("aria-valuenow", "100");
      percentText.textContent = "100%";
      bytesText.textContent = formatBytes(job.file_size) + " saved" +
        (job.content_encoding && job.bytes_downloaded
          ? ` (${formatBytes(job.bytes_downloaded)} transferred, ${job.content_encoding})` : "");
    } else if (job.status === "failed" || job.status === "cancelled") {
      track.classList.add(job.status);
      if (!known) fill.style.width = "100%";
      track.removeAttribute("aria-valuenow");
      percentText.textContent = job.status === "failed" ? "Failed" : "Cancelled";
      bytesText.textContent = job.bytes_downloaded ? `${formatBytes(job.bytes_downloaded)} received (discarded)` : "";
    } else if (job.percent !== null && job.percent !== undefined) {
      fill.style.width = `${job.percent}%`;
      track.setAttribute("aria-valuenow", String(job.percent));
      percentText.textContent = `${job.percent.toFixed(1)}%`;
      bytesText.textContent = `${formatBytes(job.bytes_downloaded)} of ${formatBytes(job.total_bytes)}` +
        (job.content_encoding ? ` (${job.content_encoding}-compressed transfer)` : "");
    } else {
      // Size unknown (or not downloading yet): show an indeterminate bar, never a made-up percentage.
      track.classList.add("indeterminate");
      fill.style.width = "";
      track.removeAttribute("aria-valuenow");
      percentText.textContent = job.stage === "downloading" ? "Total size unknown" : "";
      bytesText.textContent = job.bytes_downloaded
        ? `${formatBytes(job.bytes_downloaded)} downloaded` +
          (job.content_encoding ? ` (${job.content_encoding}-compressed)` : "")
        : "";
    }

    if (job.cancel_requested && !FINAL.has(job.status)) cancelBtn.disabled = true;

    if (job.status === "failed") {
      errorMessage.textContent = job.error || "The download failed.";
      errorCard.hidden = false;
    }
    if (job.status === "completed") {
      $("result-filename").textContent = job.filename;
      $("result-size").textContent = `${formatBytes(job.file_size)} (${job.file_size.toLocaleString()} bytes)`;
      $("result-path").textContent = job.file_path;
      $("result-final-url").textContent = job.final_url || job.url;
      $("result-type").textContent = job.content_type || "not specified by server";
      saveBtn.href = `/api/jobs/${encodeURIComponent(job.id)}/file`;
      saveBtn.setAttribute("download", job.filename);
      resultCard.hidden = false;
    }

    if (FINAL.has(job.status)) {
      stopUpdates();
      setBusy(false);
      sessionStorage.removeItem(STORAGE_KEY);
    }
  }

  async function poll() {
    pollTimer = null;
    if (!currentJobId) return;
    const jobId = currentJobId;
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
      if (res.status === 404) {
        handleLostJob();
        return;
      }
      const data = await res.json();
      render(data.job);
      if (FINAL.has(data.job.status)) return;
    } catch (_) {
      statusMessage.textContent = "Connection to the app lost, retrying...";
    }
    if (currentJobId === jobId) pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
  }

  function handleLostJob() {
    stopUpdates();
    setBusy(false);
    sessionStorage.removeItem(STORAGE_KEY);
    progressCard.hidden = true;
    showFieldError("The previous download is no longer known to the server (it may have restarted).");
  }

  function subscribe(jobId) {
    stopUpdates();
    if (!("EventSource" in window)) { poll(); return; }
    eventSource = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    eventSource.addEventListener("progress", (e) => render(JSON.parse(e.data)));
    eventSource.addEventListener("gone", handleLostJob);
    eventSource.onerror = () => {
      // The stream closes normally after the final event; otherwise fall back to polling.
      if (eventSource) { eventSource.close(); eventSource = null; }
      if (currentJobId === jobId && !pollTimer) poll();
    };
  }

  function attach(job) {
    currentJobId = job.id;
    lastVersion = -1;
    sessionStorage.setItem(STORAGE_KEY, job.id);
    errorCard.hidden = true;
    resultCard.hidden = true;
    setBusy(true);
    render(job);
    if (!FINAL.has(job.status)) subscribe(job.id);
  }

  function selectedMode() {
    const checked = form.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "http";
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (downloadBtn.disabled) return;  // guard against double submission
    showFieldError("");
    const url = urlInput.value.trim();
    if (!url) {
      showFieldError("Please enter a URL, for example https://example.com.");
      urlInput.focus();
      return;
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(url) && !/^https?:\/\//i.test(url)) {
      showFieldError("Only http:// and https:// URLs are supported.");
      return;
    }

    downloadBtn.disabled = true;
    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, mode: selectedMode() }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 202) {
        attach(data.job);
      } else if (res.status === 409 && data.job) {
        attach(data.job);
        showFieldError("This URL is already being downloaded; showing the existing download.");
      } else {
        downloadBtn.disabled = false;
        showFieldError(data.error || `Request failed (HTTP ${res.status}).`);
      }
    } catch (_) {
      downloadBtn.disabled = false;
      showFieldError("Could not reach the downloader app. Is the server running?");
    }
  });

  cancelBtn.addEventListener("click", async () => {
    if (!currentJobId) return;
    cancelBtn.disabled = true;
    statusMessage.textContent = "Cancelling...";
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(currentJobId)}/cancel`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (data.job) render(data.job);
    } catch (_) {
      cancelBtn.disabled = false;
      statusMessage.textContent = "Could not send the cancel request; try again.";
    }
  });

  // Reattach to a download that was running before a page reload.
  const savedJob = sessionStorage.getItem(STORAGE_KEY);
  if (savedJob) {
    fetch(`/api/jobs/${encodeURIComponent(savedJob)}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data && data.job) attach(data.job);
        else sessionStorage.removeItem(STORAGE_KEY);
      })
      .catch(() => sessionStorage.removeItem(STORAGE_KEY));
  }
  setBusy(false);
})();
