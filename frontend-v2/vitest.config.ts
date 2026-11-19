import { defineConfig, configDefaults } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

// Test files that must run in a plain `forks` worker instead of a VM context.
// In the VM pool the test's global object IS jsdom's window, so jsdom's real
// browser rules apply: `window.location` cannot be redefined and
// `performance` is jsdom's (no `getEntriesByType`). These files stub exactly
// those globals, so they keep the classic pool. Add a file here only for that
// reason — never to hide a failure.
const NEEDS_NODE_GLOBALS = [
  "src/lib/__tests__/chatHistory.test.ts", // spies on performance.getEntriesByType
  "src/verticals/bench_studio/__tests__/ChallengeDetail.test.tsx", // redefines window.location
];

// playwright/ holds @playwright/test specs (run via `npx playwright test`),
// not vitest tests — collecting them here fails the run.
const EXCLUDE = [...configDefaults.exclude, "playwright/**"];

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    exclude: EXCLUDE,
    // One worker per CPU. Vitest's default is CPUs − 1, i.e. 3 of the 4
    // vCPUs on a GitHub runner; the main process only hands out files.
    maxWorkers: "100%",
    projects: [
      {
        extends: true,
        test: {
          name: "vm",
          // Every test file still gets a fresh context and module registry
          // (same isolation as `forks`), but the worker process — with jsdom
          // and the node_modules graph already loaded — is reused instead of
          // being spawned again for each of the ~320 files. That spawn +
          // re-import was ~65 % of the suite's CPU time.
          pool: "vmForks",
          exclude: [...EXCLUDE, ...NEEDS_NODE_GLOBALS],
        },
      },
      {
        extends: true,
        test: {
          name: "forks",
          pool: "forks",
          include: NEEDS_NODE_GLOBALS,
        },
      },
    ],
  },
  resolve: {
    alias: [
      { find: "@", replacement: path.resolve(__dirname, "./src") },
      // The package's CJS build `require()`s refractor, which is ESM-only;
      // only Node's require(esm) papers over that, the VM pool does not. The
      // app bundler (Next) already picks this ESM build via "module".
      { find: /^react-syntax-highlighter$/, replacement: "react-syntax-highlighter/dist/esm/index.js" },
    ],
  },
});
