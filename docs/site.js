"use strict";

const copyButtons = [...document.querySelectorAll("[data-copy]")];
const resetTimers = new WeakMap();

function selectSource(source) {
  if (source instanceof HTMLTextAreaElement || source instanceof HTMLInputElement) {
    source.focus({ preventScroll: true });
    source.select();
    source.setSelectionRange(0, source.value.length);
    return;
  }
  const selection = window.getSelection();
  if (!selection) return;
  const range = document.createRange();
  range.selectNodeContents(source);
  selection.removeAllRanges();
  selection.addRange(range);
}

function legacyCopy(text) {
  const field = document.createElement("textarea");
  field.value = text;
  field.readOnly = true;
  field.tabIndex = -1;
  field.setAttribute("aria-label", "Temporary clipboard text");
  Object.assign(field.style, { position: "fixed", top: "0", left: "-9999px", opacity: "0", fontSize: "16px" });
  document.body.append(field);
  const focused = document.activeElement;
  try {
    field.focus({ preventScroll: true });
    field.select();
    field.setSelectionRange(0, text.length);
    return typeof document.execCommand === "function" && document.execCommand("copy");
  } catch {
    return false;
  } finally {
    field.remove();
    if (focused instanceof HTMLElement) focused.focus({ preventScroll: true });
  }
}

function showResult(button, copied) {
  clearTimeout(resetTimers.get(button));
  button.textContent = copied ? "Copied ✓" : "Select to copy";
  resetTimers.set(button, setTimeout(() => {
    button.innerHTML = button.dataset.originalLabel;
  }, 3500));
}

for (const button of copyButtons) {
  const source = document.getElementById(button.dataset.copy);
  const status = document.getElementById(button.dataset.copyStatus);
  if (!source || !status) continue;
  button.dataset.originalLabel = button.innerHTML;
  button.hidden = false;
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    const text = ("value" in source ? source.value : source.textContent).trim();
    const label = button.dataset.copyLabel || "Text";
    let copied = false;
    try {
      if (navigator.clipboard?.writeText && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          copied = true;
        } catch {
          // Browser permissions may deny the modern API; manual copy remains.
        }
      }
      if (!copied) copied = legacyCopy(text);
      for (const peer of copyButtons.filter(candidate => candidate.dataset.copy === button.dataset.copy)) {
        showResult(peer, copied);
      }
      if (copied) {
        status.textContent = `${label} copied. Paste it into ${label === "Agent prompt" ? "your AI agent" : "your terminal"}.`;
      } else {
        selectSource(source);
        status.textContent = `Automatic copy is unavailable. The ${label.toLowerCase()} is selected; use your device's Copy command.`;
      }
    } finally {
      button.disabled = false;
    }
  });
}
