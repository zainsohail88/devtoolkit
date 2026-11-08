// Theme toggle, copy and download buttons. Every page works without this script: the toggle
// stays hidden, and the result is still on the page to select by hand.

// The current theme was already applied by the inline script in <head>, before first paint.
// This only wires up the button.
const toggle = document.querySelector("[data-theme-toggle]");
if (toggle) {
  const root = document.documentElement;
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const current = () => root.dataset.theme || (systemDark.matches ? "dark" : "light");
  const render = () => {
    const next = current() === "dark" ? "light" : "dark";
    toggle.textContent = next === "dark" ? "Dark" : "Light";
    toggle.setAttribute("aria-label", `Switch to ${next} mode`);
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

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const text = document.getElementById(button.dataset.copy).textContent;
    try {
      await navigator.clipboard.writeText(text);
      const label = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => (button.textContent = label), 1500);
    } catch {
      button.textContent = "Copy failed";
    }
  });
});

document.querySelectorAll("[data-download]").forEach((button) => {
  button.addEventListener("click", () => {
    const text = document.getElementById(button.dataset.download).textContent;
    const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: button.dataset.filename });
    link.click();
    URL.revokeObjectURL(url);
  });
});
