/**
 * Picture gate for the bench result page (time-limit run, 28.10.2026).
 *
 * Builds the REAL `ResultView` from source with the project's compiled
 * Tailwind, serves it, and answers the bench API from the backend's contract
 * fixtures (`backend/app/verticals/bench_studio/w1_fixtures/*.json`) — no
 * backend, no real data. One picture per state × viewport × language.
 *
 *   node playwright/bench-result-shots.mjs [outDir]          (from frontend-v2/)
 *
 * Env: SHOT_ROOT = the frontend-v2 folder to build (default: this one — point
 * it at an older checkout for the "before" pictures); SHOT_FIXTURES = the
 * fixture folder (default: this repo's backend fixtures).
 * Exit code 1 = a page error or a page that scrolls sideways.
 */
import { chromium, devices } from "playwright";
import { build } from "vite";
import react from "@vitejs/plugin-react";
import postcss from "postcss";
import tailwind from "@tailwindcss/postcss";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(process.env.SHOT_ROOT ?? path.resolve(HERE, ".."));
const FIXTURES = path.resolve(
  process.env.SHOT_FIXTURES ?? path.resolve(HERE, "../../backend/app/verticals/bench_studio/w1_fixtures"),
);
const DIST = fs.mkdtempSync(path.join(os.tmpdir(), "bench-result-dist-"));
const OUT = path.resolve(process.argv[2] ?? fs.mkdtempSync(path.join(os.tmpdir(), "bench-result-shots-")));
fs.mkdirSync(OUT, { recursive: true });

const fixture = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf-8"));

async function buildFixture() {
  const cssPath = path.join(ROOT, "src/styles/globals.css");
  const compiled = await postcss([tailwind()]).process(fs.readFileSync(cssPath, "utf-8"), {
    from: cssPath,
    to: path.join(DIST, "globals.css"),
  });
  const stubs = path.join(HERE, "bench-result-stubs.tsx");
  await build({
    root: ROOT,
    configFile: false,
    logLevel: "error",
    plugins: [react()],
    resolve: {
      alias: [
        { find: /^next\/link$/, replacement: stubs },
        { find: /^next\/navigation$/, replacement: stubs },
        { find: /^@\//, replacement: path.join(ROOT, "src") + "/" },
      ],
    },
    define: { "process.env.NODE_ENV": '"production"', "process.env.NEXT_PUBLIC_API_URL": '""' },
    build: {
      outDir: DIST,
      emptyOutDir: true,
      minify: false,
      lib: { entry: path.join(HERE, "bench-result-entry.tsx"), formats: ["es"], fileName: "app" },
      rollupOptions: { output: { inlineDynamicImports: true } },
    },
  });
  const appFile = fs.readdirSync(DIST).find((f) => /^app\.(m?js)$/.test(f));
  if (!appFile) throw new Error("no bundle produced in " + DIST);
  fs.writeFileSync(path.join(DIST, "index.html"), `<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>${compiled.css}</style></head>
<body class="font-sans antialiased bg-[var(--color-bg-deep)] text-[var(--color-text-primary)] min-h-[100dvh] overflow-x-hidden">
<div id="root"></div>
<script>window.process = { env: { NODE_ENV: "production" }, platform: "linux", version: "" };</script>
<script type="module">import { mount } from "./${appFile}"; mount();</script>
</body></html>`);
}

const MIME = { ".html": "text/html", ".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css" };

function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url, "http://x");
      let p = path.join(DIST, decodeURIComponent(url.pathname));
      if (url.pathname === "/" || !fs.existsSync(p) || fs.statSync(p).isDirectory()) p = path.join(DIST, "index.html");
      res.writeHead(200, { "content-type": MIME[path.extname(p)] ?? "application/octet-stream" });
      res.end(fs.readFileSync(p));
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

// The live run with the agent's session level already read (live card).
const running = fixture("test_running");
for (const b of running.blocks)
  for (const c of b.contestants)
    if (c.harness === "omp") c.effort.observed = { source: "omp_session", level: "high", configured: null };

const STATES = [
  { name: "time-limit", test: fixture("test_aborted_time_limit") },
  { name: "live-agent", test: running },
];
const VIEWS = [
  { name: "desktop", ctx: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 } },
  { name: "phone", ctx: { ...devices["iPhone 13"] } },
];
const LOCALES = ["en", "de"];

await buildFixture();
const server = await serve();
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch();
const failures = [];
const list = fixture("test_list");

for (const view of VIEWS) {
  const ctx = await browser.newContext({ ...view.ctx, colorScheme: "dark" });
  for (const state of STATES) {
    for (const locale of LOCALES) {
      if (view.name === "desktop" && locale === "de") continue;
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
      await page.route("**/api/v1/**", (route) => {
        const u = new URL(route.request().url());
        const json = (body, status = 200) =>
          route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
        if (u.pathname === `/api/v1/bench/tests/${state.test.id}`) return json(state.test);
        if (u.pathname === "/api/v1/bench/tests") return json(list);
        if (u.pathname.endsWith("/seen")) return json({ resolved: 0 });
        if (u.pathname.endsWith("/poster")) return route.fulfill({ status: 404, body: "" });
        return json({});
      });
      await page.goto(origin, { waitUntil: "load" });
      await page.evaluate((locale) => (document.documentElement.lang = locale), locale);
      await page.waitForFunction(() => typeof window.__mountBenchResult === "function", { timeout: 30000 });
      await page.evaluate((o) => window.__mountBenchResult(o), { locale, testId: state.test.id });
      await page.getByTestId("bench-result-page").waitFor({ timeout: 30000 });
      await page.waitForTimeout(400);
      const hScroll = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      const file = path.join(OUT, `bench-${state.name}-${view.name}-${locale}.png`);
      await page.screenshot({ path: file, fullPage: true });
      console.log(`${state.name} · ${view.name} · ${locale} -> ${file}`);
      const label = `${state.name} ${view.name} ${locale}`;
      if (hScroll > 0) failures.push(`${label}: page scrolls sideways by ${hScroll}px`);
      if (errors.length) failures.push(`${label}: page errors: ${errors.join(" | ")}`);
      await page.close();
    }
  }
  await ctx.close();
}

await browser.close();
server.close();
fs.rmSync(DIST, { recursive: true, force: true });

if (failures.length) {
  console.error("\nFAIL:\n" + failures.map((f) => "  - " + f).join("\n"));
  process.exit(1);
}
console.log(`\nOK: pictures in ${OUT}`);
