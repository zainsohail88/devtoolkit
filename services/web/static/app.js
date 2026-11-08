// Copy and download buttons for results. The page works without this script.
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
