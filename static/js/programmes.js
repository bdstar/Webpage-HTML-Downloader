"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "programmeImportTask";
  const STATUS_LABELS = {
    queued: "Queued", running: "Running", pausing: "Pausing…", paused: "Paused", stopping: "Stopping…",
    stopped: "Stopped", completed: "Completed", completed_with_errors: "Completed with problems",
    failed: "Failed", interrupted: "Interrupted", empty: "Nothing to import",
  };
  const FILE_LABELS = { done: "Imported", failed: "Failed", blocked: "Blocked", skipped: "Skipped",
    pending: "Waiting", processing: "Processing" };

  let overview = null;
  let taskId = sessionStorage.getItem(STORAGE_KEY);
  let lastEventId = 0;
  let pollTimer = null;
  const exportField = ExportFolder.setup({
    input: "export-folder", browse: "export-browse-btn", reset: "export-reset-btn", status: "export-status",
    storageKey: "programmeExportFolder", category: () => $("category").value,
    endpoint: (key) => `/api/programmes/${key}/export-folder`,
  });

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" }, ...options,
    });
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
  const link = (href, text) => {
    const a = el("a", text);
    a.href = href; a.target = "_blank"; a.rel = "noopener";
    return a;
  };
  const category = () => $("category").value;
  const statItem = ([label, value]) => {
    const item = el("div", null, "stat");
    item.append(el("dt", label), el("dd", String(value)));
    return item;
  };

  // --- overview and datasets -------------------------------------------------------------------

  async function loadOverview() {
    overview = await api("/api/programmes/overview");
    const box = $("datasets");
    box.replaceChildren();
    for (const key of ["master", "bachelor"]) {
      const info = overview.categories[key];
      const s = info.stats;
      const card = el("div", null, "dataset");
      card.append(el("h3", info.label));
      const dl = el("dl", null, "stats");
      const rows = [
        ["Programmes", s.programmes], ["With programme page", s.complete],
        ["Listing data only", s.listing_only], ["Programme page only", s.detail_only],
        ["Universities (JSON files)", s.universities],
        ["Listing pages imported", s.listing_pages_total ? `${s.listing_pages_imported} of ${s.listing_pages_total}` : s.listing_pages_imported],
      ];
      dl.append(...rows.map(([label, value]) => statItem([label, value ?? 0])));
      card.append(dl, el("p", `Import folder: ${info.import_folder}`, "path small"),
        el("p", `Output: ${info.output_dir}`, "path small"));
      box.append(card);
      if (key === category() && !$("folder").dataset.touched) $("folder").value = info.import_folder;
    }
    exportField.update(Object.fromEntries(Object.entries(overview.categories).map(([key, info]) => [key, info.output_dir])),
      overview.folder_selection);
    if (!taskId) {
      const current = Object.values(overview.categories).map((c) => c.current_task).find(Boolean);
      if (current) attach(current.id);
    }
    $("delete-html").checked = overview.delete_html_default;
    updateTodo();
  }

  async function updateTodo() {
    if (!overview) return;
    const info = overview.categories[category()];
    $("todo-label").textContent = info.label;
    if (!$("base-url").dataset.touched) $("base-url").value = info.base_listing_url;
    if (!$("max-page").dataset.touched) $("max-page").value = info.stats.listing_pages_total || "";
    await Promise.all([loadPages(), loadMissing()]);
  }

  function pageQuery() {
    const params = new URLSearchParams({ base_url: $("base-url").value.trim() });
    if ($("max-page").value) params.set("max_page", $("max-page").value);
    return params;
  }

  async function loadPages() {
    $("pages-error").hidden = true;
    try {
      const data = await api(`/api/programmes/${category()}/listing-pages?${pageQuery()}`);
      $("pages-count").textContent = `(${data.missing_count} of ${data.max_page} still to save)`;
      $("pages-list").replaceChildren(...data.pages.map((p) => {
        const li = el("li"); li.append(link(p.url, `Page ${p.page}`)); return li;
      }));
      const params = pageQuery(); params.set("format", "txt");
      $("pages-export").href = `/api/programmes/${category()}/listing-pages?${params}`;
    } catch (error) {
      $("pages-error").textContent = error.message;
      $("pages-error").hidden = false;
    }
  }

  async function loadMissing() {
    const data = await api(`/api/programmes/${category()}/missing-details?limit=20`);
    $("missing-count").textContent = `(${data.count})`;
    $("missing-list").replaceChildren(...data.programmes.map((p) => {
      const li = el("li");
      li.append(link(p.program_url, `${p.program_name || p.program_url}`));
      if (p.university_name) li.append(el("span", ` — ${p.university_name}`, "muted"));
      return li;
    }));
    $("missing-export").href = `/api/programmes/${category()}/missing-details?format=txt`;
  }

  // --- task view ---------------------------------------------------------------------------------

  function attach(id) {
    taskId = id;
    lastEventId = 0;
    sessionStorage.setItem(STORAGE_KEY, id);
    $("events").replaceChildren();
    $("task-card").hidden = false;
    $("log-json").href = `/api/programmes/tasks/${id}/log`;
    $("log-csv").href = `/api/programmes/tasks/${id}/log?format=csv`;
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
      task = (await api(`/api/programmes/tasks/${taskId}`)).task;
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
    const wasActive = $("task-card").dataset.active === "true";
    $("task-card").dataset.active = String(task.is_active);
    if (wasActive && !task.is_active) { loadOverview(); exportField.refresh(); }
    schedule(task.is_active);
  }

  function renderTask(task) {
    $("task-title").textContent = `${task.label} import`;
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
    $("task-percent").textContent = `${task.percent}% — ${task.files_total - task.files_pending} of ${task.files_total} files`;
    $("task-timing").textContent = `Elapsed ${fmtSeconds(task.elapsed_seconds)}` +
      (task.eta_seconds != null && task.is_active ? ` · about ${fmtSeconds(task.eta_seconds)} left` : "");
    $("task-action").textContent = task.current_file ? `Current file: ${task.current_file}` : (task.last_action || "");
    $("task-error").hidden = !task.last_error;
    $("task-error").textContent = task.last_error ? `Last error: ${task.last_error}` : "";

    const c = task.file_counts;
    const stats = [
      ["Listing pages", `${task.listing_done} of ${task.listing_total}`],
      ["Programme pages", `${task.detail_done} of ${task.detail_total}`],
      ["Programme cards read", task.cards_found],
      ["Programmes added", task.programmes_created], ["Programmes updated", task.programmes_updated],
      ["JSON files written", task.json_files_written],
      ["Failed", c.failed || 0], ["Blocked (re-save needed)", c.blocked || 0], ["Skipped", c.skipped || 0],
      ["HTML files deleted", task.html_deleted], ["Retries", task.retries],
      ["Output folder", task.output_dir],
    ];
    $("task-stats").replaceChildren(...stats.map(statItem));

    for (const button of document.querySelectorAll("[data-action]")) {
      button.disabled = !task[`can_${button.dataset.action}`];
    }
  }

  async function loadEvents() {
    const data = await api(`/api/programmes/tasks/${taskId}/events?after=${lastEventId}`);
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
    const data = await api(`/api/programmes/tasks/${taskId}/files`);
    $("files-table").tBodies[0].replaceChildren(...data.files.map((f) => {
      const tr = el("tr", null, `file ${f.status}`);
      const details = [f.reason, f.page_number ? `page ${f.page_number}` : null,
        f.cards ? `${f.cards} cards` : null, f.deleted ? "HTML deleted" : null].filter(Boolean).join(" · ");
      tr.append(el("td", f.name, "path"), el("td", f.kind || "?"), el("td", FILE_LABELS[f.status] || f.status),
        el("td", details));
      return tr;
    }));
  }

  async function control(action) {
    try {
      renderTask((await api(`/api/programmes/tasks/${taskId}/${action}`, { method: "POST" })).task);
    } catch (error) {
      alert(error.message);
    }
    refreshTask();
  }

  // --- history ------------------------------------------------------------------------------------

  async function loadHistory() {
    const data = await api("/api/programmes/tasks?limit=100");
    $("history-body").replaceChildren(...data.tasks.map((t) => {
      const tr = el("tr");
      const open = el("button", "Open", "btn secondary small-btn");
      open.type = "button";
      open.addEventListener("click", () => { attach(t.id); window.scrollTo({ top: 0, behavior: "smooth" }); });
      const files = `${t.files_done} imported, ${t.files_failed} failed, ${t.files_blocked} blocked of ${t.files_total}`;
      const cell = el("td"); cell.append(open);
      tr.append(el("td", fmtTime(t.created_at)), el("td", t.label),
        el("td", STATUS_LABELS[t.status] || t.status), el("td", files),
        el("td", `${t.programmes_created} new, ${t.programmes_updated} updated`), cell);
      return tr;
    }));
    $("history-card").hidden = false;
  }

  // --- events -------------------------------------------------------------------------------------

  $("start-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("form-error").hidden = true;
    $("start-btn").disabled = true;
    try {
      const data = await api("/api/programmes/tasks", {
        method: "POST",
        body: JSON.stringify({ category: category(), folder: $("folder").value, export_folder: exportField.value(),
          delete_html: $("delete-html").checked }),
      });
      attach(data.task.id);
    } catch (error) {
      $("form-error").textContent = error.message;
      $("form-error").hidden = false;
      if (error.body && error.body.task) attach(error.body.task.id);
    } finally {
      $("start-btn").disabled = false;
    }
  });

  $("category").addEventListener("change", () => {
    delete $("folder").dataset.touched;
    delete $("base-url").dataset.touched;
    delete $("max-page").dataset.touched;
    loadOverview();
  });
  for (const id of ["folder", "base-url", "max-page"]) {
    $(id).addEventListener("input", () => { $(id).dataset.touched = "1"; });
  }
  $("browse-btn").addEventListener("click", () => {
    FolderPicker.choose($("folder").value.trim(), (path) => {
      $("folder").value = path;
      $("folder").dataset.touched = "1";
    }, "Choose import folder");
  });
  $("reset-folder-btn").addEventListener("click", () => {
    delete $("folder").dataset.touched;
    if (overview) $("folder").value = overview.categories[category()].import_folder;
  });
  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => control(button.dataset.action));
  }
  $("refresh-btn").addEventListener("click", () => { refreshTask(); loadOverview(); exportField.refresh(); });
  $("history-btn").addEventListener("click", loadHistory);
  $("pages-btn").addEventListener("click", loadPages);
  $("files-box").addEventListener("toggle", loadFiles);

  loadOverview().catch((error) => {
    $("form-error").textContent = `Could not load the dashboard: ${error.message}`;
    $("form-error").hidden = false;
  });
  if (taskId) attach(taskId);
})();
