import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
