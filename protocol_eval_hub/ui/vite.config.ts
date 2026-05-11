import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, Vite runs on 5173 and proxies API paths to the unified_eval_app process.
// Default matches `uvicorn ... --port 8010`. Override if your API runs elsewhere:
//   EVAL_HUB_URL=http://127.0.0.1:8080 npm run dev
const EVAL_HUB = process.env.EVAL_HUB_URL ?? "http://127.0.0.1:8010";

// In prod, `npm run build` emits to ./dist which FastAPI serves at "/".
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Bind IPv4 as well (default localhost can be [::1]-only; fixes some ERR_* from tools/browsers)
    host: "127.0.0.1",
    proxy: {
      "/risk": EVAL_HUB,
      "/pipd": EVAL_HUB,
      "/cmp": EVAL_HUB,
      "/dmp": EVAL_HUB,
      "/health": EVAL_HUB,
      "/docs": EVAL_HUB,
      "/openapi.json": EVAL_HUB,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
