// Journey tests (E6): run against the throw-away stack that
// e2e/run-journeys.sh starts (docs/journeys.md). Never point MC_JOURNEYS_BASE
// at a live install — the journeys create boards, agents, tasks and approvals.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./journeys",
  testMatch: /J-.*\.spec\.ts$/,
  // Journeys share one stack; they run one after the other.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["json", { outputFile: process.env.MC_JOURNEYS_REPORT ?? "test-results/report.json" }],
    ["html", { outputFolder: process.env.MC_JOURNEYS_HTML ?? "playwright-report", open: "never" }],
  ],
  use: {
    baseURL: process.env.MC_JOURNEYS_BASE ?? "http://localhost:18080",
    locale: "en-US",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
