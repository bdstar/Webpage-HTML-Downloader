"use strict";

/** Server-side folder browser (templates/_folder_dialog.html) shared by the downloader and importers. */
window.FolderPicker = (() => {
  const $ = (id) => document.getElementById(id);
  let ready = false;
  let folderPath = null;
  let folderParent = null;
  let onSelect = null;

  function showError(msg) {
    $("folder-error").textContent = msg || "";
    $("folder-error").hidden = !msg;
  }

  async function openFolder(path) {
    showError("");
    const res = await fetch(`/api/folders?path=${encodeURIComponent(path || "")}`, { cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Could not open the folder (HTTP ${res.status}).`);
    folderPath = data.path;
    folderParent = data.parent;
    $("folder-current").textContent = data.path || "Drives";
    $("folder-up").disabled = !data.path;
    $("folder-select").disabled = !data.path;
    $("folder-create").disabled = !data.path;
    const items = data.folders.map((f) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "folder-item";
      button.textContent = `📁 ${f.name}`;
      button.addEventListener("click", () => openFolder(f.path).catch((err) => showError(err.message)));
      li.append(button);
      return li;
    });
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "muted small";
      li.textContent = "No sub-folders. Use this folder or create a new one.";
      items.push(li);
    }
    $("folder-list").replaceChildren(...items);
    if (data.truncated) showError("Only the first 2000 folders are shown.");
  }

  function init() {
    if (ready) return;
    ready = true;
    $("folder-up").addEventListener("click", () => openFolder(folderParent || "").catch((err) => showError(err.message)));
    $("folder-drives").addEventListener("click", () => openFolder("").catch((err) => showError(err.message)));
    $("folder-create").addEventListener("click", async () => {
      const name = $("folder-new-name").value.trim();
      if (!name) { showError("Type a name for the new folder."); return; }
      const res = await fetch("/api/folders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent: folderPath, name }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showError(data.error || "Could not create the folder."); return; }
      $("folder-new-name").value = "";
      openFolder(data.path).catch((err) => showError(err.message));
    });
    $("folder-select").addEventListener("click", () => {
      if (!folderPath) return;
      $("folder-dialog").close();
      if (onSelect) onSelect(folderPath);
    });
  }

  /** Open the dialog at ``startPath`` (or the drive list); ``callback(path)`` runs when a folder is chosen. */
  async function choose(startPath, callback, title) {
    init();
    onSelect = callback;
    const heading = $("folder-dialog").querySelector("h2");
    heading.dataset.default = heading.dataset.default || heading.textContent;
    heading.textContent = title || heading.dataset.default;
    $("folder-dialog").showModal();
    try {
      await openFolder(startPath);
    } catch (_) {
      openFolder("").catch((err) => showError(err.message));
    }
  }

  return { choose };
})();
