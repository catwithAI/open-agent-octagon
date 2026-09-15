/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// WSL/容器或系统中已有较多 inotify 实例时，Vite 默认的 native watcher
// 可能触发 ENOSPC。start.sh 默认打开轮询；手动运行 Vite 时可通过
// VITE_USE_POLLING=1 启用，VITE_USE_POLLING=0 可显式关闭。
const usePolling = /^(1|true|yes)$/i.test(process.env.VITE_USE_POLLING ?? "");

export default defineConfig({
  plugins: [react()],
  server: {
    watch: usePolling ? { usePolling: true, interval: 500 } : undefined,
    proxy: {
      "/api": "http://127.0.0.1:8100",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
