import "@testing-library/jest-dom/vitest";

// jsdom has no layout, so anything that measures or scrolls is a no-op rather
// than a crash. The chat panel's stick-to-the-end logic reads all three of
// these on every render.
Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
  configurable: true,
  get() {
    return 0;
  },
});
Object.defineProperty(HTMLElement.prototype, "clientHeight", {
  configurable: true,
  get() {
    return 0;
  },
});
