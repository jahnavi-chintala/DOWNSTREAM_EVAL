import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
const EVAL_HUB = process.env.EVAL_HUB_URL ?? "http://127.0.0.1:8010";
// In dev, Vite runs on 5173 and proxies to unified_eval_app (default :8010; set EVAL_HUB_URL to override).
// In prod, `npm run build` emits to ./dist which FastAPI serves at "/".
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
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
