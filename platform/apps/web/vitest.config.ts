import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname),
    },
  },
  test: {
    exclude: ["e2e/**", "node_modules/**", ".next/**"],
    environment: "jsdom",
    environmentOptions: {
      jsdom: { url: "http://localhost:3200/" },
    },
    setupFiles: ["./vitest.setup.ts"],
    restoreMocks: true,
  },
});
