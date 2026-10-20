import react from "@vitejs/plugin-react-swc";
import { defineConfig } from "vite";

// В рабочем режиме SPA раздаёт core-api. Для разработки `npm run dev` проксирует /api на
// локально запущенный core-api (`qoima up --no-gpu`, QOIMA_HTTPS_PORT=8443).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  server: {
    proxy: {
      "/api": {
        target: "https://localhost:8443",
        changeOrigin: true,
        secure: false,
        ws: true, // WebSocket /api/v1/ws
      },
    },
  },
});
