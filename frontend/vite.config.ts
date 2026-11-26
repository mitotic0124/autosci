import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("reactflow") || id.includes("@reactflow")) return "workflow";
          if (id.includes("katex") || id.includes("remark") || id.includes("rehype") || id.includes("react-markdown")) {
            return "markdown";
          }
          if (id.includes("lucide-react")) return "icons";
          return undefined;
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/artifacts": "http://localhost:8000",
    },
  },
});
