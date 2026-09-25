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

// jsdom implements no media queries at all, and the navbar asks whether it has
// room before deciding between icons and a collapsed menu. Without this the
// hook throws on mount and every test that renders a header fails for a reason
// that has nothing to do with the header. Defaults to "wide", so a test that
// does not care gets the full navbar; a test that cares overrides it.
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
