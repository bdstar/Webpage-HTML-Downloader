"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "universityImportTask";
  const FOLDER_KEY = "universityImportFolder";
  const STATUS_LABELS = {
    queued: "Queued", running: "Running", pausing: "Pausing…", paused: "Paused", stopping: "Stopping…",
    stopped: "Stopped", completed: "Completed", completed_with_errors: "Completed with problems",
    failed: "Failed", interrupted: "Interrupted", empty: "Nothing to import",
  };
  const FILE_LABELS = { done: "Imported", failed: "Failed", blocked: "Blocked", skipped: "Skipped",
    pending: "Waiting", processing: "Processing" };
  const OUTCOME_LABELS = { created: "new JSON file", updated: "JSON updated", unchanged: "already up to date" };

  let overview = null;
  let taskId = sessionStorage.getItem(STORAGE_KEY);
  let lastEventId = 0;
  let pollTimer = null;
  let folderTimer = null;
  let folderCheck = 0;
  let taskActive = false;
  const exportField = ExportFolder.setup({
    input: "export-folder", browse: "export-browse-btn", reset: "export-reset-btn", status: "export-status",
    storageKey: "universityExportFolder", category: () => $("category").value,
    endpoint: (category) => `/api/universities/${category}/export-folder`,
    layout: "in <country>\\universities sub-folders of this folder",
  });
  const exportDefaults = () => Object.fromEntries(
    Object.entries(overview.categories).map(([key, info]) => [key, info.output_dir]));

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, cache: "no-store", ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error || `Request failed (HTTP ${response.status})`);
      error.body = body;
      throw error;
    }
    return body;
  }

  const fmtSeconds = (s) => {
    if (s == null) return "";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
  };
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const statItem = ([label, value]) => {
    const item = el("div", null, "stat");
    item.append(el("dt", label), el("dd", String(value ?? "")));
    return item;
  };

  // --- overview -----------------------------------------------------------------------------------

  async function loadOverview() {
    overview = await api("/api/universities/overview");
    const policy = $("policy");
    if (!policy.options.length) {
      for (const { value, label } of overview.policies) policy.append(new Option(label, value));
      policy.value = overview.default_policy;
      $("delete-html").checked = overview.delete_html_default;
    }
    const folder = $("folder");
    folder.dataset.default = overview.import_folder;
    if (!folder.value) {
      const saved = overview.folder_selection ? localStorage.getItem(FOLDER_KEY) : null;
      folder.value = saved || overview.import_folder;
      checkFolder();
    }
    exportField.update(exportDefaults(), overview.folder_selection);
    const box = $("datasets");
    const cards = overview.category_order.map((key) => overview.categories[key]).map((info) => {
      const card = el("div", null, "dataset");
      card.append(el("h3", info.label));
      const dl = el("dl", null, "stats");
      dl.append(...[["Universities imported", info.stats.universities],
        ["Last import", fmtTime(info.stats.last_import) || "never"]].map(statItem));
      card.append(dl, el("p", `Programme count field: ${info.count_field}`, "muted small"));
      return card;
    });
    const exported = el("div", null, "dataset");
    const dl = el("dl", null, "stats");
    dl.append(...[["JSON files", overview.export.json_files],
      ...overview.export.countries.map((c) => [c.country, c.json_files])].map(statItem));
    exported.append(el("h3", "Exported universities by country"), dl,
      el("p", `${overview.export.folder}\\<country>\\universities`, "path small"));
    box.replaceChildren(...cards, exported);
    if (!taskId && overview.current_task) attach(overview.current_task.id);
  }

  // --- import folder --------------------------------------------------------------------------------

  function folderStatus(text, cls) {
    const box = $("folder-status");
    box.textContent = text;
    box.className = `small folder-status ${cls || ""}`;
  }

  async function checkFolder() {
    const value = $("folder").value.trim();
    const ticket = ++folderCheck;
    folderStatus("Checking folder…", "muted");
    try {
      const info = await api(`/api/universities/folder?path=${encodeURIComponent(value)}`);
      if (ticket !== folderCheck) return;
      if (!info.html_files) {
        folderStatus(`No .html files found in ${info.folder}.`, "error-text");
      } else if (!info.eligible) {
        folderStatus(`${info.html_files} HTML file(s) found, but none is a university profile page.`, "error-text");
      } else {
        const extra = [info.other ? `${info.other} other HTML file(s) will be reported as skipped` : null,
          info.unreadable.length ? `${info.unreadable.length} unreadable` : null].filter(Boolean).join("; ");
        folderStatus(`${info.eligible} university page(s) ready to import` +
          (info.html_files !== info.eligible ? ` (${info.html_files} HTML files in total)` : "") +
          (extra ? ` — ${extra}.` : "."), "ok-text");
      }
    } catch (error) {
      if (ticket === folderCheck) folderStatus(error.message, "error-text");
    }
  }

  function folderChanged() {
    const value = $("folder").value.trim();
    if (overview && overview.folder_selection) {
      if (value && value !== $("folder").dataset.default) localStorage.setItem(FOLDER_KEY, value);
      else localStorage.removeItem(FOLDER_KEY);
    }
    clearTimeout(folderTimer);
    folderTimer = setTimeout(checkFolder, 400);
  }

  // --- task view --------------------------------------------------------------------------------------

  function attach(id) {
    taskId = id;
    lastEventId = 0;
    sessionStorage.setItem(STORAGE_KEY, id);
    $("events").replaceChildren();
    $("task-card").hidden = false;
    $("log-json").href = `/api/universities/tasks/${id}/log`;
    $("log-csv").href = `/api/universities/tasks/${id}/log?format=csv`;
    refreshTask();
  }

  function schedule(active) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshTask, active ? 1000 : 5000);
  }

  async function refreshTask() {
    if (!taskId) return;
    let task;
    try {
      task = (await api(`/api/universities/tasks/${taskId}`)).task;
    } catch (error) {
      if (/not found/i.test(error.message)) {
        sessionStorage.removeItem(STORAGE_KEY);
        taskId = null;
        $("task-card").hidden = true;
        return;
      }
      schedule(true);
      return;
    }
    renderTask(task);
    await Promise.all([loadEvents(), loadFiles()]);
    const wasActive = taskActive;
    taskActive = task.is_active;
    $("start-btn").disabled = taskActive;
    if (wasActive && !task.is_active) { loadOverview(); checkFolder(); exportField.refresh(); }
    schedule(task.is_active);
  }

  function renderTask(task) {
    $("task-title").textContent = `${task.label} university import`;
    const badge = $("task-badge");
    badge.textContent = STATUS_LABELS[task.status] || task.status;
    badge.className = `badge ${task.status}`;
    $("task-stage").textContent = task.folder;
    const track = $("task-track");
    track.className = "progress-track";
    if (task.status === "completed") track.classList.add("completed");
    if (["failed", "completed_with_errors"].includes(task.status)) track.classList.add("failed");
    if (task.stage === "scanning") track.classList.add("indeterminate");
    $("task-fill").style.width = `${task.percent}%`;
    track.setAttribute("aria-valuenow", String(task.percent));
    $("task-percent").textContent = `${task.files_processed} / ${task.files_total} HTML files processed — ${task.percent}%`;
    $("task-timing").textContent = `Elapsed ${fmtSeconds(task.elapsed_seconds)}` +
      (task.eta_seconds != null && task.is_active ? ` · about ${fmtSeconds(task.eta_seconds)} left` : "");
    const current = task.current_file
      ? `Current file: ${task.current_file}` + (task.current_university ? ` — ${task.current_university}` : "") +
        (task.file_stage ? ` (${task.file_stage})` : "")
      : task.last_action || "";
    $("task-action").textContent = current;
    $("task-success").textContent = task.last_success ? `Last successful import: ${task.last_success}` : "";
    $("task-error").hidden = !task.last_error;
    $("task-error").textContent = task.last_error ? `Last error: ${task.last_error}` : "";

    const c = task.file_counts;
    const stats = [
      ["Eligible HTML files", `${task.eligible_total} of ${task.files_total}`],
      ["Files processed", task.files_processed], ["Files remaining", task.files_pending],
      ["Imported (complete)", task.imported_clean], ["Imported with missing fields", task.with_warnings],
      ["Failed", c.failed || 0], ["Blocked (re-save needed)", c.blocked || 0], ["Skipped", c.skipped || 0],
      ["New JSON files", task.universities_created], ["JSON files updated", task.universities_updated],
      ["Already up to date", task.universities_unchanged],
      ["Delete HTML after import", task.delete_html ? "Enabled" : "Disabled"],
      ["HTML files deleted", task.html_deleted], ["Existing JSON files", task.policy_label],
      ["Retries", task.retries], ["Output folder", task.output_dir],
    ];
    $("task-stats").replaceChildren(...stats.map(statItem));
    for (const button of document.querySelectorAll("[data-action]")) {
      button.disabled = !task[`can_${button.dataset.action}`];
    }
  }

  async function loadEvents() {
    const data = await api(`/api/universities/tasks/${taskId}/events?after=${lastEventId}`);
    const list = $("events");
    for (const event of data.events) {
      lastEventId = event.id;
      const li = el("li", null, `event ${event.level}`);
      li.append(el("time", new Date(event.ts).toLocaleTimeString()), el("span", event.message));
      list.prepend(li);
    }
    while (list.children.length > 300) list.lastChild.remove();
  }

  async function loadFiles() {
    if (!$("files-box").open) return;
    const data = await api(`/api/universities/tasks/${taskId}/files`);
    $("files-table").tBodies[0].replaceChildren(...data.files.map((f) => {
      const tr = el("tr", null, `file ${f.status}`);
      const details = [f.reason, OUTCOME_LABELS[f.outcome], f.output_path,
        f.deleted ? "HTML deleted" : null].filter(Boolean).join(" · ");
      tr.append(el("td", f.name, "path"), el("td", f.university || ""), el("td", FILE_LABELS[f.status] || f.status),
        el("td", (f.missing || []).join(", ")), el("td", details, "path"));
      return tr;
    }));
  }

  async function control(action) {
    try {
      renderTask((await api(`/api/universities/tasks/${taskId}/${action}`, { method: "POST" })).task);
    } catch (error) {
      alert(error.message);
    }
    refreshTask();
  }

  // --- history ------------------------------------------------------------------------------------------

  async function loadHistory() {
    const data = await api("/api/universities/tasks?limit=100");
    $("history-body").replaceChildren(...data.tasks.map((t) => {
      const tr = el("tr");
      const open = el("button", "Open", "btn secondary small-btn");
      open.type = "button";
      open.addEventListener("click", () => { attach(t.id); window.scrollTo({ top: 0, behavior: "smooth" }); });
      const cell = el("td");
      cell.append(open);
      tr.append(el("td", fmtTime(t.created_at)), el("td", t.label), el("td", STATUS_LABELS[t.status] || t.status),
        el("td", `${t.files_done} imported, ${t.files_failed} failed, ${t.files_skipped} skipped of ${t.files_total}`),
        el("td", `${t.universities_created} new, ${t.universities_updated} updated, ${t.with_warnings} with missing fields`),
        cell);
      return tr;
    }));
    $("history-card").hidden = false;
  }

  // --- events ---------------------------------------------------------------------------------------------

  $("start-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("start-btn").disabled) return;
    $("form-error").hidden = true;
    $("start-btn").disabled = true;
    try {
      const data = await api("/api/universities/tasks", {
        method: "POST",
        body: JSON.stringify({ category: $("category").value, folder: $("folder").value.trim(),
          export_folder: exportField.value(), policy: $("policy").value, delete_html: $("delete-html").checked }),
      });
      taskActive = true;
      attach(data.task.id);
    } catch (error) {
      $("form-error").textContent = error.message;
      $("form-error").hidden = false;
      if (error.body && error.body.task) attach(error.body.task.id);
      $("start-btn").disabled = taskActive;
    }
  });

  $("folder").addEventListener("input", folderChanged);
  $("category").addEventListener("change", () => { if (overview) exportField.update(exportDefaults(), overview.folder_selection); });
  $("browse-btn").addEventListener("click", () => {
    FolderPicker.choose($("folder").value.trim() || $("folder").dataset.default, (path) => {
      $("folder").value = path;
      folderChanged();
    }, "Choose import folder");
  });
  $("reset-folder-btn").addEventListener("click", () => {
    $("folder").value = $("folder").dataset.default || "";
    folderChanged();
  });
  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => control(button.dataset.action));
  }
  $("refresh-btn").addEventListener("click", () => { refreshTask(); loadOverview(); checkFolder(); exportField.refresh(); });
  $("history-btn").addEventListener("click", loadHistory);
  $("files-box").addEventListener("toggle", loadFiles);

  loadOverview().catch((error) => {
    $("form-error").textContent = `Could not load the page: ${error.message}`;
    $("form-error").hidden = false;
  });
  if (taskId) attach(taskId);
})();
