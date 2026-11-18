import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy API + WebSocket ke FastAPI (default port 8100).
// Build: output ke dist/, dilayani FastAPI sebagai UI utama.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8100",
      "/crops": "http://127.0.0.1:8100",
      "/ws/stream": { target: "ws://127.0.0.1:8100", ws: true },
    },
  },
  build: { outDir: "dist" },
});
