import { defineConfig } from "vite";
import solid from "vite-plugin-solid";

export default defineConfig({
  plugins: [solid()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7878",
        changeOrigin: false,
      },
    },
  },
  build: {
    // Output into ../blpl/webapp/static so FastAPI can mount it.
    outDir: "../blpl/webapp/static",
    emptyOutDir: true,
  },
});
