import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // playwright/ holds @playwright/test specs (run via `npx playwright test`),
    // not vitest tests — collecting them here fails the run.
    exclude: ["**/node_modules/**", "playwright/**"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
