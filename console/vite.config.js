import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into console/dist, which FastAPI serves at /console. In dev (`npm run dev`),
// API calls are proxied to the local Wardlock server.
export default defineConfig({
  base: "/console/",
  plugins: [react()],
  build: { chunkSizeWarningLimit: 1500 }, // AG Grid is large; the console is an internal tool
  server: { proxy: { "/admin": "http://localhost:8000" } },
});
