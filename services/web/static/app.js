// DevToolkit web UI. Every page works without this script: forms post and reload, results
// render server-side. This file upgrades the experience: theme toggle, code editors, in-place
// results, keyboard shortcut, drag-and-drop, example loading, copy and download.
(() => {
  "use strict";

  // --- theme --------------------------------------------------------------------------
  const toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) {
    const root = document.documentElement;
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
    const current = () => root.dataset.theme || (systemDark.matches ? "dark" : "light");
    const render = () => {
      const next = current() === "dark" ? "light" : "dark";
      toggle.setAttribute("aria-label", `Switch to ${next} theme`);
      toggle.querySelectorAll("[data-theme-icon]").forEach((icon) => {
        icon.hidden = icon.dataset.themeIcon !== next;
      });
    };
    toggle.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem("theme", next);
      } catch {
        // Storage blocked (private browsing): the choice lasts for this page only.
      }
      render();
    });
    systemDark.addEventListener("change", render);
    render();
    toggle.hidden = false;
  }

  // --- editors ------------------------------------------------------------------------
  const Editor = window.DevToolkitEditor;
  // The input editor of the page (there is at most one), with its hidden textarea kept in
  // sync so plain form posts still carry the text.
  let inputEditor = null;

  function mountInputEditor(container) {
    const source = container.querySelector("[data-editor-source]");
    if (!Editor || !source) return;
    const editor = Editor.create(container, {
      doc: source.value,
      placeholder: container.dataset.placeholder || "",
      onChange: (text) => {
        source.value = text;
        editor.clearErrors();
      },
    });
    source.hidden = true;
    source.removeAttribute("required");
    inputEditor = editor;

    // Drag-and-drop a file onto the editor.
    const setDrag = (on) => (container.dataset.dragover = on ? "true" : "false");
    container.addEventListener("dragover", (event) => {
      event.preventDefault();
      setDrag(true);
    });
    container.addEventListener("dragleave", () => setDrag(false));
    container.addEventListener("drop", async (event) => {
      event.preventDefault();
      setDrag(false);
      const file = event.dataTransfer?.files?.[0];
      if (file) editor.setValue(await file.text());
    });
  }

  // Result panels: swap each <pre> for a read-only editor with highlighting.
  function mountOutputEditors(scope) {
    if (!Editor) return;
    scope.querySelectorAll("[data-file-content]").forEach((pre) => {
      if (pre.dataset.mounted) return;
      const holder = document.createElement("div");
      holder.className = "editor";
      Editor.create(holder, { doc: pre.textContent, readOnly: true });
      pre.dataset.mounted = "true";
      pre.hidden = true; // kept for Copy and Download, which read its text
      pre.insertAdjacentElement("afterend", holder);
    });
  }

  // File tabs: show one panel at a time.
  function mountFileTabs(scope) {
    const tabs = [...scope.querySelectorAll("[data-file-tab]")];
    const panels = [...scope.querySelectorAll("[data-file-panel]")];
    if (!tabs.length) return;
    const select = (index) => {
      tabs.forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.fileTab === String(index))));
      panels.forEach((panel) => (panel.hidden = panel.dataset.filePanel !== String(index)));
    };
    tabs.forEach((tab) => {
      tab.addEventListener("click", (event) => {
        event.preventDefault();
        select(tab.dataset.fileTab);
      });
    });
    select(0);
  }

  // Marks the error position from a result in the input editor.
  function showError(scope) {
    const error = scope.querySelector("[data-error]");
    if (!inputEditor) return;
    if (error && error.dataset.errorLine) {
      inputEditor.setError(
        Number(error.dataset.errorLine),
        Number(error.dataset.errorColumn || 1),
        error.textContent.trim(),
      );
    } else {
      inputEditor.clearErrors();
    }
  }

  function enhanceResult(scope) {
    mountFileTabs(scope);
    mountOutputEditors(scope);
    showError(scope);
  }

  // --- forms: submit without a reload -------------------------------------------------
  const form = document.querySelector("[data-tool-form]");
  if (form) {
    const target = document.querySelector(form.dataset.resultTarget);
    const submitButton = form.querySelector("[data-submit]");

    async function submit() {
      if (!target) return form.submit();
      const body = new FormData(form);
      submitButton?.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body,
          headers: { "X-Requested-With": "fetch" },
        });
        target.innerHTML = await response.text();
        enhanceResult(target);
        // The zip button exists only when there are files.
        const zip = form.querySelector("[data-zip]");
        if (zip) zip.hidden = !target.querySelector("[data-file-panel]");
      } catch {
        target.innerHTML =
          '<div role="alert" class="alert alert-error">The request did not go through. Check your connection and try again.</div>';
      } finally {
        submitButton?.removeAttribute("aria-busy");
      }
    }

    form.addEventListener("submit", (event) => {
      // The zip download is a normal form post (it returns a file).
      if (event.submitter && event.submitter.hasAttribute("data-zip")) return;
      event.preventDefault();
      submit();
    });
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });

    // Upload button: read the file into the editor instead of posting it.
    form.querySelectorAll("[data-file-input]").forEach((input) => {
      input.addEventListener("change", async () => {
        const file = input.files?.[0];
        if (file && inputEditor) {
          inputEditor.setValue(await file.text());
          input.value = "";
        }
      });
    });

    // Load an example without leaving the page.
    const example = form.querySelector("[data-load-example]");
    if (example) {
      example.addEventListener("click", async (event) => {
        if (!inputEditor) return;
        event.preventDefault();
        const response = await fetch(example.href, { headers: { Accept: "text/plain" } });
        if (response.ok) inputEditor.setValue(await response.text());
      });
    }

    form.querySelectorAll("[data-editor]").forEach(mountInputEditor);
    if (target) enhanceResult(target);
  }

  // --- copy and download (delegated: result markup is replaced on each run) -------------
  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const text = document.getElementById(copy.dataset.copy).textContent;
      const label = copy.innerHTML;
      try {
        await navigator.clipboard.writeText(text);
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Copy failed";
      }
      setTimeout(() => (copy.innerHTML = label), 1500);
    }
    const download = event.target.closest("[data-download]");
    if (download) {
      const text = document.getElementById(download.dataset.download).textContent;
      const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
      const link = Object.assign(document.createElement("a"), { href: url, download: download.dataset.filename });
      link.click();
      URL.revokeObjectURL(url);
    }
  });
})();
