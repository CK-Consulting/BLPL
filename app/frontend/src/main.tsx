import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

// Kill the browser's "Backspace navigates back" behaviour when focus is not in a
// text field. In the board viewer or on a button, a stray backspace would throw
// away the whole session and bounce you off the page — a real hazard in a
// long-running design tool. We only ever suppress it *outside* editable fields,
// so typing (and deleting) in the markdown editor and every input is untouched.
function isTextEntry(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === "TEXTAREA") return true;
  if (el.tagName === "INPUT") {
    const type = (el.getAttribute("type") || "text").toLowerCase();
    // The input types where Backspace deletes characters. Buttons, checkboxes,
    // radios and the like are not text entry, so Backspace on them must not
    // navigate either.
    return [
      "text", "password", "search", "email", "url", "tel", "number",
      "date", "datetime-local", "month", "week", "time",
    ].includes(type);
  }
  return false;
}

window.addEventListener("keydown", (e) => {
  if (e.key === "Backspace" && !isTextEntry(e.target)) {
    e.preventDefault();
  }
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
