"use strict";

/** "Export Folder" field shared by the importer pages: per-category default, Browse/Default buttons,
 *  a live status line and a remembered custom folder per category. Requires folder-picker.js. */
window.ExportFolder = {
  setup(opts) {
    const $ = (id) => document.getElementById(id);
    const input = $(opts.input);
    const status = $(opts.status);
    let defaults = {};
    let allowed = false;
    let shownCategory = null;
    let timer = null;
    let ticket = 0;

    const storageKey = () => `${opts.storageKey}:${opts.category()}`;
    const fallback = () => defaults[opts.category()] || "";

    function show(text, cls) {
      status.textContent = text;
      status.className = `small folder-status ${cls || ""}`;
    }

    async function check() {
      const mine = ++ticket;
      show("Checking export folder…", "muted");
      try {
        const res = await fetch(`${opts.endpoint(opts.category())}?path=${encodeURIComponent(input.value.trim())}`,
          { cache: "no-store" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Could not check the folder (HTTP ${res.status}).`);
        if (mine !== ticket) return;
        const where = opts.layout || "here";
        show(data.exists
          ? `JSON files will be saved ${where} (${data.json_files} JSON file(s) already there).`
          : `JSON files will be saved ${where}; the folder will be created by the first import.`, "ok-text");
      } catch (error) {
        if (mine === ticket) show(error.message, "error-text");
      }
    }

    function changed() {
      const value = input.value.trim();
      if (allowed && value && value !== fallback()) localStorage.setItem(storageKey(), value);
      else localStorage.removeItem(storageKey());
      clearTimeout(timer);
      timer = setTimeout(check, 400);
    }

    input.addEventListener("input", changed);
    $(opts.browse).addEventListener("click", () => {
      FolderPicker.choose(input.value.trim() || fallback(), (path) => { input.value = path; changed(); },
        "Choose export folder");
    });
    $(opts.reset).addEventListener("click", () => { input.value = fallback(); changed(); });

    return {
      /** New per-category defaults (``{category: path}``); refills the field when the category changed. */
      update(newDefaults, folderSelection) {
        defaults = newDefaults;
        allowed = folderSelection;
        if (shownCategory === opts.category()) return;
        shownCategory = opts.category();
        input.value = (allowed && localStorage.getItem(storageKey())) || fallback();
        check();
      },
      refresh: check,
      value: () => input.value.trim(),
    };
  },
};
