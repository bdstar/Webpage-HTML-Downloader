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
    fieldError.classList.remove("note-text");
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

  // --- download folder ("Browse Location") -------------------------------------------------------

  const DIR_KEY = "html-downloader-dir";
  const dirInput = $("download-dir");

  if (!dirInput.disabled && localStorage.getItem(DIR_KEY)) dirInput.value = localStorage.getItem(DIR_KEY);

  /** Folder to send with a job; empty means "the server's default folder". */
  function chosenDir() {
    if (dirInput.disabled) return "";
    const value = dirInput.value.trim();
    return value === dirInput.dataset.default ? "" : value;
  }

  function rememberDir() {
    const value = dirInput.value.trim();
    if (value && value !== dirInput.dataset.default) localStorage.setItem(DIR_KEY, value);
    else localStorage.removeItem(DIR_KEY);
  }

  $("browse-btn").addEventListener("click", () => {
    FolderPicker.choose(dirInput.value.trim() || dirInput.dataset.default, (path) => {
      dirInput.value = path;
      rememberDir();
    });
  });
  $("reset-dir-btn").addEventListener("click", () => {
    dirInput.value = dirInput.dataset.default;
    rememberDir();
  });
  dirInput.addEventListener("change", rememberDir);

  // --- URL list, .txt upload and preview/queue ---------------------------------------------------

  const BATCH_KEY = "html-downloader-batch";
  const MAX_URLS = 1000;
  const BATCH_POLL_MS = 700;
  const RETRY_BUSY_MS = 2000;
  const BUSY_STATES = new Set(["queued", "starting", "running"]);
  const batchCard = $("batch-card");
  const batchList = $("batch-list");
  const batchOverall = $("batch-overall");
  const batchCancelBtn = $("batch-cancel-btn");
  const batchClearBtn = $("batch-clear-btn");
  const fileInput = $("url-file");
  const INTERVAL_KEY = "html-downloader-interval";
  const DEFAULT_INTERVAL_S = 2;
  const MAX_INTERVAL_S = 3600;
  const intervalInput = $("download-interval");
  if (localStorage.getItem(INTERVAL_KEY) !== null) intervalInput.value = localStorage.getItem(INTERVAL_KEY);
  let batch = null;      // { source, rows: [{ url, state, jobId, percent, stage, bytes, error, filename, path }] }
  let batchTimer = null;
  let previewTimer = null;

  /** Pause between downloads in a multi-URL batch, read live so edits apply to the running batch. */
  function intervalMs() {
    const value = Number(intervalInput.value);
    if (intervalInput.value.trim() === "" || !Number.isFinite(value)) return DEFAULT_INTERVAL_S * 1000;
    return Math.min(Math.max(value, 0), MAX_INTERVAL_S) * 1000;
  }

  intervalInput.addEventListener("change", () => {
    const value = Number(intervalInput.value);
    if (intervalInput.value.trim() === "" || !Number.isFinite(value) || value < 0) {
      intervalInput.value = String(DEFAULT_INTERVAL_S);
    } else if (value > MAX_INTERVAL_S) {
      intervalInput.value = String(MAX_INTERVAL_S);
    }
    localStorage.setItem(INTERVAL_KEY, intervalInput.value);
  });

  const looksLikeHttp = (url) => !/^[a-z][a-z0-9+.-]*:/i.test(url) || /^https?:\/\//i.test(url);

  function parseUrls(text) {
    const seen = new Set();
    const urls = [];
    let duplicates = 0;
    for (const raw of text.split(/\r?\n/)) {
      const url = raw.trim();
      if (!url || url.startsWith("#")) continue;
      if (seen.has(url)) { duplicates++; continue; }
      seen.add(url);
      urls.push(url);
    }
    return { urls: urls.slice(0, MAX_URLS), duplicates, overflow: Math.max(0, urls.length - MAX_URLS) };
  }

  function rowStatus(row) {
    switch (row.state) {
      case "pending": return ["Not initiated", ""];
      case "queued": return ["Queued", ""];
      case "starting": return ["Starting…", "indeterminate"];
      case "running":
        if (row.percent !== null && row.percent !== undefined) {
          return [`Download progress (${Math.floor(row.percent)}%)`, ""];
        }
        return [`Download progress (${row.stage || "working"}${row.bytes ? `, ${formatBytes(row.bytes)}` : ""})`,
          "indeterminate"];
      case "completed": return ["Download Complete", "completed"];
      case "cancelled": return ["Cancelled", "cancelled"];
      case "invalid": return ["Skipped: only http:// and https:// URLs are supported", "failed"];
      default: return [`Failed: ${row.error || "unknown error"}`, "failed"];
    }
  }

  function buildRowElement(row, index) {
    const li = document.createElement("li");
    li.className = "batch-row";
    li.innerHTML = `<div class="row-main"><span class="row-index"></span><span class="row-url path"></span>
      <span class="row-status"></span><a class="row-save small" hidden>Save</a></div>
      <div class="progress-track mini"><div class="progress-fill"></div></div>`;
    li.querySelector(".row-index").textContent = `${index + 1}.`;
    li.querySelector(".row-url").textContent = row.url;
    return li;
  }

  function updateRow(row, li) {
    let [text, cls] = rowStatus(row);
    let stateClass = row.state;
    const waitMs = batch.nextStartAt ? batch.nextStartAt - Date.now() : 0;
    if (row.state === "queued" && waitMs > 0 && row === batch.rows.find((r) => r.state === "queued")) {
      text = `Waiting ${Math.ceil(waitMs / 1000)}s before starting`;
      stateClass = "waiting";
    }
    const status = li.querySelector(".row-status");
    status.textContent = text;
    status.className = `row-status ${stateClass}`;
    li.title = row.path ? `Saved to ${row.path}` : (row.error || "");
    const track = li.querySelector(".progress-track");
    track.className = `progress-track mini ${cls}`;
    const fillEl = track.firstElementChild;
    if (row.state === "completed" || row.state === "failed" || row.state === "cancelled" || row.state === "invalid") {
      fillEl.style.width = "100%";
    } else if (row.state === "running" && row.percent != null) {
      fillEl.style.width = `${row.percent}%`;
    } else {
      fillEl.style.width = cls === "indeterminate" ? "" : "0";
    }
    const save = li.querySelector(".row-save");
    save.hidden = row.state !== "completed";
    if (row.state === "completed") {
      save.href = `/api/jobs/${encodeURIComponent(row.jobId)}/file`;
      save.setAttribute("download", row.filename || "");
    }
  }

  function renderBatch(rebuild) {
    if (!batch) { batchCard.hidden = true; return; }
    batchCard.hidden = false;
    if (rebuild) batchList.replaceChildren(...batch.rows.map(buildRowElement));
    batch.rows.forEach((row, i) => updateRow(row, batchList.children[i]));

    const count = (s) => batch.rows.filter((r) => r.state === s).length;
    const invalid = count("invalid");
    const total = batch.rows.length - invalid;   // URLs that can actually be downloaded
    const done = count("completed");
    const failed = count("failed");
    const cancelled = count("cancelled");
    const finished = done + failed + cancelled;
    const busy = batch.rows.some((r) => BUSY_STATES.has(r.state));
    $("batch-title").textContent = `URL preview — ${batch.rows.length} URL${batch.rows.length === 1 ? "" : "s"}` +
      (batch.source ? ` from ${batch.source}` : "");
    const skipped = invalid ? ` · ${invalid} skipped (not http/https)` : "";
    $("batch-summary").textContent = (busy || finished
      ? `${done} complete, ${failed} failed${cancelled ? `, ${cancelled} cancelled` : ""}, ` +
        `${total - finished} remaining` + (batch.dir ? ` · saving to ${batch.dir}` : "")
      : "Press Download to start.") + skipped;
    const fillEl = batchOverall.firstElementChild;
    fillEl.style.width = `${total ? (finished * 100) / total : 0}%`;
    batchOverall.setAttribute("aria-valuenow", String(total ? Math.round((finished * 100) / total) : 0));
    batchOverall.className = `progress-track${!busy && finished === total && total ? (failed ? " failed" : " completed") : ""}`;
    batchCancelBtn.hidden = !busy;
    batchClearBtn.disabled = busy;
    if (!currentJobId) {
      downloadBtn.disabled = busy;
      urlInput.readOnly = busy;
      downloadBtn.textContent = busy ? "Downloading..." : (total > 1 ? `Download all (${total})` : "Download");
    }
  }

  function saveBatch() {
    if (batch) sessionStorage.setItem(BATCH_KEY, JSON.stringify(batch));
    else sessionStorage.removeItem(BATCH_KEY);
  }

  function showPreview(urls, source) {
    batch = { source, rows: urls.map((url) => ({ url, state: looksLikeHttp(url) ? "pending" : "invalid" })) };
    renderBatch(true);
    saveBatch();
  }

  function applyJob(row, job) {
    row.jobId = job.id;
    row.state = job.status;
    row.percent = job.percent;
    row.stage = job.stage_label;
    row.bytes = job.bytes_downloaded;
    row.error = job.error;
    row.filename = job.filename;
    row.path = job.file_path;
  }

  async function startRow(row) {
    row.state = "starting";
    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: row.url, mode: batch.mode, download_dir: batch.dirParam }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 202 || (res.status === 409 && data.job)) {
        applyJob(row, data.job);
      } else if (res.status === 429) {
        row.state = "queued";           // server is at its job limit; try again shortly
        row.retryAt = Date.now() + RETRY_BUSY_MS;
      } else {
        row.state = "failed";
        row.error = data.error || `Request failed (HTTP ${res.status}).`;
      }
    } catch (_) {
      row.state = "failed";
      row.error = "Could not reach the downloader app. Is the server running?";
    }
    saveBatch();
    renderBatch(false);
  }

  /** Start the next queued URL: one download at a time, after the configured pause. */
  function pump() {
    if (!batch) return;
    if (batch.rows.some((r) => r.state === "starting" || r.state === "running")) return;
    const now = Date.now();
    if (batch.nextStartAt && now < batch.nextStartAt) return;
    const next = batch.rows.find((r) => r.state === "queued" && !(r.retryAt > now));
    if (next) startRow(next);
  }

  async function pollBatch() {
    batchTimer = null;
    if (!batch) return;
    const running = batch.rows.filter((r) => r.state === "running" && r.jobId);
    await Promise.all(running.map(async (row) => {
      try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(row.jobId)}`, { cache: "no-store" });
        if (res.status === 404) {
          row.state = "failed";
          row.error = "The server no longer knows this download (it may have restarted).";
          return;
        }
        if (res.ok) {
          applyJob(row, (await res.json()).job);
          if (FINAL.has(row.state)) batch.nextStartAt = Date.now() + intervalMs();
        }
      } catch (_) { /* transient: keep polling */ }
    }));
    pump();
    saveBatch();
    renderBatch(false);
    if (batch.rows.some((r) => BUSY_STATES.has(r.state))) {
      const waitMs = batch.nextStartAt ? batch.nextStartAt - Date.now() : 0;
      const delay = waitMs > 0 ? Math.min(BATCH_POLL_MS, waitMs + 20) : BATCH_POLL_MS;
      batchTimer = setTimeout(pollBatch, delay);
    }
  }

  function startBatch(urls, source) {
    const same = batch && batch.rows.length === urls.length && batch.rows.every((r, i) => r.url === urls[i]);
    if (!same) showPreview(urls, source);
    batch.mode = selectedMode();
    batch.nextStartAt = 0;
    batch.dirParam = chosenDir();
    batch.dir = dirInput.disabled ? "" : dirInput.value.trim();
    // Start everything not yet downloaded; completed rows are kept, failed/cancelled ones are retried.
    for (const row of batch.rows) {
      if (["pending", "failed", "cancelled"].includes(row.state)) {
        Object.assign(row, { state: "queued", error: null, percent: null, bytes: 0, retryAt: 0 });
      }
    }
    errorCard.hidden = true;
    resultCard.hidden = true;
    progressCard.hidden = true;
    renderBatch(false);
    saveBatch();
    if (!batchTimer) pollBatch();
  }

  batchCancelBtn.addEventListener("click", async () => {
    if (!batch) return;
    batchCancelBtn.disabled = true;
    for (const row of batch.rows) if (row.state === "queued") row.state = "pending";
    await Promise.all(batch.rows.filter((r) => r.state === "running" && r.jobId).map((row) =>
      fetch(`/api/jobs/${encodeURIComponent(row.jobId)}/cancel`, { method: "POST" }).catch(() => null)));
    batchCancelBtn.disabled = false;
    renderBatch(false);
    saveBatch();
  });

  batchClearBtn.addEventListener("click", () => {
    batch = null;
    saveBatch();
    renderBatch(true);
    urlInput.value = "";
    downloadBtn.textContent = "Download";
    urlInput.focus();
  });

  urlInput.addEventListener("input", () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      if (batch && batch.rows.some((r) => BUSY_STATES.has(r.state))) return;
      const { urls } = parseUrls(urlInput.value);
      if (urls.length > 1) {
        showPreview(urls, null);
      } else {
        batch = null;
        saveBatch();
        renderBatch(true);
        downloadBtn.textContent = "Download";
      }
    }, 300);
  });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";  // allow choosing the same file again
    if (!file) return;
    showFieldError("");
    if (batch && batch.rows.some((r) => BUSY_STATES.has(r.state))) {
      showFieldError("Wait for the current downloads to finish (or cancel them) before loading another file.");
      return;
    }
    if (file.size > 1024 * 1024) {
      showFieldError("The .txt file is larger than 1 MB; split it into smaller files.");
      return;
    }
    const { urls, duplicates, overflow } = parseUrls(await file.text());
    if (!urls.length) {
      showFieldError(`${file.name} does not contain any URLs (one per line).`);
      return;
    }
    urlInput.value = urls.join("\n");
    showPreview(urls, file.name);
    const notes = [];
    if (duplicates) notes.push(`${duplicates} duplicate line(s) removed`);
    if (overflow) notes.push(`only the first ${MAX_URLS} URLs were loaded (${overflow} more ignored)`);
    if (notes.length) {
      showFieldError(`Note: ${notes.join("; ")}.`);
      fieldError.classList.add("note-text");
    }
  });

  // --- submit --------------------------------------------------------------------------------------

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (downloadBtn.disabled) return;  // guard against double submission
    showFieldError("");
    const { urls, overflow } = parseUrls(urlInput.value);
    if (!urls.length) {
      showFieldError("Please enter a URL, for example https://example.com.");
      urlInput.focus();
      return;
    }
    if (overflow) showFieldError(`Only the first ${MAX_URLS} URLs will be downloaded.`);
    if (urls.length > 1 || (batch && batch.source)) {
      startBatch(urls, batch && batch.source);
      return;
    }
    const url = urls[0];
    if (!looksLikeHttp(url)) {
      showFieldError("Only http:// and https:// URLs are supported.");
      return;
    }
    batch = null;
    saveBatch();
    renderBatch(true);

    downloadBtn.disabled = true;
    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, mode: selectedMode(), download_dir: chosenDir() }),
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

  // Restore a URL list/queue after a page reload; running downloads continue on the server.
  try {
    const savedBatch = JSON.parse(sessionStorage.getItem(BATCH_KEY) || "null");
    if (savedBatch && Array.isArray(savedBatch.rows)) {
      batch = savedBatch;
      for (const row of batch.rows) if (row.state === "starting") row.state = "queued";
      if (!urlInput.value) urlInput.value = batch.rows.map((r) => r.url).join("\n");
      renderBatch(true);
      if (batch.rows.some((r) => BUSY_STATES.has(r.state))) pollBatch();
    }
  } catch (_) {
    sessionStorage.removeItem(BATCH_KEY);
  }
})();
