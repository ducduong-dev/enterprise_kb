import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    // The portal never calls retrieval-api directly — every read goes through portal-api,
    // which applies the same server-side filter (INV-1).
    proxy: { "/api": { target: "http://localhost:8008", changeOrigin: true } },
  },
});
