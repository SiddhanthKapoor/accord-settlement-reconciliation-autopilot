import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/catalog": {
        target: "http://127.0.0.1:8100",
        rewrite: (path) => path.replace(/^\/catalog/, ""),
      },
    },
  },
});
