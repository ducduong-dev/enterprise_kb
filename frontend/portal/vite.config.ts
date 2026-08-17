import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    // The portal never calls retrieval-api directly — every read goes through portal-api,
    // which applies the same server-side filter (INV-1). Chat is the exception, and the same
    // exception nginx.conf makes in the built image: chat-api is its own front door, so the dev
    // server must split /api the same way or dev and the image disagree about a route.
    proxy: {
      "/api/v1/chat": {
        target: "http://localhost:8007",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/api": { target: "http://localhost:8008", changeOrigin: true },
    },
  },
});
