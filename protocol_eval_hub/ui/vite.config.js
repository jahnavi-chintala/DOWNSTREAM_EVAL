import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// In dev, Vite runs on 5173 and proxies the FastAPI paths to uvicorn on :8080.
// In prod, `npm run build` emits to ./dist which FastAPI serves at "/".
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        proxy: {
            "/risk": "http://127.0.0.1:8080",
            "/pipd": "http://127.0.0.1:8080",
            "/cmp": "http://127.0.0.1:8080",
            "/dmp": "http://127.0.0.1:8080",
            "/health": "http://127.0.0.1:8080",
            "/docs": "http://127.0.0.1:8080",
            "/openapi.json": "http://127.0.0.1:8080",
        },
    },
    build: {
        outDir: "dist",
        emptyOutDir: true,
        sourcemap: false,
    },
});
