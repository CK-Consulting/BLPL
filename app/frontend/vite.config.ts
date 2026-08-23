/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Tests run in jsdom because most of what is worth testing here is behaviour
  // that only exists in a DOM: whether a control is disabled, whether a folded
  // group hides its contents, whether an error is rendered where somebody will
  // see it. Pure functions get tested too, but they were never the risk.
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
  server: {
    port: 5173,
    // The dev server proxies /api so the frontend has a single origin in dev and
    // in prod — the session cookie then behaves identically in both.
    //
    // 7878 is `blpl serve`'s default port. Point BLPL_API elsewhere to develop
    // against a backend running somewhere else (e.g. the container on :1800, or
    // a bare uvicorn on :8000).
    proxy: { "/api": process.env.BLPL_API || "http://127.0.0.1:7878" },
  },
});
