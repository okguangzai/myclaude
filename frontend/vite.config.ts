import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/",
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          editor: [
            "@codemirror/state",
            "@codemirror/view",
            "@codemirror/commands",
            "@replit/codemirror-vim",
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
