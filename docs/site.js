"use strict";

for (const button of document.querySelectorAll("[data-copy]")) {
  if (!navigator.clipboard || !window.isSecureContext) continue;
  button.hidden = false;
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    const status = button.closest(".install-panel").querySelector("[role='status']");
    try {
      await navigator.clipboard.writeText(source.textContent.trim());
      button.textContent = "Copied ✓";
      status.textContent = "Install commands copied to the clipboard.";
    } catch {
      status.textContent = "Copy was unavailable. Select the commands to copy them manually.";
      button.textContent = "Select to copy";
      const range = document.createRange();
      range.selectNodeContents(source);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
}
