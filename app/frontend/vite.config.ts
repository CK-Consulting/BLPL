import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The backend runs in the KiCad container; the dev server proxies to it so
    // the frontend has a single origin in dev and in prod.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
